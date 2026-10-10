"""Download the source PDFs of the test fixtures and verify their sha256.

The PDFs contain personal data (leiding names, phone numbers) and are never committed:
they land in ``tests/fixtures/pdf/``, which is gitignored. The committed fixtures are the
redacted ``pdftotext -layout`` output in ``tests/fixtures/text/``.

Usage::

    python scripts/fetch_fixtures.py          # download missing PDFs, verify all
    python scripts/fetch_fixtures.py --force  # re-download every PDF

Exits non-zero when a download fails or a hash does not match. A mismatching file is
removed so that nothing downstream reads a PDF other than the recorded one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
SOURCES = FIXTURES / "sources.json"
PDF_DIR = FIXTURES / "pdf"


def sha256_of(path: Path) -> str:
    """Return the hex sha256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` (written via a temp file, so no partial PDFs remain)."""
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "vonk-fixtures/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URLs
        tmp.write_bytes(resp.read())
    tmp.replace(dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-download every PDF")
    args = parser.parse_args(argv)

    sources = json.loads(SOURCES.read_text(encoding="utf-8"))["fixtures"]
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for src in sources:
        dest = PDF_DIR / f"{src['name']}.pdf"
        if args.force or not dest.exists():
            try:
                download(src["url"], dest)
            except OSError as exc:
                print(f"FAIL {src['name']}: download error: {exc}", file=sys.stderr)
                failures += 1
                continue
        actual = sha256_of(dest)
        if actual != src["sha256"]:
            print(
                f"FAIL {src['name']}: sha256 {actual} != recorded {src['sha256']}"
                " (file removed; the PDF on the site may have changed)",
                file=sys.stderr,
            )
            dest.unlink()
            failures += 1
            continue
        print(f"ok   {src['name']}  {dest.relative_to(FIXTURES.parent.parent)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
