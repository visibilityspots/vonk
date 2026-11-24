"""poll: one stateless run against a fake service + fake WordPress (httpx.MockTransport)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vonk import cli
from vonk.config import Settings
from vonk.extract import PROMPT_VERSION, ExtractionError, finalize, parse_output
from vonk.models import ExtractedSpan
from vonk.pdf import EmptyPdfError, PdfError, PdfText
from vonk.poll import (
    VISION_PROMPT_VERSION,
    VISION_WARNING,
    PollOptions,
    cmd_poll,
    ocr_guard,
    run_poll,
)
from vonk.vision import VISION_VERSION, VisionError

FIXTURES = Path(__file__).parents[1] / "fixtures"
SOURCES = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))["fixtures"]
POSTS = json.loads((FIXTURES / "wp" / "posts_search_vonk.json").read_text(encoding="utf-8"))
BY_URL = {s["url"]: s for s in SOURCES}
SERVICE = "http://vonk.test"
WP_URL = "https://scoutstervant.be/wp-json/wp/v2"
TOKEN = "s3cret-token"
ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "VONK_API_TOKEN": TOKEN,
    "VONK_API_URL": SERVICE,
    "TAKKEN": "kabouters,jeevees",
}


class FakeWorld:
    """Fake service + WordPress behind one MockTransport, recording every request."""

    def __init__(self, *, seen=(), cache=None, service_status=None, service_down=False,
                 posts=POSTS, transcripts=None, dispatch_allowed=True):  # fmt: skip
        self.seen = list(seen)
        self.cache = cache or {}
        self.transcripts = transcripts or {}
        self.dispatch_allowed = dispatch_allowed
        self.dispatch_requests: list[dict] = []
        self.service_status = service_status
        self.service_down = service_down
        self.posts = posts
        self.requests: list[httpx.Request] = []
        self.desired: dict[str, dict] = {}
        self.reports: list[dict] = []

    def service_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "vonk.test"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "scoutstervant.be":
            return httpx.Response(200, json=self.posts, headers={"X-WP-TotalPages": "1"})
        if self.service_down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        if self.service_status and request.url.path != "/api/poll-report":
            return httpx.Response(self.service_status)
        path = request.url.path
        if path == "/api/seen":
            return httpx.Response(200, json={"posts": self.seen})
        if path.startswith("/api/cache/"):
            _, _, _, sha, version = path.split("/")
            spans = self.cache.get((sha, version))
            return (
                httpx.Response(404) if spans is None else httpx.Response(200, json={"spans": spans})
            )
        if path.startswith("/api/transcripts/"):
            _, _, _, sha, version = path.split("/")
            text = self.transcripts.get((sha, version))
            return httpx.Response(404) if text is None else httpx.Response(200, json={"text": text})
        if path == "/api/vision-dispatch":
            self.dispatch_requests.append(json.loads(request.content))
            return httpx.Response(200, json={"dispatch": self.dispatch_allowed})
        if path.startswith("/api/scopes/") and path.endswith("/desired"):
            self.desired[path.split("/")[3]] = json.loads(request.content)
            return httpx.Response(200, json={"changeset_id": "cs-1", "ops": 5})
        if path == "/api/poll-report":
            self.reports.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404)


def fake_fetch(url: str) -> PdfText:
    src = BY_URL[url]
    return PdfText(sha256=src["sha256"], text=(FIXTURES / src["text"]).read_text("utf-8"))


class FakeExtractor:
    """Returns the recorded model output for the fixture whose text it receives."""

    def __init__(self, fail_for: str | None = None):
        self.calls: list[str] = []
        self.fail_for = fail_for

    def __call__(self, text: str, kind: str):
        src = next(s for s in SOURCES if (FIXTURES / s["text"]).read_text("utf-8") == text)
        self.calls.append(src["name"])
        if src["name"] == self.fail_for:
            raise ExtractionError("Claude stopped with stop_reason='refusal'")
        raw = (FIXTURES / "llm" / f"{src['name']}.json").read_text("utf-8")
        return finalize(parse_output(raw), text, kind)


def run(world: FakeWorld, *, env=ENV, options=None, extractor=None, fetch=fake_fetch,
        dispatch=None) -> int:  # fmt: skip
    kwargs = {} if dispatch is None else {"dispatch_vision": dispatch}
    return run_poll(
        Settings.from_env(env),
        options or PollOptions(),
        transport=httpx.MockTransport(world.handler),
        fetch_pdf=fetch,
        extractor=extractor or FakeExtractor(),
        wp_url=WP_URL,
        **kwargs,
    )


def recorded_spans(name: str) -> list[dict]:
    return json.loads((FIXTURES / "llm" / f"{name}.json").read_text("utf-8"))["items"]


# --- happy path --------------------------------------------------------------------------


def test_happy_path_posts_desired_events_and_reports_ok():
    world = FakeWorld()
    extractor = FakeExtractor()
    assert run(world, extractor=extractor) == 0

    assert set(world.desired) == {"groep:2026-2027", "jeevees:2026-10", "kabouters:2026-10"}
    body = world.desired["kabouters:2026-10"]
    src = BY_URL[body["post"]["pdf_url"]]
    assert body["pdf_sha256"] == src["sha256"]
    assert body["prompt_version"] == PROMPT_VERSION
    assert body["post"]["id"] == 2677 and body["post"]["tak"] == "kabouters"
    assert body["spans"] == recorded_spans("kabouters-2026-10")
    assert [e["date"] for e in body["events"]] == [
        "2026-10-03", "2026-10-11", "2026-10-18", "2026-10-24", "2026-10-31",
    ]  # fmt: skip
    assert all(e["scope_key"] == "kabouters:2026-10" for e in body["events"])
    assert body["warnings"] == []
    assert sorted(extractor.calls) == sorted(s["name"] for s in SOURCES)

    (report,) = world.reports
    assert report["ok"] is True and report["errors"] == [] and report["warnings"] == []
    assert report["started"] <= report["finished"]
    service = world.service_requests()
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in service)
    assert service[0].url.path == "/api/seen"
    assert service[-1].url.path == "/api/poll-report"
    assert sum(r.url.path.startswith("/api/cache/") for r in service) == 3
    wp_requests = [r for r in world.requests if r.url.host == "scoutstervant.be"]
    assert wp_requests and not any("Authorization" in r.headers for r in wp_requests)


def _seen_all() -> list[dict]:
    sha = {s["post_id"]: s["sha256"] for s in SOURCES}
    return [{"post_id": p["id"], "modified": p["modified"] + "+02:00", "pdf_sha256": sha[p["id"]],
             "ocr": False} for p in POSTS if p["id"] in sha]  # fmt: skip


def _cache_all(version: str) -> dict:
    return {(s["sha256"], version): recorded_spans(s["name"]) for s in SOURCES}


def test_unchanged_posts_are_skipped_and_reextract_ignores_seen():
    seen = _seen_all()
    world = FakeWorld(seen=seen, cache=_cache_all(PROMPT_VERSION))
    assert run(world) == 0
    assert world.desired == {}
    assert world.reports[0]["ok"] is True

    world = FakeWorld(seen=seen)
    assert run(world, options=PollOptions(reextract=True)) == 0
    assert len(world.desired) == 3
    assert not any(r.url.path == "/api/seen" for r in world.requests)


def test_prompt_version_bump_reprocesses_unchanged_posts():
    world = FakeWorld(seen=_seen_all(), cache=_cache_all("0000-00-00.0"))
    assert run(world) == 0
    assert len(world.desired) == 3


def test_seen_without_sha_is_processed_again():
    seen = [{**p, "pdf_sha256": None} for p in _seen_all()]
    world = FakeWorld(seen=seen, cache=_cache_all(PROMPT_VERSION))
    assert run(world) == 0
    assert len(world.desired) == 3


def test_modified_post_is_processed_again():
    seen = [{"post_id": 2677, "modified": "2026-10-01T10:00:00+02:00", "pdf_sha256": "ab"}]
    world = FakeWorld(seen=seen)
    assert run(world) == 0
    assert "kabouters:2026-10" in world.desired


# --- extraction cache --------------------------------------------------------------------


def _cache_for(name: str, spans: list[dict]) -> dict:
    src = next(s for s in SOURCES if s["name"] == name)
    return {(src["sha256"], PROMPT_VERSION): spans}


def test_cache_hit_that_still_verifies_skips_the_llm():
    world = FakeWorld(cache=_cache_for("jeevees-2026-10", recorded_spans("jeevees-2026-10")))
    extractor = FakeExtractor()
    assert run(world, extractor=extractor) == 0
    assert "jeevees-2026-10" not in extractor.calls
    assert world.desired["jeevees:2026-10"]["spans"] == recorded_spans("jeevees-2026-10")


def test_cache_hit_that_no_longer_matches_the_text_is_re_extracted():
    stale = recorded_spans("jeevees-2026-10")
    stale[0] = {**stale[0], "date_span": "2 & 3 oktober"}  # older pdftotext output
    world = FakeWorld(cache=_cache_for("jeevees-2026-10", stale))
    extractor = FakeExtractor()
    assert run(world, extractor=extractor) == 0
    assert "jeevees-2026-10" in extractor.calls
    assert world.desired["jeevees:2026-10"]["spans"] == recorded_spans("jeevees-2026-10")


def test_fresh_ignores_the_cache():
    world = FakeWorld(cache=_cache_for("jeevees-2026-10", recorded_spans("jeevees-2026-10")))
    extractor = FakeExtractor()
    assert run(world, extractor=extractor, options=PollOptions(reextract=True, fresh=True)) == 0
    assert "jeevees-2026-10" in extractor.calls
    assert not any(r.url.path.startswith("/api/cache/") for r in world.requests)


# --- failures ----------------------------------------------------------------------------


def test_service_down_exits_nonzero_and_still_attempts_the_report():
    world = FakeWorld(service_down=True)
    assert run(world) == 1
    paths = [r.url.path for r in world.service_requests()]
    assert paths == ["/api/seen", "/api/poll-report"]


def test_401_exits_nonzero_and_reports_the_failure():
    world = FakeWorld(service_status=401)
    assert run(world) == 1
    (report,) = world.reports
    assert report["ok"] is False
    assert report["errors"] == ["GET /api/seen: HTTP 401"]
    assert world.desired == {}


def test_wrong_token_gets_401_everywhere_and_exits_nonzero():
    world = FakeWorld()
    assert run(world, env={**ENV, "VONK_API_TOKEN": "wrong"}) == 1
    assert world.reports == []  # the report itself was refused too


def test_one_failing_post_fails_the_run_but_the_others_are_posted():
    world = FakeWorld()
    assert run(world, extractor=FakeExtractor(fail_for="kabouters-2026-10")) == 1
    assert set(world.desired) == {"groep:2026-2027", "jeevees:2026-10"}
    (report,) = world.reports
    assert report["ok"] is False
    assert report["errors"][0].startswith("kabouters:2026-10 (post 2677): ")


def test_pdf_failure_is_reported():
    def broken_fetch(url):
        raise PdfError(f"{url}: download failed: 404")

    world = FakeWorld()
    assert run(world, fetch=broken_fetch) == 1
    assert len(world.reports[0]["errors"]) == 3


def test_unreadable_pdf_is_a_warning_and_does_not_fail_the_run():
    def fetch(url):
        if BY_URL[url]["name"] == "kabouters-2026-10":
            raise EmptyPdfError("PDF has no text layer and OCR found no text")
        return fake_fetch(url)

    world = FakeWorld()
    assert run(world, fetch=fetch) == 0
    assert set(world.desired) == {"groep:2026-2027", "jeevees:2026-10"}
    (report,) = world.reports
    assert report["ok"] is True
    assert report["errors"] == []
    assert any(w.startswith("kabouters:2026-10 (post 2677): ") for w in report["warnings"])


def test_wordpress_down_fails_the_run():
    world = FakeWorld()

    def handler(request):
        if request.url.host == "scoutstervant.be":
            return httpx.Response(502)
        return world.handler(request)

    code = run_poll(
        Settings.from_env(ENV),
        transport=httpx.MockTransport(handler),
        fetch_pdf=fake_fetch,
        extractor=FakeExtractor(),
        wp_url=WP_URL,
    )
    assert code == 1
    assert world.reports[0]["ok"] is False
    assert world.reports[0]["errors"][0].startswith("WordPress: ")


def test_ingest_warnings_reach_the_report_without_failing_the_run():
    no_pdf = {**POSTS[1], "id": 4001, "content": {"rendered": "<p>vonk volgt</p>"}}
    world = FakeWorld(posts=POSTS + [no_pdf])
    assert run(world) == 0
    (report,) = world.reports
    assert report["ok"] is True
    assert len(report["warnings"]) == 1 and "4001" in report["warnings"][0]


@pytest.mark.parametrize(
    "missing", ["ANTHROPIC_API_KEY", "VONK_API_TOKEN", "VONK_API_URL", "TAKKEN"]
)
def test_missing_secret_exits_2_without_network(missing, caplog):
    world = FakeWorld()
    env = {**ENV, missing: "<no value>"}
    assert run(world, env=env) == 2
    assert world.requests == []
    assert missing in caplog.text


# --- CLI ---------------------------------------------------------------------------------


def test_cli_registers_poll_with_options():
    parser, _ = cli.build_parser()
    args = parser.parse_args(["poll", "--reextract", "--fresh"])
    assert args.func is cmd_poll
    assert (args.reextract, args.fresh) == (True, True)
    args = parser.parse_args(["poll"])
    assert (args.reextract, args.fresh) == (False, False)


def test_cli_poll_without_secrets_exits_2(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["poll"]) == 2


# --- SSRF guard ----------------------------------------------------------------------------


def _run_with_wp(handler_for_wp, wp_url=WP_URL, fetch=fake_fetch):
    world = FakeWorld()

    def handler(request):
        if request.url.host != "vonk.test":
            world.requests.append(request)
            return handler_for_wp(request)
        return world.handler(request)

    code = run_poll(
        Settings.from_env(ENV),
        transport=httpx.MockTransport(handler),
        fetch_pdf=fetch,
        extractor=FakeExtractor(),
        wp_url=wp_url,
    )
    return code, world


def test_wordpress_error_reports_host_and_status_only():
    code, world = _run_with_wp(lambda r: httpx.Response(502, text="nginx internal 10.0.0.5"))
    assert code == 1
    assert world.reports[0]["errors"] == ["WordPress: scoutstervant.be: HTTP 502"]


def test_wordpress_redirect_to_internal_host_is_blocked():
    def wp(request):
        if request.url.host == "scoutstervant.be":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8500/v1/kv/x"})
        return httpx.Response(200, json=[])

    code, world = _run_with_wp(wp)
    assert code == 1
    assert [r.url.host for r in world.requests if r.url.host != "vonk.test"] == [
        "scoutstervant.be"
    ]
    (error,) = world.reports[0]["errors"]
    assert error.startswith("WordPress: 127.0.0.1: blocked")
    assert "/v1/kv" not in error


def test_foreign_wordpress_host_is_never_contacted():
    code, world = _run_with_wp(
        lambda r: httpx.Response(200, json=[]), wp_url="https://evil.example/wp-json/wp/v2"
    )
    assert code == 1
    assert [r for r in world.requests if r.url.host != "vonk.test"] == []
    assert world.reports[0]["errors"][0].startswith("WordPress: evil.example: blocked")


def test_pdf_on_foreign_host_is_blocked_by_the_default_fetcher():
    from vonk.pdf import fetch_text

    posts = [{**POSTS[1], "content": {"rendered":
              '<a class="pdfemb-viewer" href="https://evil.example/vonk.pdf">x</a>'}}]  # fmt: skip
    world = FakeWorld(posts=posts)

    def handler(request):
        if request.url.host == "evil.example":
            world.requests.append(request)
            return httpx.Response(200)
        return world.handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        code = run_poll(
            Settings.from_env(ENV),
            transport=httpx.MockTransport(handler),
            fetch_pdf=lambda url: fetch_text(client, url),
            extractor=FakeExtractor(),
            wp_url=WP_URL,
        )
    assert code == 1
    assert not any(r.url.host == "evil.example" for r in world.requests)
    assert any("evil.example: blocked" in e for e in world.reports[0]["errors"])


# --- image-only PDFs: vision transcripts -------------------------------------------------

IMAGE = next(s for s in SOURCES if s["name"] == "jeevees-2026-10")
IMAGE_TEXT = (FIXTURES / IMAGE["text"]).read_text("utf-8")
OCR_TEXT = "tesseract garbage DROPPING 12 oktober"


def image_fetch(url: str) -> PdfText:
    """The jeevees vonk arrives as an image-only PDF; its fixture text is the transcript."""
    if url == IMAGE["url"]:
        return PdfText(sha256=IMAGE["sha256"], text=OCR_TEXT, ocr=True)
    return fake_fetch(url)


class Dispatcher:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, url: str, sha: str) -> str:
        self.calls.append((url, sha))
        if self.fail:
            raise VisionError("Nomad dispatch: HTTP 403")
        return "vision/dispatch-1"


VISION_ENV = {**ENV, "VONK_VISION_DISPATCH": "1"}


def test_image_only_pdf_without_transcript_dispatches_the_vision_job():
    world = FakeWorld()
    extractor, dispatcher = FakeExtractor(), Dispatcher()
    assert run(world, env=VISION_ENV, fetch=image_fetch, extractor=extractor,
               dispatch=dispatcher) == 0  # fmt: skip
    assert dispatcher.calls == [(IMAGE["url"], IMAGE["sha256"])]
    assert world.dispatch_requests == [{"pdf_sha256": IMAGE["sha256"], "post_id": 2647}]
    assert "jeevees:2026-10" not in world.desired  # not posted, so not seen either
    assert "jeevees-2026-10" not in extractor.calls  # OCR text is never extracted from
    assert set(world.desired) == {"groep:2026-2027", "kabouters:2026-10"}
    assert world.reports[0]["ok"] is True


def test_recent_dispatch_is_not_repeated():
    world = FakeWorld(dispatch_allowed=False)
    dispatcher = Dispatcher()
    assert run(world, env=VISION_ENV, fetch=image_fetch, dispatch=dispatcher) == 0
    assert len(world.dispatch_requests) == 1 and dispatcher.calls == []
    assert "jeevees:2026-10" not in world.desired


def test_image_only_pdf_is_skipped_with_a_warning_when_dispatch_is_disabled():
    world = FakeWorld()
    dispatcher = Dispatcher()
    assert run(world, fetch=image_fetch, dispatch=dispatcher) == 0
    assert dispatcher.calls == [] and world.dispatch_requests == []
    assert "jeevees:2026-10" not in world.desired
    (report,) = world.reports
    assert report["ok"] is True
    assert any("jeevees:2026-10 (post 2647): image-only PDF skipped" in w
               for w in report["warnings"])  # fmt: skip


def test_failed_dispatch_fails_the_run():
    world = FakeWorld()
    assert run(world, env=VISION_ENV, fetch=image_fetch, dispatch=Dispatcher(fail=True)) == 1
    assert world.reports[0]["errors"] == ["jeevees:2026-10 (post 2647): Nomad dispatch: HTTP 403"]


def test_transcript_is_extracted_and_cached_under_the_vision_key():
    world = FakeWorld(transcripts={(IMAGE["sha256"], VISION_VERSION): IMAGE_TEXT})
    extractor, dispatcher = FakeExtractor(), Dispatcher()
    assert run(world, env=VISION_ENV, fetch=image_fetch, extractor=extractor,
               dispatch=dispatcher) == 0  # fmt: skip
    assert dispatcher.calls == []
    assert "jeevees-2026-10" in extractor.calls
    body = world.desired["jeevees:2026-10"]
    assert body["prompt_version"] == VISION_PROMPT_VERSION == PROMPT_VERSION + "-vision1"
    assert body["ocr"] is True
    assert body["spans"] == recorded_spans("jeevees-2026-10")
    assert VISION_WARNING in body["warnings"]
    assert any(w.startswith("vision guard: title ") for w in body["warnings"])
    assert world.desired["kabouters:2026-10"]["ocr"] is False
    assert world.desired["kabouters:2026-10"]["prompt_version"] == PROMPT_VERSION
    cache_paths = [r.url.path for r in world.requests if r.url.path.startswith("/api/cache/")]
    assert f"/api/cache/{IMAGE['sha256']}/{VISION_PROMPT_VERSION}" in cache_paths


def test_vision_cache_hit_skips_the_llm():
    world = FakeWorld(
        transcripts={(IMAGE["sha256"], VISION_VERSION): IMAGE_TEXT},
        cache={(IMAGE["sha256"], VISION_PROMPT_VERSION): recorded_spans("jeevees-2026-10")},
    )
    extractor = FakeExtractor()
    assert run(world, fetch=image_fetch, extractor=extractor) == 0
    assert "jeevees-2026-10" not in extractor.calls
    assert world.desired["jeevees:2026-10"]["spans"] == recorded_spans("jeevees-2026-10")


def _seen_image(ocr):
    return [{**p, "ocr": ocr if p["post_id"] == 2647 else False} for p in _seen_all()]


def test_seen_image_post_is_current_only_with_transcript_and_vision_cache():
    sha = IMAGE["sha256"]
    full = {**_cache_all(PROMPT_VERSION),
            (sha, VISION_PROMPT_VERSION): recorded_spans("jeevees-2026-10")}  # fmt: skip
    transcripts = {(sha, VISION_VERSION): IMAGE_TEXT}
    world = FakeWorld(seen=_seen_image(True), cache=full, transcripts=transcripts)
    assert run(world, fetch=image_fetch) == 0
    assert world.desired == {}

    # an OCR-era cache row under PROMPT_VERSION does not make an image post current
    world = FakeWorld(seen=_seen_image(True), cache=_cache_all(PROMPT_VERSION),
                      transcripts=transcripts)  # fmt: skip
    assert run(world, fetch=image_fetch) == 0
    assert set(world.desired) == {"jeevees:2026-10"}

    world = FakeWorld(seen=_seen_image(True), cache=full)  # no transcript
    assert run(world, fetch=image_fetch) == 0
    assert world.desired == {}


def test_seen_row_without_ocr_flag_is_reprocessed_once_from_the_cache():
    seen = [{k: v for k, v in p.items() if k != "ocr"} for p in _seen_all()]
    world = FakeWorld(seen=seen, cache=_cache_all(PROMPT_VERSION))
    extractor = FakeExtractor()
    assert run(world, extractor=extractor) == 0
    assert len(world.desired) == 3
    assert extractor.calls == []  # cache hits: no LLM call


def test_ocr_guard_is_case_and_space_insensitive():
    spans = [ExtractedSpan("8 nov", None, None, None, "Kamp menu", None, False),
             ExtractedSpan("14 nov", None, None, None, "Filmmarathon", None, False)]  # fmt: skip
    assert ocr_guard(spans, "zondag 8 KAMPMENU\nde kring") == [
        "vision guard: title 'Filmmarathon' not found in the OCR text"
    ]
