"""CalDAV sink: list and write own (vonk- UID) events in the Baikal calendar.

Raw WebDAV over ``httpx`` instead of the ``caldav`` library, on purpose: every write must
carry an exact precondition (``If-None-Match: *`` on create, ``If-Match: <etag>`` on update
and delete) and the ETag must be read back after each PUT (sabre/Baikal may not return one).
With plain httpx those headers are explicit, a 412 surfaces as a status code instead of a
library exception, and unit tests run against an in-memory server via ``httpx.MockTransport``.

Ownership: ``list_own`` drops every event whose UID fails ``models.is_own_uid`` before
parsing it any further, and ``apply`` / ``purge_own`` refuse non-own UIDs. Foreign events are
never read into a diff or touched.

Preconditions: ``apply`` uses the ETags seen by the most recent ``list_own`` (or written by
this sink since). The service re-fetches own events right before applying, under the scope
lock, so those ETags are the state the approved ChangeSet was re-diffed against.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urljoin, urlsplit

import httpx
from icalendar import Calendar
from icalendar import Event as ICalEvent

from vonk.models import (
    LOCATION,
    UID_DOMAIN,
    X_VONK_DATE,
    X_VONK_SCOPE,
    ChangeSet,
    Event,
    OpKind,
    OpResult,
    OwnEvent,
    Tak,
    is_own_uid,
    mint_uid,
)
from vonk.normalize import END_UNKNOWN_NOTE, TZ, activity_from_summary, event_bounds, render_title

log = logging.getLogger("vonk.caldav")

PRODID = "-//visibilityspots//vonk//NL"
HTTP_TIMEOUT = 30.0

_NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}

_REPORT_BODY = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
    <C:prop-filter name="UID">
      <C:text-match collation="i;octet">{UID_DOMAIN}</C:text-match>
    </C:prop-filter>
  </C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>
"""
"""Only events whose UID contains the own domain. Without this filter Baikal returns the
whole calendar: ``gezin`` (1332 events, 1.7 MB) took 40 s, past ``HTTP_TIMEOUT``; with it,
0.7 s (measured 2026-10-11). ``list_own`` still applies ``is_own_uid`` to every result."""


class CalDAVError(RuntimeError):
    """The CalDAV server answered unexpectedly (listing failed)."""


def calendar_url(base_url: str, user: str, calendar: str) -> str:
    """Collection URL: ``<base>/calendars/<user>/<calendar>/`` (Baikal layout).

    A ``calendar`` containing ``/`` is taken as a path relative to ``base_url``.
    """
    base = base_url if base_url.endswith("/") else base_url + "/"
    path = calendar.strip("/") if "/" in calendar else f"calendars/{user}/{calendar}"
    return urljoin(base, path + "/")


def calendar_name(collection_url: str) -> str:
    """Last path segment of a collection URL (``vonk-test`` for ``.../vonk-test/``)."""
    return urlsplit(collection_url).path.rstrip("/").rsplit("/", 1)[-1]


# --- iCalendar ---------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
"""Stripped from URL values: icalendar refuses CR/LF in a URI (and they never belong there)."""


def to_ics(uid: str, ev: Event, child_names: Mapping[Tak, str], sequence: int = 0) -> str:
    """Render one VEVENT (with VTIMEZONE for timed events). Child names only here."""
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    vevent = build_vevent(
        uid, ev, render_title(ev, child_names), dtstamp=datetime.now(UTC), sequence=sequence
    )
    cal.add_component(vevent)
    if event_bounds(ev) is not None:
        cal.add_missing_timezones()
    return cal.to_ical().decode("utf-8")


def build_vevent(
    uid: str, ev: Event, summary: str, *, dtstamp: datetime, sequence: int = 0
) -> ICalEvent:
    """The VEVENT for ``ev``, shared by the CalDAV sink and the public feed."""
    vevent = ICalEvent()
    vevent.add("uid", uid)
    vevent.add("dtstamp", dtstamp)
    vevent.add("sequence", sequence)
    vevent.add("summary", summary)
    if ev.notes:
        vevent.add("description", ev.notes)
    vevent.add("location", LOCATION)
    source_url = _CONTROL_CHARS.sub("", ev.source_url or "").strip()
    if source_url:
        vevent.add("url", source_url)
    bounds = event_bounds(ev)
    if bounds is None:
        vevent.add("dtstart", ev.date)
        vevent.add("dtend", (ev.end_date or ev.date) + timedelta(days=1))
    else:
        vevent.add("dtstart", bounds[0])
        vevent.add("dtend", bounds[1])
    vevent.add(X_VONK_SCOPE, ev.scope_key)
    vevent.add(X_VONK_DATE, ev.date.isoformat())
    return vevent


def _master_vevent(cal: Calendar) -> ICalEvent | None:
    for comp in cal.walk("VEVENT"):
        if comp.get("RECURRENCE-ID") is None:
            return comp
    return None


def _local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def parse_own(uid: str, etag: str | None, raw_ics: str) -> OwnEvent | None:
    """OwnEvent from calendar data; None when it cannot be interpreted as an event."""
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return None
    vevent = _master_vevent(cal)
    if vevent is None or vevent.get("DTSTART") is None:
        return None
    start = vevent.decoded("DTSTART")
    end = vevent.decoded("DTEND") if vevent.get("DTEND") is not None else None
    description = str(vevent.get("DESCRIPTION", ""))
    url = str(vevent.get("URL", "")) or next(
        (ln for ln in reversed(description.splitlines()) if ln.startswith("http")), ""
    )
    try:
        if isinstance(start, datetime):
            s = _local(start)
            e = _local(end) if isinstance(end, datetime) else None
            event = Event(
                scope_key=str(vevent.get(X_VONK_SCOPE, "")),
                date=s.date(),
                end_date=None,
                start=s.time().replace(tzinfo=None),
                end=e.time().replace(tzinfo=None) if e else None,
                all_day=False,
                title_activity=activity_from_summary(str(vevent.get("SUMMARY", ""))),
                notes=description,
                source_url=url,
                end_unknown=END_UNKNOWN_NOTE in description.splitlines(),
            )
        else:
            assert isinstance(start, date)
            last = end - timedelta(days=1) if isinstance(end, date) else start
            event = Event(
                scope_key=str(vevent.get(X_VONK_SCOPE, "")),
                date=start,
                end_date=last if last > start else None,
                start=None,
                end=None,
                all_day=True,
                title_activity=activity_from_summary(str(vevent.get("SUMMARY", ""))),
                notes=description,
                source_url=url,
            )
    except ValueError:
        return None
    return OwnEvent(uid=uid, etag=etag, event=event, raw_ics=raw_ics)


def _uid_of(raw_ics: str) -> str | None:
    try:
        cal = Calendar.from_ical(raw_ics)
    except ValueError:
        return None
    for comp in cal.walk("VEVENT"):
        uid = comp.get("UID")
        return None if uid is None else str(uid)
    return None


def _sequence_of(raw_ics: str) -> int:
    try:
        vevent = _master_vevent(Calendar.from_ical(raw_ics))
    except ValueError:
        return 0
    try:
        return int(vevent.get("SEQUENCE", 0)) if vevent is not None else 0
    except (TypeError, ValueError):
        return 0


# --- sink --------------------------------------------------------------------------------


class CalDAVSink:
    """``models.Sink`` over one CalDAV collection."""

    def __init__(
        self, http: httpx.Client, collection_url: str, child_names: Mapping[Tak, str]
    ) -> None:
        self.http = http
        self.url = collection_url if collection_url.endswith("/") else collection_url + "/"
        self.child_names = child_names
        self._etags: dict[str, str | None] = {}
        self._hrefs: dict[str, str] = {}
        self._raw: dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings, calendar: str | None = None, **client_kw) -> CalDAVSink:
        cal = calendar or settings.baikal_calendar
        http = httpx.Client(
            auth=(settings.baikal_user, settings.baikal_password),
            timeout=HTTP_TIMEOUT,
            **client_kw,
        )
        url = calendar_url(settings.baikal_url, settings.baikal_user, cal)
        return cls(http, url, settings.child_names)

    def _abs(self, href: str) -> str:
        return urljoin(self.url, href)

    # listing

    def _report(self) -> list[tuple[str, str | None, str]]:
        resp = self.http.request(
            "REPORT",
            self.url,
            content=_REPORT_BODY.encode("utf-8"),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise CalDAVError(f"REPORT {self.url}: HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise CalDAVError(f"REPORT {self.url}: invalid XML: {exc}") from exc
        items: list[tuple[str, str | None, str]] = []
        for response in root.findall("D:response", _NS):
            href = response.findtext("D:href", default="", namespaces=_NS)
            for propstat in response.findall("D:propstat", _NS):
                status = propstat.findtext("D:status", default="", namespaces=_NS)
                if " 200 " not in f"{status} ":
                    continue
                data = propstat.findtext("D:prop/C:calendar-data", default="", namespaces=_NS)
                etag = propstat.findtext("D:prop/D:getetag", default=None, namespaces=_NS)
                if data:
                    items.append((href, etag or None, data))
        return items

    def list_own(self, scope_key: str | None) -> list[OwnEvent]:
        """Own events (``X-VONK-SCOPE`` == ``scope_key`` when given, else all own events).

        Foreign UIDs are skipped before parsing. The scope filter only sees the X-property
        copy; callers that need SQLite identity should list with None and filter themselves.
        """
        found: list[OwnEvent] = []
        for href, etag, raw in self._report():
            uid = _uid_of(raw)
            if not is_own_uid(uid):
                continue
            assert uid is not None
            own = parse_own(uid, etag, raw)
            if own is None:
                log.warning("own event %s could not be parsed; skipped", uid)
                continue
            self._etags[uid] = etag
            self._hrefs[uid] = href
            self._raw[uid] = raw
            if scope_key is None or own.event.scope_key == scope_key:
                found.append(own)
        return found

    # writing

    def _read_etag(self, url: str) -> str | None:
        resp = self.http.get(url)
        return resp.headers.get("ETag") if resp.status_code == 200 else None

    def _put(self, uid: str, ev: Event, *, create: bool) -> OpResult:
        if create:
            url = self._abs(quote(uid, safe="") + ".ics")
            headers = {"If-None-Match": "*"}
            sequence = 0
        else:
            url = self._abs(self._hrefs.get(uid, quote(uid, safe="") + ".ics"))
            etag = self._etags.get(uid)
            if etag is None:
                return OpResult(-1, False, None, None, "no known ETag; refusing blind overwrite",
                                uid)  # fmt: skip
            headers = {"If-Match": etag}
            sequence = _sequence_of(self._raw.get(uid, "")) + 1
        body = to_ics(uid, ev, self.child_names, sequence)
        headers["Content-Type"] = "text/calendar; charset=utf-8"
        resp = self.http.put(url, content=body.encode("utf-8"), headers=headers)
        if resp.status_code not in (200, 201, 204):
            return OpResult(-1, False, resp.status_code, None, f"PUT HTTP {resp.status_code}",
                            uid)  # fmt: skip
        new_etag = self._read_etag(url)
        self._etags[uid] = new_etag
        self._hrefs[uid] = urlsplit(url).path
        self._raw[uid] = body
        return OpResult(-1, True, resp.status_code, new_etag, None, uid)

    def _delete(self, uid: str) -> OpResult:
        etag = self._etags.get(uid)
        if uid not in self._hrefs or etag is None:
            return OpResult(-1, False, None, None, "unknown event or ETag; not deleted", uid)
        resp = self.http.delete(self._abs(self._hrefs[uid]), headers={"If-Match": etag})
        if resp.status_code in (200, 204, 404):
            # 404: already gone, which is what a removal wants.
            for m in (self._etags, self._hrefs, self._raw):
                m.pop(uid, None)
            return OpResult(-1, True, resp.status_code, None, None, uid)
        return OpResult(-1, False, resp.status_code, None, f"DELETE HTTP {resp.status_code}", uid)

    def apply(self, changeset: ChangeSet, selected_op_indexes: Sequence[int]) -> list[OpResult]:
        """Apply the selected ops in order; stop at the first failure (412 or otherwise)."""
        results: list[OpResult] = []
        stopped: str | None = None
        for index in selected_op_indexes:
            op = changeset.ops[index]
            if stopped is not None:
                results.append(OpResult(index, False, None, None, stopped, op.uid))
                continue
            kind = OpKind(op.kind)
            try:
                if kind is OpKind.ADD:
                    assert op.after is not None
                    r = self._put(mint_uid(), op.after, create=True)
                elif not is_own_uid(op.uid):
                    r = OpResult(-1, False, None, None, "not an own UID; refused", op.uid)
                elif kind is OpKind.UPDATE:
                    assert op.uid is not None and op.after is not None
                    r = self._put(op.uid, op.after, create=False)
                else:
                    assert op.uid is not None
                    r = self._delete(op.uid)
            except Exception as exc:  # one failed op must not lose the record of earlier ones
                log.exception("op %d failed", index)
                r = OpResult(-1, False, None, None, f"{type(exc).__name__}", op.uid)
            results.append(OpResult(index, r.ok, r.status, r.new_etag, r.error, r.uid))
            if not r.ok:
                stopped = f"not applied: stopped after op {index} failed ({r.error})"
        return results

    def purge_own(self) -> list[OpResult]:
        """Delete every own event of the collection (rollback). Foreign events untouched."""
        results = []
        for i, own in enumerate(self.list_own(None)):
            r = self._delete(own.uid)
            results.append(OpResult(i, r.ok, r.status, None, r.error, own.uid))
        return results
