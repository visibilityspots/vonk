"""Guard: committed files must not contain personal data or infra internals (the repo is
public). Fixtures get the strict check (no ``@`` at all); every other file that git would
track gets an e-mail, phone and known-internals scan."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
FIXTURES = REPO / "tests" / "fixtures"
SOURCES = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["fixtures"]

PHONE = re.compile(r"(\+32[ /.]?|\b0)\d([ /.]?\d){7,8}\b")  # BE mobile + landline
EMAIL = re.compile(r"@")


def committed_fixture_files() -> list[Path]:
    return sorted(
        p for p in FIXTURES.rglob("*") if p.is_file() and "pdf" not in p.relative_to(FIXTURES).parts
    )


@pytest.mark.parametrize("path", committed_fixture_files(), ids=lambda p: p.name)
def test_no_phone_or_email(path):
    text = path.read_text(encoding="utf-8")
    assert not PHONE.search(text), PHONE.search(text)
    assert not EMAIL.search(text)


@pytest.mark.parametrize("src", SOURCES, ids=lambda s: s["name"])
def test_text_fixture_present_and_redaction_count(src):
    text = (FIXTURES / src["text"]).read_text(encoding="utf-8")
    assert text.strip()
    assert text.count("[REDACTED]") == src["redactions"]
    assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"])


def test_jaarvonk_fixture_keeps_only_groepspraatje_and_planning():
    src = next(s for s in SOURCES if s["kind"] == "jaarvonk")
    text = (FIXTURES / src["text"]).read_text(encoding="utf-8")
    pages = text.split("\f")
    assert pages[0].startswith("Groepspraatje")
    assert pages[1].startswith("Jaarplanning")
    assert "Zaterdag      18 oktober" in text  # known source error kept on purpose


# --- every file git would track ------------------------------------------------------------

SCANNED_SUFFIXES = {".md", ".py", ".hcl", ".yml", ".yaml", ".json", ".txt"}
EMAIL_ADDR = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
ALLOWED_EMAIL = re.compile(
    r"^noreply@|@vonk\.visibilityspots$|@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|test)$",
    re.IGNORECASE,
)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
ALLOWED_UUID = re.compile(r"^0{8}-0{4}-[0-9]0{3}-[0-9]0{3}-0{12}$")
# Split so this file does not match itself: the Baikal share UUID, the Baikal sharing table
# and the name of the Baikal DB backup taken before the first deploy.
FORBIDDEN = ("6d4b" + "f0b3", "calendar" + "instances", "db.sqlite" + ".20")


def tracked_files() -> list[Path]:
    """What ``git add -A`` would commit: cached plus untracked, minus ignored."""
    out = subprocess.run(
        ["git", "ls-files", "--others", "--cached", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted(
        REPO / line for line in out.splitlines()
        if Path(line).suffix in SCANNED_SUFFIXES and (REPO / line).is_file()
    )  # fmt: skip


TRACKED = tracked_files()


def test_scan_covers_the_repo_and_skips_ignored_planning_docs():
    rel = {p.relative_to(REPO).as_posix() for p in TRACKED}
    assert {"README.md", "IDEA.md", "nomad/vonk.hcl", "src/vonk/notify.py"} <= rel
    assert not any(r.startswith(".omc/") for r in rel)


@pytest.mark.parametrize("path", TRACKED, ids=lambda p: p.relative_to(REPO).as_posix())
def test_tracked_file_has_no_personal_data_or_internals(path):
    text = path.read_text(encoding="utf-8")
    emails = [e for e in EMAIL_ADDR.findall(text) if not ALLOWED_EMAIL.search(e)]
    assert emails == []
    assert not PHONE.search(text), PHONE.search(text)
    uuids = [u for u in UUID.findall(text) if not ALLOWED_UUID.match(u)]
    assert uuids == []
    lowered = text.lower()
    assert [f for f in FORBIDDEN if f in lowered] == []


@pytest.mark.parametrize(
    "bad",
    [
        # split literals: this file is scanned too
        "mail jan.doe@" "gmail.com",
        "bel 0471 " "23 45 67",
        "+32 " "471234567",
        "cal 6d4b" "f0b3-1111-4222-8333-444444444444",
    ],
)
def test_scanner_catches_known_leaks(bad):
    hit = (
        [e for e in EMAIL_ADDR.findall(bad) if not ALLOWED_EMAIL.search(e)]
        or PHONE.search(bad)
        or [u for u in UUID.findall(bad) if not ALLOWED_UUID.match(u)]
    )
    assert hit
