"""store: SQLite state, token lifecycle, conflict cap, heartbeats."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, date, datetime, time

from vonk.models import ChangeSet, Event, Operation, OpKind, OpResult, mint_uid
from vonk.store import LAST_COMPLETED_POLL, TOKEN_TTL_SECONDS, Store, hash_token

URL = "https://scoutstervant.be/x.pdf"


class Clock:
    def __init__(self, now: float = 1_790_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def event(day: int = 3, title: str = "Dropping") -> Event:
    return Event("kabouters:2026-10", date(2026, 10, day), None, time(14), time(17), False,
                 title, f"{title}\n{URL}", URL)  # fmt: skip


def changeset(title: str = "Dropping", scope: str = "kabouters:2026-10") -> ChangeSet:
    return ChangeSet(
        id=f"cs-{title}",
        scope_key=scope,
        ops=(Operation(OpKind.ADD, None, None, event(title=title)),),
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
    )


def make(tmp_path, clock=None) -> Store:
    return Store(str(tmp_path / "vonk.db"), clock=clock or Clock())


def test_journal_mode_is_delete(tmp_path):
    store = make(tmp_path)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"
    assert store.ping()


def test_token_is_stored_hashed_only(tmp_path):
    store = make(tmp_path)
    cs = changeset()
    store.save_changeset(cs)
    token = store.create_token(cs)
    assert len(token) >= 43  # 32 bytes url-safe
    dump = "\n".join(sqlite3.connect(tmp_path / "vonk.db").iterdump())
    assert token not in dump
    assert hash_token(token) in dump


def test_token_lifecycle(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    cs = changeset()
    store.save_changeset(cs)
    token = store.create_token(cs)
    row = store.get_token(token)
    assert row is not None and row.usable(clock())
    assert row.changeset_hash == cs.content_hash() and row.scope_key == cs.scope_key
    assert row.expires_at == clock() + TOKEN_TTL_SECONDS
    assert store.get_token("nope") is None
    assert store.pending_count() == 1
    assert store.consume_token(token) is True
    assert store.consume_token(token) is False
    assert not store.get_token(token).usable(clock())
    assert store.pending_count() == 0


def test_expired_token_cannot_be_consumed(tmp_path):
    clock = Clock()
    store = make(tmp_path, clock)
    cs = changeset()
    store.save_changeset(cs)
    token = store.create_token(cs)
    clock.now += TOKEN_TTL_SECONDS + 1
    assert store.pending_count() == 0
    assert store.consume_token(token) is False


def test_supersede_invalidates_only_that_scope(tmp_path):
    store = make(tmp_path)
    a, b = changeset("A"), changeset("B", scope="jeevees:2026-10")
    for cs in (a, b):
        store.save_changeset(cs)
    ta, tb = store.create_token(a), store.create_token(b)
    assert store.pending_changeset_hash("kabouters:2026-10") == a.content_hash()
    assert store.supersede_scope("kabouters:2026-10") == 1
    assert store.get_token(ta).superseded
    assert store.consume_token(ta) is False
    assert store.changeset_status(a.id) == "superseded"
    assert store.pending_changeset_hash("kabouters:2026-10") is None
    assert store.consume_token(tb) is True


def test_concurrent_consume_succeeds_exactly_once(tmp_path):
    store = make(tmp_path)
    cs = changeset()
    store.save_changeset(cs)
    token = store.create_token(cs)
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(store.consume_token(token))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]


def test_changeset_roundtrip_and_results(tmp_path):
    store = make(tmp_path)
    cs = changeset()
    store.save_changeset(cs, ["ingest warning"])
    loaded, warnings = store.get_changeset(cs.id)
    assert loaded == cs and loaded.content_hash() == cs.content_hash()
    assert warnings == ("ingest warning",)
    uid = mint_uid()
    store.record_results(cs.id, [OpResult(0, True, 201, '"e1"', None, uid)])
    assert store.results_for(cs.id) == [OpResult(0, True, 201, '"e1"', None, uid)]
    assert store.get_changeset("missing") is None


def test_conflict_cap_and_clearing(tmp_path):
    store = make(tmp_path)
    scope = "kabouters:2026-10"
    assert store.set_scope_pdf(scope, "sha-1") is True
    assert store.record_conflict(scope, cap=2).needs_attention is False
    state = store.record_conflict(scope, cap=2)
    assert state.needs_attention and state.conflict_count == 2
    assert store.scopes_needing_attention() == [scope]
    # same PDF again: still flagged
    assert store.set_scope_pdf(scope, "sha-1") is False
    assert store.scope_state(scope).needs_attention
    # new PDF clears it
    assert store.set_scope_pdf(scope, "sha-2") is True
    assert not store.scope_state(scope).needs_attention
    assert store.scope_state(scope).conflict_count == 0
    # clear-attention
    store.record_conflict(scope, cap=2)
    store.record_conflict(scope, cap=2)
    assert store.clear_attention(scope) is True
    assert store.scopes_needing_attention() == []
    assert store.clear_attention(scope) is False


def test_reset_conflicts_after_success(tmp_path):
    store = make(tmp_path)
    scope = "kabouters:2026-10"
    store.record_conflict(scope, cap=2)
    store.reset_conflicts(scope)
    assert store.record_conflict(scope, cap=2).needs_attention is False


def test_identity_cache_seen_desired(tmp_path):
    store = make(tmp_path)
    uid = mint_uid()
    store.set_identity(uid, "kabouters:2026-10", date(2026, 10, 3), '"e1"')
    assert store.identity_map()[uid] == ("kabouters:2026-10", date(2026, 10, 3), '"e1"')
    store.delete_identity(uid)
    assert store.identity_map() == {}

    assert store.get_cache("sha", "v1") is None
    store.put_cache("sha", "v1", [{"date_span": "3/10"}])
    assert store.get_cache("sha", "v1") == [{"date_span": "3/10"}]

    assert not store.is_seen(7)
    store.mark_seen(7, "2026-10-05T20:09:39", "sha", "kabouters:2026-10")
    assert store.seen_posts() == [
        {"post_id": 7, "modified": "2026-10-05T20:09:39", "pdf_sha256": "sha"}
    ]
    assert store.forget_seen_scope("kabouters:2026-10") == 1
    assert store.seen_posts() == []

    store.set_desired("kabouters:2026-10", [event()], "sha", ["w"])
    store.set_desired("groep:2026-2027", [], "sha2", [])
    d = store.get_desired("kabouters:2026-10")
    assert d.events == (event(),) and d.warnings == ("w",)
    assert store.desired_scopes() == ["groep:2026-2027", "kabouters:2026-10"]
    assert store.desired_events_except("groep:2026-2027") == [event()]
    assert store.get_desired("jeevees:2026-10") is None


def test_meta_and_warning_dedup(tmp_path):
    store = make(tmp_path)
    assert store.get_meta(LAST_COMPLETED_POLL) is None
    assert store.init_meta("first_start", 5.0) == 5.0
    assert store.init_meta("first_start", 9.0) == 5.0
    store.set_meta(LAST_COMPLETED_POLL, 7.0)
    assert store.get_meta(LAST_COMPLETED_POLL) == 7.0
    assert store.new_warnings(["a", "b", "a"]) == ["a", "b"]
    assert store.new_warnings(["a", "c"]) == ["c"]


def test_state_survives_reopen(tmp_path):
    store = make(tmp_path)
    store.set_meta(LAST_COMPLETED_POLL, 42.0)
    store.close()
    assert make(tmp_path).get_meta(LAST_COMPLETED_POLL) == 42.0
