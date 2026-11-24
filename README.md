# vonk

[![trivy](https://github.com/visibilityspots/vonk/actions/workflows/trivy.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/trivy.yml)
[![docker-hub-description](https://github.com/visibilityspots/vonk/actions/workflows/docker-hub-description.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/docker-hub-description.yml)
[![build status](https://github.com/visibilityspots/vonk/actions/workflows/ci.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/ci.yml)
[![gitHub release](https://img.shields.io/github/v/release/visibilityspots/vonk)](https://github.com/visibilityspots/vonk/releases)
[![feed updated](https://img.shields.io/github/last-commit/visibilityspots/vonk/gh-pages?label=feed%20updated)](https://visibilityspots.github.io/vonk/)
[![docker image size](https://img.shields.io/docker/image-size/visibilityspots/vonk/latest)](https://hub.docker.com/r/visibilityspots/vonk)
[![docker pulls](https://img.shields.io/docker/pulls/visibilityspots/vonk.svg)](https://hub.docker.com/r/visibilityspots/vonk/)

Turns the monthly vonk PDFs of Scouts en Gidsen Tervant (scoutstervant.be) into events in a
family CalDAV calendar, but only after a human has approved the changes. The same events,
for every tak, are also published as a public ICS feed that other parents can subscribe to.

## run

One image, three commands: `vonk serve` (the default), `vonk poll` and `vonk vision`
(see [Image-only vonks](#image-only-vonks)).

```
$ docker run -p 8080:8080 -v vonk-data:/data --env-file vonk.env --name vonk --rm visibilityspots/vonk:latest
$ docker run --env-file vonk-poll.env --rm visibilityspots/vonk:latest poll
```

Without secrets the service starts degraded: `/health` still answers 200 and `vonk_degraded`
is 1.

### How it works

1. `vonk poll` asks the scoutstervant.be WordPress REST API for new or changed vonk posts,
   downloads the PDFs, converts them with `pdftotext` and lets Claude locate the event spans
   for the configured takken (plus the group-wide key dates from the jaarvonk). Dates, years
   and event identity are derived by plain code, not by the model.
2. The poll result is sent to the `vonk` service, which diffs it against the events it owns
   in the Baikal calendar (UID prefix `vonk-`, domain `@vonk.visibilityspots`).
3. A non-empty diff is announced through Gotify with a link to an internal approve page.
   The link is valid for 48 hours. When it expires unapproved, the next poll report
   announces the scope again with a fresh link, so a missed notice never hides a diff.
4. Only after approval in the browser does the service write the diff over CalDAV.
5. After every poll report the service renders the public feed from the stored events and
   pushes the files that changed to GitHub Pages (see [Public ICS feed](#public-ics-feed)).

### Architecture

| Job | Type | Role |
|---|---|---|
| `vonk` | service, `count = 1` | Approve page, bearer-authenticated internal API, `/health`, `/metrics`. Sole owner of SQLite (identity, state, extraction cache, tokens) and of every CalDAV write. |
| `vonk-poll` | periodic batch, every 6 hours | Stateless. Extracts and sends results to the service API; always reports back via `/api/poll-report`; exits non-zero on any failure. |
| vision job | parameterized batch, dispatched by the poll | `vonk vision` on a GPU node with an Ollama sidecar: transcribes one image-only vonk and stores the transcript in the service. Optional: without it, or when it fails, the poll reads the poster with Claude. |

The service is reachable on the internal `https` entrypoint only.

## Configuration

All configuration is environment based; on the cluster it arrives through a Vault template
from one Vault path per job (the poll job only needs `api_token`, `anthropic_api_key` and
`takken`). Empty values and the consul-template string `<no value>` count
as missing.

| Env var | Vault key | Used by | Purpose |
|---|---|---|---|
| `BAIKAL_URL`, `BAIKAL_USER`, `BAIKAL_PASSWORD`, `BAIKAL_CALENDAR` | `baikal_*` | serve | CalDAV target |
| `GOTIFY_URL`, `GOTIFY_TOKEN` | `gotify_*` | serve | change notices |
| `VONK_API_TOKEN` | `api_token` | serve, poll | bearer token of the internal API |
| `VONK_PUBLIC_URL` | (in the job) | serve | base of the approve links |
| `CHILD_NAMES` | `child_names` | serve | JSON object `{"<tak>": "<name>"}`, shown on the approve page |
| `VONK_DB_PATH` | (in the job) | serve | SQLite path, `/data/vonk.db` on the CSI volume |
| `ANTHROPIC_API_KEY` | `anthropic_api_key` | poll | extraction |
| `TAKKEN` | `takken` | poll | the family's takken, comma separated; the service uses the `CHILD_NAMES` keys instead |
| `FEED_TAKKEN` | (in the job) | serve, poll | takken in the public feed: `all` or a list; empty = no feed |
| `VONK_FEED_REPO` | (in the job) | serve | `owner/repo` of the GitHub Pages repository |
| `VONK_FEED_TOKEN` | `feed_token` | serve | fine-grained GitHub token, that repository only, `contents: write` |
| `VONK_FEED_BRANCH` | (in the job: `gh-pages`) | serve | branch GitHub Pages serves (default `main`) |
| `VONK_API_URL` | (in the job) | poll, vision | service base URL, without `/api` |
| `VONK_VISION_DISPATCH` | (in the job) | poll | `1` dispatches the vision job for image-only PDFs; otherwise they are skipped with a warning |
| `VONK_VISION_JOB`, `VONK_VISION_NAMESPACE` | (in the job) | poll | Nomad job and namespace of the vision job (default `vonk-vision`, `default`) |
| `NOMAD_SECRETS_DIR`, `NOMAD_TOKEN` | (set by Nomad) | poll | task API socket and workload identity token for the dispatch |
| `VONK_VISION_URL` | (in the job) | vision | Ollama base URL; no default |
| `VONK_VISION_MODEL` | (in the job) | vision | vision model (default `qwen3-vl:4b`) |
| `VONK_VISION_MODEL_DIGEST` | (in the job) | vision | optional: the model digest Ollama reports must match |
| `VONK_VISION_FALLBACK_AFTER` | (in the job) | poll | hours after the first dispatch without transcript before the poll reads the poster with Claude (default 12) |
| `VONK_VISION_RETRY_AFTER` | (in the job) | poll | hours after a failed Claude fallback before it is tried again (default 24) |
| `VONK_VISION_MAX_FALLBACKS` | (in the job) | poll | Claude fallback attempts per PDF and vision version (default 3) |
| `PDF_URL`, `PDF_SHA256` | (dispatch meta) | vision | the PDF to transcribe; `NOMAD_META_PDF_URL` / `NOMAD_META_PDF_SHA256` are read first |

### Image-only vonks

Some vonks are posters without a text layer. `tesseract` loses day numbers in calendar
icons, so OCR text is never extracted from; it only serves as a weak check afterwards.
Posters are read local-first: a small vision model on a GPU node, with Claude as the
fallback when that model fails or is not configured.

1. The poll asks the service for a stored transcript of the PDF (`GET
   /api/transcripts/<sha256>/<vision version>`). Without one it dispatches the vision job
   through the Nomad task API (`$NOMAD_SECRETS_DIR/api.sock`, the poll job needs
   `identity { env = true }` and a policy with `dispatch-job` on the vision job's
   namespace). The dispatch meta carries `PDF_URL`, `PDF_SHA256` and `VISION_VERSION`
   (declare them as meta on the parameterized job). A successful dispatch is recorded
   (`POST /api/vision-dispatch`), blocks that PDF for 12 hours and marks the post seen as
   image-only, so polls in that window skip it without downloading the PDF again. A failed
   dispatch counts in `vonk_vision_errors_total` and is retried by the next poll. With
   dispatch enabled this also holds for a PDF on which OCR finds no text at all.
2. `vonk vision` (a parameterized Nomad job on a GPU node with an Ollama sidecar) downloads
   the PDF from the same allowlisted hosts, checks its sha256, renders every page at 150 dpi
   in colour and in greyscale (a larger render can run a small GPU out of memory), waits
   until Ollama lists the model, and transcribes both renders of every page. Only when both
   contain exactly the same digits is the transcript stored (engine `ollama`). A job image
   whose `VISION_VERSION` differs from the dispatched one refuses the run. Every failure
   (download, model, empty answer, digit mismatch, ...) is recorded for that PDF in the
   service (`POST /api/vision-failures`). It always reports back with `source = "vision"`,
   which has its own heartbeat gauges.
3. Claude fallback: when the vision job failed for the PDF (this vision version) after its
   latest dispatch, when its first dispatch of this version is older than
   `VONK_VISION_FALLBACK_AFTER` hours without a transcript (a re-dispatch does not reset
   that), or when dispatch is not configured at all, the poll reads the poster itself with
   Claude: the same two renders scaled to at most 1568 px on the long edge, the same prompt
   and the same digit rule (every page needs digits in both reads). A transcript that
   passes is stored with engine `claude` and extracted in the same run; the first stored
   transcript of a PDF and version always wins. A failure stores nothing, is recorded with
   an attempt count, becomes a warning, and the post stays pending: the poll does not
   download it again until `VONK_VISION_RETRY_AFTER` hours have passed, and after
   `VONK_VISION_MAX_FALLBACKS` failed attempts it only warns (the pending metrics below
   keep showing it). `vonk_vision_fallbacks_total{result="ok"|"error"}` counts attempts.
4. A poll extracts from the transcript with Claude like from a text layer and caches the
   spans under the prompt version plus `-vision1`. The ChangeSet carries the warning
   "vision transcript (<engine>): check dates against the poster", plus a warning per
   title that does not occur in the OCR text.

A failure anywhere on this path never falls back to the OCR text. Metrics:
`vonk_vision_pending` and `vonk_vision_oldest_pending_age_seconds` (dispatched PDFs without
a transcript), `vonk_vision_dispatches_total`, `vonk_vision_errors_total` (failed
dispatches and failed vision runs) and
`vonk_last_{completed,successful}_vision_timestamp`. The model has to be pulled into the
Ollama model store beforehand; pin it with `VONK_VISION_MODEL_DIGEST`. Without the vision
job (no `VONK_VISION_DISPATCH`) every image-only vonk is read by the Claude fallback.

Seen posts carry an `ocr` flag (image-only PDF). Rows from before the flag existed are
processed once more (a cache hit for text PDFs, so no LLM call and no new ChangeSet). Every
write of a seen row sets the flag, so a changed PDF never keeps the old one. Rolling back
to a version without the flag can leave a stale flag on a post that version re-processed;
after rolling forward again that costs at most one extra download of that post.

### Baikal calendar

`BAIKAL_CALENDAR` is the calendar collection name under `calendars/<BAIKAL_USER>/`, not the
display name. For a calendar shared with the `vonk` user that is the share's UUID, which
the Nomad job takes from the Vault key `baikal_gezin_uri`; `baikal_calendar` is only the
display name and is not used for the path.

### Public ICS feed

`https://visibilityspots.github.io/vonk/` (branch `gh-pages` of this repository) lists one calendar per tak
(`<tak>.ics`: that tak plus the group-wide events) and `groep.ics` (group-wide only).

- Every tak in `FEED_TAKKEN` is polled and extracted, but only the family's own takken get
  a ChangeSet; the others are stored for the feed only.
- History: every monthly vonk is its own scope (`welpen:2026-11`) and no scope is ever
  dropped, so the December vonk adds to October and November instead of replacing them.
- Scout year: the feed holds the current scout year (September to August) and anything
  later, so an early September vonk appears right away. From 1 September the previous
  year leaves the feed; SQLite and the family calendar keep it.
- No approval: the feed shows what the latest vonk says. Titles are
  `Scouts <Tak> - <activiteit>`, never a child name; descriptions are the same as in the
  family calendar (the vonk text is public on scoutstervant.be anyway).
- UIDs come from scope, date and start time (domain `@feed.vonk.visibilityspots`), so an
  edited vonk updates the event on subscribed phones instead of duplicating it.
- Only changed files are committed; a failed publish increments `vonk_feed_errors` and is
  retried after the next poll. `vonk_feed_last_publish_timestamp` shows the last success.
- Per calendar the page shows "Laatst gewijzigd": the time of the last commit that changed
  that file. The same time is exported as `vonk_feed_last_change_timestamp{feed="<tak>"}`
  (`groep` included) and kept in SQLite. A calendar without a stored time takes it from its
  last commit on GitHub once. `index.html` is written after the calendars, so it only
  changes when a calendar did.

### Privacy

The repository is public. The vonk PDFs contain names and phone numbers of leiding and are
never committed (`tests/fixtures/pdf/` is ignored; `scripts/fetch_fixtures.py` verifies
sources by URL and sha256). Only redacted `pdftotext` output is committed. Child names live
only in Vault (`child_names`) and are never logged or echoed in error messages.

## build

```
$ docker build -t visibilityspots/vonk:latest .
```

### buildx

```
$ docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
$ docker buildx build -t visibilityspots/vonk:latest --platform linux/amd64,linux/arm64 --push .
```

### local development

`pdftotext` (poppler-utils) must be installed.

```
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e '.[dev]'
$ ruff check .
$ pytest                  # default markers: no network, no API key, no Baikal
```

Opt-in test markers:

| Marker | Needs | Command |
|---|---|---|
| `llm` | `ANTHROPIC_API_KEY`, costs money | `pytest -m llm tests/golden` |
| `caldav` | scratch Baikal calendar `vonk-test` (never `gezin`) | `pytest -m caldav` |
| `net` | read-only calls to the live scoutstervant.be | `pytest -m net` |

### dgoss

I wrote some tests in a goss.yaml file which can be executed by [dgoss](https://github.com/aelsabbahy/goss/tree/master/extras/dgoss) to test the created image. `goss_wait.yaml` holds the tests back until `/health` answers.

```
$ dgoss run visibilityspots/vonk:v0.2.8 serve
INFO: Starting podman container
INFO: Container ID: d37d2dbb
INFO: Found goss_wait.yaml, waiting for it to pass before running tests
INFO: Sleeping for 0.2
INFO: Container health
INFO: Running Tests
HTTP: http://127.0.0.1:8080/health: status: matches expectation: 200
Port: tcp:8080: listening: matches expectation: true
Command: vonk runs as an unprivileged user: exit-status: matches expectation: 0
Command: vonk runs as an unprivileged user: stdout: matches expectation: ["1000"]
Command: pdftotext is installed: exit-status: matches expectation: 0
Command: vonk reports its version: exit-status: matches expectation: 0
Command: vonk reports its version: stdout: matches expectation: ["vonk "]


Total Duration: 0.699s
Count: 7, Failed: 0, Skipped: 0
INFO: Deleting container
```

### act

using [act](https://github.com/nektos/act#overview----) for local testing of the written github actions makes my life and commit history a lot easier;

```
Stage  Job ID  Job name  Workflow name           Workflow file               Events
0      tests   tests     CI                      ci.yml                      pull_request,push
0      update  update    docker-hub-description  docker-hub-description.yml  push,workflow_dispatch
0      scan    scan      trivy                   trivy.yml                   workflow_dispatch,push,schedule
1      image   image     CI                      ci.yml                      push,pull_request
```

### release

1. Set `version` in `pyproject.toml` (the only place it lives; `vonk --version` reads it)
   and tag the same semver release; CI fails a tag that differs. CI runs ruff and pytest,
   then the shared `github-workflows` `main.yml` (dgoss, multi-arch
   `linux/amd64,linux/arm64` push to Docker Hub). The repository variable
   `DOCKERHUB_REPOSITORY` is set to `vonk`.
2. Deploy with the job specs, volume and alert rules kept outside this repository: set the
   image tag and run the jobs.
3. Verify with a real call to `/health` and a forced poll, first against a test calendar,
   then the family one.
