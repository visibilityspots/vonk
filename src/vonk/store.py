"""SQLite state owned by the service.

Holds the UID map, extraction cache, desired events per scope, ChangeSets, approve tokens,
per-scope conflict state, heartbeat timestamps and per-op apply results.

One connection, one writer: the service process is the only owner of the database. Calls
from Starlette's threadpool are serialised by an ``RLock``; ``journal_mode=DELETE`` because
the file lives on NFS (no WAL shared memory). Times are stored as UTC epoch seconds.
Approve tokens are never stored: only ``sha256(token)``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

from vonk.match import Identity
from vonk.models import ChangeSet, Event, OpResult

TOKEN_TTL_SECONDS = 48 * 3600

# Heartbeat keys in the meta table.
LAST_COMPLETED_POLL = "last_completed_poll"
LAST_SUCCESSFUL_POLL = "last_successful_poll"
LAST_NEW_VONK = "last_new_vonk"
FIRST_START = "first_start"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    post_id INTEGER PRIMARY KEY,
    modified TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
    pdf_sha256 TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    spans TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (pdf_sha256, prompt_version)
);
CREATE TABLE IF NOT EXISTS identity (
    uid TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    date TEXT NOT NULL,
    last_written_etag TEXT
);
CREATE TABLE IF NOT EXISTS desired (
    scope_key TEXT PRIMARY KEY,
    events TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    warnings TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS changesets (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    ingest_warnings TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    changeset_id TEXT NOT NULL,
    changeset_hash TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scopes (
    scope_key TEXT PRIMARY KEY,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    last_pdf_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS op_results (
    changeset_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    status INTEGER,
    new_etag TEXT,
    error TEXT,
    uid TEXT,
    applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notified_warnings (
    text_hash TEXT PRIMARY KEY,
    first_seen REAL NOT NULL
);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenRow:
    changeset_id: str
    changeset_hash: str
    scope_key: str
    expires_at: float
    used_at: float | None
    superseded: bool

    def usable(self, now: float) -> bool:
        return self.used_at is None and not self.superseded and self.expires_at > now


@dataclass(frozen=True, slots=True)
class ScopeState:
    conflict_count: int = 0
    needs_attention: bool = False
    last_pdf_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Desired:
    events: tuple[Event, ...]
    pdf_sha256: str
    warnings: tuple[str, ...]


class Store:
    """SQLite-backed state. ``clock`` returns UTC epoch seconds (injectable for tests)."""

    def __init__(self, path: str, clock=time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        """True when the database answers a trivial query (used by /health)."""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # --- seen posts ----------------------------------------------------------------------

    def seen_posts(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT post_id, modified, pdf_sha256 FROM seen ORDER BY post_id")
        return [dict(r) for r in rows]

    def is_seen(self, post_id: int) -> bool:
        return bool(self._query("SELECT 1 FROM seen WHERE post_id=?", (post_id,)))

    def mark_seen(self, post_id: int, modified: str, pdf_sha256: str, scope_key: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO seen (post_id, modified, pdf_sha256, scope_key, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET modified=excluded.modified,"
                " pdf_sha256=excluded.pdf_sha256, scope_key=excluded.scope_key,"
                " updated_at=excluded.updated_at",
                (post_id, modified, pdf_sha256, scope_key, self.clock()),
            )

    def forget_seen_scope(self, scope_key: str) -> int:
        """Drop seen rows of a scope, so the next poll posts it again."""
        with self._tx() as c:
            return c.execute("DELETE FROM seen WHERE scope_key=?", (scope_key,)).rowcount

    # --- extraction cache ----------------------------------------------------------------

    def get_cache(self, pdf_sha256: str, prompt_version: str) -> list[dict[str, Any]] | None:
        rows = self._query(
            "SELECT spans FROM cache WHERE pdf_sha256=? AND prompt_version=?",
            (pdf_sha256, prompt_version),
        )
        return json.loads(rows[0]["spans"]) if rows else None

    def put_cache(self, pdf_sha256: str, prompt_version: str, spans: list[dict]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache (pdf_sha256, prompt_version, spans, created_at)"
                " VALUES (?,?,?,?)",
                (pdf_sha256, prompt_version, json.dumps(spans), self.clock()),
            )

    # --- identity map --------------------------------------------------------------------

    def identity_map(self) -> dict[str, Identity]:
        rows = self._query("SELECT uid, scope_key, date, last_written_etag FROM identity")
        return {
            r["uid"]: Identity(
                r["scope_key"], date.fromisoformat(r["date"]), r["last_written_etag"]
            )
            for r in rows
        }

    def set_identity(self, uid: str, scope_key: str, day: date, etag: str | None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO identity (uid, scope_key, date, last_written_etag)"
                " VALUES (?,?,?,?)",
                (uid, scope_key, day.isoformat(), etag),
            )

    def delete_identity(self, uid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM identity WHERE uid=?", (uid,))

    # --- desired events ------------------------------------------------------------------

    def set_desired(
        self, scope_key: str, events: Sequence[Event], pdf_sha256: str, warnings: Sequence[str]
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO desired (scope_key, events, pdf_sha256, warnings,"
                " updated_at) VALUES (?,?,?,?,?)",
                (
                    scope_key,
                    json.dumps([e.to_dict() for e in events]),
                    pdf_sha256,
                    json.dumps(list(warnings)),
                    self.clock(),
                ),
            )

    def get_desired(self, scope_key: str) -> Desired | None:
        rows = self._query("SELECT * FROM desired WHERE scope_key=?", (scope_key,))
        if not rows:
            return None
        r = rows[0]
        return Desired(
            tuple(Event.from_dict(e) for e in json.loads(r["events"])),
            r["pdf_sha256"],
            tuple(json.loads(r["warnings"])),
        )

    def desired_scopes(self) -> list[str]:
        return [r["scope_key"] for r in self._query("SELECT scope_key FROM desired ORDER BY 1")]

    def desired_all(self) -> list[tuple[str, tuple[Event, ...], float]]:
        """Every scope's desired events with the time they were stored, by scope key."""
        rows = self._query("SELECT scope_key, events, updated_at FROM desired ORDER BY 1")
        return [
            (
                r["scope_key"],
                tuple(Event.from_dict(e) for e in json.loads(r["events"])),
                r["updated_at"],
            )
            for r in rows
        ]

    def desired_events_except(self, scope_key: str) -> list[Event]:
        """Desired events of every other scope (``possible_conflict`` candidates)."""
        rows = self._query("SELECT events FROM desired WHERE scope_key<>?", (scope_key,))
        return [Event.from_dict(e) for r in rows for e in json.loads(r["events"])]

    # --- changesets and tokens -----------------------------------------------------------

    def save_changeset(self, cs: ChangeSet, ingest_warnings: Sequence[str] = ()) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO changesets (id, scope_key, content_hash, data, ingest_warnings,"
                " status, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    cs.id,
                    cs.scope_key,
                    cs.content_hash(),
                    json.dumps(cs.to_dict()),
                    json.dumps(list(ingest_warnings)),
                    "pending",
                    self.clock(),
                ),
            )

    def get_changeset(self, changeset_id: str) -> tuple[ChangeSet, tuple[str, ...]] | None:
        rows = self._query(
            "SELECT data, ingest_warnings FROM changesets WHERE id=?", (changeset_id,)
        )
        if not rows:
            return None
        cs = ChangeSet.from_dict(json.loads(rows[0]["data"]))
        return cs, tuple(json.loads(rows[0]["ingest_warnings"]))

    def changeset_status(self, changeset_id: str) -> str | None:
        rows = self._query("SELECT status FROM changesets WHERE id=?", (changeset_id,))
        return rows[0]["status"] if rows else None

    def set_changeset_status(self, changeset_id: str, status: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE changesets SET status=? WHERE id=?", (status, changeset_id))

    def create_token(
        self, cs: ChangeSet, ttl_seconds: float = TOKEN_TTL_SECONDS
    ) -> str:
        """Mint a 32-byte url-safe token bound to ``cs``; only its sha256 is stored."""
        token = secrets.token_urlsafe(32)
        with self._tx() as c:
            c.execute(
                "INSERT INTO tokens (token_hash, changeset_id, changeset_hash, scope_key,"
                " expires_at) VALUES (?,?,?,?,?)",
                (hash_token(token), cs.id, cs.content_hash(), cs.scope_key,
                 self.clock() + ttl_seconds),
            )  # fmt: skip
        return token

    def get_token(self, token: str) -> TokenRow | None:
        rows = self._query(
            "SELECT changeset_id, changeset_hash, scope_key, expires_at, used_at, superseded"
            " FROM tokens WHERE token_hash=?",
            (hash_token(token),),
        )
        if not rows:
            return None
        r = rows[0]
        return TokenRow(
            r["changeset_id"], r["changeset_hash"], r["scope_key"], r["expires_at"],
            r["used_at"], bool(r["superseded"]),
        )  # fmt: skip

    def consume_token(self, token: str) -> bool:
        """Atomically mark the token used; False when it was used, expired or superseded."""
        now = self.clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tokens SET used_at=? WHERE token_hash=? AND used_at IS NULL"
                " AND expires_at>? AND superseded=0",
                (now, hash_token(token), now),
            )
            return cur.rowcount == 1

    def supersede_scope(self, scope_key: str) -> int:
        """Invalidate every unused token and pending ChangeSet of ``scope_key``."""
        with self._tx() as c:
            c.execute(
                "UPDATE changesets SET status='superseded' WHERE scope_key=? AND status='pending'",
                (scope_key,),
            )
            return c.execute(
                "UPDATE tokens SET superseded=1 WHERE scope_key=? AND used_at IS NULL"
                " AND superseded=0",
                (scope_key,),
            ).rowcount

    def pending_changeset_hash(self, scope_key: str) -> str | None:
        """Hash of the ChangeSet behind a still-usable token of ``scope_key``, if any."""
        rows = self._query(
            "SELECT changeset_hash FROM tokens WHERE scope_key=? AND used_at IS NULL"
            " AND superseded=0 AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (scope_key, self.clock()),
        )
        return rows[0]["changeset_hash"] if rows else None

    def pending_count(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM tokens WHERE used_at IS NULL AND superseded=0"
            " AND expires_at>?",
            (self.clock(),),
        )
        return int(rows[0]["n"])

    # --- apply results -------------------------------------------------------------------

    def record_results(self, changeset_id: str, results: Sequence[OpResult]) -> None:
        now = self.clock()
        with self._tx() as c:
            c.executemany(
                "INSERT INTO op_results (changeset_id, idx, ok, status, new_etag, error, uid,"
                " applied_at) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (changeset_id, r.index, int(r.ok), r.status, r.new_etag, r.error, r.uid, now)
                    for r in results
                ],
            )

    def results_for(self, changeset_id: str) -> list[OpResult]:
        rows = self._query(
            "SELECT idx, ok, status, new_etag, error, uid FROM op_results WHERE changeset_id=?"
            " ORDER BY rowid",
            (changeset_id,),
        )
        return [
            OpResult(r["idx"], bool(r["ok"]), r["status"], r["new_etag"], r["error"], r["uid"])
            for r in rows
        ]

    # --- per-scope conflict state --------------------------------------------------------

    def scope_state(self, scope_key: str) -> ScopeState:
        rows = self._query(
            "SELECT conflict_count, needs_attention, last_pdf_sha256 FROM scopes"
            " WHERE scope_key=?",
            (scope_key,),
        )
        if not rows:
            return ScopeState()
        r = rows[0]
        return ScopeState(r["conflict_count"], bool(r["needs_attention"]), r["last_pdf_sha256"])

    def _ensure_scope(self, c: sqlite3.Connection, scope_key: str) -> None:
        c.execute("INSERT OR IGNORE INTO scopes (scope_key) VALUES (?)", (scope_key,))

    def record_conflict(self, scope_key: str, cap: int) -> ScopeState:
        """Count one 409/412; at ``cap`` consecutive conflicts the scope needs attention."""
        with self._tx() as c:
            self._ensure_scope(c, scope_key)
            c.execute(
                "UPDATE scopes SET conflict_count=conflict_count+1,"
                " needs_attention=CASE WHEN conflict_count+1>=? THEN 1 ELSE needs_attention END"
                " WHERE scope_key=?",
                (cap, scope_key),
            )
        return self.scope_state(scope_key)

    def reset_conflicts(self, scope_key: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE scopes SET conflict_count=0 WHERE scope_key=?", (scope_key,))

    def clear_attention(self, scope_key: str) -> bool:
        """Clear ``needs attention`` and the conflict counter; True if it was set."""
        with self._tx() as c:
            return (
                c.execute(
                    "UPDATE scopes SET needs_attention=0, conflict_count=0"
                    " WHERE scope_key=? AND (needs_attention=1 OR conflict_count>0)",
                    (scope_key,),
                ).rowcount
                == 1
            )

    def set_scope_pdf(self, scope_key: str, pdf_sha256: str) -> bool:
        """Remember the scope's PDF hash; a *changed* hash clears needs-attention.

        Returns True when the hash differs from the previous one (or is the first).
        """
        with self._tx() as c:
            self._ensure_scope(c, scope_key)
            row = c.execute(
                "SELECT last_pdf_sha256 FROM scopes WHERE scope_key=?", (scope_key,)
            ).fetchone()
            changed = row["last_pdf_sha256"] != pdf_sha256
            if changed:
                c.execute(
                    "UPDATE scopes SET last_pdf_sha256=?, needs_attention=0, conflict_count=0"
                    " WHERE scope_key=?",
                    (pdf_sha256, scope_key),
                )
            return changed

    def scopes_needing_attention(self) -> list[str]:
        return [
            r["scope_key"]
            for r in self._query("SELECT scope_key FROM scopes WHERE needs_attention=1 ORDER BY 1")
        ]

    # --- heartbeat timestamps ------------------------------------------------------------

    def get_meta(self, key: str) -> float | None:
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return float(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: float) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    def init_meta(self, key: str, value: float) -> float:
        """Set ``key`` only if absent; return the stored value."""
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?,?)", (key, value))
        stored = self.get_meta(key)
        assert stored is not None
        return stored

    # --- warning dedup -------------------------------------------------------------------

    def new_warnings(self, warnings: Sequence[str]) -> list[str]:
        """Return the warnings not notified before, and remember them."""
        fresh: list[str] = []
        with self._tx() as c:
            for w in dict.fromkeys(warnings):
                h = hashlib.sha256(w.encode("utf-8")).hexdigest()
                cur = c.execute(
                    "INSERT OR IGNORE INTO notified_warnings (text_hash, first_seen) VALUES (?,?)",
                    (h, self.clock()),
                )
                if cur.rowcount == 1:
                    fresh.append(w)
        return fresh
