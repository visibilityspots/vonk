"""Shared data contract for every vonk module.

This module is the single definition of the domain types. Other modules import from here and
never redefine ``Event``, ``ChangeSet`` or friends. Everything is a frozen dataclass so that
values can be hashed, compared and passed between threads without defensive copies.

Every type that crosses the internal API (poll job -> service) has ``to_dict()`` /
``from_dict()`` producing plain JSON-compatible values: dates as ``YYYY-MM-DD``, times as
``HH:MM[:SS]``, datetimes as ISO 8601 with offset.

Personal data rule: nothing in here carries child names. ``Event.title_activity`` is the
activity only; the ``<kindnaam> scouts: `` prefix is applied when rendering the calendar
event, never stored in or hashed from these types.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

# --- constants ---------------------------------------------------------------------------

UID_PREFIX = "vonk-"
"""Every event this tool creates has a UID starting with this prefix."""

UID_DOMAIN = "@vonk.visibilityspots"
"""Every event this tool creates has a UID ending with this domain."""

TZ = "Europe/Brussels"
"""Wall-clock time zone of all timed events."""

LOCATION = "Scoutslokalen Tervant"
"""LOCATION property of every event."""

X_VONK_SCOPE = "X-VONK-SCOPE"
"""iCalendar X-property carrying a copy of ``Scope.key`` (SQLite is the source of truth)."""

X_VONK_DATE = "X-VONK-DATE"
"""iCalendar X-property carrying a copy of ``Event.date`` as ``YYYY-MM-DD``."""

_UID_RE = re.compile(rf"^{re.escape(UID_PREFIX)}[^\s@]+{re.escape(UID_DOMAIN)}$")


def is_own_uid(uid: str | None) -> bool:
    """Return True when ``uid`` was minted by this tool (prefix *and* domain match).

    This is the only ownership test: events failing it are never modified or deleted.
    """
    return bool(uid) and _UID_RE.match(uid) is not None


def mint_uid() -> str:
    """Mint a fresh, random own UID: ``vonk-<uuid4>@vonk.visibilityspots``.

    A UID is minted once, when an event is first created, and never derived from content.
    """
    return f"{UID_PREFIX}{uuid.uuid4()}{UID_DOMAIN}"


# --- takken ------------------------------------------------------------------------------


class Tak(StrEnum):
    """Age group ("tak") of Scouts en Gidsen Tervant, plus ``GROEP`` for group-wide dates."""

    KAPOENEN = "kapoenen"
    KABOUTERS = "kabouters"
    WELPEN = "welpen"
    JOGIS = "jogis"
    """Jong gidsen."""
    JEEVEES = "jeevees"
    """Jong verkenners."""
    GIDSEN = "gidsen"
    VERKENNERS = "verkenners"
    JINS = "jins"
    GROEP = "groep"
    """Whole group: jaarvonk key dates (kamp, kerstfeestje, ...)."""


CATEGORY_TO_TAK: Mapping[int, Tak] = {
    9: Tak.KAPOENEN,  # Vonk kapoenen
    10: Tak.KABOUTERS,  # Vonk kabouters
    11: Tak.WELPEN,  # Vonk welpen
    12: Tak.JOGIS,  # Vonk jong gidsen
    13: Tak.JEEVEES,  # Vonk jong verkenners
    14: Tak.GIDSEN,  # Vonk gidsen
    15: Tak.VERKENNERS,  # Vonk verkenners
    16: Tak.JINS,  # Vonk jins
}
"""WordPress ``Vonk <tak>`` category id -> tak (verified against /wp-json/wp/v2/categories,
2026-10-09). The category is a hint, not a guarantee: also match on title/filename/content."""

LEIDING_CATEGORY_TO_TAK: Mapping[int, Tak] = {
    22: Tak.KAPOENEN,  # Kapoenen Leiding
    21: Tak.KABOUTERS,  # Kabouter Leiding
    23: Tak.WELPEN,  # Welpen Leiding
    24: Tak.JOGIS,  # Jogi Leiding
    25: Tak.JEEVEES,  # JeeVee Leiding
    27: Tak.GIDSEN,  # Gidsen Leiding
    26: Tak.VERKENNERS,  # Verkenner Leiding
    20: Tak.JINS,  # Jin Leiding
}
"""WordPress ``<tak> Leiding`` category id -> tak. Some vonks land here instead of in the
``Vonk <tak>`` category; a weaker hint than ``CATEGORY_TO_TAK``."""


# --- source posts ------------------------------------------------------------------------

VonkKind = Literal["monthly", "jaarvonk"]


@dataclass(frozen=True, slots=True)
class VonkPost:
    """One vonk post found through the WordPress REST API.

    ``tak`` is None when no tak could be resolved (that must yield a warning, not a skip);
    ``pdf_url`` is None when the post body has no PDF link (idem).
    """

    id: int
    modified: datetime
    title: str
    link: str
    pdf_url: str | None
    categories: tuple[int, ...]
    tak: Tak | None
    kind: VonkKind
    date: datetime | None = None
    """Publication date of the post; year-inference anchor when the filename/title has none."""


# --- scope -------------------------------------------------------------------------------

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SCOUT_YEAR_RE = re.compile(r"^(\d{4})-(\d{4})$")


@dataclass(frozen=True, slots=True)
class Scope:
    """The period a vonk covers: the unit of supersession, diffing and removal.

    ``period`` is ``YYYY-MM`` for a monthly vonk or ``YYYY-YYYY`` (consecutive years) for a
    scout year (jaarvonk, tak ``groep``). Removals only ever target events of the same scope.
    """

    tak: Tak
    period: str

    def __post_init__(self) -> None:
        if not isinstance(self.tak, Tak):
            raise TypeError(f"Scope.tak must be a Tak, got {type(self.tak).__name__}")
        if _MONTH_RE.match(self.period):
            return
        m = _SCOUT_YEAR_RE.match(self.period)
        if m and int(m.group(2)) == int(m.group(1)) + 1:
            return
        raise ValueError(f"invalid scope period {self.period!r} (want YYYY-MM or YYYY-YYYY+1)")

    @property
    def key(self) -> str:
        """Stable string form, e.g. ``kabouters:2026-10`` or ``groep:2026-2027``."""
        return f"{self.tak.value}:{self.period}"

    @property
    def is_scout_year(self) -> bool:
        """True for a ``YYYY-YYYY`` period."""
        return _SCOUT_YEAR_RE.match(self.period) is not None

    @classmethod
    def parse(cls, key: str) -> Scope:
        """Inverse of ``key``. Raises ValueError on malformed input."""
        tak, sep, period = key.partition(":")
        if not sep:
            raise ValueError(f"invalid scope key {key!r} (want '<tak>:<period>')")
        try:
            return cls(Tak(tak), period)
        except ValueError as exc:
            raise ValueError(f"invalid scope key {key!r}: {exc}") from None

    @classmethod
    def monthly(cls, tak: Tak, year: int, month: int) -> Scope:
        """Scope of a monthly vonk, e.g. ``Scope.monthly(Tak.KABOUTERS, 2026, 10)``."""
        return cls(tak, f"{year:04d}-{month:02d}")

    @classmethod
    def scout_year(cls, start_year: int, tak: Tak = Tak.GROEP) -> Scope:
        """Scope of a scout year (September ``start_year`` .. August ``start_year + 1``)."""
        return cls(tak, f"{start_year:04d}-{start_year + 1:04d}")


# --- extraction --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedSpan:
    """One activity as located by the extractor: verbatim substrings of the PDF text.

    Every non-None ``*_span`` must occur verbatim in the ``pdftotext -layout`` output; the
    extractor verifies that. Interpretation (dates, years, times) happens in plain code
    (``normalize``), never in the LLM.
    """

    date_span: str
    end_date_span: str | None
    start_span: str | None
    end_span: str | None
    title_span: str
    description_span: str | None
    cancelled: bool

    def spans(self) -> tuple[str, ...]:
        """All non-None spans, for the verbatim check."""
        return tuple(
            s
            for s in (
                self.date_span,
                self.end_date_span,
                self.start_span,
                self.end_span,
                self.title_span,
                self.description_span,
            )
            if s is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_span": self.date_span,
            "end_date_span": self.end_date_span,
            "start_span": self.start_span,
            "end_span": self.end_span,
            "title_span": self.title_span,
            "description_span": self.description_span,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ExtractedSpan:
        return cls(
            date_span=d["date_span"],
            end_date_span=d.get("end_date_span"),
            start_span=d.get("start_span"),
            end_span=d.get("end_span"),
            title_span=d["title_span"],
            description_span=d.get("description_span"),
            cancelled=bool(d.get("cancelled", False)),
        )


# --- events ------------------------------------------------------------------------------


def _iso_or_none(v: date | time | None) -> str | None:
    return None if v is None else v.isoformat()


@dataclass(frozen=True, slots=True)
class Event:
    """A desired or existing calendar event, without child names.

    - ``date`` / ``end_date`` are local (Europe/Brussels) calendar dates; ``end_date`` is
      *inclusive* (last day of a multi-day activity) and None for single-day events.
    - ``start`` / ``end`` are naive wall-clock times in ``TZ``; both None when ``all_day``.
    - ``end_unknown`` marks an open-ended time (``18u - ....``); the sink then uses a default
      end and adds ``einduur onbekend`` to the notes.
    - ``title_activity`` is the activity only (e.g. ``Dropping``); the renderer prefixes it.
    """

    scope_key: str
    date: date
    end_date: date | None
    start: time | None
    end: time | None
    all_day: bool
    title_activity: str
    notes: str
    source_url: str
    warnings: tuple[str, ...] = ()
    end_unknown: bool = False

    def __post_init__(self) -> None:
        if self.all_day and (self.start is not None or self.end is not None):
            raise ValueError("all-day event cannot have start/end times")
        if self.end_date is not None and self.end_date < self.date:
            raise ValueError(f"end_date {self.end_date} before date {self.date}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "date": self.date.isoformat(),
            "end_date": _iso_or_none(self.end_date),
            "start": _iso_or_none(self.start),
            "end": _iso_or_none(self.end),
            "all_day": self.all_day,
            "title_activity": self.title_activity,
            "notes": self.notes,
            "source_url": self.source_url,
            "warnings": list(self.warnings),
            "end_unknown": self.end_unknown,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Event:
        return cls(
            scope_key=d["scope_key"],
            date=date.fromisoformat(d["date"]),
            end_date=date.fromisoformat(d["end_date"]) if d.get("end_date") else None,
            start=time.fromisoformat(d["start"]) if d.get("start") else None,
            end=time.fromisoformat(d["end"]) if d.get("end") else None,
            all_day=bool(d["all_day"]),
            title_activity=d["title_activity"],
            notes=d.get("notes", ""),
            source_url=d.get("source_url", ""),
            warnings=tuple(d.get("warnings", ())),
            end_unknown=bool(d.get("end_unknown", False)),
        )


@dataclass(frozen=True, slots=True)
class OwnEvent:
    """An event read back from a sink whose UID passes ``is_own_uid``.

    ``event`` is reconstructed from the calendar data (scope from SQLite, falling back to
    ``X-VONK-SCOPE``); ``raw_ics`` is the VCALENDAR text as stored, kept so a write can
    preserve properties this tool does not manage. ``etag`` is None if the server sent none.
    """

    uid: str
    etag: str | None
    event: Event
    raw_ics: str

    def __post_init__(self) -> None:
        if not is_own_uid(self.uid):
            raise ValueError(f"not an own UID: {self.uid!r}")


# --- diff --------------------------------------------------------------------------------


class OpKind(StrEnum):
    """Kind of a ChangeSet operation."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


class OpFlag(StrEnum):
    """Annotations on an Operation for the approve page. Members are plain strings."""

    MOVED = "moved"
    """Remove + add pair of the same activity on another date."""
    MANUALLY_EDITED = "manually_edited"
    """Live ETag differs from the last written one: Jan edited it; kept unless ticked."""
    RENAME_ONLY = "rename_only"
    """Only the rendered title changed (child-name map change)."""
    ORPHAN = "orphan"
    """Own UID without SQLite row or X-properties; never re-added, removal only if ticked."""
    POSSIBLE_CONFLICT = "possible_conflict"
    """Jaarvonk and monthly vonk disagree on the date of the same activity (display only)."""
    MASS_REMOVAL = "mass_removal"
    """Part of a ChangeSet that removes more than 50% of a scope's events."""


@dataclass(frozen=True, slots=True)
class Operation:
    """One candidate change.

    - add: ``uid`` None (minted at apply time), ``before`` None, ``after`` set.
    - update: ``uid``, ``before`` and ``after`` set.
    - remove: ``uid`` and ``before`` set, ``after`` None.
    """

    kind: OpKind
    uid: str | None
    before: Event | None
    after: Event | None
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = OpKind(self.kind)
        if kind is OpKind.ADD and (self.before is not None or self.after is None):
            raise ValueError("add needs after and no before")
        if kind is OpKind.UPDATE and (self.before is None or self.after is None):
            raise ValueError("update needs before and after")
        if kind is OpKind.REMOVE and (self.before is None or self.after is not None):
            raise ValueError("remove needs before and no after")
        if kind is not OpKind.ADD and not is_own_uid(self.uid):
            raise ValueError(f"{kind.value} needs an own UID, got {self.uid!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": OpKind(self.kind).value,
            "uid": self.uid,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "flags": [str(f) for f in self.flags],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Operation:
        return cls(
            kind=OpKind(d["kind"]),
            uid=d.get("uid"),
            before=Event.from_dict(d["before"]) if d.get("before") else None,
            after=Event.from_dict(d["after"]) if d.get("after") else None,
            flags=tuple(d.get("flags", ())),
        )


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """All candidate operations for one scope, awaiting approval.

    ``content_hash()`` identifies the candidate set; an approve token is bound to it. Ticking
    a subset of ops on the approve page selects indexes and does not change the hash.
    ``created_at`` must be timezone-aware.
    """

    id: str
    scope_key: str
    ops: tuple[Operation, ...]
    warnings: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("ChangeSet.created_at must be timezone-aware")

    def content_hash(self) -> str:
        """Deterministic sha256 (hex) over the canonical JSON of ``ops`` (order matters)."""
        canonical = json.dumps(
            [op.to_dict() for op in self.ops],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def is_empty(self) -> bool:
        return not self.ops

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope_key": self.scope_key,
            "ops": [op.to_dict() for op in self.ops],
            "warnings": list(self.warnings),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ChangeSet:
        return cls(
            id=d["id"],
            scope_key=d["scope_key"],
            ops=tuple(Operation.from_dict(o) for o in d["ops"]),
            warnings=tuple(d.get("warnings", ())),
            created_at=datetime.fromisoformat(d["created_at"]),
        )


# --- sinks -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpResult:
    """Outcome of applying one operation.

    ``status`` is the HTTP status of the write (None when no request was made);
    ``new_etag`` is the ETag read back after the write (None for removals or if unknown);
    ``uid`` is the event's UID (the freshly minted one for an add).
    """

    index: int
    ok: bool
    status: int | None
    new_etag: str | None
    error: str | None
    uid: str | None = None


@runtime_checkable
class Sink(Protocol):
    """A calendar destination. ``CalDAVSink`` now, an ICS feed later.

    Implementations must only ever list, write or delete events passing ``is_own_uid``.
    """

    def list_own(self, scope_key: str | None) -> list[OwnEvent]:
        """Own events, limited to ``scope_key`` when given, else all own events."""
        ...

    def apply(self, changeset: ChangeSet, selected_op_indexes: Sequence[int]) -> list[OpResult]:
        """Apply the selected ops of ``changeset``; one OpResult per selected index.

        Stops at the first precondition failure (412) and reports the remaining ops as not
        applied.
        """
        ...
