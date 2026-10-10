"""Compute a ChangeSet (add/update/remove) for a scope from desired and own events.

Rules (plan "Key design rules"):

- Only existing own events whose identity resolves to ``scope_key`` take part; events of
  another scope on the same date (month-edge spill-over) are never touched.
- Updates compare dates, times, all-day, ``end_unknown``, the normalised title and the
  notes body. The source-URL line of the notes is ignored, so a re-upload of the same
  content under a new PDF URL is not a change.
- ``manually_edited``: live ETag differs from the last written one; the op is kept but
  flagged (the approve page leaves it unticked).
- ``rename_only``: only the rendered calendar title differs (child-name map changed).
- ``moved``: an unpaired remove and add with the same normalised title on different dates
  (for a ``manually_edited`` remove whose title Jan changed: the same non-empty notes
  body). When the remove side is ``manually_edited`` the add carries that flag too, so the page
  leaves both unticked (ticking only the add would duplicate the kept, hand-edited event).
- ``orphan``: own UID with neither a store row nor X-properties; offered for removal only.
- ``mass_removal``: removals exceed 50% of the scope's existing events.
- ``possible_conflict``: an added/updated event resembles an event of a related scope
  (jaarvonk vs monthly vonk) on another date. Display only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from icalendar import Calendar

from vonk.match import (
    SIMILARITY_THRESHOLD,
    IdentityMap,
    Resolved,
    match,
    notes_body,
    title_similarity,
)
from vonk.models import ChangeSet, Event, Operation, OpFlag, OpKind, OwnEvent, Scope, Tak
from vonk.normalize import normalize_title, render_title

MASS_REMOVAL_RATIO = 0.5
CONFLICT_MAX_DAYS = 7


def _comparable(ev: Event) -> tuple:
    return (
        ev.date,
        ev.end_date,
        ev.start,
        ev.end,
        ev.all_day,
        ev.end_unknown,
        normalize_title(ev.title_activity),
        notes_body(ev),
    )


def live_summary(raw_ics: str) -> str | None:
    """SUMMARY of the first VEVENT in ``raw_ics``; None if absent or unparsable."""
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return None
    for comp in cal.walk("VEVENT"):
        summary = comp.get("SUMMARY")
        return None if summary is None else str(summary)
    return None


def _sort_key(ev: Event) -> tuple:
    return (ev.date, ev.start is not None, ev.start or datetime.min.time(), ev.title_activity)


def _related_tak(scope_key: str) -> Tak | None:
    try:
        return Scope.parse(scope_key).tak
    except ValueError:
        return None


def possible_conflicts(
    events_a: Iterable[Event], events_b: Iterable[Event], max_days: int = CONFLICT_MAX_DAYS
) -> list[tuple[Event, Event]]:
    """Pairs (a, b) that look like the same activity on different dates, where one side is
    a ``groep`` (jaarvonk) scope and the other a tak scope.

    "Same activity" = equal normalised title or difflib similarity >= 0.6, at most
    ``max_days`` apart.
    """
    b_list = list(events_b)
    found: list[tuple[Event, Event]] = []
    for a in events_a:
        tak_a = _related_tak(a.scope_key)
        for b in b_list:
            tak_b = _related_tak(b.scope_key)
            if tak_a is None or tak_b is None or (tak_a is Tak.GROEP) == (tak_b is Tak.GROEP):
                continue
            if a.date == b.date or abs((a.date - b.date).days) > max_days:
                continue
            same = normalize_title(a.title_activity) == normalize_title(b.title_activity)
            if same or title_similarity(a.title_activity, b.title_activity) >= (
                SIMILARITY_THRESHOLD
            ):
                found.append((a, b))
    return found


def build_changeset(
    scope_key: str,
    desired: Sequence[Event],
    existing_own: Iterable[OwnEvent],
    identity_map: IdentityMap,
    *,
    child_names: Mapping[Tak, str] | None = None,
    related_events: Sequence[Event] = (),
    changeset_id: str | None = None,
    created_at: datetime | None = None,
) -> ChangeSet:
    """Diff ``desired`` (all events of one scope) against the calendar's own events.

    ``existing_own`` may contain events of other scopes; they are ignored. Orphans present
    in it become ``orphan`` remove ops. ``child_names`` enables ``rename_only`` detection
    (compares the rendered title with the live SUMMARY). ``related_events`` are desired
    events of other scopes used for ``possible_conflict`` flags.
    """
    Scope.parse(scope_key)
    for ev in desired:
        if ev.scope_key != scope_key:
            raise ValueError(f"desired event of scope {ev.scope_key!r} in diff of {scope_key!r}")

    result = match(desired, existing_own, identity_map)
    in_scope_pairs = [(d, r) for d, r in result.pairs if r.scope_key == scope_key]
    removes = [r for r in result.removes if r.scope_key == scope_key]
    scope_existing = len(in_scope_pairs) + len(removes)

    conflict_targets = [ev for ev, _ in possible_conflicts(desired, related_events)]

    def extra_flags(ev: Event) -> list[str]:
        return [OpFlag.POSSIBLE_CONFLICT] if any(ev is c for c in conflict_targets) else []

    updates: list[Operation] = []
    for want, have in sorted(in_scope_pairs, key=lambda p: _sort_key(p[0])):
        flags = _update_flags(want, have, child_names)
        if flags is None:
            continue
        updates.append(
            Operation(
                OpKind.UPDATE, have.own.uid, have.own.event, want, (*flags, *extra_flags(want))
            )
        )

    adds = sorted(result.adds, key=_sort_key)
    removes.sort(key=lambda r: (_sort_key(r.own.event), r.own.uid))

    # moved: pair each remove with the nearest-dated add of the same normalised title.
    moved: list[tuple[Resolved, Event]] = []
    for r in list(removes):
        title = normalize_title(r.own.event.title_activity)
        candidates = [a for a in adds if normalize_title(a.title_activity) == title]
        body = notes_body(r.own.event)
        if not candidates and r.manually_edited and body:
            candidates = [a for a in adds if notes_body(a) == body]
        if not candidates:
            continue
        best = min(candidates, key=lambda a: (abs((a.date - r.date).days), _sort_key(a)))
        adds.remove(best)
        removes.remove(r)
        moved.append((r, best))

    mass = scope_existing > 0 and (len(removes) + len(moved)) / scope_existing > (
        MASS_REMOVAL_RATIO
    )

    def remove_op(r: Resolved, flags: list[str]) -> Operation:
        if r.manually_edited:
            flags = [*flags, OpFlag.MANUALLY_EDITED]
        if mass:
            flags = [*flags, OpFlag.MASS_REMOVAL]
        return Operation(OpKind.REMOVE, r.own.uid, r.own.event, None, tuple(flags))

    ops: list[Operation] = list(updates)
    for r, a in moved:
        ops.append(remove_op(r, [OpFlag.MOVED]))
        edited = (OpFlag.MANUALLY_EDITED,) if r.manually_edited else ()
        ops.append(Operation(OpKind.ADD, None, None, a, (OpFlag.MOVED, *edited, *extra_flags(a))))
    ops.extend(Operation(OpKind.ADD, None, None, a, tuple(extra_flags(a))) for a in adds)
    ops.extend(remove_op(r, []) for r in removes)
    for own in sorted(result.orphans, key=lambda o: o.uid):
        ops.append(Operation(OpKind.REMOVE, own.uid, own.event, None, (OpFlag.ORPHAN,)))

    warnings: list[str] = []
    if mass:
        warnings.append(
            f"LET OP: {len(removes) + len(moved)} van {scope_existing} events in {scope_key} "
            "zouden verwijderd worden (meer dan de helft)"
        )
    return ChangeSet(
        id=changeset_id or uuid.uuid4().hex,
        scope_key=scope_key,
        ops=tuple(ops),
        warnings=tuple(warnings),
        **({"created_at": created_at} if created_at is not None else {}),
    )


def _update_flags(
    want: Event, have: Resolved, child_names: Mapping[Tak, str] | None
) -> list[str] | None:
    """Flags for an update of ``have`` to ``want``; None when nothing changed."""
    flags: list[str] = []
    if _comparable(want) == _comparable(have.own.event):
        if child_names is None:
            return None
        live = live_summary(have.own.raw_ics)
        if live is None or live == render_title(want, child_names):
            return None
        flags.append(OpFlag.RENAME_ONLY)
    if have.manually_edited:
        flags.append(OpFlag.MANUALLY_EDITED)
    return flags
