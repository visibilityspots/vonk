"""caldav_sink: ownership filter, preconditions, ETag read-back, iCalendar shape."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from icalendar import Calendar

from vonk.models import ChangeSet, Event, Operation, OpKind, Tak, is_own_uid, mint_uid
from vonk.normalize import open_end
from vonk.sinks.caldav_sink import CalDAVError, calendar_name, calendar_url, parse_own, to_ics

URL = "https://scoutstervant.be/x.pdf"
SCOPE = "kabouters:2026-10"

FOREIGN = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:dentist-123@example.org
DTSTAMP:20261001T000000Z
DTSTART;VALUE=DATE:20261003
SUMMARY:Tandarts
END:VEVENT
END:VCALENDAR
"""

LOOKALIKE = FOREIGN.replace("dentist-123@example.org", "vonk-123@example.org")


def ev(day=3, title="Dropping", start=time(14), end=time(17), scope=SCOPE) -> Event:
    return Event(scope, date(2026, 10, day), None, start, end, False, title,
                 f"{title}\n{URL}", URL)  # fmt: skip


def cs_of(*ops: Operation) -> ChangeSet:
    return ChangeSet(id="cs", scope_key=SCOPE, ops=ops, created_at=datetime.now(UTC))


def add(e: Event) -> Operation:
    return Operation(OpKind.ADD, None, None, e)


def test_calendar_url_and_name():
    url = calendar_url("https://b.test/dav.php/", "vonk", "vonk-test")
    assert url == "https://b.test/dav.php/calendars/vonk/vonk-test/"
    assert calendar_name(url) == "vonk-test"
    assert calendar_url("https://b.test/dav.php", "vonk", "calendars/jan/gezin") == (
        "https://b.test/dav.php/calendars/jan/gezin/"
    )


def test_timed_event_ics_shape_and_roundtrip():
    e = ev(25, start=time(14), end=None)
    e = Event(e.scope_key, e.date, None, time(14), open_end(e.date, time(14)), False,
              "Dropping", f"x\neinduur onbekend\n{URL}", URL, end_unknown=True)  # fmt: skip
    uid = mint_uid()
    ics = to_ics(uid, e, {Tak.KABOUTERS: "Anna"})
    assert "BEGIN:VTIMEZONE" in ics and "TZID:Europe/Brussels" in ics
    assert "DTSTART;TZID=Europe/Brussels:20261025T140000" in ics
    assert "DTEND;TZID=Europe/Brussels:20261025T170000" in ics  # DST day, wall time
    vevent = next(iter(Calendar.from_ical(ics).walk("VEVENT")))
    assert str(vevent["SUMMARY"]) == "Anna scouts: Dropping"
    assert str(vevent["LOCATION"]) == "Scoutslokalen Tervant"
    assert str(vevent["X-VONK-SCOPE"]) == SCOPE and str(vevent["X-VONK-DATE"]) == "2026-10-25"
    own = parse_own(uid, '"e1"', ics)
    assert own.event == e and own.etag == '"e1"' and "SUMMARY" in own.raw_ics


def test_all_day_multi_day_uses_exclusive_dtend():
    e = Event("groep:2026-2027", date(2027, 7, 1), date(2027, 7, 10), None, None, True,
              "Kamp", "", "")  # fmt: skip
    ics = to_ics(mint_uid(), e, {})
    assert "DTSTART;VALUE=DATE:20270701" in ics and "DTEND;VALUE=DATE:20270711" in ics
    assert "VTIMEZONE" not in ics
    assert "SUMMARY:Scouts: Kamp" in ics
    assert parse_own(mint_uid(), None, ics).event == e


def test_list_own_skips_foreign_and_lookalike_uids(fake_caldav, make_sink):
    fake_caldav.seed("dentist.ics", FOREIGN)
    fake_caldav.seed("lookalike.ics", LOOKALIKE)
    uid = mint_uid()
    fake_caldav.seed("own.ics", to_ics(uid, ev(), {}))
    sink = make_sink(fake_caldav)
    own = sink.list_own(None)
    assert [o.uid for o in own] == [uid]
    assert sink.list_own(SCOPE)[0].uid == uid
    assert sink.list_own("jeevees:2026-10") == []


def test_stripped_x_properties_give_empty_scope(fake_caldav, make_sink):
    uid = mint_uid()
    fake_caldav.seed("own.ics", to_ics(uid, ev(), {}))

    def strip(v):
        del v["X-VONK-SCOPE"]
        del v["X-VONK-DATE"]

    fake_caldav.edit(uid, strip)
    (own,) = make_sink(fake_caldav).list_own(None)
    assert own.event.scope_key == ""
    assert own.event.date == date(2026, 10, 3)


def test_add_uses_if_none_match_and_reads_etag_back(fake_caldav, make_sink):
    sink = make_sink(fake_caldav)
    (r,) = sink.apply(cs_of(add(ev())), [0])
    assert r.ok and r.status == 201 and is_own_uid(r.uid)
    put = next(q for q in fake_caldav.requests if q.method == "PUT")
    assert put.headers["If-None-Match"] == "*"
    assert "If-Match" not in put.headers
    href, etag, _ = fake_caldav.by_uid()[r.uid]
    assert r.new_etag == etag  # fake returns no ETag on PUT: it was read back via GET
    assert any(q.method == "GET" and q.url.path == href for q in fake_caldav.requests)


def test_update_and_remove_use_if_match(fake_caldav, make_sink):
    uid = mint_uid()
    etag = fake_caldav.seed("own.ics", to_ics(uid, ev(), {}))
    sink = make_sink(fake_caldav)
    (have,) = sink.list_own(None)
    upd = Operation(OpKind.UPDATE, uid, have.event, ev(start=time(15)))
    (r,) = sink.apply(cs_of(upd), [0])
    assert r.ok and r.uid == uid
    put = [q for q in fake_caldav.requests if q.method == "PUT"][-1]
    assert put.headers["If-Match"] == etag
    assert "SEQUENCE:1" in put.content.decode()
    new_etag = fake_caldav.by_uid()[uid][1]
    assert r.new_etag == new_etag
    rem = Operation(OpKind.REMOVE, uid, have.event, None)
    (r2,) = sink.apply(cs_of(rem), [0])
    assert r2.ok
    delete = next(q for q in fake_caldav.requests if q.method == "DELETE")
    assert delete.headers["If-Match"] == new_etag
    assert fake_caldav.items == {}


def test_412_stops_and_reports_the_rest(fake_caldav, make_sink):
    uid = mint_uid()
    fake_caldav.seed("own.ics", to_ics(uid, ev(), {}))
    sink = make_sink(fake_caldav)
    (have,) = sink.list_own(None)
    fake_caldav.edit(uid, lambda v: v.__setitem__("SUMMARY", "Anna scouts: Bowling"))
    ops = (
        Operation(OpKind.UPDATE, uid, have.event, ev(start=time(15))),
        add(ev(10, "Bosspel")),
    )
    results = sink.apply(cs_of(*ops), [0, 1])
    assert [r.ok for r in results] == [False, False]
    assert results[0].status == 412
    assert results[1].status is None and "not applied" in results[1].error
    assert len(fake_caldav.items) == 1  # the add was never sent


def test_selected_subset_only(fake_caldav, make_sink):
    sink = make_sink(fake_caldav)
    results = sink.apply(cs_of(add(ev(3)), add(ev(10, "Bosspel"))), [1])
    assert [r.index for r in results] == [1]
    assert len(fake_caldav.items) == 1


def test_purge_own_leaves_foreign_untouched(fake_caldav, make_sink):
    foreign_etag = fake_caldav.seed("dentist.ics", FOREIGN)
    sink = make_sink(fake_caldav)
    sink.apply(cs_of(add(ev(3)), add(ev(10, "Bosspel"))), [0, 1])
    results = sink.purge_own()
    assert len(results) == 2 and all(r.ok for r in results)
    assert list(fake_caldav.items) == ["/dav.php/calendars/vonk/vonk-test/dentist.ics"]
    assert fake_caldav.items["/dav.php/calendars/vonk/vonk-test/dentist.ics"][0] == foreign_etag
    assert not any(
        q.url.path.endswith("dentist.ics") and q.method in ("PUT", "DELETE")
        for q in fake_caldav.requests
    )


def test_report_failure_raises(fake_caldav, make_sink):
    fake_caldav.fail_next["REPORT"] = 401
    with pytest.raises(CalDAVError):
        make_sink(fake_caldav).list_own(None)


def test_unexpected_exception_becomes_a_failed_op_and_keeps_earlier_results(
    fake_caldav, make_sink, monkeypatch
):
    import vonk.sinks.caldav_sink as mod

    real = mod.to_ics

    def flaky(uid, e, names, sequence=0):
        if e.title_activity == "Boom":
            raise RuntimeError("secret internal detail")
        return real(uid, e, names, sequence)

    monkeypatch.setattr(mod, "to_ics", flaky)
    sink = make_sink(fake_caldav)
    results = sink.apply(cs_of(add(ev(3)), add(ev(10, "Boom")), add(ev(17, "Later"))), [0, 1, 2])
    assert [r.ok for r in results] == [True, False, False]
    assert results[0].uid in fake_caldav.by_uid()
    assert results[1].error == "RuntimeError"
    assert "not applied" in results[2].error
    assert len(fake_caldav.items) == 1


def test_crlf_in_source_url_is_stripped_not_raised(fake_caldav, make_sink):
    bad = "https://scoutstervant.be/x.pdf\r\nATTENDEE:mailto:evil@example.org"
    e = Event(SCOPE, date(2026, 10, 3), None, time(14), time(17), False, "Dropping", "", bad)
    ics = to_ics(mint_uid(), e, {})
    vevent = next(iter(Calendar.from_ical(ics).walk("VEVENT")))
    assert str(vevent["URL"]) == "https://scoutstervant.be/x.pdfATTENDEE:mailto:evil@example.org"
    assert "ATTENDEE" not in vevent
    (r,) = make_sink(fake_caldav).apply(cs_of(add(e)), [0])
    assert r.ok
