"""metrics: gauges initialised from SQLite, first-start fallback, degraded flag."""

from __future__ import annotations

import re

from vonk.metrics import Metrics
from vonk.store import LAST_COMPLETED_POLL, LAST_NEW_VONK, LAST_SUCCESSFUL_POLL, Store

NAMES = (
    "vonk_last_completed_poll_timestamp",
    "vonk_last_successful_poll_timestamp",
    "vonk_last_new_vonk_timestamp",
    "vonk_degraded",
    "vonk_pending_changesets",
    "vonk_apply_errors_total",
    "vonk_scopes_needing_attention",
)


def value(text: str, name: str) -> float:
    m = re.search(rf"^{name} (\S+)$", text, re.M)
    assert m, f"{name} missing"
    return float(m.group(1))


def test_first_start_initialises_heartbeats_to_process_start(tmp_path):
    store = Store(str(tmp_path / "db"))
    m = Metrics()
    m.init_from_store(store, process_start=1000.0)
    text = m.render().decode()
    for name in NAMES:
        assert re.search(rf"^{name} ", text, re.M), name
    assert value(text, "vonk_last_completed_poll_timestamp") == 1000.0
    assert value(text, "vonk_last_successful_poll_timestamp") == 1000.0
    assert value(text, "vonk_last_new_vonk_timestamp") == 1000.0


def test_restart_exports_persisted_values_not_the_new_start(tmp_path):
    store = Store(str(tmp_path / "db"))
    Metrics().init_from_store(store, process_start=1000.0)
    store.set_meta(LAST_COMPLETED_POLL, 2000.0)
    store.set_meta(LAST_SUCCESSFUL_POLL, 1500.0)
    store.set_meta(LAST_NEW_VONK, 1200.0)
    store.close()

    m = Metrics()
    m.init_from_store(Store(str(tmp_path / "db")), process_start=9999.0)
    text = m.render().decode()
    assert value(text, "vonk_last_completed_poll_timestamp") == 2000.0
    assert value(text, "vonk_last_successful_poll_timestamp") == 1500.0
    assert value(text, "vonk_last_new_vonk_timestamp") == 1200.0


def test_never_polled_keeps_first_start_across_restarts(tmp_path):
    store = Store(str(tmp_path / "db"))
    Metrics().init_from_store(store, process_start=1000.0)
    m = Metrics()
    m.init_from_store(store, process_start=5000.0)  # restart, poll still never ran
    assert value(m.render().decode(), "vonk_last_completed_poll_timestamp") == 1000.0


def test_refresh_counts_attention_scopes(tmp_path):
    store = Store(str(tmp_path / "db"))
    m = Metrics()
    m.init_from_store(store, 1.0)
    store.record_conflict("kabouters:2026-10", cap=1)
    m.refresh(store)
    assert value(m.render().decode(), "vonk_scopes_needing_attention") == 1.0


def test_degraded_service_exports_flag(make_world):
    w = make_world({"VONK_API_TOKEN": "x", "GOTIFY_TOKEN": "<no value>"}, with_sink=False)
    text = w.client.get("/metrics").text
    assert value(text, "vonk_degraded") == 1.0
    healthy = make_world()
    assert value(healthy.client.get("/metrics").text, "vonk_degraded") == 0.0
