"""extract: request shape, verbatim verification and cross-check, with a fake Claude client.

The fake client returns the recorded model output in ``tests/fixtures/llm/*.json``. Running
that through verification and ``normalize`` must give ``tests/fixtures/expected.json``
exactly, which keeps the hand-built expected file and the recorded output consistent.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from vonk.extract import (
    JAARVONK_PROMPT,
    MODEL,
    MONTHLY_PROMPT,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    SPAN_SCHEMA,
    ExtractionError,
    cross_check,
    extract,
    occurs_verbatim,
    verify_spans,
)
from vonk.models import ExtractedSpan, Scope, Tak, VonkPost
from vonk.normalize import normalize

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCES = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["fixtures"]
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["fixtures"]
COMPARED = ("date", "end_date", "start", "end", "all_day", "end_unknown", "title_activity")


def text_of(name: str) -> str:
    return (FIXTURES / "text" / f"{name}.txt").read_text(encoding="utf-8")


def recorded(name: str) -> str:
    return (FIXTURES / "llm" / f"{name}.json").read_text(encoding="utf-8")


def post_of(src: dict) -> VonkPost:
    return VonkPost(
        id=src["post_id"],
        modified=datetime.fromisoformat(src["post_modified"]),
        title=src["title"],
        link=src["post_link"],
        pdf_url=src["url"],
        categories=tuple(src["categories"]),
        tak=Tak(src["tak"]),
        kind=src["kind"],
        date=datetime.fromisoformat(src["post_date"]),
    )


class FakeClient:
    """Mimics ``anthropic.Anthropic().beta.messages.create``; records every call."""

    def __init__(self, text: str | None = None, *, stop_reason="end_turn", exc=None):
        self.calls: list[dict] = []
        self._text, self._stop, self._exc = text, stop_reason, exc
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        content = [] if self._text is None else [SimpleNamespace(type="text", text=self._text)]
        return SimpleNamespace(stop_reason=self._stop, content=content)


def span(date="11/10", title="bosspel", **kw) -> ExtractedSpan:
    base = dict(
        date_span=date,
        end_date_span=None,
        start_span=None,
        end_span=None,
        title_span=title,
        description_span=None,
        cancelled=False,
    )
    return ExtractedSpan(**{**base, **kw})


# --- recorded output end to end ----------------------------------------------------------


@pytest.mark.parametrize("src", SOURCES, ids=lambda s: s["name"])
def test_recorded_output_verifies_and_normalises_to_expected(src):
    client = FakeClient(recorded(src["name"]))
    result = extract(text_of(src["name"]), src["kind"], client=client)

    assert result.warnings == ()
    events = normalize(result.spans, post_of(src), Scope.parse(src["scope"]))
    got = [{k: v for k, v in e.to_dict().items() if k in COMPARED} for e in events]
    want = [{k: v for k, v in e.items() if k in COMPARED} for e in EXPECTED[src["name"]]["events"]]
    assert got == want


@pytest.mark.parametrize("src", SOURCES, ids=lambda s: s["name"])
def test_expected_title_keys_match_reference_titles(src):
    for event in EXPECTED[src["name"]]["events"]:
        title = event["title_activity"].casefold()
        assert any(key.casefold() in title for key in event["title_keys"]), event


def test_request_shape_and_only_pdf_text_is_sent():
    text = text_of("kabouters-2026-10")
    client = FakeClient(recorded("kabouters-2026-10"))
    extract(text, "monthly", client=client)

    (call,) = client.calls
    assert call["model"] == MODEL == "claude-sonnet-5-5"
    assert call["system"] == MONTHLY_PROMPT
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["messages"] == [{"role": "user", "content": f"<vonk>\n{text}\n</vonk>"}]
    assert "tool_choice" not in call and "thinking" not in call


def test_jaarvonk_uses_its_own_prompt():
    client = FakeClient(recorded("jaarvonk-2026-2027"))
    extract(text_of("jaarvonk-2026-2027"), "jaarvonk", client=client)
    assert client.calls[0]["system"] == JAARVONK_PROMPT


def test_prompts_demand_verbatim_spans_and_cancellations():
    for prompt in (MONTHLY_PROMPT, JAARVONK_PROMPT):
        assert "VERBATIM" in prompt
        assert "never paraphrase" in prompt.lower()
        assert "Cancellations are items too" in prompt
    for word in ("kamp", "kampinschrijving", "kerstfeestje", "Powell", "groepsactiviteit",
                 "dropping", "kampmenu", "minimarkt"):  # fmt: skip
        assert word in JAARVONK_PROMPT
    assert "weekly activity" in JAARVONK_PROMPT


def test_schema_is_strict():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert OUTPUT_SCHEMA["required"] == ["items"]
    assert SPAN_SCHEMA["additionalProperties"] is False
    assert set(SPAN_SCHEMA["required"]) == set(SPAN_SCHEMA["properties"])
    assert set(SPAN_SCHEMA["properties"]) == set(span().to_dict())
    assert PROMPT_VERSION


# --- verification ------------------------------------------------------------------------


def test_occurs_verbatim_tolerates_layout_whitespace_only():
    text = "Vandaag trekken we het bos in! Tijd om te ravotten, te rennen en\n      vooral veel"
    assert occurs_verbatim("te rennen en vooral veel", text)
    assert occurs_verbatim("te  rennen\nen vooral", text)
    assert not occurs_verbatim("te rennen en vooral heel veel", text)
    assert not occurs_verbatim("Te rennen", text)  # case matters
    assert not occurs_verbatim("   ", text)


def test_paraphrased_required_span_drops_the_item_with_a_warning():
    text = "11/10 14u-17u\nWe spelen een bosspel!"
    kept, warnings = verify_spans([span(title="boswandeling"), span()], text)
    assert kept == [span()]
    assert len(warnings) == 1 and "boswandeling" in warnings[0]


def test_unverifiable_optional_span_is_cleared_with_a_warning():
    text = "11/10 14u-17u\nWe spelen een bosspel!"
    kept, warnings = verify_spans(
        [span(start_span="14u", end_span="18u", description_span="We spelen een bosspel!")], text
    )
    assert kept == [span(start_span="14u", description_span="We spelen een bosspel!")]
    assert len(warnings) == 1 and "end_span" in warnings[0]


def test_extract_reports_dropped_spans_as_warnings():
    data = json.loads(recorded("jeevees-2026-10"))
    data["items"][3]["title_span"] = "Nachtdropping"  # paraphrase
    result = extract(text_of("jeevees-2026-10"), "monthly", client=FakeClient(json.dumps(data)))
    assert len(result.spans) == 4
    assert any("Nachtdropping" in w for w in result.warnings)
    assert any(w.startswith("cross-check: 5 date-led lines") for w in result.warnings)


# --- cross-check -------------------------------------------------------------------------


def test_cross_check_monthly_counts_date_led_lines():
    text = text_of("kabouters-2026-10")
    spans = [span()] * 5
    assert cross_check(text, spans, "monthly") == []
    assert cross_check(text, spans[:4], "monthly") == [
        "cross-check: 5 date-led lines in the text but 4 items"
    ]
    assert cross_check(text, spans * 2, "monthly")


def test_cross_check_jaarvonk_flags_missing_planning_rows_only():
    text = text_of("jaarvonk-2026-2027")
    assert cross_check(text, [span()] * 13, "jaarvonk") == []
    assert cross_check(text, [span()] * 14, "jaarvonk") == []  # + kamp from the prose
    (warning,) = cross_check(text, [span()] * 12, "jaarvonk")
    assert "13 named planning rows" in warning


# --- failures ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client",
    [
        FakeClient('{"items": [{"date_span": "11/10"}]}'),
        FakeClient("not json"),
        FakeClient('{"events": []}'),
        FakeClient(None),
        FakeClient('{"items": []}', stop_reason="refusal"),
        FakeClient('{"items": [', stop_reason="max_tokens"),
        FakeClient(exc=ConnectionError("boom")),
    ],
    ids=["missing-field", "bad-json", "wrong-key", "no-text", "refusal", "max-tokens", "api"],
)
def test_extraction_errors(client):
    with pytest.raises(ExtractionError):
        extract("11/10 bosspel", "monthly", client=client)


def test_empty_text_is_an_error_without_calling_the_api():
    client = FakeClient('{"items": []}')
    with pytest.raises(ExtractionError, match="empty"):
        extract("  \n", "monthly", client=client)
    assert client.calls == []
