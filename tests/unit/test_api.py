"""api: bearer auth, wire shapes, desired -> ChangeSet, supersession, poll report, degraded
mode, orphans, the per-scope lock and an end-to-end poll -> approve run (AC 2, 8, 9, 12)."""

from __future__ import annotations

import json
import re
import threading
import time as _time
from datetime import date, time
from pathlib import Path

import httpx
import pytest

from vonk.config import Settings
from vonk.extract import finalize, parse_output
from vonk.models import Event, mint_uid
from vonk.pdf import PdfText
from vonk.poll import PollOptions, run_poll
from vonk.sinks.caldav_sink import to_ics
from vonk.store import LAST_COMPLETED_POLL, LAST_SUCCESSFUL_POLL

URL = "https://scoutstervant.be/x.pdf"
SCOPE = "kabouters:2026-10"
SHA = "a" * 64


def ev(day=3, title="Dropping", start=time(14), end=time(17), scope=SCOPE) -> Event:
    return Event(scope, date(2026, 10, day), None, start, end, False, title,
                 f"{title}\n{URL}", URL)  # fmt: skip


def body(events, *, post_id=2677, sha=SHA, modified="2026-10-05T20:09:39", warnings=()):
    return {
        "post": {"id": post_id, "modified": modified, "title": "Vonk oktober"},
        "pdf_sha256": sha,
        "prompt_version": "v1",
        "spans": [{"date_span": "3/10", "title_span": "Dropping"}],
        "events": [e.to_dict() for e in events],
        "warnings": list(warnings),
    }


def post_desired(w, events, scope=SCOPE, **kw):
    return w.client.post(f"/api/scopes/{scope}/desired", json=body(events, **kw), headers=w.auth)


# --- auth --------------------------------------------------------------------------------

ROUTES = [
    ("GET", "/api/seen", None),
    ("GET", f"/api/cache/{SHA}/v1", None),
    ("POST", f"/api/scopes/{SCOPE}/desired", body([ev()])),
    ("POST", "/api/poll-report", {"ok": True, "errors": [], "warnings": ["w"]}),
]


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic api-s3cret", "Bearer "])
@pytest.mark.parametrize(("method", "path", "payload"), ROUTES)
def test_bad_or_missing_bearer_is_401_without_side_effects(world, header, method, path, payload):
    headers = {"Authorization": header} if header else {}
    resp = world.client.request(method, path, json=payload, headers=headers)
    assert resp.status_code == 401
    assert world.store.seen_posts() == []
    assert world.store.get_cache(SHA, "v1") is None
    assert world.store.get_desired(SCOPE) is None
    assert world.store.get_meta(LAST_COMPLETED_POLL) is None
    assert world.store.pending_count() == 0
    assert world.gotify.messages == []
    assert world.caldav.requests == []


# --- desired -----------------------------------------------------------------------------


def test_desired_announces_changeset_and_stores_seen_and_cache(world):
    resp = post_desired(world, [ev(3), ev(10, "Bosspel")], warnings=["regex telt 3"])
    assert resp.status_code == 200
    answer = resp.json()
    assert answer["ops"] == 2 and answer["changeset_id"] and answer["orphans"] == 0
    (msg,) = world.gotify.messages
    assert "2 nieuw" in msg["message"] and "regex telt 3" in msg["message"]
    (link,) = world.gotify.links()
    assert re.fullmatch(r"/c/[A-Za-z0-9_-]{43}", link)
    seen = world.client.get("/api/seen", headers=world.auth).json()
    assert seen == {"posts": [{"post_id": 2677, "modified": "2026-10-05T20:09:39",
                               "pdf_sha256": SHA}]}  # fmt: skip
    cache = world.client.get(f"/api/cache/{SHA}/v1", headers=world.auth)
    assert cache.json() == {"spans": body([])["spans"]}
    assert world.client.get(f"/api/cache/{SHA}/v2", headers=world.auth).status_code == 404
    assert world.caldav.items == {}  # nothing written before approval


def test_same_desired_twice_is_not_announced_twice(world):
    post_desired(world, [ev()])
    again = post_desired(world, [ev()]).json()
    assert again["unchanged"] is True
    assert len(world.gotify.messages) == 1


def test_newer_desired_supersedes_the_older_token(world):
    post_desired(world, [ev()])
    post_desired(world, [ev(), ev(10, "Bosspel")], sha="b" * 64)
    old, new = world.gotify.links()
    assert world.client.get(old).status_code == 410
    assert world.client.get(new).status_code == 200
    assert world.store.pending_count() == 1


def test_empty_diff_is_not_announced_and_supersedes(world):
    post_desired(world, [ev()])
    (link,) = world.gotify.links()
    answer = post_desired(world, [], sha="b" * 64).json()
    assert answer == {"changeset_id": None, "ops": 0, "orphans": 0}
    assert len(world.gotify.messages) == 1
    assert world.client.get(link).status_code == 410


def test_desired_validation(world):
    bad_scope = world.client.post("/api/scopes/nope/desired", json=body([ev()]),
                                  headers=world.auth)  # fmt: skip
    assert bad_scope.status_code == 400
    other = post_desired(world, [ev(scope="jeevees:2026-10")])
    assert other.status_code == 400
    garbage = world.client.post(f"/api/scopes/{SCOPE}/desired", content=b"{",
                                headers=world.auth)  # fmt: skip
    assert garbage.status_code == 400
    assert world.store.get_desired(SCOPE) is None


def test_calendar_down_gives_502_and_post_is_not_marked_seen(world):
    world.caldav.fail_next["REPORT"] = 503
    assert post_desired(world, [ev()]).status_code == 502
    assert world.store.seen_posts() == []


def test_new_post_moves_last_new_vonk(world):
    world.clock.now += 100
    post_desired(world, [ev()])
    text = world.client.get("/metrics").text
    m = re.search(r"^vonk_last_new_vonk_timestamp (\S+)$", text, re.M)
    assert float(m.group(1)) == world.clock.now
    world.clock.now += 100
    post_desired(world, [ev(), ev(10, "Bosspel")], sha="b" * 64)  # same post, new version
    assert world.store.get_meta("last_new_vonk") == world.clock.now - 100


def test_orphans_go_to_their_own_changeset(world):
    uid = mint_uid()
    orphan_ics = to_ics(uid, ev(5, "Oud"), {})
    world.caldav.seed("orphan.ics", orphan_ics)

    def strip(v):
        del v["X-VONK-SCOPE"]
        del v["X-VONK-DATE"]

    world.caldav.edit(uid, strip)
    answer = post_desired(world, [ev()]).json()
    assert answer["ops"] == 1 and answer["orphans"] == 1
    scope_cs, orphan_cs = (
        world.store.get_changeset(t.changeset_id)[0]
        for t in (world.store.get_token(link.split("/")[-1]) for link in world.gotify.links())
    )
    assert scope_cs.scope_key == SCOPE and all(op.uid != uid for op in scope_cs.ops)
    assert orphan_cs.scope_key == "orphans" and [op.uid for op in orphan_cs.ops] == [uid]
    # a second post for another scope does not repeat the orphan notice
    post_desired(world, [ev(scope="jeevees:2026-10")], scope="jeevees:2026-10", post_id=2647)
    assert sum("orphans" in m["title"] for m in world.gotify.messages) == 1


def test_adopts_events_from_x_properties_with_empty_store(world):
    """DB lost: own events with X-properties are adopted, not re-added (0 adds)."""
    for i, e in enumerate([ev(3), ev(10, "Bosspel")]):
        world.caldav.seed(f"{i}.ics", to_ics(mint_uid(), e, world.settings.child_names))
    answer = post_desired(world, [ev(3), ev(10, "Bosspel")]).json()
    assert answer["ops"] == 0 and world.gotify.messages == []


# --- poll report -------------------------------------------------------------------------


def test_poll_report_sets_heartbeats_and_notifies_new_warnings_once(world):
    world.clock.now = 2_000_000_000.0
    report = {"started": "x", "finished": "y", "ok": True, "errors": [],
              "warnings": ["post 12: geen PDF-link", "post 13: onbekende tak"]}  # fmt: skip
    assert world.client.post("/api/poll-report", json=report, headers=world.auth).json() == {}
    assert world.store.get_meta(LAST_SUCCESSFUL_POLL) == 2_000_000_000.0
    text = world.client.get("/metrics").text
    assert "vonk_last_completed_poll_timestamp 2e+09" in text
    assert "vonk_last_successful_poll_timestamp 2e+09" in text
    (msg,) = world.gotify.messages
    assert "geen PDF-link" in msg["message"] and "onbekende tak" in msg["message"]
    world.client.post("/api/poll-report", json=report, headers=world.auth)
    assert len(world.gotify.messages) == 1


def test_failed_poll_report_only_moves_completed(world):
    world.clock.now = 2_000_000_000.0
    report = {"ok": False, "errors": ["boom"], "warnings": []}
    world.client.post("/api/poll-report", json=report, headers=world.auth)
    assert world.store.get_meta(LAST_COMPLETED_POLL) == 2_000_000_000.0
    assert world.store.get_meta(LAST_SUCCESSFUL_POLL) is None
    bad = world.client.post("/api/poll-report", json={"ok": "yes"}, headers=world.auth)
    assert bad.status_code == 400


# --- degraded ----------------------------------------------------------------------------


def test_degraded_mode(make_world):
    env = {"VONK_API_TOKEN": "api-s3cret", "BAIKAL_PASSWORD": "<no value>"}
    w = make_world(env, with_sink=False)
    assert w.client.get("/health").status_code == 200
    assert w.client.get("/health").json()["degraded"] is True
    assert w.client.get("/api/seen", headers=w.auth).status_code == 503
    assert post_desired(w, [ev()]).status_code == 503
    assert w.client.get("/api/seen").status_code == 401
    ok = w.client.post("/api/poll-report", json={"ok": True}, headers=w.auth)
    assert ok.status_code == 200
    assert w.client.post("/api/poll-report", json={"ok": True}).status_code == 401


def test_degraded_without_api_token_is_503(make_world):
    w = make_world({}, with_sink=False)
    assert w.client.get("/health").status_code == 200
    assert w.client.post("/api/poll-report", json={"ok": True}, headers=w.auth).status_code == 503


# --- per-scope lock ----------------------------------------------------------------------


def test_desired_waits_for_the_scope_lock(world):
    done = threading.Event()

    def submit():
        world.service.submit_desired(SCOPE, body([ev()]))
        done.set()

    with world.service.lock(SCOPE):
        t = threading.Thread(target=submit)
        t.start()
        _time.sleep(0.2)
        assert not done.is_set()
        assert world.store.get_desired(SCOPE) is None
    t.join(5)
    assert done.is_set()
    # another scope is not blocked by this one
    with world.service.lock(SCOPE):
        world.service.submit_desired("jeevees:2026-10",
                                     body([ev(scope="jeevees:2026-10")], post_id=1))  # fmt: skip


# --- end to end: poll job -> service -> approve -> calendar (AC 2) -----------------------

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCES = json.loads((FIXTURES / "sources.json").read_text("utf-8"))["fixtures"]
POSTS = json.loads((FIXTURES / "wp" / "posts_search_vonk.json").read_text("utf-8"))
POLL_ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "VONK_API_TOKEN": "api-s3cret",
    "VONK_API_URL": "http://vonk.test",
    "TAKKEN": "kabouters,jeevees",
}


def _fetch(url: str) -> PdfText:
    src = next(s for s in SOURCES if s["url"] == url)
    return PdfText(sha256=src["sha256"], text=(FIXTURES / src["text"]).read_text("utf-8"))


def _extract(text: str, kind: str):
    src = next(s for s in SOURCES if (FIXTURES / s["text"]).read_text("utf-8") == text)
    raw = (FIXTURES / "llm" / f"{src['name']}.json").read_text("utf-8")
    return finalize(parse_output(raw), text, kind)


def _poll(w, options=None) -> int:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wp.test":
            return httpx.Response(200, json=POSTS, headers={"X-WP-TotalPages": "1"})
        resp = w.client.request(request.method, request.url.raw_path.decode(),
                                content=request.content, headers=dict(request.headers))
        return httpx.Response(resp.status_code, content=resp.content,
                              headers={"Content-Type": resp.headers.get("content-type", "")})
    # fmt: skip
    return run_poll(
        Settings.from_env(POLL_ENV),
        options or PollOptions(),
        transport=httpx.MockTransport(handler),
        fetch_pdf=_fetch,
        extractor=_extract,
        wp_url="https://wp.test/wp-json/wp/v2",
        allowed_hosts=frozenset({"wp.test"}),
    )


def _approve_all_announced(w) -> None:
    for link in w.gotify.links():
        page = w.client.get(link)
        if page.status_code != 200:
            continue
        h = re.search(r'name="changeset_hash" value="([0-9a-f]{64})"', page.text).group(1)
        ticked = re.findall(r'name="op" value="(\d+)" id="op\d+"\s+checked', page.text)
        resp = w.client.post(link, data={"changeset_hash": h, "op": ticked})
        assert resp.status_code == 200, resp.text


def test_end_to_end_poll_approve_then_second_poll_is_empty(world):
    assert _poll(world) == 0
    scopes = {m["title"].rsplit(" ", 1)[-1] for m in world.gotify.messages}
    assert {"groep:2026-2027", "jeevees:2026-10", "kabouters:2026-10"} <= scopes
    _approve_all_announced(world)
    own = world.caldav.by_uid()
    assert len(own) >= 5
    kab = [ics for _, _, ics in own.values() if "X-VONK-SCOPE:kabouters:2026-10" in ics]
    assert len(kab) == 5 and all("SUMMARY:Anna scouts: " in ics for ics in kab)
    assert len(world.store.identity_map()) == len(own)
    assert world.client.get("/metrics").text.count("vonk_pending_changesets 0.0") == 1

    # simulated phone edit that drops the X-properties: still matched through SQLite
    uid = next(iter(own))

    def strip(v):
        del v["X-VONK-SCOPE"]
        del v["X-VONK-DATE"]

    world.caldav.edit(uid, strip)
    before = dict(world.caldav.items)
    notices = len(world.gotify.messages)

    assert _poll(world) == 0  # unchanged posts: skipped as seen
    assert _poll(world, PollOptions(reextract=True)) == 0  # AC 2: forced, cache reused
    assert len(world.gotify.messages) == notices  # 0 adds, 0 updates, 0 removes
    assert world.caldav.items == before
    assert world.store.pending_count() == 0


def test_cli_registers_service_commands_next_to_poll(capsys):
    from vonk import cli

    parser, _ = cli.build_parser()
    for argv in (["poll"], ["serve"], ["dry-run"], ["purge-own", "--calendar", "vonk-test"],
                 ["clear-attention", SCOPE]):  # fmt: skip
        assert callable(parser.parse_args(argv).func)
    with pytest.raises(SystemExit):
        parser.parse_args(["purge-own"])  # --calendar is mandatory


def test_cli_purge_own_and_dry_run_refuse_without_settings(monkeypatch, tmp_path, capsys):
    from vonk import cli

    for name in ("BAIKAL_URL", "BAIKAL_USER", "BAIKAL_PASSWORD", "BAIKAL_CALENDAR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VONK_DB_PATH", str(tmp_path / "none.db"))
    assert cli.main(["purge-own", "--calendar", "vonk-test"]) == 2
    assert cli.main(["dry-run"]) == 2
    assert not (tmp_path / "none.db").exists()
