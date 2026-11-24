"""Stateless poll run: ingest, extract, normalise and POST desired events to the service API.

``vonk poll`` holds no state: what it has seen and the extraction cache live in the service
(``vonk serve``). Every run ends with ``POST /api/poll-report`` (also on failure), which
drives the liveness gauges. Exit codes: 0 ok, 1 any failure (service unreachable, 401,
download/extraction errors), 2 missing poll secrets (no network calls at all).

An image-only PDF (no text layer) is extracted from its vision transcript, never from OCR
text: when the service has no transcript yet, the poll dispatches the vision job (at most
once per 12 hours per PDF) and leaves the post pending, so a later poll picks the transcript
up. When that job failed for the PDF, produced nothing within ``VONK_VISION_FALLBACK_AFTER``
hours, or dispatch is not configured, the poll reads the poster itself with Claude (two
renders, the same digit rule), stores that transcript with engine ``claude`` and goes on in
the same run. A failed Claude read is retried after ``VONK_VISION_RETRY_AFTER`` hours, at
most ``VONK_VISION_MAX_FALLBACKS`` times; meanwhile the post is not downloaded again.
Such extractions are cached under ``VISION_PROMPT_VERSION``.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from vonk import __version__
from vonk.config import Settings
from vonk.extract import (
    PROMPT_VERSION,
    ExtractionError,
    ExtractionResult,
    cross_check,
    extract,
    verify_spans,
)
from vonk.ingest import (
    ALLOWED_HOSTS,
    WP_API_URL,
    IngestedPost,
    describe_http_error,
    host_guard,
    ingest,
)
from vonk.models import Event, ExtractedSpan, Scope, VonkKind, VonkPost
from vonk.pdf import EmptyPdfError, PdfError, PdfText, fetch_text, render_pages
from vonk.vision import (
    CLAUDE_MAX_EDGE,
    ENGINE_CLAUDE,
    RENDER_DPI,
    VISION_VERSION,
    VisionError,
    claude_read_page,
    nomad_dispatch,
    transcribe,
)

log = logging.getLogger("vonk.poll")

HTTP_TIMEOUT = 30.0
USER_AGENT = f"vonk/{__version__} (+https://github.com/visibilityspots/vonk)"

Normalizer = Callable[[Sequence[ExtractedSpan], VonkPost, Scope, list[str]], list[Event]]
Extractor = Callable[[str, VonkKind], ExtractionResult]
PdfFetcher = Callable[[str], PdfText]
VisionDispatcher = Callable[[str, str], str]
"""(pdf_url, pdf_sha256) -> dispatched job id."""

VISION_PROMPT_VERSION = PROMPT_VERSION + "-vision1"
"""Cache key of extractions from a vision transcript; text-layer PDFs keep PROMPT_VERSION."""
VISION_WARNING = "vision transcript ({engine}): check dates against the poster"
"""Fixed ChangeSet warning on every extraction from a transcript; names the engine."""

Seen = tuple[datetime, str | None, bool | None]
"""(last processed ``modified``, PDF sha256, image-only PDF or None when not recorded)."""


class ServiceError(RuntimeError):
    """The vonk service could not be reached or answered with an error (fatal for a run)."""


def _default_normalizer(
    spans: Sequence[ExtractedSpan], post: VonkPost, scope: Scope, warnings: list[str]
) -> list[Event]:
    from vonk.normalize import normalize

    return normalize(spans, post, scope, warnings)


class ServiceClient:
    """Bearer-authenticated client for the service's internal ``/api``."""

    def __init__(self, http: httpx.Client, base_url: str, token: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            resp = self._http.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ServiceError(
                f"{method} {path}: service unreachable ({type(exc).__name__})"
            ) from exc
        return resp

    @staticmethod
    def _fail(method: str, path: str, resp: httpx.Response) -> ServiceError:
        return ServiceError(f"{method} {path}: HTTP {resp.status_code}")

    def seen(self) -> dict[int, Seen]:
        """post id -> (last processed ``modified`` timestamp, PDF sha256, ocr flag)."""
        resp = self._request("GET", "/api/seen")
        if not resp.is_success:
            raise self._fail("GET", "/api/seen", resp)
        try:
            return {
                int(p["post_id"]): (
                    datetime.fromisoformat(p["modified"]), p.get("pdf_sha256"), p.get("ocr")
                )
                for p in resp.json()["posts"]
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"GET /api/seen: malformed body: {exc}") from exc

    def cached_spans(self, sha256: str, prompt_version: str) -> list[ExtractedSpan] | None:
        """Cached spans for ``sha256 + prompt_version``; None on a miss (404)."""
        path = f"/api/cache/{sha256}/{prompt_version}"
        resp = self._request("GET", path)
        if resp.status_code == 404:
            return None
        if not resp.is_success:
            raise self._fail("GET", path, resp)
        try:
            return [ExtractedSpan.from_dict(s) for s in resp.json()["spans"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"GET {path}: malformed body: {exc}") from exc

    def transcript(self, sha256: str, vision_version: str) -> tuple[str, str] | None:
        """Stored vision transcript of the PDF and its engine; None on a miss (404)."""
        path = f"/api/transcripts/{sha256}/{vision_version}"
        resp = self._request("GET", path)
        if resp.status_code == 404:
            return None
        if not resp.is_success:
            raise self._fail("GET", path, resp)
        try:
            body = resp.json()
            return str(body["text"]), str(body.get("engine") or "ollama")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ServiceError(f"GET {path}: malformed body: {exc}") from exc

    def put_transcript(
        self, sha256: str, vision_version: str, text: str, engine: str = "ollama"
    ) -> bool:
        """Store a transcript; False when one was stored before (the first one wins)."""
        path = f"/api/transcripts/{sha256}/{vision_version}"
        resp = self._request("PUT", path, json={"text": text, "engine": engine})
        if not resp.is_success:
            raise self._fail("PUT", path, resp)
        try:
            return bool(resp.json()["stored"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"PUT {path}: malformed body: {exc}") from exc

    def vision_status(self, sha256: str, vision_version: str) -> dict[str, Any]:
        """``dispatch`` (False while a successful dispatch is recent), ``dispatch_age``
        (seconds since the first dispatch of this version), ``dispatched_at``,
        ``failed_at`` (vision job) and ``fallback_age`` / ``fallback_attempts`` (Claude)."""
        path = f"/api/vision-dispatch/{sha256}"
        resp = self._request("GET", path, params={"vision_version": vision_version})
        if not resp.is_success:
            raise self._fail("GET", path, resp)
        try:
            body = dict(resp.json())
            body["dispatch"] = bool(body["dispatch"])
            return body
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"GET {path}: malformed body: {exc}") from exc

    def vision_failure(
        self,
        sha256: str,
        vision_version: str,
        reason: str,
        engine: str,
        post: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a failed vision transcription of the PDF; ``post`` (``{id, modified,
        scope_key}``) also marks the post seen as image-only."""
        body: dict[str, Any] = {"pdf_sha256": sha256, "vision_version": vision_version,
                                "reason": reason, "engine": engine}  # fmt: skip
        if post is not None:
            body["post"] = dict(post)
        resp = self._request("POST", "/api/vision-failures", json=body)
        if not resp.is_success:
            raise self._fail("POST", "/api/vision-failures", resp)

    def vision_dispatch(
        self, sha256: str, post_id: int, ok: bool, *, modified: str = "", scope_key: str = ""
    ) -> None:
        """Report the outcome of a vision dispatch (only a success is recorded; it also
        marks the post seen as image-only, which needs ``modified`` and ``scope_key``)."""
        body: dict[str, Any] = {"pdf_sha256": sha256, "post_id": post_id, "ok": ok}
        if ok:
            body |= {"modified": modified, "scope_key": scope_key,
                     "vision_version": VISION_VERSION}  # fmt: skip
        resp = self._request("POST", "/api/vision-dispatch", json=body)
        if not resp.is_success:
            raise self._fail("POST", "/api/vision-dispatch", resp)

    def post_desired(self, scope_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        path = f"/api/scopes/{scope_key}/desired"
        resp = self._request("POST", path, json=body)
        if not resp.is_success:
            raise self._fail("POST", path, resp)
        try:
            return dict(resp.json())
        except ValueError:
            return {}

    def poll_report(self, body: Mapping[str, Any]) -> None:
        resp = self._request("POST", "/api/poll-report", json=body)
        if not resp.is_success:
            raise self._fail("POST", "/api/poll-report", resp)


@dataclass(frozen=True, slots=True)
class PollOptions:
    reextract: bool = False
    """Process every post, ignoring what the service has seen."""
    fresh: bool = False
    """Ignore the extraction cache and call the LLM again."""


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def ocr_guard(spans: Sequence[ExtractedSpan], ocr_text: str) -> list[str]:
    """Weak check of a vision extraction: every title should also occur in the OCR text
    (case- and space-insensitive). A miss is a warning, never a reason to drop the item."""
    haystack = _squash(ocr_text)
    if not haystack:
        return ["vision guard: no OCR text available to check the titles against"]
    return [
        f"vision guard: title {s.title_span!r} not found in the OCR text"
        for s in spans
        if _squash(s.title_span) not in haystack
    ]


def post_to_dict(post: VonkPost) -> dict[str, Any]:
    return {
        "id": post.id,
        "modified": post.modified.isoformat(),
        "title": post.title,
        "link": post.link,
        "pdf_url": post.pdf_url,
        "categories": list(post.categories),
        "tak": post.tak.value if post.tak else None,
        "kind": post.kind,
        "date": post.date.isoformat() if post.date else None,
    }


class PollRun:
    """One poll run; collaborators are injected so tests run fully offline."""

    def __init__(
        self,
        settings: Settings,
        service: ServiceClient,
        wp_http: httpx.Client,
        *,
        options: PollOptions | None = None,
        fetch_pdf: PdfFetcher | None = None,
        extractor: Extractor | None = None,
        normalizer: Normalizer | None = None,
        dispatch_vision: VisionDispatcher | None = None,
        vision_client: Any = None,
        render: Callable[..., list[bytes]] = render_pages,
        wp_url: str = WP_API_URL,
        allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.settings = settings
        self.service = service
        self.wp_http = wp_http
        self.options = options or PollOptions()
        self.fetch_pdf = fetch_pdf or (lambda url: fetch_text(wp_http, url, allowed_hosts))
        self.extractor = extractor or self._extract_with_claude
        self.normalizer = normalizer or _default_normalizer
        self.dispatch_vision = dispatch_vision or (
            lambda url, sha: nomad_dispatch(settings, url, sha)
        )
        self._vision_client = vision_client
        self.render = render
        self.wp_url = wp_url
        self.now = now
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def _extract_with_claude(self, text: str, kind: VonkKind) -> ExtractionResult:
        import anthropic

        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return extract(text, kind, client=client)

    def extraction(
        self, item: IngestedPost, sha256: str, text: str, version: str = PROMPT_VERSION
    ) -> ExtractionResult:
        """Cached spans when they still verify against ``text``, else a fresh run."""
        kind = item.post.kind
        if not self.options.fresh:
            cached = self.service.cached_spans(sha256, version)
            if cached is not None:
                kept, problems = verify_spans(cached, text)
                if not problems:
                    log.info("%s: using cached spans (%d)", item.scope.key, len(kept))
                    return ExtractionResult(tuple(kept), tuple(cross_check(text, kept, kind)))
                log.warning("%s: cached spans no longer match the text; re-extracting",
                            item.scope.key)  # fmt: skip
        return self.extractor(text, kind)

    @property
    def vision_enabled(self) -> bool:
        """An image-only PDF can be read: by the dispatched job or by the Claude fallback."""
        return self.settings.vonk_vision_dispatch or self.settings.anthropic_api_key is not None

    def fallback_due(self, status: Mapping[str, Any]) -> bool:
        """True when the poll should read the poster itself with Claude: dispatch is not
        configured, the vision job failed for this PDF (this vision version) after its latest
        dispatch, or the first dispatch of this version is older than
        ``VONK_VISION_FALLBACK_AFTER`` hours without a transcript."""
        if self.settings.anthropic_api_key is None:
            return False
        if not self.settings.vonk_vision_dispatch:
            return True
        failed, dispatched = status.get("failed_at"), status.get("dispatched_at")
        if failed is not None and (dispatched is None or failed >= dispatched):
            return True
        age = status.get("dispatch_age")
        return age is not None and age >= self.settings.vonk_vision_fallback_after * 3600

    def vision_action(self, status: Mapping[str, Any]) -> str:
        """What to do for an image-only PDF without transcript: ``claude`` (read it now),
        ``backoff`` (a Claude attempt failed less than ``VONK_VISION_RETRY_AFTER`` hours
        ago), ``exhausted`` (``VONK_VISION_MAX_FALLBACKS`` attempts failed), ``dispatch``,
        ``wait`` (a dispatch is in its window) or ``skip`` (no way to read it)."""
        if self.fallback_due(status):
            attempts = status.get("fallback_attempts") or 0
            if attempts >= self.settings.vonk_vision_max_fallbacks:
                return "exhausted"
            age = status.get("fallback_age")
            if age is not None and age < self.settings.vonk_vision_retry_after * 3600:
                return "backoff"
            return "claude"
        if not self.settings.vonk_vision_dispatch:
            return "skip"
        return "dispatch" if status.get("dispatch") else "wait"

    def _exhausted(self, item: IngestedPost, status: Mapping[str, Any]) -> None:
        label = f"{item.scope.key} (post {item.post.id})"
        msg = (f"{label}: Claude vision fallback gave up after "
               f"{status.get('fallback_attempts')} attempts; the post stays pending")  # fmt: skip
        self.warnings.append(msg)
        log.warning("%s", msg)

    def claude_transcript(self, item: IngestedPost, pdf: PdfText) -> tuple[str, str] | None:
        """Read the poster with Claude: a colour and a greyscale render of every page (long
        edge at most ``CLAUDE_MAX_EDGE``) must agree on the digits. Stores the transcript
        (engine ``claude``) and returns it with its engine (an earlier stored transcript
        wins). On failure records it, warns and returns None (the post stays pending); a
        service error is a run error for this post only."""
        label = f"{item.scope.key} (post {item.post.id})"
        log.info("%s: transcribing the image-only PDF with Claude", label)
        try:
            pages = self.render(pdf.data, dpi=RENDER_DPI, max_edge=CLAUDE_MAX_EDGE)
            gray = self.render(pdf.data, dpi=RENDER_DPI, gray=True, max_edge=CLAUDE_MAX_EDGE)
            client = self.vision_client()
            text = transcribe(pages, gray, lambda png: claude_read_page(client, png))
        except (PdfError, VisionError) as exc:
            reason = f"Claude vision fallback failed: {exc}"
            self.warnings.append(f"{label}: {reason}")
            log.warning("%s: %s", label, reason)
            post = {"id": item.post.id, "modified": item.post.modified.isoformat(),
                    "scope_key": item.scope.key}  # fmt: skip
            try:
                self.service.vision_failure(pdf.sha256, VISION_VERSION, reason, ENGINE_CLAUDE,
                                            post)  # fmt: skip
            except ServiceError as err:
                self.errors.append(f"{label}: Claude fallback failure not recorded: {err}")
                log.error("%s: Claude fallback failure not recorded: %s", label, err)
            return None
        try:
            if self.service.put_transcript(pdf.sha256, VISION_VERSION, text, ENGINE_CLAUDE):
                log.info("%s: stored the Claude transcript (%d chars)", label, len(text))
                return text, ENGINE_CLAUDE
            log.info("%s: a transcript was stored meanwhile; using that one", label)
            return self.service.transcript(pdf.sha256, VISION_VERSION) or (text, ENGINE_CLAUDE)
        except ServiceError as err:
            self.errors.append(f"{label}: Claude transcript not stored: {err}")
            log.error("%s: Claude transcript not stored: %s", label, err)
            return None

    def vision_client(self) -> Any:
        if self._vision_client is None:
            import anthropic

            self._vision_client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._vision_client

    def request_vision(
        self, item: IngestedPost, pdf: PdfText, status: Mapping[str, Any]
    ) -> None:
        """No transcript yet: dispatch the vision job (unless one is recent) and move on."""
        post, label = item.post, f"{item.scope.key} (post {item.post.id})"
        assert post.pdf_url is not None
        if not self.settings.vonk_vision_dispatch:
            self.warnings.append(f"{label}: image-only PDF skipped (vision dispatch disabled)")
            log.warning("%s: image-only PDF skipped (vision dispatch disabled)", label)
            return
        if not status.get("dispatch"):
            log.info("%s: vision job dispatched recently; waiting for its transcript", label)
            return
        try:
            job_id = self.dispatch_vision(post.pdf_url, pdf.sha256)
        except VisionError:
            # not recorded, so the next poll dispatches again; counted as a vision error
            self._report_dispatch(item, pdf.sha256, ok=False)
            raise
        log.info("%s: image-only PDF, dispatched vision job %s", label, job_id)
        self._report_dispatch(item, pdf.sha256, ok=True)

    def _report_dispatch(self, item: IngestedPost, sha256: str, *, ok: bool) -> None:
        """Tell the service how a dispatch went. A failure here is counted as a run error
        but never masks the dispatch error or stops the remaining posts."""
        label = f"{item.scope.key} (post {item.post.id})"
        try:
            self.service.vision_dispatch(sha256, item.post.id, ok, scope_key=item.scope.key,
                                         modified=item.post.modified.isoformat())  # fmt: skip
        except ServiceError as exc:
            if ok:
                self.errors.append(f"{label}: vision job dispatched but not recorded: {exc}")
                log.error("%s: vision job DISPATCHED BUT NOT RECORDED (the next poll may "
                          "dispatch it again): %s", label, exc)  # fmt: skip
            else:
                self.errors.append(f"{label}: failed dispatch not reported: {exc}")
                log.error("%s: failed dispatch not reported: %s", label, exc)

    def vision_pending(self, seen: Seen | None, item: Any) -> bool:
        """True for an unchanged image-only post that waits for its transcript: its vision
        job is in the 12 hour window, a failed Claude fallback is in its backoff, or the
        fallback attempts are used up. No download, no OCR, no dispatch, no Claude call."""
        if (
            seen is None
            or seen[0] != item.post.modified
            or not seen[1]
            or seen[2] is not True
            or not self.vision_enabled
        ):
            return False
        sha = seen[1]
        if self.service.transcript(sha, VISION_VERSION) is not None:
            return False
        status = self.service.vision_status(sha, VISION_VERSION)
        action = self.vision_action(status)
        if action == "exhausted":
            self._exhausted(item, status)
            return True
        if action in ("wait", "backoff"):
            log.info("%s (post %s): waiting for the vision transcript (%s)",
                     item.scope.key, item.post.id, action)  # fmt: skip
            return True
        return False

    def process(self, item: IngestedPost) -> None:
        post, scope = item.post, item.scope
        assert post.pdf_url is not None  # ingest only yields posts with a PDF link
        try:
            pdf = self.fetch_pdf(post.pdf_url)
        except EmptyPdfError as exc:
            # OCR found nothing, but the vision model may still read the poster
            if not (self.vision_enabled and exc.sha256):
                raise
            pdf = PdfText(sha256=exc.sha256, text="", ocr=True, data=exc.data)
        engine = ""
        if pdf.ocr:
            stored = self.service.transcript(pdf.sha256, VISION_VERSION)
            if stored is None:
                status = self.service.vision_status(pdf.sha256, VISION_VERSION)
                action = self.vision_action(status)
                if action == "exhausted":
                    self._exhausted(item, status)
                    return
                if action == "backoff":
                    log.info("%s: Claude fallback failed recently; waiting", scope.key)
                    return
                if action != "claude":
                    self.request_vision(item, pdf, status)
                    return
                stored = self.claude_transcript(item, pdf)
                if stored is None:
                    return
            (text, engine), version = stored, VISION_PROMPT_VERSION
        else:
            text, version = pdf.text, PROMPT_VERSION
        result = self.extraction(item, pdf.sha256, text, version)
        warnings = list(result.warnings)
        if pdf.ocr:
            warnings.append(VISION_WARNING.format(engine=engine))
            warnings.extend(ocr_guard(result.spans, pdf.text))
        events = self.normalizer(result.spans, post, scope, warnings)
        body = {
            "post": post_to_dict(post),
            "pdf_sha256": pdf.sha256,
            "prompt_version": version,
            "ocr": pdf.ocr,
            "spans": [s.to_dict() for s in result.spans],
            "events": [e.to_dict() for e in events],
            "warnings": warnings,
        }
        answer = self.service.post_desired(scope.key, body)
        log.info("%s: posted %d events (%d warnings), changeset %s",
                 scope.key, len(events), len(warnings), answer.get("changeset_id"))  # fmt: skip

    def is_current(self, seen: Seen | None, item: Any) -> bool:
        """True when the post is unchanged AND its PDF was extracted with this prompt version.

        A ``PROMPT_VERSION`` bump thus re-extracts every post once, without ``--reextract``.
        An image-only PDF also needs its vision transcript; a seen row without the ocr flag
        (stored before vision transcripts existed) is processed once more.
        """
        if seen is None or seen[0] != item.post.modified or not seen[1] or seen[2] is None:
            return False
        sha, ocr = seen[1], seen[2]
        if not ocr:
            return self.service.cached_spans(sha, PROMPT_VERSION) is not None
        return (
            self.service.transcript(sha, VISION_VERSION) is not None
            and self.service.cached_spans(sha, VISION_PROMPT_VERSION) is not None
        )

    def run(self) -> int:
        started = self.now()
        try:
            seen = {} if self.options.reextract else self.service.seen()
            result = ingest(self.wp_http, self.settings.poll_takken, self.wp_url)
            self.warnings.extend(result.warnings)
            for warning in result.warnings:
                log.warning("ingest: %s", warning)
            for item in result.posts:
                if self.is_current(seen.get(item.post.id), item):
                    continue
                if self.vision_pending(seen.get(item.post.id), item):
                    continue
                try:
                    self.process(item)
                except EmptyPdfError as exc:
                    # an unreadable vonk must not block every later poll; warn once instead
                    self.warnings.append(f"{item.scope.key} (post {item.post.id}): {exc}")
                    log.warning("%s: %s", item.scope.key, exc)
                except (PdfError, ExtractionError, VisionError) as exc:
                    self.errors.append(f"{item.scope.key} (post {item.post.id}): {exc}")
                    log.error("%s: %s", item.scope.key, exc)
        except ServiceError as exc:
            self.errors.append(str(exc))
            log.error("%s", exc)
        except httpx.HTTPError as exc:
            self.errors.append(f"WordPress: {describe_http_error(exc)}")
            log.error("WordPress: %s", describe_http_error(exc))
        except Exception as exc:  # report anything unexpected, then fail the run
            self.errors.append(f"unexpected {type(exc).__name__}: {exc}")
            log.exception("poll failed")
        finally:
            ok = not self.errors
            report = {
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
                log.error("poll report not delivered: %s", exc)
        return 0 if ok else 1


def run_poll(
    settings: Settings,
    options: PollOptions | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    **kwargs: Any,
) -> int:
    """Entry point: check secrets, build HTTP clients and run once.

    ``allowed_hosts`` limits every WordPress and PDF request, including redirect hops.
    """
    missing = settings.missing_secrets("poll")
    if missing or settings.errors:
        log.error(
            "vonk poll: missing or invalid settings: %s", ", ".join(missing + list(settings.errors))
        )
        return 2
    assert settings.vonk_api_url and settings.vonk_api_token
    client_kw: dict[str, Any] = {"timeout": HTTP_TIMEOUT, "headers": {"User-Agent": USER_AGENT}}
    if transport is not None:
        client_kw["transport"] = transport
    with (
        httpx.Client(**client_kw) as service_http,
        httpx.Client(
            follow_redirects=True, event_hooks={"request": [host_guard(allowed_hosts)]}, **client_kw
        ) as wp_http,
    ):
        service = ServiceClient(service_http, settings.vonk_api_url, settings.vonk_api_token)
        return PollRun(
            settings, service, wp_http, options=options, allowed_hosts=allowed_hosts, **kwargs
        ).run()


def cmd_poll(args: argparse.Namespace) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="VONK %(levelname)s %(name)s: %(message)s")
    options = PollOptions(reextract=args.reextract, fresh=args.fresh)
    return run_poll(Settings.from_env(), options)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add ``vonk poll [--reextract] [--fresh]`` to the CLI."""
    parser = subparsers.add_parser("poll", help="run one stateless poll against the service")
    parser.add_argument(
        "--reextract", action="store_true", help="process every post, ignoring /api/seen"
    )
    parser.add_argument(
        "--fresh", action="store_true", help="ignore the extraction cache and call the LLM"
    )
    parser.set_defaults(func=cmd_poll)
