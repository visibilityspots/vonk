"""vision: Ollama client (mocked HTTP), double-run digit check, Nomad task-API dispatch
(mocked unix socket transport) and the ``vonk vision`` run against a fake service."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from vonk import cli
from vonk.config import Settings
from vonk.vision import (
    PAGE_TIMEOUT,
    PROMPT,
    VISION_VERSION,
    VisionError,
    cmd_vision,
    digits,
    nomad_dispatch,
    run_vision,
    transcribe,
    transcribe_page,
    wait_for_model,
)

OLLAMA = "http://ollama.test:11434"
SERVICE = "http://vonk.test"
TOKEN = "s3cret-token"
MODEL = "qwen3-vl:4b"
DIGEST = "ab12" * 16
PDF = b"%PDF-1.4\nposter"
SHA = hashlib.sha256(PDF).hexdigest()
PDF_URL = "https://scoutstervant.be/wp-content/uploads/vonk.pdf"
ENV = {
    "VONK_API_TOKEN": TOKEN,
    "VONK_API_URL": SERVICE,
    "VONK_VISION_URL": OLLAMA,
    "NOMAD_META_PDF_URL": PDF_URL,
    "NOMAD_META_PDF_SHA256": SHA,
}
TRANSCRIPT = "Zondag 8 november 10u30 Kampmenu\nZaterdag 14 november 21u Filmmarathon"


def tags(*models: dict) -> httpx.Response:
    return httpx.Response(200, json={"models": list(models)})


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- wait_for_model ----------------------------------------------------------------------


def test_wait_for_model_returns_when_listed():
    http = client(lambda r: tags({"name": MODEL, "model": MODEL, "digest": DIGEST}))
    wait_for_model(http, OLLAMA, MODEL, digest=DIGEST)
    wait_for_model(http, OLLAMA + "/", MODEL, digest="sha256:" + DIGEST[:12])


def test_wait_for_model_waits_for_ollama_to_come_up():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/tags"
        if len(calls) < 3:
            raise httpx.ConnectError("refused", request=request)
        return tags({"name": MODEL})

    clock = Clock()
    wait_for_model(client(handler), OLLAMA, MODEL, sleep=clock.sleep, clock=clock)
    assert len(calls) == 3 and clock.sleeps == [5.0, 5.0]


def test_wait_for_model_fails_loudly_when_the_model_stays_absent():
    clock = Clock()
    with pytest.raises(VisionError, match=f"model {MODEL} is not available.*after 300s"):
        wait_for_model(client(lambda r: tags({"name": "other:1b"})), OLLAMA, MODEL,
                       sleep=clock.sleep, clock=clock)  # fmt: skip


def test_wait_for_model_reports_an_unreachable_server():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    clock = Clock()
    with pytest.raises(VisionError, match="Ollama not reachable"):
        wait_for_model(client(handler), OLLAMA, MODEL, timeout=10, sleep=clock.sleep, clock=clock)


def test_wait_for_model_rejects_another_digest_at_once():
    clock = Clock()
    with pytest.raises(VisionError, match="digest"):
        wait_for_model(client(lambda r: tags({"name": MODEL, "digest": "ff" * 32})), OLLAMA,
                       MODEL, digest=DIGEST, sleep=clock.sleep, clock=clock)  # fmt: skip
    assert clock.sleeps == []


# --- transcribe_page / transcribe --------------------------------------------------------


def test_transcribe_page_sends_the_test_request():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant",
                                                     "content": f"  {TRANSCRIPT}\n"}})  # fmt: skip

    assert transcribe_page(client(handler), OLLAMA, MODEL, b"png-bytes") == TRANSCRIPT
    (request,) = seen
    assert request.method == "POST" and str(request.url) == f"{OLLAMA}/api/chat"
    assert json.loads(request.content) == {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
                "images": [base64.b64encode(b"png-bytes").decode()],
            }
        ],
    }
    assert request.extensions["timeout"]["read"] == PAGE_TIMEOUT == 900


def test_prompt_is_the_tested_wording():
    assert PROMPT.startswith("This image is a monthly activity calendar (a 'vonk') of one age")
    assert PROMPT.endswith("do not add anything that is not on the poster.")


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(200, json={"error": "x"}), "malformed"),
        (httpx.Response(200, json={"message": {"content": "  "}}), "empty transcript"),
    ],
)
def test_transcribe_page_errors(response, match):
    with pytest.raises(VisionError, match=match):
        transcribe_page(client(lambda r: response), OLLAMA, MODEL, b"png")


def test_digits_in_order():
    assert digits("Zondag 8 november 10u30 - 22/11 14u-16u30") == [
        "8", "10", "30", "22", "11", "14", "16", "30",
    ]  # fmt: skip


def test_transcribe_reads_both_renders_and_keeps_the_first():
    runs = {b"p1@150": "Zondag 8 nov 10u30 Kampmenu", b"p1@200": "zondag 8 nov 10u30 kampmenu",
            b"p2@150": "Zat 28 nov Minimarkt", b"p2@200": "Zat 28 nov Minimarkt"}  # fmt: skip
    calls = []

    def read(png):
        calls.append(png)
        return runs[png]

    text = transcribe([b"p1@150", b"p2@150"], [b"p1@200", b"p2@200"], read)
    assert text == "Zondag 8 nov 10u30 Kampmenu\fZat 28 nov Minimarkt"
    assert calls == [b"p1@150", b"p1@200", b"p2@150", b"p2@200"]


def test_transcribe_refuses_runs_that_disagree_on_digits():
    answers = iter(["Zondag 8 nov 10u30", "Zondag 9 nov 10u30"])
    with pytest.raises(VisionError, match=r"page 1: two runs disagree"):
        transcribe([b"p1"], [b"p1-hi"], lambda png: next(answers))
    with pytest.raises(VisionError, match="page count"):
        transcribe([b"p1"], [b"p1", b"p2"], lambda png: "x")


# --- Nomad dispatch ----------------------------------------------------------------------

NOMAD_ENV = {
    "NOMAD_SECRETS_DIR": "/secrets",
    "NOMAD_TOKEN": "nomad-wi-token",
    "VONK_VISION_JOB": "my-vision-job",
    "VONK_VISION_NAMESPACE": "my-namespace",
}


def test_nomad_dispatch_posts_meta_with_the_workload_token():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"DispatchedJobID": "my-vision-job/dispatch-1-abc"})

    settings = Settings.from_env(NOMAD_ENV)
    job = nomad_dispatch(settings, PDF_URL, SHA, transport=httpx.MockTransport(handler))
    assert job == "my-vision-job/dispatch-1-abc"
    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == "/v1/job/my-vision-job/dispatch"
    assert request.url.params["namespace"] == "my-namespace"
    assert request.headers["X-Nomad-Token"] == "nomad-wi-token"
    assert json.loads(request.content) == {
        "Meta": {"PDF_URL": PDF_URL, "PDF_SHA256": SHA, "VISION_VERSION": VISION_VERSION}
    }


@pytest.mark.parametrize("answer", [{}, {"DispatchedJobID": ""}, {"DispatchedJobID": None}])
def test_nomad_dispatch_without_a_job_id_is_an_error(answer):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
    with pytest.raises(VisionError, match="no DispatchedJobID"):
        nomad_dispatch(Settings.from_env(NOMAD_ENV), PDF_URL, SHA, transport=transport)


def test_nomad_dispatch_uses_the_task_api_socket(monkeypatch):
    made = {}

    class FakeTransport(httpx.MockTransport):
        def __init__(self, *, uds):
            made["uds"] = uds
            super().__init__(lambda r: httpx.Response(200, json={"DispatchedJobID": "j"}))

    monkeypatch.setattr(httpx, "HTTPTransport", FakeTransport)
    assert nomad_dispatch(Settings.from_env(NOMAD_ENV), PDF_URL, SHA) == "j"
    assert made == {"uds": "/secrets/api.sock"}


def test_nomad_dispatch_defaults_and_failures():
    settings = Settings.from_env({"NOMAD_SECRETS_DIR": "/secrets", "NOMAD_TOKEN": "t"})
    assert (settings.vonk_vision_job, settings.vonk_vision_namespace) == ("vonk-vision", "default")
    with pytest.raises(VisionError, match="HTTP 403"):
        nomad_dispatch(settings, PDF_URL, SHA,
                       transport=httpx.MockTransport(lambda r: httpx.Response(403)))  # fmt: skip

    def down(request):
        raise httpx.ConnectError("no socket", request=request)

    with pytest.raises(VisionError, match="ConnectError"):
        nomad_dispatch(settings, PDF_URL, SHA, transport=httpx.MockTransport(down))
    with pytest.raises(VisionError, match="NOMAD_SECRETS_DIR and NOMAD_TOKEN"):
        nomad_dispatch(Settings.from_env({"NOMAD_TOKEN": "<no value>"}), PDF_URL, SHA,
                       transport=httpx.MockTransport(lambda r: httpx.Response(200)))  # fmt: skip


# --- vonk vision -------------------------------------------------------------------------


class FakeWorld:
    """Fake service + Ollama + WordPress uploads behind one MockTransport."""

    def __init__(self, *, stored=None, answers=None, pdf=PDF, model_listed=True):
        self.stored = dict(stored or {})
        self.answers = list(answers or [TRANSCRIPT, TRANSCRIPT])
        self.pdf = pdf
        self.model_listed = model_listed
        self.requests: list[httpx.Request] = []
        self.reports: list[dict] = []

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "scoutstervant.be":
            return httpx.Response(200, content=self.pdf)
        if host == "ollama.test":
            if path == "/api/tags":
                return tags({"name": MODEL, "digest": DIGEST}) if self.model_listed else tags()
            return httpx.Response(200, json={"message": {"content": self.answers.pop(0)}})
        assert host == "vonk.test"
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        if path == "/api/poll-report":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={})
        _, _, _, sha, version = path.split("/")
        if request.method == "PUT":
            self.stored[(sha, version)] = json.loads(request.content)["text"]
            return httpx.Response(200, json={})
        text = self.stored.get((sha, version))
        return httpx.Response(404) if text is None else httpx.Response(200, json={"text": text})


def run(world: FakeWorld, env=ENV, *, renders=None) -> int:
    clock = Clock()
    renders = [] if renders is None else renders

    def render(data, *, dpi):
        renders.append((data, dpi))
        return [f"page-1@{dpi}".encode()]

    return run_vision(
        Settings.from_env(env),
        env,
        transport=httpx.MockTransport(world.handler),
        render=render,
        sleep=clock.sleep,
        monotonic=clock,
    )


def test_vision_run_stores_the_transcript_and_reports_as_vision():
    world = FakeWorld()
    assert run(world) == 0
    assert world.stored == {(SHA, VISION_VERSION): TRANSCRIPT}
    (report,) = world.reports
    assert report["source"] == "vision" and report["ok"] is True and report["errors"] == []
    chats = [json.loads(r.content) for r in world.requests if r.url.path == "/api/chat"]
    images = [base64.b64decode(c["messages"][0]["images"][0]) for c in chats]
    assert images == [b"page-1@150", b"page-1@200"]  # one page, two renders


def test_vision_run_renders_the_pdf_at_150_and_200_dpi():
    renders = []
    assert run(FakeWorld(), renders=renders) == 0
    assert renders == [(PDF, 150), (PDF, 200)]


def test_vision_run_refuses_another_vision_version():
    world = FakeWorld()
    assert run(world, {**ENV, "NOMAD_META_VISION_VERSION": "1999-01-01.1"}) == 1
    assert world.stored == {}
    assert set(world.hosts()) == {"vonk.test"}
    (report,) = world.reports
    assert report["source"] == "vision" and report["ok"] is False
    assert "vision version 1999-01-01.1" in report["errors"][0]

    world = FakeWorld()
    assert run(world, {**ENV, "VISION_VERSION": VISION_VERSION}) == 0
    assert (SHA, VISION_VERSION) in world.stored


def test_vision_run_accepts_plain_meta_names():
    env = {k: v for k, v in ENV.items() if not k.startswith("NOMAD_META_")}
    world = FakeWorld()
    assert run(world, {**env, "PDF_URL": PDF_URL, "PDF_SHA256": SHA.upper()}) == 0
    assert (SHA, VISION_VERSION) in world.stored


def test_vision_run_skips_a_pdf_that_already_has_a_transcript():
    world = FakeWorld(stored={(SHA, VISION_VERSION): "old"})
    assert run(world) == 0
    assert set(world.hosts()) == {"vonk.test"}
    assert world.reports[0]["ok"] is True


def test_vision_run_refuses_a_pdf_with_another_sha256():
    world = FakeWorld(pdf=b"%PDF-1.4\nother poster")
    assert run(world) == 1
    assert world.stored == {}
    assert "ollama.test" not in world.hosts()
    (report,) = world.reports
    assert report["source"] == "vision" and report["ok"] is False
    assert "differs from the dispatched" in report["errors"][0]


def test_vision_run_does_not_store_when_the_digits_disagree():
    world = FakeWorld(answers=["Zondag 8 november 10u30", "Zondag 18 november 10u30"])
    assert run(world) == 1
    assert world.stored == {}
    assert "two runs disagree" in world.reports[0]["errors"][0]


def test_vision_run_fails_when_the_model_is_missing():
    world = FakeWorld(model_listed=False)
    assert run(world) == 1
    assert world.stored == {}
    assert "not available" in world.reports[0]["errors"][0]


def test_vision_run_blocks_a_pdf_on_a_foreign_host():
    world = FakeWorld()
    env = {**ENV, "NOMAD_META_PDF_URL": "https://evil.example/vonk.pdf"}
    assert run(world, env) == 1
    assert "evil.example" not in world.hosts()
    assert "blocked" in world.reports[0]["errors"][0]


@pytest.mark.parametrize(
    "missing", ["VONK_API_TOKEN", "VONK_API_URL", "VONK_VISION_URL", "NOMAD_META_PDF_URL",
                "NOMAD_META_PDF_SHA256"],
)  # fmt: skip
def test_vision_without_settings_or_meta_exits_2_without_network(missing, caplog):
    world = FakeWorld()
    assert run(world, {**ENV, missing: "<no value>" if missing.startswith("VONK") else ""}) == 2
    assert world.requests == []
    assert missing.removeprefix("NOMAD_META_") in caplog.text


def test_cli_registers_vision(monkeypatch):
    parser, _ = cli.build_parser()
    assert parser.parse_args(["vision"]).func is cmd_vision
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PDF_URL", raising=False)
    monkeypatch.delenv("PDF_SHA256", raising=False)
    assert cli.main(["vision"]) == 2
