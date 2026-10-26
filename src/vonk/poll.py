"""Stateless poll run: ingest, extract, normalise and POST desired events to the service API.

``vonk poll`` holds no state: what it has seen and the extraction cache live in the service
(``vonk serve``). Every run ends with ``POST /api/poll-report`` (also on failure), which
drives the liveness gauges. Exit codes: 0 ok, 1 any failure (service unreachable, 401,
download/extraction errors), 2 missing poll secrets (no network calls at all).
"""

from __future__ import annotations

import argparse
import logging
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

log = logging.getLogger("vonk.poll")

HTTP_TIMEOUT = 30.0
USER_AGENT = f"vonk/{__version__} (+https://github.com/visibilityspots/vonk)"

Normalizer = Callable[[Sequence[ExtractedSpan], VonkPost, Scope, list[str]], list[Event]]
Extractor = Callable[[str, VonkKind], ExtractionResult]
PdfFetcher = Callable[[str], PdfText]


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

    def seen(self) -> dict[int, tuple[datetime, str | None]]:
        """post id -> (last processed ``modified`` timestamp, PDF sha256)."""
        resp = self._request("GET", "/api/seen")
        if not resp.is_success:
            raise self._fail("GET", "/api/seen", resp)
        try:
            return {
                int(p["post_id"]): (datetime.fromisoformat(p["modified"]), p.get("pdf_sha256"))
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
        self.wp_url = wp_url
        self.now = now
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def _extract_with_claude(self, text: str, kind: VonkKind) -> ExtractionResult:
        import anthropic

        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return extract(text, kind, client=client)

    def extraction(self, item: IngestedPost, pdf: PdfText) -> ExtractionResult:
        """Cached spans when they still verify against ``pdf.text``, else a fresh run."""
        kind = item.post.kind
        if not self.options.fresh:
            cached = self.service.cached_spans(pdf.sha256, PROMPT_VERSION)
            if cached is not None:
                kept, problems = verify_spans(cached, pdf.text)
                if not problems:
                    log.info("%s: using cached spans (%d)", item.scope.key, len(kept))
                    return ExtractionResult(tuple(kept), tuple(cross_check(pdf.text, kept, kind)))
                log.warning("%s: cached spans no longer match the text; re-extracting",
                            item.scope.key)  # fmt: skip
        return self.extractor(pdf.text, kind)

    def process(self, item: IngestedPost) -> None:
        post, scope = item.post, item.scope
        assert post.pdf_url is not None  # ingest only yields posts with a PDF link
        pdf = self.fetch_pdf(post.pdf_url)
        result = self.extraction(item, pdf)
        warnings = list(result.warnings)
        events = self.normalizer(result.spans, post, scope, warnings)
        body = {
            "post": post_to_dict(post),
            "pdf_sha256": pdf.sha256,
            "prompt_version": PROMPT_VERSION,
            "spans": [s.to_dict() for s in result.spans],
            "events": [e.to_dict() for e in events],
            "warnings": warnings,
        }
        answer = self.service.post_desired(scope.key, body)
        log.info("%s: posted %d events (%d warnings), changeset %s",
                 scope.key, len(events), len(warnings), answer.get("changeset_id"))  # fmt: skip

    def is_current(self, seen: tuple[datetime, str | None] | None, item: Any) -> bool:
        """True when the post is unchanged AND its PDF was extracted with this prompt version.

        A ``PROMPT_VERSION`` bump thus re-extracts every post once, without ``--reextract``.
        """
        if seen is None or seen[0] != item.post.modified or not seen[1]:
            return False
        return self.service.cached_spans(seen[1], PROMPT_VERSION) is not None

    def run(self) -> int:
        started = self.now()
        try:
            seen = {} if self.options.reextract else self.service.seen()
            result = ingest(self.wp_http, self.settings.takken, self.wp_url)
            self.warnings.extend(result.warnings)
            for warning in result.warnings:
                log.warning("ingest: %s", warning)
            for item in result.posts:
                if self.is_current(seen.get(item.post.id), item):
                    continue
                try:
                    self.process(item)
                except EmptyPdfError as exc:
                    # an unreadable vonk must not block every later poll; warn once instead
                    self.warnings.append(f"{item.scope.key} (post {item.post.id}): {exc}")
                    log.warning("%s: %s", item.scope.key, exc)
                except (PdfError, ExtractionError) as exc:
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
