"""notify: Gotify transport and message content (incl. mass-removal warning, AC 8)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

import httpx

from vonk.models import ChangeSet, Event, Operation, OpFlag, OpKind, OpResult, Tak, mint_uid
from vonk.notify import (
    PRIORITY_HIGH,
    PRIORITY_INFO,
    GotifyNotifier,
    NullNotifier,
    changeset_message,
    redact,
    result_message,
    warnings_message,
)

URL = "https://scoutstervant.be/x.pdf"


def ev(day=3, title="Dropping") -> Event:
    return Event("kabouters:2026-10", date(2026, 10, day), None, time(14), time(17), False,
                 title, URL, URL)  # fmt: skip


def cs(*ops, warnings=()) -> ChangeSet:
    return ChangeSet("cs", "kabouters:2026-10", ops, warnings, datetime.now(UTC))


def test_gotify_posts_message_with_app_token():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    n = GotifyNotifier(httpx.Client(transport=httpx.MockTransport(handler)), "https://g.test/",
                       "tok")  # fmt: skip
    assert n.send("t", "m", 7) is True
    (req,) = seen
    assert str(req.url) == "https://g.test/message"
    assert req.headers["X-Gotify-Key"] == "tok"
    body = json.loads(req.content)
    assert body["title"] == "t" and body["message"] == "m" and body["priority"] == 7


def test_gotify_failures_are_swallowed():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    n = GotifyNotifier(httpx.Client(transport=httpx.MockTransport(down)), "https://g", "t")
    assert n.send("t", "m") is False
    bad = GotifyNotifier(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), "https://g", "t"
    )
    assert bad.send("t", "m") is False
    assert NullNotifier().send("t", "m") is False


def test_changeset_message_has_summary_link_and_warnings():
    title, msg, prio = changeset_message(
        cs(Operation(OpKind.ADD, None, None, ev()), warnings=("cs warning",)),
        "https://vonk.test/c/TOKEN",
        ["onbekende tak"],
    )
    assert "kabouters:2026-10" in title
    assert "1 nieuw, 0 gewijzigd, 0 verwijderd" in msg
    assert "https://vonk.test/c/TOKEN" in msg
    assert "cs warning" in msg and "onbekende tak" in msg
    assert "massale" not in msg and prio == PRIORITY_INFO


def test_mass_removal_is_prominent():
    uid = mint_uid()
    op = Operation(OpKind.REMOVE, uid, ev(), None, (OpFlag.MASS_REMOVAL,))
    _, msg, prio = changeset_message(cs(op), "https://vonk.test/c/x")
    assert msg.startswith("LET OP: massale verwijdering")
    assert "**" not in msg
    assert prio == PRIORITY_HIGH


def test_result_and_warning_messages():
    c = cs(Operation(OpKind.ADD, None, None, ev()), Operation(OpKind.ADD, None, None, ev(10)))
    _, msg, prio = result_message(c, [OpResult(0, True, 201, '"e"', None),
                                      OpResult(1, False, 412, None, "PUT HTTP 412")])  # fmt: skip
    assert "1 van 2" in msg and "PUT HTTP 412" in msg and prio == PRIORITY_HIGH
    _, msg, _ = warnings_message(["post 12 zonder PDF-link"])
    assert "post 12 zonder PDF-link" in msg


# --- plain text and child-name redaction (L4, L7) -------------------------------------------


def _recording_notifier(child_names=None):
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GotifyNotifier(client, "https://g.test", "tok", child_names), seen


def test_gotify_messages_are_plain_text_so_pdf_markdown_is_inert():
    n, seen = _recording_notifier()
    hostile = "Dropping [klik](https://evil.example) ![x](https://evil.example/t.png)"
    title, msg, prio = changeset_message(
        cs(Operation(OpKind.ADD, None, None, ev(title=hostile))), "https://vonk.test/c/x"
    )
    assert n.send(title, msg, prio)
    (body,) = seen
    assert body["extras"] == {"client::display": {"contentType": "text/plain"}}
    assert hostile in body["message"]  # shown literally, never rendered as a link/image


def test_child_names_are_redacted_to_the_tak_case_insensitively():
    n, seen = _recording_notifier({Tak.KABOUTERS: "Anna", Tak.JEEVEES: "Bert"})
    n.send("vonk: anna", "- add 2026-10-03 ANNA scouts: Dropping\n- ⚠ Bert mist de PDF")
    (body,) = seen
    text = body["title"] + body["message"]
    assert "anna" not in text.lower() and "bert" not in text.lower()
    assert body["title"] == "vonk: kabouters"
    assert "kabouters scouts: Dropping" in body["message"]
    assert "jeevees mist de PDF" in body["message"]


def test_redact_without_names_is_identity():
    assert redact("Anna scouts", {}) == "Anna scouts"


def test_notifier_built_by_the_app_factory_redacts_child_names(tmp_path):
    from starlette.testclient import TestClient

    from vonk.config import Settings
    from vonk.main import build_service, create_app
    from vonk.store import Store

    settings = Settings.from_env({
        "GOTIFY_URL": "https://gotify.test",
        "GOTIFY_TOKEN": "gotify-token",
        "VONK_API_TOKEN": "api-token",
        "CHILD_NAMES": json.dumps({"kabouters": "Anna"}),
    })  # fmt: skip
    service = build_service(settings, store=Store(str(tmp_path / "v.db")), use_default_sink=False)
    assert isinstance(service.notifier, GotifyNotifier)
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    service.notifier.http = httpx.Client(transport=httpx.MockTransport(handler))
    with TestClient(create_app(service)) as client:
        resp = client.post(
            "/api/poll-report",
            json={"ok": True, "warnings": ["post 9 'Vonk van ANNA': no PDF link"]},
            headers={"Authorization": "Bearer api-token"},
        )
    assert resp.status_code < 300
    (body,) = seen
    assert "anna" not in body["message"].lower()
    assert "Vonk van kabouters" in body["message"]


def test_redact_leaves_links_byte_for_byte_and_does_not_cascade():
    names = {Tak.KABOUTERS: "Ella", Tak.WELPEN: "Bo"}
    link = "https://vonk.test/c/xBoQ9ellaZ-Bo_ella"
    text = f"Bo en ELLA: Bosspel\nNakijken en goedkeuren: {link}"
    out = redact(text, names)
    assert out == f"welpen en kabouters: Bosspel\nNakijken en goedkeuren: {link}"
    assert "kawelpen" not in out  # "Bo" is not matched inside the replacement "kabouters"


def test_redact_only_whole_words():
    assert redact("Bosspel met Bo, bo! Bob", {Tak.WELPEN: "Bo"}) == (
        "Bosspel met welpen, welpen! Bob"
    )


def test_redact_regex_special_names():
    names = {Tak.KABOUTERS: "A.B", Tak.WELPEN: "Jo-(Ann)"}
    assert redact("A.B en jo-(ann) maar niet AxB", names) == "kabouters en welpen maar niet AxB"


def test_gotify_send_keeps_the_approve_token_intact():
    n, seen = _recording_notifier({Tak.KABOUTERS: "Ella", Tak.WELPEN: "Bo"})
    op = Operation(OpKind.ADD, None, None, ev(title="Ella scouts: Bosspel"))
    link = "https://vonk.test/c/xBoQ9ellaZ"
    assert n.send(*changeset_message(cs(op), link))
    (body,) = seen
    assert body["message"].endswith(link)
    assert "kabouters scouts: Bosspel" in body["message"]
