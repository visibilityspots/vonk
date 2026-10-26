"""feed: per-tak ICS rendering, stable UIDs, the GitHub contents publisher, feed-only takken
in the service (stored, never proposed) and publishing after a poll report."""

from __future__ import annotations

import base64
import json
from datetime import date, time

import httpx
from icalendar import Calendar

from vonk.config import Settings
from vonk.feed import (
    FEED_UID_DOMAIN,
    GitHubFeedPublisher,
    build_feed,
    feed_title,
    feed_uid,
    render_calendar,
)
from vonk.models import Event, Tak, is_own_uid

from .conftest import ENV

URL = "https://scoutstervant.be/x.pdf"
STORED = 1_790_000_000.0
REPO = "visibilityspots/vonk-feed"


def ev(scope="welpen:2026-11", day=7, title="Een tegen allen", start=time(14), end=time(16, 30)):
    month = 11 if scope.endswith("-11") else 12
    return Event(scope, date(2026, month, day), None, start, end, start is None, title,
                 f"{title}\n{URL}", URL)  # fmt: skip


GROUP = Event("groep:2026-2027", date(2026, 12, 20), None, None, None, True, "Kerstfeestje",
              "", URL)  # fmt: skip


def events_of(ics: str) -> list:
    return list(Calendar.from_ical(ics).walk("VEVENT"))


# --- rendering ---------------------------------------------------------------------------


def test_titles_name_the_tak_never_a_child():
    assert feed_title(ev()) == "Scouts Welpen - Een tegen allen"
    assert feed_title(ev("jogis:2026-11")) == "Scouts Jogi's - Een tegen allen"
    assert feed_title(GROUP) == "Scouts - Kerstfeestje"


def test_tak_feed_holds_its_own_and_the_group_events_only():
    scopes = [
        ("welpen:2026-11", (ev(), ev(day=22, title="Spionnendag")), STORED),
        ("kabouters:2026-11", (ev("kabouters:2026-11", title="Bosspel"),), STORED),
        ("groep:2026-2027", (GROUP,), STORED),
    ]
    files = build_feed(scopes, (Tak.WELPEN, Tak.KABOUTERS, Tak.JINS), REPO)
    assert set(files) == {"welpen.ics", "kabouters.ics", "jins.ics", "groep.ics", "index.html"}
    welpen = [str(e["SUMMARY"]) for e in events_of(files["welpen.ics"])]
    assert welpen == [
        "Scouts Welpen - Een tegen allen",
        "Scouts Welpen - Spionnendag",
        "Scouts - Kerstfeestje",
    ]
    assert [str(e["SUMMARY"]) for e in events_of(files["jins.ics"])] == ["Scouts - Kerstfeestje"]
    assert [str(e["SUMMARY"]) for e in events_of(files["groep.ics"])] == ["Scouts - Kerstfeestje"]
    index = files["index.html"]
    assert 'href="webcal://visibilityspots.github.io/vonk-feed/welpen.ics"' in index
    assert ("https://calendar.google.com/calendar/render?cid=webcal%3A%2F%2F"
            "visibilityspots.github.io%2Fvonk-feed%2Fwelpen.ics") in index  # fmt: skip


def test_rendering_is_deterministic_and_uids_are_stable_and_not_own():
    items = [(ev(), STORED), (ev(day=22, title="Spionnendag"), STORED)]
    first, second = render_calendar("x", items), render_calendar("x", list(reversed(items)))
    assert first == second
    uids = [str(e["UID"]) for e in events_of(first)]
    assert uids[0] == feed_uid(ev())
    assert all(u.endswith(FEED_UID_DOMAIN) and not is_own_uid(u) for u in uids)
    # a changed title keeps the UID, so phones update the event instead of duplicating it
    renamed = render_calendar("x", [(ev(title="Iets anders"), STORED)])
    assert str(events_of(renamed)[0]["UID"]) == uids[0]


def test_events_sharing_date_and_start_get_distinct_uids():
    items = [(ev(title="A"), STORED), (ev(title="B"), STORED)]
    uids = {str(e["UID"]) for e in events_of(render_calendar("x", items))}
    assert len(uids) == 2


def test_timed_events_carry_a_vtimezone_and_all_day_events_dates():
    ics = render_calendar("x", [(ev(), STORED), (GROUP, STORED)])
    assert "BEGIN:VTIMEZONE" in ics and "TZID:Europe/Brussels" in ics
    timed, all_day = events_of(ics)
    assert timed["DTSTART"].dt.hour == 14
    assert all_day["DTSTART"].dt == date(2026, 12, 20)


# --- publisher ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}
        self.puts: list[dict] = []
        self.auth: set[str] = set()
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("Authorization", ""))
        if self.fail:
            return httpx.Response(503)
        path = request.url.path.removeprefix(f"/repos/{REPO}/contents/")
        if request.method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            sha, data = self.files[path]
            content = base64.b64encode(data).decode()
            return httpx.Response(200, json={"sha": sha, "content": content})
        body = json.loads(request.content)
        if path in self.files and body.get("sha") != self.files[path][0]:
            return httpx.Response(409)
        self.puts.append({"path": path, **body})
        self.files[path] = (f"sha{len(self.puts)}", base64.b64decode(body["content"]))
        return httpx.Response(201, json={})


def publisher(gh: FakeGitHub) -> GitHubFeedPublisher:
    client = httpx.Client(transport=httpx.MockTransport(gh.handler))
    return GitHubFeedPublisher(client, REPO, "gh-token", "main")


def test_publisher_creates_updates_and_skips_unchanged_files():
    gh = FakeGitHub()
    pub = publisher(gh)
    assert pub.publish({"a.ics": "one", "b.ics": "two"}) == ["a.ics", "b.ics"]
    assert pub.publish({"a.ics": "one", "b.ics": "two"}) == []
    assert pub.publish({"a.ics": "one", "b.ics": "three"}) == ["b.ics"]
    assert gh.files["b.ics"][1] == b"three"
    assert gh.puts[-1]["sha"] == "sha2" and gh.puts[-1]["branch"] == "main"
    assert gh.auth == {"Bearer gh-token"}


# --- config ------------------------------------------------------------------------------


def test_feed_settings():
    s = Settings.from_env({**ENV, "TAKKEN": "kabouters,jeevees", "FEED_TAKKEN": "all",
                           "VONK_FEED_REPO": REPO, "VONK_FEED_TOKEN": "gh-s3cret"})  # fmt: skip
    assert s.feed_enabled and Tak.GROEP not in s.feed_takken
    assert s.poll_takken[:2] == (Tak.KABOUTERS, Tak.JEEVEES)
    assert set(s.poll_takken) == set(Tak) - {Tak.GROEP}
    assert s.follows(Tak.KABOUTERS) and s.follows(Tak.GROEP) and not s.follows(Tak.WELPEN)
    assert "gh-s3cret" not in repr(s)
    service = Settings.from_env(ENV)  # no TAKKEN: the CHILD_NAMES keys are the own takken
    assert not service.feed_enabled
    assert service.follows(Tak.KABOUTERS) and not service.follows(Tak.WELPEN)
    bare = Settings.from_env({k: v for k, v in ENV.items() if k != "CHILD_NAMES"})
    assert bare.follows(Tak.WELPEN)  # neither: everything, as before the feed
    bad = Settings.from_env({**ENV, "FEED_TAKKEN": "welpen,bevers"})
    assert "FEED_TAKKEN has an unknown tak 'bevers'" in bad.errors


# --- service -----------------------------------------------------------------------------


def feed_world(make_world, gh: FakeGitHub):
    env = {**ENV, "FEED_TAKKEN": "all",  # own takken from CHILD_NAMES, like on the cluster
           "VONK_FEED_REPO": REPO, "VONK_FEED_TOKEN": "gh-token"}  # fmt: skip
    w = make_world(env)
    w.service.feed = publisher(gh)
    w.service.feed_in_background = False
    return w


def desired_body(events, post_id):
    return {
        "post": {"id": post_id, "modified": "2026-10-23T15:35:48", "title": "Vonk"},
        "pdf_sha256": "b" * 64,
        "prompt_version": "v1",
        "spans": [],
        "events": [e.to_dict() for e in events],
        "warnings": [],
    }


def test_feed_only_tak_is_stored_but_never_proposed(make_world):
    w = feed_world(make_world, FakeGitHub())
    resp = w.client.post("/api/scopes/welpen:2026-11/desired",
                         json=desired_body([ev()], 2734), headers=w.auth)  # fmt: skip
    assert resp.json()["feed_only"] is True and resp.json()["changeset_id"] is None
    assert w.gotify.messages == [] and w.store.pending_count() == 0
    assert w.store.get_desired("welpen:2026-11") is not None
    followed = w.client.post("/api/scopes/kabouters:2026-11/desired",
                             json=desired_body([ev("kabouters:2026-11")], 2735), headers=w.auth)
    assert followed.json()["changeset_id"]  # own tak still goes through approval


def test_poll_report_publishes_the_feed_and_survives_github_errors(make_world):
    gh = FakeGitHub()
    w = feed_world(make_world, gh)
    w.client.post("/api/scopes/welpen:2026-11/desired",
                  json=desired_body([ev()], 2734), headers=w.auth)  # fmt: skip
    assert w.client.post("/api/poll-report", json={"ok": True}, headers=w.auth).status_code == 200
    assert "welpen.ics" in gh.files and "index.html" in gh.files
    welpen = events_of(gh.files["welpen.ics"][1].decode())
    assert [str(e["SUMMARY"]) for e in welpen] == ["Scouts Welpen - Een tegen allen"]
    written = len(gh.puts)
    w.client.post("/api/poll-report", json={"ok": True}, headers=w.auth)
    assert len(gh.puts) == written  # nothing changed, nothing committed
    gh.fail = True
    assert w.client.post("/api/poll-report", json={"ok": True}, headers=w.auth).status_code == 200
    assert w.service.metrics.feed_errors._value.get() == 1
