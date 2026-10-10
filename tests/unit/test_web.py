"""web: approve page and the token lifecycle (AC 6), conflicts and the 409 cap (AC 10),
hand-edits, grouping, /health."""

from __future__ import annotations

import re
import threading
from dataclasses import replace
from datetime import date, time

import pytest

from vonk import cli
from vonk.models import Event, Tak
from vonk.store import TOKEN_TTL_SECONDS

URL = "https://scoutstervant.be/x.pdf"
SCOPE = "kabouters:2026-10"


def ev(day=3, title="Dropping", start=time(14), end=time(17), scope=SCOPE) -> Event:
    return Event(scope, date(2026, 10, day), None, start, end, False, title,
                 f"{title}\n{URL}", URL)  # fmt: skip


def desire(w, events, *, sha="a" * 64, scope=SCOPE, post_id=2677):
    body = {
        "post": {"id": post_id, "modified": "2026-10-05T20:09:39"},
        "pdf_sha256": sha,
        "prompt_version": "v1",
        "spans": [],
        "events": [e.to_dict() for e in events],
        "warnings": [],
    }
    resp = w.client.post(f"/api/scopes/{scope}/desired", json=body, headers=w.auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def form(page: str, *, all_ops=False) -> dict:
    h = re.search(r'name="changeset_hash" value="([0-9a-f]{64})"', page).group(1)
    pattern = r'name="op" value="(\d+)" id="op\d+"\s+' + ("" if all_ops else "checked")
    return {"changeset_hash": h, "op": re.findall(pattern, page)}


def last_link(w) -> str:
    return w.gotify.links()[-1]


def approve(w, link=None, **kw):
    link = link or last_link(w)
    return w.client.post(link, data=form(w.client.get(link).text, **kw))


def baseline(w, events=None):
    desire(w, events or [ev(3), ev(10, "Bosspel"), ev(17, "Zwemmen")])
    assert approve(w).status_code == 200


# --- token lifecycle ---------------------------------------------------------------------


def test_get_renders_without_side_effects(world):
    baseline(world)
    desire(world, [ev(3, start=time(15)), ev(10, "Bosspel"), ev(17, "Zwemmen")], sha="b" * 64)
    link = last_link(world)
    etags = {u: e for u, (_, e, _) in world.caldav.by_uid().items()}
    writes = [r for r in world.caldav.requests if r.method in ("PUT", "DELETE")]
    for _ in range(2):
        page = world.client.get(link)
        assert page.status_code == 200
        assert "Gewijzigd" in page.text and "Anna scouts: Dropping" in page.text
    assert {u: e for u, (_, e, _) in world.caldav.by_uid().items()} == etags
    assert [r for r in world.caldav.requests if r.method in ("PUT", "DELETE")] == writes
    assert world.store.get_token(link.split("/")[-1]).used_at is None


def test_first_post_applies_second_is_410(world):
    desire(world, [ev(3), ev(10, "Bosspel")])
    link = last_link(world)
    data = form(world.client.get(link).text)
    first = world.client.post(link, data=data)
    assert first.status_code == 200
    assert len(world.caldav.items) == 2
    assert world.gotify.messages[-1]["title"].startswith("vonk: toegepast")
    second = world.client.post(link, data=data)
    assert second.status_code == 410
    assert len(world.caldav.items) == 2
    assert world.client.get(link).status_code == 410


def test_unknown_token_is_404(world):
    assert world.client.get("/c/doesnotexist").status_code == 404
    assert world.client.post("/c/doesnotexist", data={"changeset_hash": "x"}).status_code == 404


def test_expired_token_is_410(world):
    desire(world, [ev()])
    link = last_link(world)
    data = form(world.client.get(link).text)
    world.clock.now += TOKEN_TTL_SECONDS + 1
    assert world.client.get(link).status_code == 410
    assert world.client.post(link, data=data).status_code == 410
    assert world.caldav.items == {}


def test_superseded_token_is_410(world):
    desire(world, [ev()])
    old = last_link(world)
    data = form(world.client.get(old).text)
    desire(world, [ev(), ev(10, "Bosspel")], sha="b" * 64)
    assert world.client.post(old, data=data).status_code == 410
    assert world.caldav.items == {}


def test_wrong_changeset_hash_is_409_and_keeps_the_token(world):
    desire(world, [ev()])
    link = last_link(world)
    resp = world.client.post(link, data={"changeset_hash": "0" * 64, "op": ["0"]})
    assert resp.status_code == 409
    assert world.caldav.items == {}
    assert approve(world, link).status_code == 200


def test_oversized_body_is_413_and_keeps_the_token(world):
    desire(world, [ev()])
    link = last_link(world)
    h = form(world.client.get(link).text)["changeset_hash"]
    resp = world.client.post(link, data={"changeset_hash": h, "op": ["0"] * 20000})
    assert resp.status_code == 413
    assert world.caldav.items == {}
    assert world.store.get_token(link.split("/")[-1]).used_at is None
    assert approve(world, link).status_code == 200


def test_invalid_selection_is_400(world):
    desire(world, [ev()])
    link = last_link(world)
    h = form(world.client.get(link).text)["changeset_hash"]
    assert world.client.post(link, data={"changeset_hash": h, "op": ["7"]}).status_code == 400
    assert world.client.post(link, data={"changeset_hash": h, "op": ["x"]}).status_code == 400
    assert approve(world, link).status_code == 200


def test_concurrent_double_post_applies_once(world):
    desire(world, [ev(3), ev(10, "Bosspel")])
    link = last_link(world)
    token = link.split("/")[-1]
    h = form(world.client.get(link).text)["changeset_hash"]
    statuses: list[int] = []
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        statuses.append(world.service.approve(token, h, [0, 1]).status)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 410, 410, 410]
    assert len(world.caldav.items) == 2


# --- changed live state ------------------------------------------------------------------


def _phone_edit(w, title="Bowling"):
    uid = next(u for u, (_, _, ics) in w.caldav.by_uid().items() if "Dropping" in ics)
    w.caldav.edit(uid, lambda v: v.__setitem__("SUMMARY", f"Anna scouts: {title}"))
    return uid


def test_changed_live_state_is_409_writes_nothing_and_reannounces(world):
    baseline(world)
    desire(world, [ev(3), ev(10, "Bosspel")], sha="b" * 64)  # Zwemmen removed
    link = last_link(world)
    data = form(world.client.get(link).text)
    _phone_edit(world)
    items = dict(world.caldav.items)
    resp = world.client.post(link, data=data)
    assert resp.status_code == 409
    assert world.caldav.items == items
    assert last_link(world) != link  # a new ChangeSet was announced
    assert world.client.get(link).status_code == 410
    assert world.client.get(last_link(world)).status_code == 200


def test_two_conflicts_set_needs_attention_and_stop_announcing(world):
    baseline(world)
    desire(world, [ev(3), ev(10, "Bosspel")], sha="b" * 64)
    _phone_edit(world, "Bowling")
    assert approve(world).status_code == 409  # conflict 1 -> new notice
    link2 = last_link(world)
    data2 = form(world.client.get(link2).text)
    _phone_edit(world, "Kegelen")
    notices = len(world.gotify.messages)
    assert world.client.post(link2, data=data2).status_code == 409  # conflict 2 -> cap
    new_msgs = world.gotify.messages[notices:]
    assert [m["title"] for m in new_msgs] == [f"vonk: {SCOPE} vraagt aandacht"]
    assert world.gotify.links()[-1] == link2  # no third ChangeSet notice
    assert world.store.scopes_needing_attention() == [SCOPE]
    assert "vonk_scopes_needing_attention 1.0" in world.client.get("/metrics").text
    # same PDF re-posted: still quiet
    desire(world, [ev(3), ev(10, "Bosspel")], sha="b" * 64)
    assert len(world.gotify.messages) == notices + 1
    # a new PDF sha clears it and announces again
    desire(world, [ev(3), ev(10, "Bosspel"), ev(24, "Fuif")], sha="c" * 64)
    assert world.store.scopes_needing_attention() == []
    assert world.gotify.links()[-1] != link2
    assert "vonk_scopes_needing_attention 0.0" in world.client.get("/metrics").text


def test_clear_attention_cli_forgets_seen_posts(world, monkeypatch, capsys):
    desire(world, [ev()])
    world.store.record_conflict(SCOPE, cap=2)
    world.store.record_conflict(SCOPE, cap=2)
    monkeypatch.setenv("VONK_DB_PATH", world.store.path)
    assert cli.main(["clear-attention", SCOPE]) == 0
    assert "cleared" in capsys.readouterr().out
    assert world.store.scopes_needing_attention() == []
    assert world.store.seen_posts() == []  # next poll re-posts and announces


def test_412_mid_apply_stops_and_creates_new_changeset(world):
    baseline(world)
    desire(world, [ev(3, start=time(15)), ev(10, "Bosspel", start=time(15)), ev(17, "Zwemmen")],
           sha="b" * 64)  # fmt: skip
    link = last_link(world)
    data = form(world.client.get(link).text)
    world.caldav.fail_next["PUT"] = 412
    resp = world.client.post(link, data=data)
    assert resp.status_code == 409
    assert "412" in resp.text and "not applied" in resp.text
    assert world.store.scope_state(SCOPE).conflict_count == 1
    assert last_link(world) != link
    assert "vonk_apply_errors_total 1.0" in world.client.get("/metrics").text


def test_successful_apply_resets_the_conflict_counter(world):
    baseline(world)
    desire(world, [ev(3), ev(10, "Bosspel")], sha="b" * 64)
    _phone_edit(world)
    assert approve(world).status_code == 409
    assert approve(world).status_code == 200
    assert world.store.scope_state(SCOPE).conflict_count == 0


# --- hand-edits and grouping -------------------------------------------------------------


def test_hand_edited_event_is_unticked_and_kept(world):
    baseline(world)
    uid = next(u for u, (_, _, ics) in world.caldav.by_uid().items() if "Dropping" in ics)
    world.caldav.edit(uid, lambda v: v.__setitem__("DESCRIPTION", "Bowling, zelf aangepast"))
    desire(world, [ev(3, start=time(15)), ev(10, "Bosspel"), ev(24, "Fuif")], sha="b" * 64)
    page = world.client.get(last_link(world)).text
    assert "Met de hand aangepast" in page
    assert approve(world).status_code == 200
    ics = world.caldav.by_uid()[uid][2]
    assert "zelf aangepast" in ics and "T140000" in ics  # Jan's version kept
    assert sum("Fuif" in ics for _, _, ics in world.caldav.by_uid().values()) == 1
    # ticking it explicitly overwrites
    desire(world, [ev(3, start=time(15)), ev(10, "Bosspel"), ev(24, "Fuif")], sha="c" * 64)
    assert approve(world, all_ops=True).status_code == 200
    assert "T150000" in world.caldav.by_uid()[uid][2]


@pytest.mark.parametrize(
    "prop, value",
    [("DESCRIPTION", "Bowling, zelf aangepast"), ("SUMMARY", "Anna scouts: Bowling")],
)
def test_moved_hand_edited_default_approve_creates_no_duplicate(world, prop, value):
    baseline(world)
    uid = next(u for u, (_, _, ics) in world.caldav.by_uid().items() if "Dropping" in ics)
    world.caldav.edit(uid, lambda v: v.__setitem__(prop, value))
    edited_ics = world.caldav.by_uid()[uid][2]
    desire(world, [ev(4), ev(10, "Bosspel"), ev(17, "Zwemmen")], sha="b" * 64)  # moved 3 -> 4
    page = world.client.get(last_link(world)).text
    assert "Met de hand aangepast (2)" in page
    assert approve(world).status_code == 200
    assert len(world.caldav.items) == 3  # no second Dropping
    assert world.caldav.by_uid()[uid][2] == edited_ics  # Jan's version kept


def test_mass_removal_banner(world):
    baseline(world)
    desire(world, [ev(3)], sha="b" * 64)
    page = world.client.get(last_link(world)).text
    assert "LET OP: deze wijzigingen verwijderen meer dan de helft" in page
    assert "massale verwijdering" in world.gotify.messages[-1]["message"]


def test_moved_group(world):
    baseline(world)
    desire(world, [ev(4), ev(10, "Bosspel"), ev(17, "Zwemmen")], sha="b" * 64)
    page = world.client.get(last_link(world)).text
    assert "Verplaatst (2)" in page


def test_child_name_change_is_rename_only(world):
    baseline(world, [ev(3)])
    names = {Tak.KABOUTERS: "Annelies"}
    world.service.settings = replace(world.settings, child_names=names)
    world.service.sink.child_names = names
    desire(world, [ev(3)], sha="b" * 64)
    page = world.client.get(last_link(world)).text
    assert "Enkel nieuwe titel (1)" in page and "Annelies scouts: Dropping" in page
    assert approve(world).status_code == 200
    (ics,) = [ics for _, _, ics in world.caldav.by_uid().values()]
    assert "SUMMARY:Annelies scouts: Dropping" in ics


def test_possible_conflict_shown_side_by_side(world):
    desire(world, [ev(17, "Trofee", scope="groep:2026-2027")], scope="groep:2026-2027",
           post_id=1)  # fmt: skip
    desire(world, [ev(18, "Trofee")])
    page = world.client.get(last_link(world)).text
    assert "Mogelijk conflict (groep:2026-2027)" in page


def test_health_is_200_and_makes_no_external_calls(world):
    resp = world.client.get("/health")
    assert resp.status_code == 200 and resp.json()["db"] is True
    assert world.caldav.requests == [] and world.gotify.messages == []
