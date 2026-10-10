"""Gotify notices: new ChangeSets, apply results and poll warnings.

A failed notice is logged and swallowed: notifying is never a reason to lose state or to
fail an API call. Notices carry activities and counts only, never child names: they are
sent as plain text (PDF-derived text cannot inject markdown links or images), and any
configured child name that slips into a notice anyway is replaced by its tak.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

import httpx

from vonk.models import ChangeSet, OpFlag, OpKind, OpResult, Tak

log = logging.getLogger("vonk.notify")

HTTP_TIMEOUT = 10.0
PRIORITY_INFO = 5
PRIORITY_HIGH = 8


class Notifier(Protocol):
    def send(self, title: str, message: str, priority: int = PRIORITY_INFO) -> bool: ...


class GotifyNotifier:
    """POST ``{GOTIFY_URL}/message`` with the application token in ``X-Gotify-Key``."""

    def __init__(
        self,
        http: httpx.Client,
        url: str,
        token: str,
        child_names: Mapping[Tak, str] | None = None,
    ) -> None:
        self.http = http
        self.url = url.rstrip("/") + "/message"
        self.token = token
        self.child_names = dict(child_names or {})

    def send(self, title: str, message: str, priority: int = PRIORITY_INFO) -> bool:
        title = redact(title, self.child_names)
        message = redact(message, self.child_names)
        try:
            resp = self.http.post(
                self.url,
                headers={"X-Gotify-Key": self.token},
                json={
                    "title": title,
                    "message": message,
                    "priority": priority,
                    "extras": {"client::display": {"contentType": "text/plain"}},
                },
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            log.error("gotify unreachable: %s", type(exc).__name__)
            return False
        if not resp.is_success:
            log.error("gotify answered HTTP %s", resp.status_code)
            return False
        return True


_URL = re.compile(r"https?://\S+")
"""Links (approve URLs carry a random token) are never redacted."""


def redact(text: str, child_names: Mapping[Tak, str]) -> str:
    """Replace each configured child name, as a whole word and case-insensitively, by its tak.

    One compiled alternation and a single pass, so a replacement is never matched again;
    URLs in ``text`` are left byte-for-byte untouched.
    """
    by_name = {name.casefold(): tak.value for tak, name in child_names.items() if name}
    if not by_name:
        return text
    alternation = "|".join(re.escape(n) for n in sorted(by_name, key=len, reverse=True))
    pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)

    def plain(part: str) -> str:
        return pattern.sub(lambda m: by_name.get(m.group(0).casefold(), m.group(0)), part)

    out: list[str] = []
    last = 0
    for link in _URL.finditer(text):
        out += [plain(text[last : link.start()]), link.group(0)]
        last = link.end()
    out.append(plain(text[last:]))
    return "".join(out)


class NullNotifier:
    """Used when Gotify is not configured (degraded start): log only."""

    def send(self, title: str, message: str, priority: int = PRIORITY_INFO) -> bool:
        log.warning("notice not sent (Gotify not configured): %s", title)
        return False


def summarize(cs: ChangeSet) -> str:
    adds = sum(1 for op in cs.ops if op.kind == OpKind.ADD)
    updates = sum(1 for op in cs.ops if op.kind == OpKind.UPDATE)
    removes = sum(1 for op in cs.ops if op.kind == OpKind.REMOVE)
    return f"{adds} nieuw, {updates} gewijzigd, {removes} verwijderd"


def changeset_message(
    cs: ChangeSet, link: str, ingest_warnings: Sequence[str] = ()
) -> tuple[str, str, int]:
    """(title, message, priority) announcing a ChangeSet for approval."""
    mass = any(OpFlag.MASS_REMOVAL in op.flags for op in cs.ops)
    lines: list[str] = []
    if mass:
        lines += ["LET OP: massale verwijdering (meer dan de helft van de events)", ""]
    lines.append(f"{cs.scope_key}: {summarize(cs)}")
    for op in cs.ops:
        ev = op.after or op.before
        assert ev is not None
        when = ev.date.isoformat() + (f" {ev.start:%H:%M}" if ev.start else "")
        lines.append(f"- {OpKind(op.kind).value} {when} {ev.title_activity}")
    lines += [f"- ⚠ {w}" for w in (*cs.warnings, *ingest_warnings)]
    lines += ["", f"Nakijken en goedkeuren: {link}"]
    priority = PRIORITY_HIGH if mass else PRIORITY_INFO
    return f"vonk: wijzigingen voor {cs.scope_key}", "\n".join(lines), priority


def result_message(cs: ChangeSet, results: Sequence[OpResult]) -> tuple[str, str, int]:
    ok = sum(1 for r in results if r.ok)
    failed = [r for r in results if not r.ok]
    lines = [f"{cs.scope_key}: {ok} van {len(results)} wijzigingen geschreven"]
    lines += [f"- op {r.index}: {r.error or r.status}" for r in failed]
    priority = PRIORITY_HIGH if failed else PRIORITY_INFO
    return f"vonk: toegepast op {cs.scope_key}", "\n".join(lines), priority


def warnings_message(warnings: Sequence[str]) -> tuple[str, str, int]:
    return "vonk: waarschuwingen", "\n".join(f"- {w}" for w in warnings), PRIORITY_HIGH
