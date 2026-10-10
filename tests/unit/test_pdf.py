"""pdf: bounded download, sha256 and pdftotext on a generated one-line PDF."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from vonk.ingest import host_guard
from vonk.pdf import PdfError, download, fetch_text, pdftotext, sha256_hex

FIXTURES = Path(__file__).parents[1] / "fixtures"


def make_pdf(text: str) -> bytes:
    """A minimal valid single-page PDF showing ``text`` (ASCII) in Helvetica."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


PDF = make_pdf("Zaterdag 24 oktober Dropping")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pdftotext_extracts_layout_text():
    assert "Zaterdag 24 oktober Dropping" in pdftotext(PDF)


def test_pdftotext_rejects_garbage():
    with pytest.raises(PdfError, match="pdftotext exited"):
        pdftotext(b"%PDF-1.4\nnot really a pdf")


def test_sha256_hex():
    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_fetch_text_downloads_hashes_and_converts():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})

    result = fetch_text(_client(handler), "https://scoutstervant.be/u/vonk.pdf")
    assert result.sha256 == hashlib.sha256(PDF).hexdigest()
    assert "Dropping" in result.text
    assert [str(r.url) for r in seen] == ["https://scoutstervant.be/u/vonk.pdf"]


def test_download_rejects_declared_oversize():
    client = _client(lambda r: httpx.Response(200, content=PDF))
    with pytest.raises(PdfError, match="Content-Length"):
        download(client, "https://scoutstervant.be/u/big.pdf", max_bytes=len(PDF) - 1)


def test_download_rejects_streamed_oversize_without_content_length():
    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(PDF))  # no Content-Length

    with pytest.raises(PdfError, match="exceeds"):
        download(_client(handler), "https://scoutstervant.be/u/big.pdf", max_bytes=100)


def test_download_rejects_non_pdf_and_http_errors():
    with pytest.raises(PdfError, match="not a PDF"):
        download(_client(lambda r: httpx.Response(200, text="<html>")), "https://scoutstervant.be/u/x.pdf")
    with pytest.raises(PdfError, match="download failed"):
        download(_client(lambda r: httpx.Response(404)), "https://scoutstervant.be/u/x.pdf")


def test_download_wraps_transport_errors():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(PdfError, match="download failed"):
        download(_client(handler), "https://scoutstervant.be/u/x.pdf")


@pytest.mark.net
def test_live_pdf_matches_recorded_sha256():
    src = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["fixtures"][0]
    with httpx.Client(timeout=60) as client:
        result = fetch_text(client, src["url"])
    assert result.sha256 == src["sha256"]
    assert result.text.strip()


# --- SSRF guard ----------------------------------------------------------------------------


def _guarded(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), event_hooks={"request": [host_guard()]}
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/vonk.pdf",
        "http://scoutstervant.be/vonk.pdf",
        "https://scoutstervant.be:8443/vonk.pdf",
        "https://127.0.0.1/vonk.pdf",
    ],
)
def test_download_blocks_foreign_hosts_without_any_request(url):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=PDF)

    with pytest.raises(PdfError, match="blocked"):
        download(_client(handler), url)
    assert seen == []


@pytest.mark.parametrize(
    "target", ["http://127.0.0.1:4646/v1/jobs", "https://consul.service.internal/secret"]
)
def test_download_blocks_redirect_to_internal_host(target):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "scoutstervant.be":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=PDF)

    with pytest.raises(PdfError) as info:
        download(_guarded(handler), "https://scoutstervant.be/u/vonk.pdf")
    assert seen == ["https://scoutstervant.be/u/vonk.pdf"]
    message = str(info.value)
    assert "blocked" in message
    assert "/v1/jobs" not in message and "/secret" not in message


def test_download_follows_redirect_within_allowed_hosts():
    def handler(request):
        if request.url.host == "scoutstervant.be":
            return httpx.Response(301, headers={"Location": "https://www.scoutstervant.be/v.pdf"})
        return httpx.Response(200, content=PDF)

    assert download(_guarded(handler), "https://scoutstervant.be/v.pdf") == PDF


def test_download_error_names_host_and_status_only():
    with pytest.raises(PdfError) as info:
        download(_client(lambda r: httpx.Response(503, text="stack trace at /srv/x")),
                 "https://scoutstervant.be/private/path.pdf")  # fmt: skip
    assert str(info.value) == "download failed: scoutstervant.be: HTTP 503"
