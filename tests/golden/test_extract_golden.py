"""Golden test: real Claude calls over the three October 2026 fixtures (marker ``llm``).

Run with ``pytest -m llm tests/golden``. It needs ``ANTHROPIC_API_KEY`` and costs money.
Three runs per fixture must each yield ``tests/fixtures/expected.json`` after normalisation
(acceptance criterion 1). ``expected.json`` is a DRAFT until Jan has reviewed it; see
``tests/fixtures/expected.README.md``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from vonk.extract import extract
from vonk.models import Scope, Tak, VonkPost
from vonk.normalize import normalize

pytestmark = pytest.mark.llm

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCES = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["fixtures"]
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["fixtures"]
EXACT = ("date", "end_date", "start", "end", "all_day", "end_unknown")
RUNS = 3


@pytest.fixture(scope="module")
def client():
    import anthropic

    return anthropic.Anthropic()


@pytest.mark.parametrize("run", range(1, RUNS + 1), ids=lambda r: f"run{r}")
@pytest.mark.parametrize("src", SOURCES, ids=lambda s: s["name"])
def test_golden(src, run, client):
    text = (FIXTURES / src["text"]).read_text(encoding="utf-8")
    post = VonkPost(
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
    result = extract(text, src["kind"], client=client)
    events = sorted(
        normalize(result.spans, post, Scope.parse(src["scope"])),
        key=lambda e: (e.date, e.start is not None, e.start),
    )
    expected = EXPECTED[src["name"]]["events"]

    got = [{k: e.to_dict()[k] for k in EXACT} for e in events]
    assert got == [{k: e[k] for k in EXACT} for e in expected], result.warnings
    for event, want in zip(events, expected, strict=True):
        # the short title is the model's own summary; the verbatim title is in the notes
        title = f"{event.title_activity}\n{event.notes}".casefold()
        assert any(key.casefold() in title for key in want["title_keys"]), (
            event.title_activity,
            want["title_keys"],
        )
        assert len(event.title_activity) <= 60, event.title_activity
