"""CalDAV integration against the scratch Baikal calendar ``vonk-test`` (AC 4).

Run with ``pytest -m caldav`` and BAIKAL_URL / BAIKAL_USER / BAIKAL_PASSWORD set
(BAIKAL_CALENDAR optional, a name or a path relative to BAIKAL_URL). The test refuses to run
against any calendar whose name is not ``vonk-test``: it must never touch ``gezin``.

A foreign event is seeded, three apply cycles and a ``purge-own`` run, and the foreign
event's ETag must be identical afterwards.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, time

import httpx
import pytest

from vonk.models import ChangeSet, Event, Operation, OpKind
from vonk.sinks.caldav_sink import CalDAVSink, calendar_name, calendar_url

pytestmark = pytest.mark.caldav

SCRATCH = "vonk-test"
URL = "https://scoutstervant.be/vonk-test.pdf"
SCOPE = "kabouters:2026-10"


@pytest.fixture
def sink():
    missing = [n for n in ("BAIKAL_URL", "BAIKAL_USER", "BAIKAL_PASSWORD") if not os.environ.get(n)]
    if missing:
        pytest.fail(f"caldav tests need {', '.join(missing)}")
    url = calendar_url(
        os.environ["BAIKAL_URL"],
        os.environ["BAIKAL_USER"],
        os.environ.get("BAIKAL_CALENDAR", SCRATCH),
    )
    if calendar_name(url) != SCRATCH:
        pytest.fail(f"refusing to run against {calendar_name(url)!r}: only {SCRATCH!r} allowed")
    http = httpx.Client(auth=(os.environ["BAIKAL_USER"], os.environ["BAIKAL_PASSWORD"]),
                        timeout=30)  # fmt: skip
    with http:
        yield CalDAVSink(http, url, {})


def ev(day: int, title: str, start: time | None = time(14)) -> Event:
    if start is None:
        return Event(SCOPE, date(2026, 10, day), None, None, None, True, title, URL, URL)
    return Event(SCOPE, date(2026, 10, day), None, start, time(17), False, title,
                 f"{title}\n{URL}", URL)  # fmt: skip


def cs(*ops: Operation) -> ChangeSet:
    return ChangeSet(id=uuid.uuid4().hex, scope_key=SCOPE, ops=ops, created_at=datetime.now(UTC))


def _foreign_ics(uid: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//vonk-test//foreign//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261001T000000Z\r\n"
        "DTSTART;VALUE=DATE:20261010\r\nDTEND;VALUE=DATE:20261011\r\n"
        "SUMMARY:Foreign event (vonk integration test)\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def _etag(http: httpx.Client, url: str) -> str | None:
    resp = http.get(url)
    resp.raise_for_status()
    return resp.headers.get("ETag")


def test_foreign_event_survives_three_cycles_and_purge(sink):
    http = sink.http
    foreign_uid = f"foreign-{uuid.uuid4()}@example.org"
    foreign_url = sink.url + f"{foreign_uid}.ics"
    put = http.put(foreign_url, content=_foreign_ics(foreign_uid).encode(),
                   headers={"If-None-Match": "*", "Content-Type": "text/calendar"})  # fmt: skip
    assert put.status_code in (201, 204), put.status_code
    foreign_etag = _etag(http, foreign_url)
    assert foreign_etag

    try:
        sink.purge_own()  # leftovers of an earlier aborted run

        # cycle 1: three adds (timed, open-ended DST day, all-day)
        r1 = sink.apply(cs(*(Operation(OpKind.ADD, None, None, e) for e in
                            (ev(3, "Dropping"), ev(25, "Bosspel"), ev(31, "Geen scouts", None)))),
                        [0, 1, 2])  # fmt: skip
        assert all(r.ok for r in r1), r1
        own = {o.uid: o for o in sink.list_own(SCOPE)}
        assert set(own) == {r.uid for r in r1}
        assert all(own[r.uid].etag == r.new_etag for r in r1)  # ETag read back after PUT

        # cycle 2: update one, remove one, add one
        upd_uid, rem_uid = r1[0].uid, r1[1].uid
        r2 = sink.apply(cs(
            Operation(OpKind.UPDATE, upd_uid, own[upd_uid].event, ev(3, "Dropping", time(15))),
            Operation(OpKind.REMOVE, rem_uid, own[rem_uid].event, None),
            Operation(OpKind.ADD, None, None, ev(17, "Zwemmen")),
        ), [0, 1, 2])  # fmt: skip
        assert all(r.ok for r in r2), r2
        own = {o.uid: o for o in sink.list_own(None)}
        assert rem_uid not in own and own[upd_uid].event.start == time(15)

        # cycle 3: a stale ETag is refused with 412 and nothing else is written
        sink._etags[upd_uid] = '"stale"'
        r3 = sink.apply(cs(
            Operation(OpKind.UPDATE, upd_uid, own[upd_uid].event, ev(3, "Dropping", time(16))),
            Operation(OpKind.ADD, None, None, ev(24, "Fuif")),
        ), [0, 1])  # fmt: skip
        assert r3[0].status == 412 and not r3[1].ok
        assert len(sink.list_own(None)) == len(own)

        assert _etag(http, foreign_url) == foreign_etag
        purged = sink.purge_own()
        assert purged and all(r.ok for r in purged)
        assert sink.list_own(None) == []
        assert _etag(http, foreign_url) == foreign_etag
    finally:
        http.delete(foreign_url, headers={"If-Match": foreign_etag})
