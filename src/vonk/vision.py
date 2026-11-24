"""Vision transcripts of image-only vonk PDFs: an Ollama client, a Claude page reader, the
Nomad dispatch and the ``vonk vision`` command.

An image-only vonk (a poster without a text layer) is never extracted from tesseract
output. Instead the poll dispatches ``vonk vision`` as a parameterized batch job next to an
Ollama server with a vision model. That job downloads the PDF, checks its sha256, renders
every page twice at 150 dpi (colour and greyscale), transcribes each render and stores the
transcript in the service only when both contain the same digits (dates and times are the
part a model gets wrong). The dispatch carries ``VISION_VERSION``; a job image with another
version refuses the run instead of storing a transcript under a key the poll does not ask
for. Every failure is recorded per PDF in the service, so the poll can fall back to
reading the poster with Claude (same renders, same prompt, same digit rule). The next poll
extracts from the stored transcript as from any text layer.

Exit codes of ``vonk vision``: 0 ok, 1 any failure (always reported to the service with
``source = "vision"``), 2 missing settings or job meta (no network calls at all).
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from vonk.config import Settings
from vonk.extract import FALLBACK_BETA, MODEL
from vonk.ingest import ALLOWED_HOSTS, describe_http_error, host_guard
from vonk.pdf import PdfError, download, render_pages, sha256_hex

log = logging.getLogger("vonk.vision")

VISION_VERSION = "2026-11-24.2"
"""Keys the stored transcript: bump on any change to the prompt, the renders or the default
model."""

PROMPT = (
    "This image is a monthly activity calendar (a 'vonk') of one age group of a Flemish "
    "scouts group. Transcribe it to plain text: one line per activity, in the order shown. "
    "Each line: the date exactly as written (weekday, day number, month), then the times as "
    "written, then the activity name, then any remarks (place, what to bring). Copy the "
    "words as written; do not add anything that is not on the poster. "
    "Output only the transcription lines: no title, no headings, no notes or comments."
)

PAGE_TIMEOUT = 15 * 60.0
"""One page takes several minutes on a small GPU; allow 15 minutes per call."""
MODEL_WAIT_TIMEOUT = 300.0
MODEL_WAIT_INTERVAL = 5.0
NOMAD_TIMEOUT = 30.0
HTTP_TIMEOUT = 30.0

PDF_URL_ENV = ("NOMAD_META_PDF_URL", "PDF_URL")
PDF_SHA256_ENV = ("NOMAD_META_PDF_SHA256", "PDF_SHA256")
VISION_VERSION_ENV = ("NOMAD_META_VISION_VERSION", "VISION_VERSION")

RENDER_DPI = 150
"""Both reads use 150 dpi: a larger render runs a small GPU out of memory. The second read
is a greyscale render, so the two runs are not one guess on the same pixels."""
CLAUDE_MAX_TOKENS = 4096

ENGINE_OLLAMA = "ollama"
ENGINE_CLAUDE = "claude"

PageReader = Callable[[bytes], str]


class VisionError(RuntimeError):
    """The vision model or the job dispatch failed, or the transcript is not trustworthy."""


# --- Ollama ------------------------------------------------------------------------------


def _same_digest(expected: str, actual: str) -> bool:
    """True when ``actual`` matches ``expected``; a short (``ollama list``) digest is a
    prefix of the full one."""
    want = expected.lower().removeprefix("sha256:")
    have = actual.lower().removeprefix("sha256:")
    return bool(want) and have.startswith(want)


def wait_for_model(
    http: httpx.Client,
    url: str,
    model: str,
    *,
    digest: str | None = None,
    timeout: float = MODEL_WAIT_TIMEOUT,
    interval: float = MODEL_WAIT_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll ``/api/tags`` until Ollama answers and lists ``model``.

    Raises ``VisionError`` after ``timeout`` seconds (server down or model absent), and at
    once when ``digest`` is set and the listed model has another digest.
    """
    deadline = clock() + timeout
    tags = f"{url.rstrip('/')}/api/tags"
    while True:
        try:
            resp = http.get(tags, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            models = resp.json().get("models") or []
        except httpx.HTTPError as exc:
            problem = f"Ollama not reachable: {describe_http_error(exc)}"
        except (ValueError, AttributeError) as exc:
            problem = f"Ollama /api/tags: malformed body: {type(exc).__name__}"
        else:
            found = next(
                (m for m in models if isinstance(m, Mapping) and model in (m.get("name"),
                                                                           m.get("model"))),
                None,
            )  # fmt: skip
            if found is not None:
                if digest and not _same_digest(digest, str(found.get("digest", ""))):
                    raise VisionError(
                        f"model {model} has digest {found.get('digest')!r}, expected {digest!r}"
                    )
                return
            problem = f"model {model} is not available in Ollama (pull it first)"
        if clock() >= deadline:
            raise VisionError(f"{problem} after {timeout:.0f}s")
        log.info("waiting for Ollama: %s", problem)
        sleep(interval)


def transcribe_page(
    http: httpx.Client, url: str, model: str, png: bytes, *, timeout: float = PAGE_TIMEOUT
) -> str:
    """Ask the model for a plain-text transcript of one page image."""
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "user", "content": PROMPT, "images": [base64.b64encode(png).decode()]}
        ],
    }
    try:
        resp = http.post(f"{url.rstrip('/')}/api/chat", json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise VisionError(f"Ollama /api/chat unreachable: {describe_http_error(exc)}") from exc
    if not resp.is_success:
        raise VisionError(
            f"Ollama /api/chat: HTTP {resp.status_code} (server error, e.g. the model runner "
            f"crashed or ran out of memory){_ollama_error(resp)}"
        )
    try:
        answer = resp.json()
        content = answer["message"]["content"]
    except (ValueError, KeyError, TypeError) as exc:
        raise VisionError(f"Ollama /api/chat: malformed body: {type(exc).__name__}") from exc
    if not isinstance(content, str) or not content.strip():
        reason = answer.get("done_reason") if isinstance(answer, Mapping) else None
        raise VisionError(
            f"Ollama /api/chat: the model returned empty content (done_reason={reason!r})"
        )
    return content.strip()


def _ollama_error(resp: httpx.Response) -> str:
    """``: <message>`` from Ollama's ``{"error": ...}`` body, shortened; else empty."""
    try:
        message = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return f": {str(message)[:200]}" if message else ""


def claude_read_page(client: Any, png: bytes, *, model: str = MODEL) -> str:
    """Ask Claude for a plain-text transcript of one page image (same prompt as Ollama)."""
    image = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=CLAUDE_MAX_TOKENS,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": image},
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
    except Exception as exc:  # anthropic.APIError and transport errors alike
        raise VisionError(f"Claude API call failed: {type(exc).__name__}: {exc}") from exc
    stop = getattr(response, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        raise VisionError(f"Claude stopped with stop_reason={stop!r}")
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        raise VisionError("Claude returned no transcript")
    return text


def digits(text: str) -> list[str]:
    """Every run of digits in ``text``, in order (day numbers, times, years)."""
    return re.findall(r"\d+", text)


def transcribe(
    pages: Sequence[bytes], second_pages: Sequence[bytes], read_page: PageReader
) -> str:
    """Transcribe every page twice, once per render; the digit tokens must be identical.

    Returns the run on ``pages`` of every page, joined by ``\\f``. A mismatch raises
    ``VisionError``: the model is guessing at the numbers, so nothing may be stored.
    """
    if len(pages) != len(second_pages):
        raise VisionError(f"renders differ in page count ({len(pages)} vs {len(second_pages)})")
    texts = []
    for number, (png, second_png) in enumerate(zip(pages, second_pages, strict=True), 1):
        first = read_page(png)
        second = read_page(second_png)
        if digits(first) != digits(second):
            raise VisionError(
                f"page {number}: two runs disagree on the digits "
                f"({' '.join(digits(first))} vs {' '.join(digits(second))})"
            )
        texts.append(first)
    return "\f".join(texts)


# --- Nomad dispatch ----------------------------------------------------------------------


def nomad_dispatch(
    settings: Settings,
    pdf_url: str,
    pdf_sha256: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Dispatch the vision job through the Nomad task API socket; returns the job id.

    Uses ``$NOMAD_SECRETS_DIR/api.sock`` and the workload identity token in ``NOMAD_TOKEN``
    (the poll job needs ``identity { env = true }`` and a policy with ``dispatch-job``).
    """
    if not settings.nomad_secrets_dir or not settings.nomad_token:
        raise VisionError("vision dispatch needs NOMAD_SECRETS_DIR and NOMAD_TOKEN")
    if transport is None:
        sock = Path(settings.nomad_secrets_dir) / "api.sock"
        transport = httpx.HTTPTransport(uds=str(sock))
    path = f"/v1/job/{settings.vonk_vision_job}/dispatch"
    body = {
        "Meta": {"PDF_URL": pdf_url, "PDF_SHA256": pdf_sha256, "VISION_VERSION": VISION_VERSION}
    }
    try:
        with httpx.Client(transport=transport, timeout=NOMAD_TIMEOUT) as http:
            resp = http.post(
                f"http://localhost{path}",
                params={"namespace": settings.vonk_vision_namespace},
                headers={"X-Nomad-Token": settings.nomad_token},
                json=body,
            )
            resp.raise_for_status()
            job_id = resp.json().get("DispatchedJobID")
    except httpx.HTTPStatusError as exc:
        raise VisionError(f"Nomad dispatch: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise VisionError(f"Nomad dispatch failed: {type(exc).__name__}") from exc
    except (ValueError, AttributeError) as exc:
        raise VisionError(f"Nomad dispatch: malformed body: {type(exc).__name__}") from exc
    if not isinstance(job_id, str) or not job_id:
        raise VisionError("Nomad dispatch: no DispatchedJobID in the answer")
    return job_id


# --- vonk vision -------------------------------------------------------------------------


def _meta(env: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


class VisionRun:
    """One ``vonk vision`` run; collaborators are injected so tests run fully offline."""

    def __init__(
        self,
        settings: Settings,
        service: Any,
        pdf_http: httpx.Client,
        ollama_http: httpx.Client,
        *,
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
        render: Callable[..., list[bytes]] = render_pages,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.settings = settings
        self.service = service
        self.pdf_http = pdf_http
        self.ollama_http = ollama_http
        self.allowed_hosts = allowed_hosts
        self.render = render
        self.sleep = sleep
        self.monotonic = monotonic
        self.now = now
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def transcript(self, pdf_url: str, pdf_sha256: str) -> str:
        data = download(self.pdf_http, pdf_url, allowed_hosts=self.allowed_hosts)
        actual = sha256_hex(data)
        if actual != pdf_sha256:
            raise VisionError(f"PDF sha256 {actual} differs from the dispatched {pdf_sha256}")
        pages = self.render(data, dpi=RENDER_DPI)
        second_pages = self.render(data, dpi=RENDER_DPI, gray=True)
        url, model = self.settings.vonk_vision_url, self.settings.vonk_vision_model
        assert url is not None
        wait_for_model(self.ollama_http, url, model,
                       digest=self.settings.vonk_vision_model_digest, sleep=self.sleep,
                       clock=self.monotonic)  # fmt: skip
        log.info("transcribing %d page(s) twice with %s", len(pages), model)

        def read(png: bytes) -> str:
            return transcribe_page(self.ollama_http, url, model, png)

        return transcribe(pages, second_pages, read)

    def fail(self, pdf_sha256: str, reason: str) -> None:
        """Count a failure and record it for this PDF, so the poll can fall back to Claude."""
        from vonk.poll import ServiceError

        self.errors.append(f"vision {pdf_sha256}: {reason}")
        log.error("%s", reason)
        try:
            self.service.vision_failure(pdf_sha256, reason, ENGINE_OLLAMA)
        except ServiceError as exc:
            log.error("vision failure not recorded: %s", exc)

    def run(self, pdf_url: str, pdf_sha256: str, vision_version: str | None = None) -> int:
        """``vision_version`` is the dispatcher's VISION_VERSION (None: not sent)."""
        from vonk.poll import ServiceError

        started = self.now()
        try:
            if vision_version is not None and vision_version != VISION_VERSION:
                raise VisionError(
                    f"dispatched for vision version {vision_version}, this image has "
                    f"{VISION_VERSION}; update the job image or the poll"
                )
            if self.service.transcript(pdf_sha256, VISION_VERSION) is not None:
                log.info("transcript of %s already stored; nothing to do", pdf_sha256)
            else:
                text = self.transcript(pdf_url, pdf_sha256)
                self.service.put_transcript(pdf_sha256, VISION_VERSION, text, ENGINE_OLLAMA)
                log.info("stored transcript of %s (%d chars)", pdf_sha256, len(text))
        except (PdfError, VisionError, ServiceError) as exc:
            self.fail(pdf_sha256, str(exc))
        except Exception as exc:  # report anything unexpected, then fail the run
            self.fail(pdf_sha256, f"unexpected {type(exc).__name__}: {exc}")
            log.exception("vision failed")
        ok = not self.errors
        report = {
            "source": "vision",
            "started": started.isoformat(),
            "finished": self.now().isoformat(),
            "ok": ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        try:
            self.service.poll_report(report)
        except ServiceError as exc:
            ok = False
            log.error("vision report not delivered: %s", exc)
        return 0 if ok else 1


def run_vision(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    **kwargs: Any,
) -> int:
    """Entry point: check settings and job meta, build HTTP clients and run once."""
    from vonk.poll import USER_AGENT, ServiceClient

    env = os.environ if env is None else env
    pdf_url, pdf_sha256 = _meta(env, PDF_URL_ENV), _meta(env, PDF_SHA256_ENV)
    vision_version = _meta(env, VISION_VERSION_ENV)
    problems = settings.missing_secrets("vision") + list(settings.errors)
    if pdf_url is None:
        problems.append("PDF_URL")
    if pdf_sha256 is None:
        problems.append("PDF_SHA256")
    if problems:
        log.error("vonk vision: missing or invalid settings: %s", ", ".join(problems))
        return 2
    assert settings.vonk_api_url and settings.vonk_api_token and pdf_url and pdf_sha256
    client_kw: dict[str, Any] = {"timeout": HTTP_TIMEOUT, "headers": {"User-Agent": USER_AGENT}}
    if transport is not None:
        client_kw["transport"] = transport
    with (
        httpx.Client(**client_kw) as service_http,
        httpx.Client(**client_kw) as ollama_http,
        httpx.Client(
            follow_redirects=True, event_hooks={"request": [host_guard(allowed_hosts)]}, **client_kw
        ) as pdf_http,
    ):
        service = ServiceClient(service_http, settings.vonk_api_url, settings.vonk_api_token)
        return VisionRun(
            settings, service, pdf_http, ollama_http, allowed_hosts=allowed_hosts, **kwargs
        ).run(pdf_url, pdf_sha256.lower(), vision_version)


def cmd_vision(args: argparse.Namespace) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="VONK %(levelname)s %(name)s: %(message)s")
    return run_vision(Settings.from_env())


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add ``vonk vision`` to the CLI (PDF_URL / PDF_SHA256 come from the environment)."""
    parser = subparsers.add_parser(
        "vision", help="transcribe one image-only vonk PDF with the vision model"
    )
    parser.set_defaults(func=cmd_vision)
