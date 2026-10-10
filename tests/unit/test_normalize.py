from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from vonk.models import Event, ExtractedSpan, Scope, Tak, VonkPost
from vonk.normalize import (
    END_UNKNOWN_NOTE,
    Anchor,
    activity_from_summary,
    event_bounds,
    infer_anchor,
    infer_year,
    normalize,
    parse_dates,
    parse_times,
    render_title,
)

TEXT = Path(__file__).parent.parent / "fixtures" / "text"
PDF = "https://scoutstervant.be/wp-content/uploads/2026/09/vonk-jevees-oktober-2026.pdf"


def post(
    title="Vonk oktober",
    pdf_url: str | None = PDF,
    posted=datetime(2026, 9, 22, 19, 30),
    kind="monthly",
    tak: Tak | None = Tak.JEEVEES,
) -> VonkPost:
    return VonkPost(
        id=1,
        modified=posted,
        title=title,
        link="https://scoutstervant.be/2026/09/22/vonk-oktober/",
        pdf_url=pdf_url,
        categories=(13,),
        tak=tak,
        kind=kind,
        date=posted,
    )


def span(date_span, start=None, end=None, title="Activiteit", desc=None, cancelled=False,
         end_date=None) -> ExtractedSpan:  # fmt: skip
    return ExtractedSpan(
        date_span=date_span,
        end_date_span=end_date,
        start_span=start,
        end_span=end,
        title_span=title,
        description_span=desc,
        cancelled=cancelled,
    )


OKT = Scope.monthly(Tak.JEEVEES, 2026, 10)
JAAR = Scope.scout_year(2026)


# --- dates -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("11/10", date(2026, 10, 11), None),
        ("3/10-4/10", date(2026, 10, 3), date(2026, 10, 4)),
        ("Zondag 11 oktober", date(2026, 10, 11), None),
        ("2 en 3 oktober", date(2026, 10, 2), date(2026, 10, 3)),
        ("Zaterdag 17 okt", date(2026, 10, 17), None),
        ("za 17 okt.", date(2026, 10, 17), None),
        ("11/10/26", date(2026, 10, 11), None),
        ("30/9 - 1/10", date(2026, 9, 30), date(2026, 10, 1)),
    ],
)
def test_parse_dates_monthly(text, start, end):
    got_start, got_end, warnings = parse_dates(text, None, Anchor(2026, 10, "filename"))
    assert (got_start, got_end) == (start, end)
    assert warnings == []


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("Weekend       20,21februari", date(2027, 2, 20), date(2027, 2, 21)),
        ("Zondag          11april", date(2027, 4, 11), None),
        ("Zaterdag      3 oktober", date(2026, 10, 3), None),
        (
            "maandag 12 juli tot en met donderdag\n22 juli 2027",
            date(2027, 7, 12),
            date(2027, 7, 22),
        ),
        ("12 tot 22 juli 2027", date(2027, 7, 12), date(2027, 7, 22)),
    ],
)
def test_parse_dates_scout_year(text, start, end):
    got_start, got_end, _ = parse_dates(text, None, Anchor(2026, 9, "scout_year"))
    assert (got_start, got_end) == (start, end)


def test_end_date_span_is_used():
    start, end, _ = parse_dates("Zaterdag 3/10", "zondag 4/10", Anchor(2026, 10, "filename"))
    assert (start, end) == (date(2026, 10, 3), date(2026, 10, 4))


def test_date_span_with_times_ignores_times():
    start, end, _ = parse_dates("11/10 14u-17u", None, Anchor(2026, 10, "filename"))
    assert (start, end) == (date(2026, 10, 11), None)


@pytest.mark.parametrize("bad", ["", "geen datum", "31/2", "14u"])
def test_parse_dates_errors(bad):
    with pytest.raises(ValueError):
        parse_dates(bad, None, Anchor(2026, 10, "filename"))


def test_weekday_mismatch_warns_jaarvonk_fixture_line():
    # jaarvonk fixture: "Zaterdag 18 oktober" while 2026-10-18 is a Sunday.
    assert "Zaterdag      18 oktober" in (TEXT / "jaarvonk-2026-2027.txt").read_text()
    d, _, warnings = parse_dates("Zaterdag      18 oktober", None, Anchor(2026, 9, "scout_year"))
    assert d == date(2026, 10, 18)
    assert len(warnings) == 1 and "weekdag klopt niet" in warnings[0] and "zondag" in warnings[0]
    _, _, ok = parse_dates("Zondag 18 oktober", None, Anchor(2026, 9, "scout_year"))
    assert ok == []


# --- year inference ----------------------------------------------------------------------


def test_filename_anchor_spills_into_january():
    p = post(
        title="Vonk december",
        pdf_url="https://x/uploads/2026/11/vonk-december-2026.pdf",
        posted=datetime(2026, 11, 28),
    )
    anchor = infer_anchor(p, Scope.monthly(Tak.JEEVEES, 2026, 12))
    assert anchor == Anchor(2026, 12, "filename")
    events = normalize([span("3 januari")], p, Scope.monthly(Tak.JEEVEES, 2026, 12))
    assert events[0].date == date(2027, 1, 3)
    assert events[0].warnings == ()


def test_august_post_of_september_vonk():
    p = post(title="Vonk september", pdf_url="https://x/vonk.pdf", posted=datetime(2026, 8, 25))
    scope = Scope.monthly(Tak.JEEVEES, 2026, 9)
    assert infer_anchor(p, scope) == Anchor(2026, 9, "filename")
    w: list[str] = []
    events = normalize([span("Zaterdag 5 september"), span("26/9")], p, scope, w)
    assert [e.date for e in events] == [date(2026, 9, 5), date(2026, 9, 26)]
    assert w == []
    assert all(e.warnings == () for e in events)


def test_anchor_from_filename_beats_title_and_post_date():
    p = post(title="Vonk", pdf_url=PDF, posted=datetime(2026, 9, 22))
    assert infer_anchor(p, OKT) == Anchor(2026, 10, "filename")


def test_anchor_month_in_title_year_from_post_date():
    p = post(title="Vonk januari", pdf_url="https://x/scan-1.pdf", posted=datetime(2026, 12, 20))
    assert infer_anchor(p, Scope.monthly(Tak.JEEVEES, 2027, 1)) == Anchor(2027, 1, "filename")


def test_post_date_anchor_when_no_month_anywhere():
    p = post(
        title="Vonk", pdf_url="https://x/Halloween-Document-1.pdf", posted=datetime(2026, 10, 5)
    )
    assert infer_anchor(p, OKT) == Anchor(2026, 10, "post_date")


def test_scout_year_anchor():
    p = post(title="Jaarvonk 2026-2027", kind="jaarvonk", tak=Tak.GROEP)
    assert infer_anchor(p, JAAR) == Anchor(2026, 9, "scout_year")
    assert infer_year(8, Anchor(2026, 9, "scout_year")) == (2027, None)
    assert infer_year(9, Anchor(2026, 9, "scout_year")) == (2026, None)


def test_post_date_anchor_ambiguity_warns():
    # Anchor October, month September: -1 (this year) and +11 (next year) both fit.
    year, warning = infer_year(9, Anchor(2026, 10, "post_date"))
    assert year == 2026
    assert warning is not None and "dubbelzinnig" in warning


def test_filename_anchor_out_of_window_warns():
    year, warning = infer_year(6, Anchor(2026, 10, "filename"))
    assert year == 2026  # offset -4 beats +8
    assert warning is not None and "buiten" in warning


def test_anchor_scope_mismatch_is_reported():
    w: list[str] = []
    normalize([span("11/10")], post(), Scope.monthly(Tak.JEEVEES, 2026, 11), w)
    assert any("wijkt af" in m for m in w)


def test_unparseable_span_is_skipped_with_warning():
    w: list[str] = []
    events = normalize([span("ooit"), span("11/10")], post(), OKT, w)
    assert len(events) == 1
    assert any("overgeslagen" in m for m in w)


# --- times -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "want_start", "want_end", "unknown"),
    [
        ("14u-17u", None, time(14), time(17), False),
        ("14u - 16u30", None, time(14), time(16, 30), False),
        ("14 – 17u", None, time(14), time(17), False),
        ("19u - 21u00", None, time(19), time(21), False),
        ("14u", "17u", time(14), time(17), False),
        ("18u - ....", None, time(18), None, True),
        ("10u - ...", None, time(10), None, True),
        ("14u - …", None, time(14), None, True),
        ("19u", None, time(19), None, True),
        ("Avond", None, time(19), None, True),
        (None, None, None, None, False),
    ],
)
def test_parse_times(start, end, want_start, want_end, unknown):
    got = parse_times(start, end)
    assert (got.start, got.end, got.end_unknown) == (want_start, want_end, unknown)


def test_24u_is_midnight_next_day():
    ev = normalize([span("Woensdag 24 december", start="24u", title="Middernachtmis")],
                   post(kind="jaarvonk", tak=Tak.GROEP), JAAR)[0]  # fmt: skip
    assert ev.date == date(2026, 12, 25)
    assert ev.start == time(0, 0)
    assert ev.end_unknown and ev.end == time(3, 0)


def test_open_ended_gets_three_hours_and_note():
    ev = normalize([span("Zaterdag 24 oktober", start="18u - ....", title="Dropping",
                         desc="Dropping")], post(), OKT)[0]  # fmt: skip
    assert ev.start == time(18) and ev.end == time(21)
    assert ev.end_unknown and not ev.all_day
    assert ev.notes.splitlines() == ["Dropping", END_UNKNOWN_NOTE, PDF]


def test_dst_end_25_oct_2026():
    before = normalize([span("24/10", start="14u-17u")], post(), OKT)[0]
    after = normalize([span("25/10", start="14u-17u")], post(), OKT)[0]
    b0, b1 = event_bounds(before)
    a0, a1 = event_bounds(after)
    assert b0.utcoffset() == timedelta(hours=2)
    assert a0.utcoffset() == timedelta(hours=1)
    assert a1 - a0 == timedelta(hours=3)
    # Open-ended across the switch: +3h wall time, i.e. 4h of real time.
    night = normalize([span("25/10", start="0u30 - ...")], post(), OKT)[0]
    assert night.end == time(3, 30)
    n0, n1 = event_bounds(night)
    assert n1.astimezone(n0.tzinfo) - n0 == timedelta(hours=3)  # wall difference
    assert n1.timestamp() - n0.timestamp() == 4 * 3600


def test_event_bounds_crossing_midnight_and_all_day():
    ev = normalize([span("Zaterdag 24 oktober", start="23u - ...")], post(), OKT)[0]
    start, end = event_bounds(ev)
    assert end.date() == date(2026, 10, 25) and end.time() == time(2, 0)
    allday = normalize([span("3/10-4/10", title="Geen scouts")], post(), OKT)[0]
    assert event_bounds(allday) is None


# --- event shape -------------------------------------------------------------------------


def test_cancelled_and_multiday_are_all_day():
    cancelled = normalize([span("Zaterdag 3 oktober", start="14u-17u", title="Powell party",
                                cancelled=True)], post(), OKT)[0]  # fmt: skip
    assert cancelled.all_day and cancelled.start is None and cancelled.end is None
    assert cancelled.title_activity == "Geen scouts (Powell party)"
    geen = normalize([span("11/10", start="14u-17u", title="Geen scouts!")], post(), OKT)[0]
    assert geen.all_day and geen.title_activity == "Geen scouts!"
    multi = normalize([span("3/10-4/10", start="14u-17u", title="Weekend")], post(), OKT)[0]
    assert multi.all_day and multi.end_date == date(2026, 10, 4)


def test_no_time_is_all_day_and_notes_shape():
    ev = normalize([span("Zaterdag 17 oktober", title="Jonggivertrophy",
                         desc="We gaan naar de\n   jonggivertrophy")], post(), OKT)[0]  # fmt: skip
    assert ev.all_day and ev.end_date is None
    assert ev.notes == f"We gaan naar de jonggivertrophy\n{PDF}"
    assert ev.source_url == PDF
    assert ev.scope_key == "jeevees:2026-10"


def test_source_url_falls_back_to_post_link():
    ev = normalize([span("11/10")], post(pdf_url=None), OKT)[0]
    assert ev.source_url == "https://scoutstervant.be/2026/09/22/vonk-oktober/"


def test_render_title_and_inverse():
    names = {Tak.JEEVEES: "Kind"}
    ev = normalize([span("11/10", title="Bosspel")], post(), OKT)[0]
    assert render_title(ev, names) == "Kind scouts: Bosspel"
    assert render_title(ev, {}) == "Scouts: Bosspel"
    groep = Event(JAAR.key, date(2026, 10, 24), None, None, None, True, "Dropping", "", "")
    assert render_title(groep, names) == "Scouts: Dropping"
    assert activity_from_summary("Kind scouts: Bosspel") == "Bosspel"
    assert activity_from_summary("Scouts: Dropping") == "Dropping"
    assert activity_from_summary("Tandarts: 14u") == "Tandarts: 14u"


def test_child_names_never_in_events():
    ev = normalize([span("11/10", title="Bosspel")], post(), OKT)[0]
    assert "Kind" not in repr(ev)


# --- real fixtures (hand-made spans, no LLM) ---------------------------------------------


def _check_verbatim(text: str, spans: list[ExtractedSpan]) -> None:
    for s in spans:
        for part in s.spans():
            assert part in text, part


def test_kabouters_fixture():
    text = (TEXT / "kabouters-2026-10.txt").read_text()
    spans = [
        span("3/10-4/10", title="Geen scouts dit weekend, want het is Powell Party!",
             cancelled=True),
        span("11/10", start="14u-17u", title="bosspel",
             desc="Vandaag trekken we het bos in! Tijd om te ravotten, te rennen en"),
        span("18/10", start="14u-17u", title="geven we ons lokaal een leuke make-over!"),
        span("24/10", start="14u-17u", title="We worden gedropt"),
        span("31/10", start="14u-17u", title="We gaan koken!"),
    ]  # fmt: skip
    _check_verbatim(text, spans)
    p = post(title="Vonk oktober", tak=Tak.KABOUTERS, posted=datetime(2026, 10, 5, 20, 9),
             pdf_url="https://scoutstervant.be/wp-content/uploads/2026/10/Orange-Purple-"
             "Illustrative-Halloween-Page-Border-A4-Document-1.pdf")  # fmt: skip
    scope = Scope.monthly(Tak.KABOUTERS, 2026, 10)
    w: list[str] = []
    events = normalize(spans, p, scope, w)
    assert w == []
    assert [(e.date, e.end_date, e.start, e.end, e.all_day) for e in events] == [
        (date(2026, 10, 3), date(2026, 10, 4), None, None, True),
        (date(2026, 10, 11), None, time(14), time(17), False),
        (date(2026, 10, 18), None, time(14), time(17), False),
        (date(2026, 10, 24), None, time(14), time(17), False),
        (date(2026, 10, 31), None, time(14), time(17), False),
    ]
    assert all(e.warnings == () and e.scope_key == "kabouters:2026-10" for e in events)


def test_jeevees_fixture():
    text = (TEXT / "jeevees-2026-10.txt").read_text()
    spans = [
        span("2 en 3 oktober", title="Geen scouts dit weekend", cancelled=True),
        span("Zondag 11 oktober", start="10u - ...", title="Kaartenverkoop voor kampmenu!"),
        span("Zaterdag 17 oktober", title="We gaan naar de jonggivertrophy"),
        span("Zaterdag 24 oktober", start="18u - ....", title="Dropping"),
        span("Zaterdag 31 oktober", start="14u - 17u", title="ruiltocht in halloween thema"),
    ]
    _check_verbatim(text, spans)
    w: list[str] = []
    events = normalize(spans, post(), OKT, w)
    assert w == []
    assert [(e.date, e.end_date, e.start, e.end, e.end_unknown) for e in events] == [
        (date(2026, 10, 2), date(2026, 10, 3), None, None, False),
        (date(2026, 10, 11), None, time(10), time(13), True),
        (date(2026, 10, 17), None, None, None, False),
        (date(2026, 10, 24), None, time(18), time(21), True),
        (date(2026, 10, 31), None, time(14), time(17), False),
    ]
    assert all(e.warnings == () for e in events)
    assert END_UNKNOWN_NOTE in events[3].notes


def test_jaarvonk_fixture():
    text = (TEXT / "jaarvonk-2026-2027.txt").read_text()
    spans = [
        span("Zaterdag      18 oktober", start="14u - 17u", title="scouts"),
        span("Zondag        18 oktober", title="Jonggiver en Giver trophy"),
        span("Zaterdag      31 oktober", start="14 – 17u", title="scouts"),
        span("Zaterdag      28 november", start="14u - …", title="Minimarkt"),
        span("Weekend       20,21februari", title="Kampplanning", cancelled=True),
        span("Zaterdag        10 april", start="Avond", title="Powell Quiz"),
        span("Vrijdag         28 mei", start="19u", title="Kampinschrijving"),
    ]
    _check_verbatim(text, spans)
    p = post(title="Jaarvonk 2026-2027", kind="jaarvonk", tak=Tak.GROEP,
             pdf_url="https://scoutstervant.be/wp-content/uploads/2026/10/Jaarvonk-26-27.pdf",
             posted=datetime(2026, 10, 8))  # fmt: skip
    events = normalize(spans, p, JAAR)
    assert [e.date for e in events] == [
        date(2026, 10, 18),
        date(2026, 10, 18),
        date(2026, 10, 31),
        date(2026, 11, 28),
        date(2027, 2, 20),
        date(2027, 4, 10),
        date(2027, 5, 28),
    ]
    assert any("weekdag klopt niet" in w for w in events[0].warnings)
    assert events[1].warnings == ()
    assert events[4].all_day and events[4].end_date == date(2027, 2, 21)
    assert events[4].title_activity == "Geen scouts (Kampplanning)"
    assert events[5].start == time(19) and events[5].end_unknown
    assert events[6].start == time(19) and events[6].end == time(22) and events[6].end_unknown
