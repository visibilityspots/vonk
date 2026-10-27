# vonk

[![trivy](https://github.com/visibilityspots/vonk/actions/workflows/trivy.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/trivy.yml)
[![docker-hub-description](https://github.com/visibilityspots/vonk/actions/workflows/docker-hub-description.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/docker-hub-description.yml)
[![build status](https://github.com/visibilityspots/vonk/actions/workflows/ci.yml/badge.svg)](https://github.com/visibilityspots/vonk/actions/workflows/ci.yml)
[![gitHub release](https://img.shields.io/github/v/release/visibilityspots/vonk)](https://github.com/visibilityspots/vonk/releases)
[![docker image size](https://img.shields.io/docker/image-size/visibilityspots/vonk/latest)](https://hub.docker.com/r/visibilityspots/vonk)
[![docker pulls](https://img.shields.io/docker/pulls/visibilityspots/vonk.svg)](https://hub.docker.com/r/visibilityspots/vonk/)

Turns the monthly vonk PDFs of Scouts en Gidsen Tervant (scoutstervant.be) into events in a
family CalDAV calendar, but only after a human has approved the changes. The same events,
for every tak, are also published as a public ICS feed that other parents can subscribe to.

## run

One image, two commands: `vonk serve` (the default) and `vonk poll`.

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
4. Only after approval in the browser does the service write the diff over CalDAV.
5. After every poll report the service renders the public feed from the stored events and
   pushes the files that changed to GitHub Pages (see [Public ICS feed](#public-ics-feed)).

### Architecture

| Job | Type | Role |
|---|---|---|
| `vonk` | service, `count = 1` | Approve page, bearer-authenticated internal API, `/health`, `/metrics`. Sole owner of SQLite (identity, state, extraction cache, tokens) and of every CalDAV write. |
| `vonk-poll` | periodic batch, every 6 hours | Stateless. Extracts and sends results to the service API; always reports back via `/api/poll-report`; exits non-zero on any failure. |

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
| `VONK_API_URL` | (in the job) | poll | service base URL, without `/api` |

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
