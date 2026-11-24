"""Stateless poll run: ingest, extract, normalise and POST desired events to the service API.

``vonk poll`` holds no state: what it has seen and the extraction cache live in the service
(``vonk serve``). Every run ends with ``POST /api/poll-report`` (also on failure), which
drives the liveness gauges. Exit codes: 0 ok, 1 any failure (service unreachable, 401,
download/extraction errors), 2 missing poll secrets (no network calls at all).

An image-only PDF (no text layer) is extracted from its vision transcript, never from OCR
text: when the service has no transcript yet, the poll dispatches the vision job (at most
once per 12 hours per PDF) and leaves the post unseen, so the next poll picks the transcript
up. Such extractions are cached under ``VISION_PROMPT_VERSION``.
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
from vonk.pdf import EmptyPdfError, PdfError, PdfText, fetch_text
from vonk.vision import VISION_VERSION, VisionError, nomad_dispatch

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
VISION_WARNING = "vision transcript: check dates against the poster"

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

    def transcript(self, sha256: str, vision_version: str) -> str | None:
        """Stored vision transcript of the PDF; None on a miss (404)."""
        path = f"/api/transcripts/{sha256}/{vision_version}"
        resp = self._request("GET", path)
        if resp.status_code == 404:
            return None
        if not resp.is_success:
            raise self._fail("GET", path, resp)
        try:
            return str(resp.json()["text"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"GET {path}: malformed body: {exc}") from exc

    def put_transcript(self, sha256: str, vision_version: str, text: str) -> None:
        path = f"/api/transcripts/{sha256}/{vision_version}"
        resp = self._request("PUT", path, json={"text": text})
        if not resp.is_success:
            raise self._fail("PUT", path, resp)

    def dispatch_allowed(self, sha256: str) -> bool:
        """False while a successful vision dispatch of this PDF is still recent."""
        path = f"/api/vision-dispatch/{sha256}"
        resp = self._request("GET", path)
        if not resp.is_success:
            raise self._fail("GET", path, resp)
        try:
            return bool(resp.json()["dispatch"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(f"GET {path}: malformed body: {exc}") from exc

    def vision_dispatch(
        self, sha256: str, post_id: int, ok: bool, *, modified: str = "", scope_key: str = ""
    ) -> None:
        """Report the outcome of a vision dispatch (only a success is recorded; it also
        marks the post seen as image-only, which needs ``modified`` and ``scope_key``)."""
        body: dict[str, Any] = {"pdf_sha256": sha256, "post_id": post_id, "ok": ok}
        if ok:
            body |= {"modified": modified, "scope_key": scope_key}
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

    def request_vision(self, item: IngestedPost, pdf: PdfText) -> None:
        """No transcript yet: dispatch the vision job (unless one is recent) and move on."""
        post, label = item.post, f"{item.scope.key} (post {item.post.id})"
        assert post.pdf_url is not None
        if not self.settings.vonk_vision_dispatch:
            self.warnings.append(f"{label}: image-only PDF skipped (vision dispatch disabled)")
            log.warning("%s: image-only PDF skipped (vision dispatch disabled)", label)
            return
        if not self.service.dispatch_allowed(pdf.sha256):
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
        """True for an unchanged image-only post that waits for its transcript while its
        vision job is in the 12 hour window: no download, no OCR, no dispatch."""
        if (
            seen is None
            or seen[0] != item.post.modified
            or not seen[1]
            or seen[2] is not True
            or not self.settings.vonk_vision_dispatch
        ):
            return False
        sha = seen[1]
        if self.service.transcript(sha, VISION_VERSION) is not None:
            return False
        if self.service.dispatch_allowed(sha):
            return False
        log.info("%s (post %s): waiting for the vision transcript", item.scope.key, item.post.id)
        return True

    def process(self, item: IngestedPost) -> None:
        post, scope = item.post, item.scope
        assert post.pdf_url is not None  # ingest only yields posts with a PDF link
        try:
            pdf = self.fetch_pdf(post.pdf_url)
        except EmptyPdfError as exc:
            # OCR found nothing, but the vision model may still read the poster
            if not (self.settings.vonk_vision_dispatch and exc.sha256):
                raise
            pdf = PdfText(sha256=exc.sha256, text="", ocr=True)
        if pdf.ocr:
            transcript = self.service.transcript(pdf.sha256, VISION_VERSION)
            if transcript is None:
                self.request_vision(item, pdf)
                return
            text, version = transcript, VISION_PROMPT_VERSION
        else:
            text, version = pdf.text, PROMPT_VERSION
        result = self.extraction(item, pdf.sha256, text, version)
        warnings = list(result.warnings)
        if pdf.ocr:
            warnings.append(VISION_WARNING)
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
