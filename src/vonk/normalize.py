"""Turn extracted spans into Events.

Covers dates, year inference, the weekday check and times in Europe/Brussels. All
interpretation happens here in plain code; the extractor only returns verbatim spans.

Child names never enter this module's output: ``Event.title_activity`` is the activity only.
``render_title`` applies the ``<kindnaam> scouts: `` prefix when writing to a calendar.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from vonk.models import TZ as TZ_NAME
from vonk.models import Event, ExtractedSpan, Scope, Tak, VonkPost

TZ = ZoneInfo(TZ_NAME)

OPEN_END_DURATION = timedelta(hours=3)
"""Default length of an open-ended activity (``18u - ....``), added in wall time."""

END_UNKNOWN_NOTE = "einduur onbekend"

EVENING_START = time(19, 0)
"""Start time assumed for a bare ``Avond``."""

MONTHS: Mapping[str, int] = {
    "januari": 1, "jan": 1,
    "februari": 2, "febr": 2, "feb": 2,
    "maart": 3, "mrt": 3, "maa": 3,
    "april": 4, "apr": 4,
    "mei": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "augustus": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip

WEEKDAYS: Mapping[str, int] = {
    "maandag": 0, "ma": 0,
    "dinsdag": 1, "di": 1,
    "woensdag": 2, "wo": 2,
    "donderdag": 3, "do": 3,
    "vrijdag": 4, "vr": 4,
    "zaterdag": 5, "za": 5,
    "zondag": 6, "zo": 6,
}  # fmt: skip

_DUTCH_WEEKDAY = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")


def _alternation(words: Sequence[str]) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


# One date item: optional weekday, a day, then a numeric month (11/10[/26]) or a month name
# (11 oktober [2026], 21februari). A bare day (the "2" of "2 en 3 oktober") borrows the month
# of the next item; it must not be a time (14u) or part of a longer number.
_DATE_ITEM_RE = re.compile(
    rf"(?:(?P<wd>{_alternation(list(WEEKDAYS))})\.?\s+)?"
    r"(?<![\d/])(?P<day>\d{1,2})"
    r"(?:"
    r"\s*/\s*(?P<nmonth>\d{1,2})(?:\s*/\s*(?P<nyear>\d{4}|\d{2}))?(?![\d/])"
    rf"|\s*(?P<month>{_alternation(list(MONTHS))})\.?(?![a-z])(?:\s+(?P<year>\d{{4}})(?!\d))?"
    r"|(?!\s*(?:u|h|:|\d))"
    r")",
    re.IGNORECASE,
)

_TIME_TOKEN_RE = re.compile(r"(?<![\d/])(\d{1,2})(?:\s*[uh:]\s*(\d{2})?)?(?![\d/])", re.I)
_NUMERIC_DATE_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}(?:\s*/\s*\d{2,4})?")
_OPEN_END_RE = re.compile(r"\.{2,}|…")
_WS_RE = re.compile(r"\s+")


def collapse_ws(text: str) -> str:
    """Collapse every whitespace run to one space and strip."""
    return _WS_RE.sub(" ", text).strip()


# --- anchor and year inference ------------------------------------------------------------

AnchorSource = Literal["filename", "post_date", "scout_year"]

WINDOWS: Mapping[AnchorSource, tuple[int, int]] = {
    "filename": (-1, 2),
    "post_date": (-1, 11),
    "scout_year": (0, 11),
}
"""Allowed month offsets of an event relative to its anchor, per anchor source."""


@dataclass(frozen=True, slots=True)
class Anchor:
    """Month + year a vonk is about; the reference point for year inference.

    ``source`` ``filename`` covers a month found in the PDF filename or post title (the
    narrow window); ``post_date`` falls back to the publication date (the wide window);
    ``scout_year`` is September of a jaarvonk's start year (exactly one year fits).
    """

    year: int
    month: int
    source: AnchorSource

    @property
    def window(self) -> tuple[int, int]:
        return WINDOWS[self.source]


def _month_year_in(text: str) -> tuple[int | None, int | None]:
    tokens = re.findall(r"[a-z]+|\d+", text.casefold())
    month = next((MONTHS[t] for t in tokens if t in MONTHS), None)
    year = next((int(t) for t in tokens if re.fullmatch(r"20\d{2}", t)), None)
    return month, year


def _closest_year(month: int, anchor_year: int, anchor_month: int, window: tuple[int, int]):
    """Year for ``month`` closest to the anchor. Returns (year, ok, ambiguous)."""
    lo, hi = window
    scored = []
    for y in (anchor_year - 1, anchor_year, anchor_year + 1):
        offset = (y - anchor_year) * 12 + (month - anchor_month)
        scored.append((abs(offset), y, lo <= offset <= hi))
    inside = [s for s in scored if s[2]]
    pool = inside or scored
    best = min(pool)
    return best[1], bool(inside), len(inside) > 1


def infer_anchor(post: VonkPost, scope: Scope) -> Anchor:
    """Anchor month/year for ``post``.

    Jaarvonk (scout-year scope): September of the start year. Otherwise the month from the
    PDF filename, then the post title; its year from the same text, else the year that puts
    that month closest to the post date. Without a month in either: the post date.
    """
    if scope.is_scout_year:
        return Anchor(int(scope.period[:4]), 9, "scout_year")
    posted = post.date or post.modified
    candidates: list[str] = []
    if post.pdf_url:
        candidates.append(unquote(urlparse(post.pdf_url).path.rsplit("/", 1)[-1]))
    candidates.append(post.title)
    for text in candidates:
        month, year = _month_year_in(text)
        if month is None:
            continue
        if year is None:
            year, _, _ = _closest_year(month, posted.year, posted.month, WINDOWS["post_date"])
        return Anchor(year, month, "filename")
    return Anchor(posted.year, posted.month, "post_date")


def infer_year(month: int, anchor: Anchor) -> tuple[int, str | None]:
    """Year for a date in ``month``, closest to ``anchor`` within its window.

    Returns the year and a warning when the choice is ambiguous or outside the window.
    """
    year, ok, ambiguous = _closest_year(month, anchor.year, anchor.month, anchor.window)
    if not ok:
        return year, (
            f"maand {month} valt buiten het venster rond {anchor.month:02d}/{anchor.year} "
            f"({anchor.source}); jaar {year} gekozen"
        )
    if ambiguous:
        return year, (
            f"jaar van maand {month} is dubbelzinnig rond {anchor.month:02d}/{anchor.year} "
            f"({anchor.source}); jaar {year} gekozen"
        )
    return year, None


# --- dates -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DateItem:
    day: int
    month: int | None
    year: int | None
    weekday: int | None
    text: str


def _date_items(text: str) -> list[_DateItem]:
    items: list[_DateItem] = []
    for m in _DATE_ITEM_RE.finditer(text):
        if m.group("nmonth"):
            month: int | None = int(m.group("nmonth"))
        elif m.group("month"):
            month = MONTHS[m.group("month").casefold()]
        else:
            month = None
        raw_year = m.group("nyear") or m.group("year")
        year = None if raw_year is None else int(raw_year) + (2000 if len(raw_year) == 2 else 0)
        wd = m.group("wd")
        items.append(
            _DateItem(
                day=int(m.group("day")),
                month=month,
                year=year,
                weekday=None if wd is None else WEEKDAYS[wd.casefold()],
                text=collapse_ws(m.group(0)),
            )
        )
    # A bare day borrows month (and year) of the next item that has one.
    resolved: list[_DateItem] = []
    for i, item in enumerate(items):
        month, year = item.month, item.year
        for later in items[i + 1 :]:
            if month is None and later.month is not None:
                month = later.month
                year = year if year is not None else later.year
            if year is None and later.year is not None and later.month is not None:
                year = later.year
            if month is not None and year is not None:
                break
        if month is None:
            continue
        resolved.append(_DateItem(item.day, month, year, item.weekday, item.text))
    return resolved


def parse_dates(
    date_span: str, end_date_span: str | None, anchor: Anchor
) -> tuple[date, date | None, list[str]]:
    """Parse a date span (and optional end-date span) into (start, inclusive end, warnings).

    Raises ValueError when no date can be found or a date does not exist.
    """
    items = _date_items(date_span)
    if end_date_span:
        items += _date_items(end_date_span)
    if not items:
        raise ValueError(f"geen datum gevonden in {collapse_ws(date_span)!r}")
    warnings: list[str] = []
    dates: list[date] = []
    for item in items:
        assert item.month is not None
        year = item.year
        if year is None:
            year, warning = infer_year(item.month, anchor)
            if warning:
                warnings.append(f"{item.text}: {warning}")
        try:
            d = date(year, item.month, item.day)
        except ValueError:
            raise ValueError(f"ongeldige datum {item.text!r}") from None
        if dates and d < dates[-1] and item.year is None:
            d = date(d.year + 1, d.month, d.day)
        if item.weekday is not None and d.weekday() != item.weekday:
            warnings.append(
                f"weekdag klopt niet: {item.text!r} maar {d.isoformat()} is een "
                f"{_DUTCH_WEEKDAY[d.weekday()]}"
            )
        dates.append(d)
    start, end = dates[0], dates[-1]
    if end < start:
        raise ValueError(f"einddatum voor begindatum in {collapse_ws(date_span)!r}")
    return start, (end if end != start else None), warnings


# --- times -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedTimes:
    """Times of one activity. ``day_offset`` is 1 for a ``24u`` start (midnight)."""

    start: time | None
    end: time | None
    end_unknown: bool
    day_offset: int = 0
    warning: str | None = None


def _to_time(hour: int, minute: int) -> tuple[time, int]:
    if hour == 24 and minute == 0:
        return time(0, 0), 1
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"ongeldig uur {hour}u{minute:02d}")
    return time(hour, minute), 0


def parse_times(start_span: str | None, end_span: str | None) -> ParsedTimes:
    """Parse ``14u-17u``, ``14u - 16u30``, ``14 – 17u``, ``18u - ....``, ``19u``, ``Avond``.

    The whole range may sit in ``start_span``. One time without an end is open-ended.
    """
    text = " - ".join(s for s in (start_span, end_span) if s)
    if not text.strip():
        return ParsedTimes(None, None, False)
    cleaned = _NUMERIC_DATE_RE.sub(" ", text)
    tokens = [(int(h), int(m or 0)) for h, m in _TIME_TOKEN_RE.findall(cleaned)]
    if not tokens:
        if "avond" in text.casefold():
            return ParsedTimes(
                EVENING_START, None, True, warning="'Avond' zonder uur: start op 19u aangenomen"
            )
        return ParsedTimes(None, None, False)
    start, offset = _to_time(*tokens[0])
    if len(tokens) == 1 or _OPEN_END_RE.search(cleaned):
        return ParsedTimes(start, None, True, offset)
    end, _ = _to_time(*tokens[1])
    return ParsedTimes(start, end, False, offset)


def open_end(day: date, start: time) -> time:
    """Default end for an open-ended start: start + 3h in Europe/Brussels wall time."""
    return (datetime.combine(day, start, tzinfo=TZ) + OPEN_END_DURATION).time()


def event_bounds(event: Event) -> tuple[datetime, datetime] | None:
    """Aware start/end datetimes (Europe/Brussels) of a timed event; None for all-day.

    An end time at or before the start means the end falls on the next day.
    """
    if event.all_day or event.start is None:
        return None
    start = datetime.combine(event.date, event.start, tzinfo=TZ)
    end_time = event.end if event.end is not None else open_end(event.date, event.start)
    end = datetime.combine(event.date, end_time, tzinfo=TZ)
    if end <= start:
        end += timedelta(days=1)
    return start, end


# --- events ------------------------------------------------------------------------------


def _is_cancelled(span: ExtractedSpan) -> bool:
    if span.cancelled:
        return True
    texts = (span.title_span, span.description_span or "")
    return any("geen scouts" in collapse_ws(t).casefold() for t in texts)


def _title(span: ExtractedSpan, cancelled: bool) -> str:
    title = collapse_ws(span.title_span)
    if cancelled and "geen scouts" not in title.casefold():
        return f"Geen scouts ({title})" if title else "Geen scouts"
    return title


def normalize_span(span: ExtractedSpan, post: VonkPost, scope: Scope, anchor: Anchor) -> Event:
    """One span -> one Event. Raises ValueError when the date cannot be parsed."""
    start_date, end_date, warnings = parse_dates(span.date_span, span.end_date_span, anchor)
    cancelled = _is_cancelled(span)
    times = parse_times(span.start_span, span.end_span)
    if times.warning:
        warnings.append(times.warning)
    all_day = cancelled or end_date is not None or times.start is None
    source_url = post.pdf_url or post.link
    notes = [collapse_ws(span.description_span or "")]
    start = end = None
    end_unknown = False
    if not all_day:
        assert times.start is not None
        start_date += timedelta(days=times.day_offset)
        start = times.start
        end_unknown = times.end_unknown
        end = open_end(start_date, start) if end_unknown else times.end
        if end_unknown:
            notes.append(END_UNKNOWN_NOTE)
    notes.append(source_url)
    return Event(
        scope_key=scope.key,
        date=start_date,
        end_date=end_date,
        start=start,
        end=end,
        all_day=all_day,
        title_activity=_title(span, cancelled),
        notes="\n".join(n for n in notes if n),
        source_url=source_url,
        warnings=tuple(warnings),
        end_unknown=end_unknown,
    )


def normalize(
    spans: Sequence[ExtractedSpan],
    post: VonkPost,
    scope: Scope,
    warnings: list[str] | None = None,
) -> list[Event]:
    """Normalise all spans of one vonk into Events for ``scope``.

    Per-event problems (weekday mismatch, ambiguous year) end up in ``Event.warnings``. A span
    whose date cannot be parsed is skipped and reported in ``warnings`` when given.
    """
    anchor = infer_anchor(post, scope)
    anchor_period = f"{anchor.year:04d}-{anchor.month:02d}"
    if warnings is not None and not scope.is_scout_year and anchor_period != scope.period:
        warnings.append(f"anker {anchor_period} ({anchor.source}) wijkt af van scope {scope.key}")
    events: list[Event] = []
    for span in spans:
        try:
            events.append(normalize_span(span, post, scope, anchor))
        except ValueError as exc:
            if warnings is not None:
                warnings.append(f"overgeslagen: {exc}")
    return events


# --- titles ------------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Comparison form of a title: casefolded, whitespace collapsed."""
    return collapse_ws(title).casefold()


def render_title(event: Event, child_names: Mapping[Tak, str]) -> str:
    """Calendar title: ``<kindnaam> scouts: <activiteit>``, group events ``Scouts: <...>``.

    A tak without a configured child name falls back to the group form.
    """
    tak = Tak(event.scope_key.partition(":")[0]) if event.scope_key else Tak.GROEP
    name = child_names.get(tak) if tak is not Tak.GROEP else None
    prefix = f"{name} scouts" if name else "Scouts"
    return f"{prefix}: {event.title_activity}"


def activity_from_summary(summary: str) -> str:
    """Inverse of ``render_title``: strip a leading ``... scouts: `` / ``Scouts: `` prefix."""
    head, sep, rest = summary.partition(": ")
    if sep and head.casefold().rstrip().endswith("scouts"):
        return rest.strip()
    return summary.strip()
