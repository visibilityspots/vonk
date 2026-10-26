"""Service core and the internal bearer-authenticated API used by the poll job (/api/*).

``Service`` owns every state transition: storing desired events, diffing against the
calendar, superseding and announcing ChangeSets, and the approve flow used by ``web``. All
of that runs in Starlette's threadpool under a per-scope ``threading.Lock``, so a poll never
diffs against a half-applied calendar and never supersedes a ChangeSet mid-apply.

Wire shapes (matched by ``vonk.poll.ServiceClient``):

- ``GET  /api/seen`` -> ``{"posts": [{post_id, modified, pdf_sha256}]}``
- ``GET  /api/cache/{sha}/{prompt_version}`` -> ``{"spans": [...]}`` or 404
- ``POST /api/scopes/{scope_key}/desired`` ``{post, pdf_sha256, prompt_version, spans,
  events, warnings}`` -> ``{"changeset_id": str|None, "ops": int, ...}``
- ``POST /api/poll-report`` ``{started, finished, ok, errors, warnings}`` -> ``{}``

Every route needs ``Authorization: Bearer <VONK_API_TOKEN>`` (constant-time compare); a
missing or wrong token is a 401 before anything else happens. In degraded mode every route
but ``poll-report`` answers 503.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vonk.config import Settings
from vonk.diff import build_changeset
from vonk.feed import GitHubFeedPublisher, build_feed
from vonk.ingest import describe_http_error
from vonk.match import resolve
from vonk.metrics import Metrics
from vonk.models import (
    ChangeSet,
    Event,
    Operation,
    OpFlag,
    OpKind,
    OpResult,
    OwnEvent,
    Scope,
    Sink,
    Tak,
)
from vonk.notify import (
    PRIORITY_HIGH,
    Notifier,
    changeset_message,
    result_message,
    warnings_message,
)
from vonk.store import LAST_COMPLETED_POLL, LAST_NEW_VONK, LAST_SUCCESSFUL_POLL, Store

log = logging.getLogger("vonk.api")

ORPHAN_SCOPE = "orphans"
"""Pseudo-scope for own UIDs with neither a store row nor X-properties (removal only)."""

CONFLICT_CAP = 2
"""Consecutive 409/412 on a scope after which it needs attention and stops announcing."""

SINK_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, RuntimeError)


def _write_hash(cs: ChangeSet) -> str:
    """Hash of what a ChangeSet would write: ``possible_conflict`` is display-only and
    depends on other scopes' desired events, so it must not turn an approve into a 409."""
    ops = tuple(
        replace(op, flags=tuple(f for f in op.flags if f != OpFlag.POSSIBLE_CONFLICT))
        for op in cs.ops
    )
    return replace(cs, ops=ops).content_hash()


class BadRequest(ValueError):
    """The request body is malformed (400)."""


@dataclass(slots=True)
class ApproveOutcome:
    status: int
    message: str
    changeset: ChangeSet | None = None
    results: list[OpResult] = field(default_factory=list)


@dataclass(slots=True)
class ViewOutcome:
    status: int
    message: str = ""
    changeset: ChangeSet | None = None
    ingest_warnings: tuple[str, ...] = ()
    needs_attention: bool = False
    related: tuple[Event, ...] = ()


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sink: Sink | None,
        notifier: Notifier,
        metrics: Metrics,
        *,
        feed: GitHubFeedPublisher | None = None,
        feed_in_background: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sink = sink
        self.notifier = notifier
        self.metrics = metrics
        self.feed = feed
        self.feed_in_background = feed_in_background
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._feed_lock = threading.Lock()

    @property
    def degraded(self) -> bool:
        return self.settings.degraded or self.sink is None

    def follows(self, scope_key: str) -> bool:
        """True when ``scope_key`` goes through the approve flow (the family's takken)."""
        if scope_key == ORPHAN_SCOPE:
            return True
        return self.settings.follows(Tak(scope_key.partition(":")[0]))

    def related_events(self, scope_key: str) -> list[Event]:
        """Desired events of the other followed scopes (``possible_conflict`` candidates)."""
        return [
            e
            for e in self.store.desired_events_except(scope_key)
            if self.follows(e.scope_key)
        ]

    def lock(self, scope_key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(scope_key, threading.Lock())

    # --- diffing -------------------------------------------------------------------------

    def _require_sink(self) -> Sink:
        if self.sink is None:
            raise RuntimeError("no calendar sink configured (degraded)")
        return self.sink

    def own_events(self, scope_key: str) -> tuple[list[OwnEvent], dict]:
        """Own events belonging to ``scope_key`` (SQLite identity first, X-property copy as
        fallback), or the orphans for ``ORPHAN_SCOPE``. Refreshes the sink's ETags."""
        identity = self.store.identity_map()
        everything = self._require_sink().list_own(None)
        resolved, orphans = resolve(everything, identity)
        if scope_key == ORPHAN_SCOPE:
            return orphans, identity
        return [r.own for r in resolved if r.scope_key == scope_key], identity

    def build(self, scope_key: str) -> ChangeSet:
        """Fresh ChangeSet for ``scope_key`` from the stored desired events and the calendar."""
        own, identity = self.own_events(scope_key)
        if scope_key == ORPHAN_SCOPE:
            ops = tuple(
                Operation(OpKind.REMOVE, o.uid, o.event, None, (OpFlag.ORPHAN,))
                for o in sorted(own, key=lambda o: o.uid)
            )
            return ChangeSet(id=uuid.uuid4().hex, scope_key=ORPHAN_SCOPE, ops=ops)
        desired = self.store.get_desired(scope_key)
        return build_changeset(
            scope_key,
            desired.events if desired else (),
            own,
            identity,
            child_names=self.settings.child_names,
            related_events=self.related_events(scope_key),
        )

    def _announce(self, cs: ChangeSet, ingest_warnings: Sequence[str]) -> str:
        self.store.save_changeset(cs, ingest_warnings)
        token = self.store.create_token(cs)
        base = (self.settings.vonk_public_url or "").rstrip("/")
        self.notifier.send(*changeset_message(cs, f"{base}/c/{token}", ingest_warnings))
        self.metrics.refresh(self.store)
        log.info("%s: announced changeset %s (%d ops)", cs.scope_key, cs.id, len(cs.ops))
        return token

    def propose(self, scope_key: str, ingest_warnings: Sequence[str] = ()) -> dict[str, Any]:
        """Diff and announce a scope. Caller holds ``lock(scope_key)``."""
        cs = self.build(scope_key)
        if not cs.is_empty and self.store.pending_changeset_hash(scope_key) == cs.content_hash():
            return {"changeset_id": None, "ops": len(cs.ops), "unchanged": True}
        self.store.supersede_scope(scope_key)
        self.metrics.refresh(self.store)
        if cs.is_empty:
            return {"changeset_id": None, "ops": 0}
        if self.store.scope_state(scope_key).needs_attention:
            log.warning("%s needs attention: changeset not announced", scope_key)
            return {"changeset_id": None, "ops": len(cs.ops), "needs_attention": True}
        self._announce(cs, ingest_warnings)
        return {"changeset_id": cs.id, "ops": len(cs.ops)}

    # --- internal API operations ---------------------------------------------------------

    def submit_desired(self, scope_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            Scope.parse(scope_key)
            post = body["post"]
            post_id = int(post["id"])
            modified = str(post["modified"])
            sha = str(body["pdf_sha256"])
            prompt_version = str(body["prompt_version"])
            spans = list(body["spans"])
            events = [Event.from_dict(e) for e in body["events"]]
            warnings = [str(w) for w in body.get("warnings", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise BadRequest(f"invalid desired body: {exc}") from exc
        if any(e.scope_key != scope_key for e in events):
            raise BadRequest("event scope_key differs from the URL scope")
        with self.lock(scope_key):
            new_post = not self.store.is_seen(post_id)
            self.store.put_cache(sha, prompt_version, spans)
            self.store.set_desired(scope_key, events, sha, warnings)
            self.store.set_scope_pdf(scope_key, sha)  # a new PDF clears needs-attention
            if self.follows(scope_key):
                answer = self.propose(scope_key, warnings)
            else:  # feed-only tak: stored for the public feed, never proposed for CalDAV
                answer = {"changeset_id": None, "ops": 0, "feed_only": True}
            self.store.mark_seen(post_id, modified, sha, scope_key)
            if new_post:
                now = self.clock()
                self.store.set_meta(LAST_NEW_VONK, now)
                self.metrics.last_new_vonk.set(now)
        with self.lock(ORPHAN_SCOPE):
            answer["orphans"] = self.propose(ORPHAN_SCOPE)["ops"]
        return answer

    def poll_report(self, body: Mapping[str, Any]) -> None:
        if not isinstance(body, Mapping) or not isinstance(body.get("ok"), bool):
            raise BadRequest("poll report needs a boolean 'ok'")
        now = self.clock()
        self.store.set_meta(LAST_COMPLETED_POLL, now)
        self.metrics.last_completed_poll.set(now)
        if body["ok"]:
            self.store.set_meta(LAST_SUCCESSFUL_POLL, now)
            self.metrics.last_successful_poll.set(now)
        for err in body.get("errors") or []:
            log.error("poll reported: %s", err)
        warnings = [str(w) for w in body.get("warnings") or []]
        fresh = self.store.new_warnings(warnings)
        if fresh:
            self.notifier.send(*warnings_message(fresh))
        if self.feed is not None:
            if self.feed_in_background:
                threading.Thread(target=self.publish_feed, name="feed", daemon=True).start()
            else:
                self.publish_feed()

    def publish_feed(self) -> list[str]:
        """Render the public feed from the stored desired events and push what changed.

        Runs after every poll report, so a failed publish is retried by the next poll.
        A publish that is still running makes this one a no-op.
        """
        if self.feed is None or not self._feed_lock.acquire(blocking=False):
            return []
        try:
            files = build_feed(
                self.store.desired_all(), self.settings.feed_takken, self.feed.repo
            )
            changed = self.feed.publish(files)
        except httpx.HTTPError as exc:
            self.metrics.feed_errors.inc()
            log.error("feed publish failed: %s", describe_http_error(exc))
            return []
        except (KeyError, ValueError, RuntimeError) as exc:
            self.metrics.feed_errors.inc()
            log.error("feed publish failed: %s: %s", type(exc).__name__, exc)
            return []
        finally:
            self._feed_lock.release()
        self.metrics.feed_last_publish.set(self.clock())
        log.info("feed: %d files checked, changed: %s", len(files), ", ".join(changed) or "none")
        return changed

    # --- approve flow --------------------------------------------------------------------

    def view(self, token: str) -> ViewOutcome:
        row = self.store.get_token(token)
        if row is None:
            return ViewOutcome(404, "Onbekende link.")
        if not row.usable(self.clock()):
            return ViewOutcome(410, "Deze link is al gebruikt, verlopen of vervangen.")
        found = self.store.get_changeset(row.changeset_id)
        if found is None:
            return ViewOutcome(404, "Onbekende wijzigingenset.")
        cs, ingest_warnings = found
        return ViewOutcome(
            200,
            changeset=cs,
            ingest_warnings=ingest_warnings,
            needs_attention=self.store.scope_state(cs.scope_key).needs_attention,
            related=tuple(self.related_events(cs.scope_key)),
        )

    def _conflict(self, scope_key: str, ingest_warnings: Sequence[str]) -> str:
        """Count a 409/412; announce a fresh ChangeSet unless the cap is reached."""
        state = self.store.record_conflict(scope_key, CONFLICT_CAP)
        self.store.supersede_scope(scope_key)
        self.metrics.refresh(self.store)
        if state.needs_attention:
            self.notifier.send(
                f"vonk: {scope_key} vraagt aandacht",
                f"{state.conflict_count} conflicten na elkaar; geen nieuwe voorstellen meer tot "
                f"een nieuwe vonk binnenkomt of `vonk clear-attention {scope_key}`.",
                PRIORITY_HIGH,
            )
            return "Te veel conflicten na elkaar: deze scope vraagt aandacht."
        fresh = self.build(scope_key)
        if fresh.is_empty:
            return "De kalender klopt intussen al."
        self._announce(fresh, ingest_warnings)
        return "Er is een nieuw voorstel verstuurd."

    def _record_identity(self, cs: ChangeSet, results: Sequence[OpResult]) -> None:
        for r in results:
            if not r.ok or r.uid is None:
                continue
            op = cs.ops[r.index]
            if op.kind == OpKind.REMOVE:
                self.store.delete_identity(r.uid)
            else:
                assert op.after is not None
                self.store.set_identity(r.uid, op.after.scope_key, op.after.date, r.new_etag)

    def approve(
        self, token: str, changeset_hash: str, selected: Sequence[int]
    ) -> ApproveOutcome:
        row = self.store.get_token(token)
        if row is None:
            return ApproveOutcome(404, "Onbekende link.")
        if self.sink is None:
            return ApproveOutcome(503, "De kalender is niet geconfigureerd.")
        with self.lock(row.scope_key):
            row = self.store.get_token(token)
            assert row is not None
            if not row.usable(self.clock()):
                return ApproveOutcome(410, "Deze link is al gebruikt, verlopen of vervangen.")
            if not hmac.compare_digest(changeset_hash.encode(), row.changeset_hash.encode()):
                return ApproveOutcome(409, "Het formulier hoort bij een andere wijzigingenset.")
            found = self.store.get_changeset(row.changeset_id)
            if found is None:
                return ApproveOutcome(404, "Onbekende wijzigingenset.")
            cs, ingest_warnings = found
            if any(i < 0 or i >= len(cs.ops) for i in selected):
                return ApproveOutcome(400, "Ongeldige selectie.", cs)
            selected = sorted(set(selected))
            try:
                fresh = self.build(cs.scope_key)
            except SINK_ERRORS as exc:
                self.metrics.apply_errors.inc()
                log.error("%s: listing calendar failed: %s", cs.scope_key, exc)
                return ApproveOutcome(502, "De kalender is niet bereikbaar; probeer later.", cs)
            if not self.store.consume_token(token):
                return ApproveOutcome(410, "Deze link is al gebruikt, verlopen of vervangen.")
            if _write_hash(fresh) != _write_hash(cs):
                self.store.set_changeset_status(cs.id, "conflict")
                msg = self._conflict(cs.scope_key, ingest_warnings)
                return ApproveOutcome(
                    409, f"De kalender is intussen veranderd; niets geschreven. {msg}", cs
                )
            results = self._require_sink().apply(cs, selected)
            self.store.record_results(cs.id, results)
            self._record_identity(cs, results)
            failed = [r for r in results if not r.ok]
            self.notifier.send(*result_message(cs, results))
            if not failed:
                self.store.set_changeset_status(cs.id, "applied")
                self.store.reset_conflicts(cs.scope_key)
                self.metrics.refresh(self.store)
                return ApproveOutcome(200, f"{len(results)} wijzigingen geschreven.", cs, results)
            self.store.set_changeset_status(cs.id, "failed")
            self.metrics.apply_errors.inc()
            if any(r.status == 412 for r in failed):
                msg = self._conflict(cs.scope_key, ingest_warnings)
                return ApproveOutcome(
                    409, f"Een event werd intussen gewijzigd (412); gestopt. {msg}", cs, results
                )
            self.metrics.refresh(self.store)
            return ApproveOutcome(502, "Schrijven naar de kalender mislukte.", cs, results)


# --- HTTP layer --------------------------------------------------------------------------


def _authorized(service: Service, request: Request) -> bool:
    expected = service.settings.vonk_api_token
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    if expected is None or scheme.lower() != "bearer" or not given:
        return False
    return hmac.compare_digest(given.strip().encode("utf-8"), expected.encode("utf-8"))


def _guard(service: Service, request: Request, *, allow_degraded: bool = False):
    if service.settings.vonk_api_token is None:
        return JSONResponse({"error": "service degraded: no API token"}, status_code=503)
    if not _authorized(service, request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if service.degraded and not allow_degraded:
        return JSONResponse({"error": "service degraded"}, status_code=503)
    return None


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise BadRequest("body is not JSON") from exc


def routes(service: Service) -> list[Route]:
    async def seen(request: Request) -> Response:
        if (denied := _guard(service, request)) is not None:
            return denied
        return JSONResponse({"posts": await run_in_threadpool(service.store.seen_posts)})

    async def cache(request: Request) -> Response:
        if (denied := _guard(service, request)) is not None:
            return denied
        spans = await run_in_threadpool(
            service.store.get_cache,
            request.path_params["sha"],
            request.path_params["prompt_version"],
        )
        if spans is None:
            return JSONResponse({"error": "not cached"}, status_code=404)
        return JSONResponse({"spans": spans})

    async def desired(request: Request) -> Response:
        if (denied := _guard(service, request)) is not None:
            return denied
        scope_key = request.path_params["scope_key"]
        try:
            body = await _json(request)
            answer = await run_in_threadpool(service.submit_desired, scope_key, body)
        except BadRequest as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except SINK_ERRORS as exc:
            log.error("%s: diff failed: %s", scope_key, exc)
            return JSONResponse({"error": "calendar unavailable"}, status_code=502)
        return JSONResponse(answer)

    async def poll_report(request: Request) -> Response:
        if (denied := _guard(service, request, allow_degraded=True)) is not None:
            return denied
        try:
            body = await _json(request)
            await run_in_threadpool(service.poll_report, body)
        except BadRequest as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({})

    return [
        Route("/api/seen", seen, methods=["GET"]),
        Route("/api/cache/{sha}/{prompt_version}", cache, methods=["GET"]),
        Route("/api/scopes/{scope_key}/desired", desired, methods=["POST"]),
        Route("/api/poll-report", poll_report, methods=["POST"]),
    ]
