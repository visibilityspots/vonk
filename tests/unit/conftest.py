"""Shared fakes for the service tests: an in-memory CalDAV server, a Gotify recorder and a
factory that wires store + sink + notifier + Starlette app together. Nothing leaves memory."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from icalendar import Calendar
from starlette.testclient import TestClient

from vonk.config import Settings
from vonk.main import build_service, create_app
from vonk.notify import GotifyNotifier
from vonk.sinks.caldav_sink import CalDAVSink
from vonk.store import Store

API_TOKEN = "api-s3cret"
COLLECTION = "/dav.php/calendars/vonk/vonk-test/"
BASE = "https://baikal.test"
ENV = {
    "BAIKAL_URL": f"{BASE}/dav.php/",
    "BAIKAL_USER": "vonk",
    "BAIKAL_PASSWORD": "pw",
    "BAIKAL_CALENDAR": "vonk-test",
    "GOTIFY_URL": "https://gotify.test",
    "GOTIFY_TOKEN": "gotify-token",
    "VONK_API_TOKEN": API_TOKEN,
    "VONK_PUBLIC_URL": "https://vonk.test",
    "CHILD_NAMES": json.dumps({"kabouters": "Anna", "jeevees": "Bert"}),
}


class FakeCalDAV:
    """Just enough sabre/Baikal: REPORT calendar-query, GET, PUT and DELETE with
    If-Match / If-None-Match preconditions. ``puts_return_etag`` mimics servers that omit
    the ETag on PUT (the sink must read it back)."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}  # href -> (etag, ics)
        self._etag = itertools.count(1)
        self.requests: list[httpx.Request] = []
        self.fail_next: dict[str, int] = {}  # method -> status to return once

    def new_etag(self) -> str:
        return f'"e{next(self._etag)}"'

    def seed(self, href: str, ics: str) -> str:
        etag = self.new_etag()
        self.items[COLLECTION + href] = (etag, ics)
        return etag

    def by_uid(self) -> dict[str, tuple[str, str, str]]:
        """uid -> (href, etag, ics)."""
        out = {}
        for href, (etag, ics) in self.items.items():
            for comp in Calendar.from_ical(ics).walk("VEVENT"):
                out[str(comp["UID"])] = (href, etag, ics)
        return out

    def edit(self, uid: str, change) -> str:
        """Simulate a phone edit: ``change(vevent)`` mutates it; a new ETag is assigned."""
        href, _, ics = self.by_uid()[uid]
        cal = Calendar.from_ical(ics)
        for comp in cal.walk("VEVENT"):
            change(comp)
        etag = self.new_etag()
        self.items[href] = (etag, cal.to_ical().decode())
        return etag

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        fail = self.fail_next.pop(request.method, None)
        if fail:
            return httpx.Response(fail)
        if request.method == "REPORT":
            parts = []
            for href, (etag, ics) in sorted(self.items.items()):
                data = ics.replace("&", "&amp;").replace("<", "&lt;")
                parts.append(
                    f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
                    f"<D:getetag>{etag}</D:getetag><C:calendar-data>{data}</C:calendar-data>"
                    "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
                )
            body = (
                '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" '
                'xmlns:C="urn:ietf:params:xml:ns:caldav">' + "".join(parts) + "</D:multistatus>"
            )
            return httpx.Response(207, content=body.encode())
        current = self.items.get(path)
        if request.method == "GET":
            if current is None:
                return httpx.Response(404)
            return httpx.Response(200, content=current[1].encode(), headers={"ETag": current[0]})
        if_match = request.headers.get("If-Match")
        if request.method == "PUT":
            if request.headers.get("If-None-Match") == "*" and current is not None:
                return httpx.Response(412)
            if if_match is not None and (current is None or current[0] != if_match):
                return httpx.Response(412)
            self.items[path] = (self.new_etag(), request.content.decode())
            return httpx.Response(201 if current is None else 204)
        if request.method == "DELETE":
            if current is None:
                return httpx.Response(404)
            if if_match is not None and current[0] != if_match:
                return httpx.Response(412)
            del self.items[path]
            return httpx.Response(204)
        return httpx.Response(405)


@dataclass
class Gotify:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Gotify-Key"] == "gotify-token"
        self.messages.append(json.loads(request.content))
        return httpx.Response(200, json={"id": len(self.messages)})

    def links(self) -> list[str]:
        out = []
        for m in self.messages:
            for line in m["message"].splitlines():
                if "https://vonk.test/c/" in line:
                    out.append(line.split("https://vonk.test")[1].strip())
        return out


class Clock:
    def __init__(self, now: float = 1_790_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    settings: Settings
    store: Store
    caldav: FakeCalDAV
    gotify: Gotify
    clock: Clock
    service: Any
    client: TestClient

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def make_world(tmp_path):
    clients: list[TestClient] = []

    def factory(env: dict[str, str] | None = None, *, with_sink: bool = True) -> World:
        settings = Settings.from_env(ENV if env is None else env)
        clock = Clock()
        store = Store(str(tmp_path / "vonk.db"), clock=clock)
        caldav = FakeCalDAV()
        gotify = Gotify()
        sink = None
        if with_sink:
            sink = CalDAVSink(
                httpx.Client(transport=httpx.MockTransport(caldav.handler)),
                BASE + COLLECTION,
                settings.child_names,
            )
        notifier = GotifyNotifier(
            httpx.Client(transport=httpx.MockTransport(gotify.handler)),
            "https://gotify.test",
            "gotify-token",
        )
        service = build_service(
            settings, store=store, sink=sink, notifier=notifier, clock=clock,
            use_default_sink=False,
        )  # fmt: skip
        client = TestClient(create_app(service))
        clients.append(client)
        return World(settings, store, caldav, gotify, clock, service, client)

    yield factory
    for c in clients:
        c.close()


@pytest.fixture
def world(make_world) -> World:
    return make_world()


@pytest.fixture
def fake_caldav() -> FakeCalDAV:
    return FakeCalDAV()


@pytest.fixture
def make_sink():
    def factory(caldav: FakeCalDAV, child_names=None) -> CalDAVSink:
        http = httpx.Client(transport=httpx.MockTransport(caldav.handler))
        return CalDAVSink(http, BASE + COLLECTION, child_names or {})

    return factory
