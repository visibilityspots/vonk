"""Service entry point: build the Starlette app and run it with uvicorn.

Starts degraded when required settings are missing (``config.missing_secrets('serve')``):
the app still serves ``/health`` (200) and ``/metrics`` (``vonk_degraded 1``); without
Baikal settings there is no sink, and ``/api/*`` answers 503 except ``poll-report``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx
from starlette.applications import Starlette

from vonk.api import Service
from vonk.api import routes as api_routes
from vonk.config import Settings
from vonk.feed import GitHubFeedPublisher
from vonk.metrics import Metrics
from vonk.models import Sink
from vonk.notify import GotifyNotifier, Notifier, NullNotifier
from vonk.sinks.caldav_sink import CalDAVSink
from vonk.store import Store
from vonk.web import routes as web_routes

log = logging.getLogger("vonk.main")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def default_sink(settings: Settings) -> Sink | None:
    if not (
        settings.baikal_url
        and settings.baikal_user
        and settings.baikal_password
        and settings.baikal_calendar
    ):
        return None
    return CalDAVSink.from_settings(settings)


def default_feed(settings: Settings) -> GitHubFeedPublisher | None:
    if not settings.feed_enabled:
        return None
    assert settings.feed_repo and settings.feed_token
    return GitHubFeedPublisher(
        httpx.Client(), settings.feed_repo, settings.feed_token, settings.feed_branch
    )


def default_notifier(settings: Settings) -> Notifier:
    if not (settings.gotify_url and settings.gotify_token):
        return NullNotifier()
    return GotifyNotifier(
        httpx.Client(), settings.gotify_url, settings.gotify_token, settings.child_names
    )


def build_service(
    settings: Settings,
    *,
    store: Store | None = None,
    sink: Sink | None = None,
    notifier: Notifier | None = None,
    feed: GitHubFeedPublisher | None = None,
    clock: Callable[[], float] = time.time,
    use_default_sink: bool = True,
) -> Service:
    store = store or Store(settings.vonk_db_path, clock=clock)
    if sink is None and use_default_sink:
        sink = default_sink(settings)
    metrics = Metrics()
    service = Service(
        settings,
        store,
        sink,
        notifier or default_notifier(settings),
        metrics,
        feed=feed or default_feed(settings),
        clock=clock,
    )
    metrics.init_from_store(store, clock())
    metrics.degraded.set(1 if service.degraded else 0)
    if service.degraded:
        problems = settings.missing_secrets("serve") + list(settings.errors)
        log.error("starting degraded; missing or invalid: %s", ", ".join(problems) or "sink")
    return service


def create_app(service: Service) -> Starlette:
    return Starlette(routes=[*api_routes(service), *web_routes(service)])


def serve(settings: Settings, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    import uvicorn

    app = create_app(build_service(settings))
    # No access log: approve URLs carry the token (/c/<token>) and must not end up in Loki.
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, proxy_headers=True)
    return 0
