"""PDF download, sha256 and `pdftotext -layout` text extraction, with an OCR fallback."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from vonk.ingest import ALLOWED_HOSTS, check_url, describe_http_error

MAX_PDF_BYTES = 25 * 1024 * 1024
"""Upper bound for one vonk PDF; Canva exports are a few MB at most."""

DOWNLOAD_TIMEOUT = 60.0
PDFTOTEXT_TIMEOUT = 60.0
OCR_TIMEOUT = 120.0
OCR_DPI = 300
"""Image-only vonks are low-res JPEG exports; tesseract needs them upscaled to read them."""
OCR_LANG = "nld"


class PdfError(RuntimeError):
    """Download or text extraction of a PDF failed."""


class EmptyPdfError(PdfError):
    """The PDF has no text layer and OCR found no text either."""


@dataclass(frozen=True, slots=True)
class PdfText:
    """A downloaded PDF: its sha256 (hex) and its ``pdftotext -layout`` (or OCR) output."""

    sha256: str
    text: str
    ocr: bool = False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    timeout: float = DOWNLOAD_TIMEOUT,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
) -> bytes:
    """Stream ``url`` into memory, aborting once more than ``max_bytes`` arrive.

    Only https URLs on ``allowed_hosts`` are fetched; redirect hops are checked by the
    client's ``host_guard`` hook. Errors name the host and status only.
    """
    try:
        check_url(url, allowed_hosts)
        host = httpx.URL(url).host
        with client.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise PdfError(f"{host}: Content-Length {declared} exceeds {max_bytes} bytes")
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise PdfError(f"{host}: body exceeds {max_bytes} bytes")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise PdfError(f"download failed: {describe_http_error(exc)}") from exc
    data = b"".join(chunks)
    if not data.startswith(b"%PDF"):
        raise PdfError(f"{host}: not a PDF (missing %PDF header)")
    return data


def pdftotext(data: bytes, *, timeout: float = PDFTOTEXT_TIMEOUT) -> str:
    """Run ``pdftotext -layout`` on ``data`` in a private temp dir; returns UTF-8 text."""
    with tempfile.TemporaryDirectory(prefix="vonk-pdf-") as tmp:
        src = Path(tmp) / "in.pdf"
        dst = Path(tmp) / "out.txt"
        src.write_bytes(data)
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(src), str(dst)],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PdfError("pdftotext not found (install poppler-utils)") from exc
        except subprocess.TimeoutExpired as exc:
            raise PdfError(f"pdftotext timed out after {timeout}s") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise PdfError(f"pdftotext exited {proc.returncode}: {stderr}")
        if not dst.exists():
            raise PdfError("pdftotext produced no output file")
        return dst.read_text(encoding="utf-8")


def _run(cmd: list[str], timeout: float, package: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise PdfError(f"{cmd[0]} not found (install {package})") from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfError(f"{cmd[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise PdfError(f"{cmd[0]} exited {proc.returncode}: {stderr}")


def ocr(data: bytes, *, dpi: int = OCR_DPI, lang: str = OCR_LANG,
        timeout: float = OCR_TIMEOUT) -> str:  # fmt: skip
    """Render every page with ``pdftoppm`` and read it with ``tesseract``; pages split by \\f."""
    with tempfile.TemporaryDirectory(prefix="vonk-ocr-") as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(data)
        _run(["pdftoppm", "-r", str(dpi), "-png", str(src), str(Path(tmp) / "page")],
             timeout, "poppler-utils")  # fmt: skip
        pages = []
        for png in sorted(Path(tmp).glob("page-*.png")):
            out = png.with_suffix("")
            _run(["tesseract", str(png), str(out), "-l", lang], timeout,
                 f"tesseract-ocr and tesseract-ocr-{lang}")  # fmt: skip
            pages.append(out.with_suffix(".txt").read_text(encoding="utf-8"))
        return "\f".join(pages)


def fetch_text(
    client: httpx.Client, url: str, allowed_hosts: frozenset[str] = ALLOWED_HOSTS
) -> PdfText:
    """Download ``url`` and return its sha256 and layout text.

    A PDF without a text layer (a scan or image export) goes through OCR instead; when
    that finds nothing either, ``EmptyPdfError`` is raised.
    """
    data = download(client, url, allowed_hosts=allowed_hosts)
    sha256 = sha256_hex(data)
    text = pdftotext(data)
    if text.strip():
        return PdfText(sha256=sha256, text=text)
    text = ocr(data)
    if not text.strip():
        raise EmptyPdfError("PDF has no text layer and OCR found no text")
    return PdfText(sha256=sha256, text=text, ocr=True)
