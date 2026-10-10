"""Match desired events to existing own events within the same (scope, date).

Identity of an existing own event comes from the store (SQLite): ``uid -> (scope_key, date,
last_written_etag)``. The ``X-VONK-SCOPE`` / ``X-VONK-DATE`` copies (already folded into
``OwnEvent.event`` by the sink) are only a fallback to *adopt* an event the store does not
know. An own UID with neither is an *orphan*: it is never matched and never re-added.

Within one (scope, date) group the pairing runs in passes, each greedy and deterministic:

1. exact start time and equal normalised title;
2. equal normalised title, nearest start time;
3. title similarity >= ``SIMILARITY_THRESHOLD`` (difflib), best ratio first;
4. exact same start time (same slot, activity renamed);
5. a ``manually_edited`` existing event (Jan changed title and time, or the title of an
   all-day event, on his phone) pairs with a remaining desired event: equal notes body
   first (the text we wrote still identifies the activity), then nearest start (all-day vs
   all-day is distance 0). The result is an update flagged manually_edited, unticked by
   default, so the edit is kept instead of a remove + a duplicate add.

Unpaired desired events are adds, unpaired existing events are removes.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time
from typing import NamedTuple

from vonk.models import Event, OwnEvent, Scope
from vonk.normalize import collapse_ws, normalize_title

SIMILARITY_THRESHOLD = 0.6

_NO_TIME_DISTANCE = 24 * 60
"""Distance (minutes) between a timed and an all-day event: worse than any real gap."""


class Identity(NamedTuple):
    """Store row for one own UID. Plain ``(scope_key, date, etag)`` tuples work too."""

    scope_key: str
    date: date
    last_written_etag: str | None


IdentityMap = Mapping[str, Identity | tuple[str, date, str | None]]


@dataclass(frozen=True, slots=True)
class Resolved:
    """An existing own event with its identity and where that identity came from."""

    own: OwnEvent
    scope_key: str
    date: date
    last_written_etag: str | None
    source: str
    """``store`` (SQLite row) or ``adopted`` (X-properties)."""

    @property
    def manually_edited(self) -> bool:
        """Live ETag differs from the one we last wrote (only known for store rows)."""
        return self.last_written_etag is not None and self.own.etag != self.last_written_etag


def resolve(
    existing: Iterable[OwnEvent], identity_map: IdentityMap
) -> tuple[list[Resolved], list[OwnEvent]]:
    """Split own events into resolved ones (store row, else X-properties) and orphans."""
    resolved: list[Resolved] = []
    orphans: list[OwnEvent] = []
    for own in existing:
        row = identity_map.get(own.uid)
        if row is not None:
            scope_key, day, etag = row
            resolved.append(Resolved(own, scope_key, day, etag, "store"))
            continue
        if _valid_scope(own.event.scope_key):
            resolved.append(Resolved(own, own.event.scope_key, own.event.date, None, "adopted"))
        else:
            orphans.append(own)
    return resolved, orphans


def _valid_scope(key: str) -> bool:
    if not key:
        return False
    try:
        Scope.parse(key)
    except ValueError:
        return False
    return True


def _minutes(t: time | None) -> int | None:
    return None if t is None else t.hour * 60 + t.minute


def _start_distance(a: Event, b: Event) -> int:
    ma, mb = _minutes(a.start), _minutes(b.start)
    if ma is None and mb is None:
        return 0
    if ma is None or mb is None:
        return _NO_TIME_DISTANCE
    return abs(ma - mb)


def notes_body(ev: Event) -> str:
    """Notes without the source-URL line, whitespace collapsed."""
    lines = (ln for ln in ev.notes.splitlines() if ln.strip() != ev.source_url.strip())
    return collapse_ws(" ".join(lines))


def title_similarity(a: str, b: str) -> float:
    """difflib ratio of two titles after normalisation."""
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


@dataclass(slots=True)
class MatchResult:
    pairs: list[tuple[Event, Resolved]] = field(default_factory=list)
    adds: list[Event] = field(default_factory=list)
    removes: list[Resolved] = field(default_factory=list)
    orphans: list[OwnEvent] = field(default_factory=list)


def _pair_group(desired: list[Event], existing: list[Resolved], result: MatchResult) -> None:
    left_d = list(desired)
    left_e = list(existing)

    def run_pass(score) -> None:
        # score(d, e) -> sortable key or None when not eligible; lowest key pairs first.
        while True:
            best = None
            for i, d in enumerate(left_d):
                for j, e in enumerate(left_e):
                    s = score(d, e)
                    if s is not None and (best is None or (s, i, j) < best):
                        best = (s, i, j)
            if best is None:
                return
            _, i, j = best
            result.pairs.append((left_d.pop(i), left_e.pop(j)))

    def same_title(d: Event, e: Event) -> bool:
        return normalize_title(d.title_activity) == normalize_title(e.title_activity)

    def exact(d: Event, r: Resolved):
        e = r.own.event
        return 0 if same_title(d, e) and _start_distance(d, e) == 0 else None

    def nearest_same_title(d: Event, r: Resolved):
        return _start_distance(d, r.own.event) if same_title(d, r.own.event) else None

    def by_similarity(d: Event, r: Resolved):
        ratio = title_similarity(d.title_activity, r.own.event.title_activity)
        return (-ratio, _start_distance(d, r.own.event)) if ratio >= SIMILARITY_THRESHOLD else None

    def same_slot(d: Event, r: Resolved):
        return 0 if d.start is not None and d.start == r.own.event.start else None

    def hand_edited(d: Event, r: Resolved):
        if not r.manually_edited:
            return None
        return (notes_body(d) != notes_body(r.own.event), _start_distance(d, r.own.event))

    for score in (exact, nearest_same_title, by_similarity, same_slot, hand_edited):
        run_pass(score)
    result.adds.extend(left_d)
    result.removes.extend(left_e)


def match(
    desired: Sequence[Event],
    existing: Iterable[OwnEvent],
    identity_map: IdentityMap,
) -> MatchResult:
    """Pair desired events with existing own events per (scope_key, date)."""
    resolved, orphans = resolve(existing, identity_map)
    result = MatchResult(orphans=orphans)
    want: dict[tuple[str, date], list[Event]] = defaultdict(list)
    have: dict[tuple[str, date], list[Resolved]] = defaultdict(list)
    for ev in desired:
        want[(ev.scope_key, ev.date)].append(ev)
    for r in resolved:
        have[(r.scope_key, r.date)].append(r)
    for key in sorted(set(want) | set(have)):
        _pair_group(want.get(key, []), have.get(key, []), result)
    return result
