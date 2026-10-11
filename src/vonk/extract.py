"""LLM span extraction: locate activities as verbatim spans of the PDF text.

Also runs the regex cross-check of date tokens against the returned events.

The model only *locates* text: every returned field is a substring of the ``pdftotext``
output, except ``summary``, a short label that ``normalize`` sanitises and that never
replaces the verbatim title (it goes into the notes). Interpretation (dates, years, times)
happens in ``vonk.normalize``. Only the PDF text is sent: child names never reach the API
(they are applied when rendering events).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from vonk.models import ExtractedSpan, VonkKind

MODEL = "claude-sonnet-5-5"
"""Latest Sonnet-class model (verified with the claude-api skill, 2026-10-10)."""

PROMPT_VERSION = "2026-10-11.1"
"""Bump on any change to the prompts, schema or model: it keys the extraction cache."""

MAX_TOKENS = 16000

FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Server-side refusal fallback (``fallbacks="default"``) for Sonnet 5.5 on the Claude API."""

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

SPAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "date_span": {"type": "string"},
        "end_date_span": _NULLABLE_STRING,
        "start_span": _NULLABLE_STRING,
        "end_span": _NULLABLE_STRING,
        "title_span": {"type": "string"},
        "description_span": _NULLABLE_STRING,
        "cancelled": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": [
        "date_span",
        "end_date_span",
        "start_span",
        "end_span",
        "title_span",
        "description_span",
        "cancelled",
        "summary",
    ],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": SPAN_SCHEMA}},
    "required": ["items"],
    "additionalProperties": False,
}

_RULES = """\
Rules for every field:
- Copy every span VERBATIM from the text: exact characters, exact spelling, exact
  punctuation. Never paraphrase, translate, correct typos, reorder words or add text.
  A span may cover several lines; keep the text as it appears.
- date_span: the date as written for the item (e.g. "11/10", "Zondag 11 oktober",
  "3/10-4/10", "2 en 3 oktober"). For a range written as one token, put the whole token
  in date_span and set end_date_span to null.
- end_date_span: only when the last day is written separately from the first day
  (e.g. "donderdag 22 juli" in "van maandag 12 juli tot en met donderdag 22 juli"),
  else null.
- start_span / end_span: the start and end time as written (e.g. "14u", "17u", "....");
  a part of the day such as "Avond" is a start_span too; null when no time is given.
  For "14u-17u" use start_span "14u" and end_span "17u".
- title_span: the shortest verbatim text naming the activity (e.g. "Dropping",
  "Powell party", "bosspel"). When there is no name, use the first sentence describing it.
- description_span: the verbatim description belonging to the item, or null.
- cancelled: true when the text says there is no activity that day ("Geen scouts").
  Cancellations are items too: always include them. A "Geen scouts" remark on the same
  row or date as a named event belongs to that event: return ONE item with cancelled
  true and title_span the event name (e.g. "Powell party"), not two items.
- summary: the ONLY field you write yourself instead of copying. One Dutch word, at most
  three, naming the activity for a calendar title (e.g. "Bosspel", "Make-over",
  "Halloweentocht", "Kamp"). Base it on the item's own text only. For a cancellation
  name the event that replaces the activity (e.g. "Powell party"), or "Geen scouts"
  when the text names none.
- Do not invent items. Do not include phone numbers, names of people or e-mail addresses.
Return JSON matching the schema: {"items": [...]}, in the order they appear in the text.
"""

MONTHLY_PROMPT = (
    """\
Below is the plain-text layout of a monthly "vonk" (activity calendar) of one age group
of a Flemish scouts group. Extract one item per dated activity or cancellation.

"""
    + _RULES
)

JAARVONK_PROMPT = (
    """\
Below is the plain-text layout of the "jaarvonk": the scout-year overview of a Flemish
scouts group. Extract ONLY group-wide key dates: kamp, kampinschrijving, kerstfeestje,
Powell (party, after work, quiz), groepsactiviteit, dropping, kampmenu, minimarkt,
trophy, eetdag, middernachtmis, kampplanning and similar named events, including
"Geen scouts" days. A row of the yearly planning that only gives a date and the usual
hours (e.g. "Zondag 11 oktober 14u - 17u") is a weekly activity: do NOT include it.
Leave out events only for the leiding (voorkamp) or only for some groups after the kamp
(nakamp), and whole months without a specific date. When the same event appears both in
the introduction and in the planning table, return it once, using the planning row.

"""
    + _RULES
)


class ExtractionError(RuntimeError):
    """The LLM call failed or returned output that does not match the schema."""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    spans: tuple[ExtractedSpan, ...]
    warnings: tuple[str, ...]


# --- verification ------------------------------------------------------------------------


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def occurs_verbatim(span: str, text: str) -> bool:
    """True when ``span`` occurs in ``text``.

    Runs of whitespace are compared as one space: ``pdftotext -layout`` pads columns and
    wraps lines, and a multi-line span is the same text whether it keeps that layout or
    not. Every other character must match exactly.
    """
    needle = _collapse(span)
    return bool(needle) and needle in _collapse(text)


def verify_spans(
    spans: Iterable[ExtractedSpan], text: str
) -> tuple[list[ExtractedSpan], list[str]]:
    """Keep only spans found verbatim in ``text``.

    A missing ``date_span`` or ``title_span`` drops the whole item; a missing optional
    span is set to None. Each case adds a warning.
    """
    kept: list[ExtractedSpan] = []
    warnings: list[str] = []
    for span in spans:
        if not occurs_verbatim(span.date_span, text) or not occurs_verbatim(span.title_span, text):
            warnings.append(
                f"dropped item: date {span.date_span!r} / title {span.title_span!r} "
                "not found verbatim in the PDF text"
            )
            continue
        changes: dict[str, None] = {}
        for name in ("end_date_span", "start_span", "end_span", "description_span"):
            value = getattr(span, name)
            if value is not None and not occurs_verbatim(value, text):
                changes[name] = None
                warnings.append(f"{span.date_span!r}: {name} {value!r} not found verbatim; ignored")
        kept.append(replace(span, **changes) if changes else span)
    return kept, warnings


# --- regex cross-check -------------------------------------------------------------------

_MONTH_WORDS = (
    "januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december"
)
_WEEKDAYS = "maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|weekend"
_DATE_LED_LINE = re.compile(
    rf"^\s*(?:(?:{_WEEKDAYS})\s+)?(?:"
    rf"\d{{1,2}}(?:\s*(?:,|en|-|–|/|tot)\s*\d{{1,2}})*\s*(?:{_MONTH_WORDS})"
    rf"|\d{{1,2}}/\d{{1,2}}(?:\s*[-–]\s*\d{{1,2}}/\d{{1,2}})?"
    rf")(?![a-z])",
    re.IGNORECASE | re.MULTILINE,
)
_TIME_COLUMN = re.compile(r"^[\d\s:u.…–\-]*$", re.IGNORECASE)


def date_led_lines(text: str) -> list[str]:
    """Lines that start with a date token (optionally after a weekday)."""
    return [m.group(0) + text[m.end() :].split("\n", 1)[0] for m in _DATE_LED_LINE.finditer(text)]


def activity_rows(text: str) -> int:
    """Jaarvonk planning rows: a date column followed by a named activity column."""
    count = 0
    for m in _DATE_LED_LINE.finditer(text):
        rest = text[m.end() :].split("\n", 1)[0]
        if not re.match(r"\s{2,}\S", rest):
            continue  # prose, not a table row
        columns = [c for c in re.split(r"\s{2,}", rest.strip()) if c]
        if any(re.search(r"[a-z]", c, re.I) and not _TIME_COLUMN.match(c) for c in columns):
            count += 1
    return count


def cross_check(text: str, spans: Sequence[ExtractedSpan], kind: VonkKind) -> list[str]:
    """Compare the number of date tokens in ``text`` with the number of items.

    Monthly: every line starting with a date is one activity, so the counts must match.
    Jaarvonk: every planning row with a named activity is a key date; the introduction
    can add more (kamp), so only fewer items than rows is suspicious.
    """
    if kind == "jaarvonk":
        rows = activity_rows(text)
        if len(spans) < rows:
            return [f"cross-check: {rows} named planning rows but only {len(spans)} key dates"]
        return []
    tokens = len(date_led_lines(text))
    if tokens != len(spans):
        return [f"cross-check: {tokens} date-led lines in the text but {len(spans)} items"]
    return []


# --- LLM call ----------------------------------------------------------------------------


def _default_client() -> Any:
    import anthropic

    return anthropic.Anthropic()


def parse_output(raw: str) -> list[ExtractedSpan]:
    """Parse the model's JSON text into spans; raises ExtractionError on bad shape."""
    try:
        data = json.loads(raw)
        items = data["items"]
        if not isinstance(items, list):
            raise TypeError("items is not a list")
        spans = [ExtractedSpan.from_dict(item) for item in items]
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractionError(f"model output does not match the schema: {exc}") from exc
    for span in spans:
        if not isinstance(span.date_span, str) or not isinstance(span.title_span, str):
            raise ExtractionError("model output has a non-string date_span/title_span")
    return spans


def call_model(text: str, kind: VonkKind, client: Any, model: str = MODEL) -> list[ExtractedSpan]:
    """One structured-output request; returns the raw (unverified) spans."""
    prompt = JAARVONK_PROMPT if kind == "jaarvonk" else MONTHLY_PROMPT
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=prompt,
            messages=[{"role": "user", "content": f"<vonk>\n{text}\n</vonk>"}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
    except Exception as exc:  # anthropic.APIError and transport errors alike
        raise ExtractionError(f"Claude API call failed: {type(exc).__name__}: {exc}") from exc
    stop = getattr(response, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        raise ExtractionError(f"Claude stopped with stop_reason={stop!r}")
    raw = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if raw is None:
        raise ExtractionError("Claude returned no text block")
    return parse_output(raw)


def finalize(raw_spans: Sequence[ExtractedSpan], text: str, kind: VonkKind) -> ExtractionResult:
    """Verify spans against ``text`` and run the cross-check (used for cache hits too)."""
    kept, warnings = verify_spans(raw_spans, text)
    warnings.extend(cross_check(text, kept, kind))
    return ExtractionResult(spans=tuple(kept), warnings=tuple(warnings))


def extract(
    text: str, kind: VonkKind, *, client: Any | None = None, model: str = MODEL
) -> ExtractionResult:
    """Extract verified spans from the ``pdftotext -layout`` output of one vonk."""
    if not text.strip():
        raise ExtractionError("PDF text is empty")
    raw_spans = call_model(text, kind, client or _default_client(), model)
    return finalize(raw_spans, text, kind)
