"""ingest: WP REST parsing, tak/period resolution and warnings, on recorded responses."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from vonk.ingest import (
    derive_period,
    extract_pdf_url,
    ingest,
    is_jaarvonk,
    pdf_filename,
    resolve_tak,
    tak_from_keywords,
)
from vonk.models import Tak

WP = Path(__file__).parents[1] / "fixtures" / "wp"
POSTS = json.loads((WP / "posts_search_vonk.json").read_text(encoding="utf-8"))
BASE = "https://wp.test/wp-json/wp/v2"


def _post(post_id, title, content, categories=(), date="2026-09-22T19:00:00"):
    return {
        "id": post_id,
        "date": date,
        "modified": date,
        "title": {"rendered": title},
        "link": f"https://scoutstervant.be/?p={post_id}",
        "categories": list(categories),
        "content": {"rendered": content},
    }


def _pdfemb(url):
    return f'<div><a href="{url}" class="pdfemb-viewer" data-width="max">x</a></div>'


def _client(posts, *, pages=1, requests=None):
    """MockTransport serving ``posts`` for every query, split over ``pages`` pages."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        params = parse_qs(urlsplit(str(request.url)).query)
        page = int(params["page"][0])
        chunk = posts[page - 1 :: pages]
        return httpx.Response(200, json=chunk, headers={"X-WP-TotalPages": str(pages)})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- parsing -----------------------------------------------------------------------------


def test_extract_pdf_url_from_recorded_pdfemb_block():
    jeevees = next(p for p in POSTS if p["id"] == 2647)
    assert extract_pdf_url(jeevees["content"]["rendered"]) == (
        "https://scoutstervant.be/wp-content/uploads/2026/09/vonk-jevees-oktober-2026.pdf"
    )


def test_extract_pdf_url_prefers_pdfemb_and_falls_back_to_any_pdf_href():
    html = '<a href="/x/other.pdf">a</a><a class="pdfemb-viewer" href="/x/vonk.pdf">b</a>'
    assert extract_pdf_url(html) == "/x/vonk.pdf"
    assert (
        extract_pdf_url('<p><a href="https://s/u/a.PDF?x=1">a</a></p>') == "https://s/u/a.PDF?x=1"
    )
    assert extract_pdf_url('<a href="https://s/page/">no pdf</a>') is None
    assert extract_pdf_url("") is None


def test_pdf_filename_is_decoded_and_lowercased():
    assert pdf_filename("https://s/u/VONK-OKTOBER%20welpen.pdf") == "vonk-oktober welpen.pdf"
    assert pdf_filename(None) == ""


@pytest.mark.parametrize(
    ("text", "tak"),
    [
        ("vonk-jevees-oktober-2026.pdf", Tak.JEEVEES),
        ("JV’S OKTOBER", Tak.JEEVEES),
        ("Vonk jong verkenners", Tak.JEEVEES),
        ("vonk-oktober-jogi.pdf", Tak.JOGIS),
        ("Vonk jong gidsen", Tak.JOGIS),
        ("vonk-okt_jins.pdf", Tak.JINS),
        ("oktober-verkenners-vonk.pdf", Tak.VERKENNERS),
        ("oktober-gidsen-.pdf", Tak.GIDSEN),
        ("vonk-oktober-welpen.pdf", Tak.WELPEN),
        ("vonk-kapoenen-oktober.pdf", Tak.KAPOENEN),
        ("Kabouters in Oktober", Tak.KABOUTERS),
        ("Vonk oktober", None),
        ("welpen en kabouters samen", None),  # ambiguous -> unknown
        ("jinsbroek", None),
    ],
)
def test_tak_from_keywords(text, tak):
    assert tak_from_keywords(text) is tak


def test_resolve_tak_order_category_then_title_then_filename_then_leiding():
    assert resolve_tak([10], "Vonk JV", "vonk-gidsen.pdf") is Tak.KABOUTERS
    assert resolve_tak([6], "Vonk welpen", "vonk-gidsen.pdf") is Tak.WELPEN
    assert resolve_tak([6], "Vonk oktober", "vonk-gidsen.pdf") is Tak.GIDSEN
    assert resolve_tak([21], "Vonk oktober", "doc-1.pdf") is Tak.KABOUTERS
    assert resolve_tak([6], "Vonk oktober", "doc-1.pdf") is None


def test_is_jaarvonk_by_title():
    assert is_jaarvonk("Jaarvonk 2026-2027")
    assert not is_jaarvonk("Vonk oktober")


@pytest.mark.parametrize(
    ("kind", "title", "filename", "posted", "period", "n_warnings"),
    [
        ("monthly", "Vonk Oktober", "vonk-jevees-oktober-2026.pdf", "2026-09-22", "2026-10", 0),
        ("monthly", "Vonk oktober", "orange-halloween-1.pdf", "2026-10-05", "2026-10", 0),
        ("monthly", "Vonk Oktober", "vonk-okt_jins.pdf", "2026-09-22", "2026-10", 0),
        # August post of a September vonk; December post of a January vonk.
        ("monthly", "Vonk september", "x.pdf", "2026-08-28", "2026-09", 0),
        ("monthly", "Vonk januari", "x.pdf", "2026-12-20", "2027-01", 0),
        # A late post of the previous month's vonk stays in that month.
        ("monthly", "Vonk december", "x.pdf", "2027-01-02", "2026-12", 0),
        ("monthly", "Vonk", "x.pdf", "2026-11-03", "2026-11", 1),
        ("monthly", "Vonk september-oktober", "x.pdf", "2026-08-30", "2026-09", 1),
        ("jaarvonk", "Jaarvonk 2026-2027", "jaarvonk-26-27.pdf", "2026-10-08", "2026-2027", 0),
        ("jaarvonk", "Jaarvonk", "jaarvonk-26-27.pdf", "2026-10-08", "2026-2027", 0),
        ("jaarvonk", "Jaarvonk", "jaarvonk.pdf", "2027-03-01", "2026-2027", 1),
        ("jaarvonk", "Jaarvonk", "jaarvonk.pdf", "2026-08-25", "2026-2027", 1),
    ],
)
def test_derive_period(kind, title, filename, posted, period, n_warnings):
    got, warnings = derive_period(kind, title, filename, datetime.fromisoformat(posted))
    assert got == period
    assert len(warnings) == n_warnings


# --- ingest over HTTP --------------------------------------------------------------------


def test_ingest_recorded_posts_for_configured_takken():
    requests: list[httpx.Request] = []
    result = ingest(_client(POSTS, requests=requests), [Tak.KABOUTERS, Tak.JEEVEES], BASE)

    assert [p.scope.key for p in result.posts] == [
        "groep:2026-2027",
        "jeevees:2026-10",
        "kabouters:2026-10",
    ]
    assert result.warnings == ()
    by_key = {p.scope.key: p.post for p in result.posts}
    jaar = by_key["groep:2026-2027"]
    assert (jaar.kind, jaar.tak, jaar.id) == ("jaarvonk", Tak.GROEP, 2686)
    assert jaar.modified.isoformat() == "2026-10-08T17:15:45+02:00"
    kab = by_key["kabouters:2026-10"]
    assert kab.pdf_url.endswith(
        "Orange-Purple-Illustrative-Halloween-Page-Border-A4-Document-1.pdf"
    )
    assert (kab.kind, kab.categories) == ("monthly", (10,))

    queries = [parse_qs(urlsplit(str(r.url)).query) for r in requests]
    assert queries[0]["categories"] == ["10,13,21,25"]  # Vonk + Leiding categories
    assert any(q.get("search") == ["vonk"] for q in queries)
    assert all(q["per_page"] == ["100"] for q in queries)
    assert all(str(r.url).startswith(f"{BASE}/posts?") for r in requests)


def test_ingest_all_takken_resolves_every_recorded_post():
    result = ingest(_client(POSTS), list(Tak), BASE)
    assert len(result.posts) == len(POSTS)
    assert {p.post.tak for p in result.posts} == set(Tak)
    assert result.warnings == ()


def test_ingest_paginates():
    requests: list[httpx.Request] = []
    result = ingest(_client(POSTS, pages=2, requests=requests), list(Tak), BASE)
    assert len(result.posts) == len(POSTS)
    pages = sorted(parse_qs(urlsplit(str(r.url)).query)["page"][0] for r in requests)
    assert pages == ["1", "1", "2", "2"]  # two queries x two pages


def test_missing_pdf_and_unknown_tak_are_warnings_not_silent_skips():
    extra = [
        _post(3001, "Vonk november", "<p>De vonk volgt nog.</p>", categories=[10]),
        _post(3002, "Vonk november", _pdfemb("https://s/u/document-2.pdf"), categories=[6]),
        _post(3003, "Nieuws van de groep", "<p>geen vonk</p>", categories=[6]),
    ]
    result = ingest(_client(POSTS + extra), [Tak.KABOUTERS], BASE)

    assert len(result.warnings) == 2
    no_pdf = next(w for w in result.warnings if "3001" in w)
    unknown = next(w for w in result.warnings if "3002" in w)
    assert "no PDF link" in no_pdf
    assert "unknown tak" in unknown
    assert not any("3003" in w for w in result.warnings)  # not a vonk: ignored
    assert 3001 not in {p.post.id for p in result.posts}


def test_newest_post_per_scope_wins():
    newer = _post(
        3100,
        "Vonk oktober (aangepast)",
        _pdfemb("https://s/u/kabouters-oktober-v2.pdf"),
        categories=[10],
        date="2026-10-07T10:00:00",
    )
    result = ingest(_client(POSTS + [newer]), [Tak.KABOUTERS], BASE)
    kab = next(p for p in result.posts if p.scope.key == "kabouters:2026-10")
    assert kab.post.id == 3100


def test_ingest_raises_on_http_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        ingest(client, [Tak.KABOUTERS], BASE)


@pytest.mark.net
def test_live_wp_ingest_finds_vonks():
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        result = ingest(client, list(Tak))
    assert result.posts
    assert all(p.post.pdf_url for p in result.posts)
