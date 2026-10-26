"""Prometheus metrics of the service (own registry, so tests can build several apps).

The poll heartbeat gauges are persisted in SQLite and exported from there at startup; on
the very first start they are initialised to the process start time, so a poll job that
never runs still ages them out (plan "Liveness").
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from vonk.store import (
    FIRST_START,
    LAST_COMPLETED_POLL,
    LAST_NEW_VONK,
    LAST_SUCCESSFUL_POLL,
    Store,
)

CONTENT_TYPE = CONTENT_TYPE_LATEST

_HEARTBEATS = (LAST_COMPLETED_POLL, LAST_SUCCESSFUL_POLL, LAST_NEW_VONK)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.last_completed_poll = Gauge(
            "vonk_last_completed_poll_timestamp",
            "Unix time of the last poll report (any outcome)",
            registry=r,
        )
        self.last_successful_poll = Gauge(
            "vonk_last_successful_poll_timestamp",
            "Unix time of the last poll report with ok=true",
            registry=r,
        )
        self.last_new_vonk = Gauge(
            "vonk_last_new_vonk_timestamp", "Unix time a new vonk post was last seen", registry=r
        )
        self.degraded = Gauge(
            "vonk_degraded", "1 when required settings are missing or invalid", registry=r
        )
        self.pending_changesets = Gauge(
            "vonk_pending_changesets", "ChangeSets with a usable approve token", registry=r
        )
        self.apply_errors = Counter(
            "vonk_apply_errors", "Failed CalDAV apply operations", registry=r
        )
        self.feed_last_publish = Gauge(
            "vonk_feed_last_publish_timestamp",
            "Unix time the public ICS feed was last published (or found unchanged)",
            registry=r,
        )
        self.feed_errors = Counter(
            "vonk_feed_errors", "Failed publishes of the public ICS feed", registry=r
        )
        self.scopes_needing_attention = Gauge(
            "vonk_scopes_needing_attention",
            "Scopes that stopped announcing after repeated conflicts",
            registry=r,
        )

    def init_from_store(self, store: Store, process_start: float) -> None:
        """Export persisted heartbeats; absent ones start at the first process start."""
        first = store.init_meta(FIRST_START, process_start)
        gauges = {
            LAST_COMPLETED_POLL: self.last_completed_poll,
            LAST_SUCCESSFUL_POLL: self.last_successful_poll,
            LAST_NEW_VONK: self.last_new_vonk,
        }
        for key in _HEARTBEATS:
            value = store.get_meta(key)
            gauges[key].set(first if value is None else value)
        self.refresh(store)

    def refresh(self, store: Store) -> None:
        """Recompute the DB-derived gauges (cheap; no external calls)."""
        self.pending_changesets.set(store.pending_count())
        self.scopes_needing_attention.set(len(store.scopes_needing_attention()))

    def render(self) -> bytes:
        return generate_latest(self.registry)
