# vonk

Turns the monthly vonk PDFs of Scouts en Gidsen Tervant (scoutstervant.be) into events in a
family CalDAV calendar, but only after a human has approved the changes.

## How it works

1. `vonk poll` asks the scoutstervant.be WordPress REST API for new or changed vonk posts,
   downloads the PDFs, converts them with `pdftotext` and lets Claude locate the event spans
   for the configured takken (plus the group-wide key dates from the jaarvonk). Dates, years
   and event identity are derived by plain code, not by the model.
2. The poll result is sent to the `vonk` service, which diffs it against the events it owns
   in the Baikal calendar (UID prefix `vonk-`, domain `@vonk.visibilityspots`).
3. A non-empty diff is announced through Gotify with a link to an internal approve page.
4. Only after approval in the browser does the service write the diff over CalDAV.

### Architecture (one image, two Nomad jobs)

| Job | Type | Role |
|---|---|---|
| `vonk` (`nomad/vonk.hcl`) | service, `count = 1` | Approve page, bearer-authenticated internal API, `/health`, `/metrics`. Sole owner of SQLite (identity, state, extraction cache, tokens) and of every CalDAV write. |
| `vonk-poll` (`nomad/vonk-poll.hcl`) | periodic batch, every 6 hours | Stateless. Extracts and sends results to the service API; always reports back via `/api/poll-report`; exits non-zero on any failure. |

Both are served from the same image: `vonk serve` (default command) and `vonk poll`. The
service is reachable on the internal `https` entrypoint only. Without secrets it starts
degraded: `/health` still answers 200 and `vonk_degraded` is 1.

## Local development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest                  # default markers: no network, no API key, no Baikal
```

`pdftotext` (poppler-utils) must be installed. Opt-in test markers:

| Marker | Needs | Command |
|---|---|---|
| `llm` | `ANTHROPIC_API_KEY`, costs money | `pytest -m llm tests/golden` |
| `caldav` | scratch Baikal calendar `vonk-test` (never `gezin`) | `pytest -m caldav` |
| `net` | read-only calls to the live scoutstervant.be | `pytest -m net` |

Image: `podman build -t vonk:dev .` then `dgoss run vonk:dev` (see `goss.yaml`).

## Configuration

All configuration is environment based; on the cluster it arrives through a Vault template
from `the service's Vault path` (service) and `the poll job's Vault path` (poll job: `api_token`,
`anthropic_api_key`, `takken`), one path per job as the Vault policy requires. Empty values and the consul-template string `<no value>` count
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
| `TAKKEN` | `takken` | poll | takken to process, comma separated |
| `VONK_API_URL` | (in the job) | poll | service base URL, without `/api` |

`BAIKAL_CALENDAR` is the calendar collection name under `calendars/<BAIKAL_USER>/`, not the
display name. For a calendar shared with the `vonk` user that is the share's UUID, which
the Nomad job takes from the Vault key `baikal_gezin_uri`; `baikal_calendar` is only the
display name and is not used for the path.

## Deploy

In short:

1. Tag a semver release; CI runs ruff and pytest, then the shared `github-workflows`
   `main.yml` (dgoss, multi-arch `linux/amd64,linux/arm64` push to Docker Hub). Set the
   repository variable `DOCKERHUB_REPOSITORY` to `vonk`.
2. Seed the image into the zot registry (see the `cluster runbook` skill).
3. Create `<nas-share>/vonk/` on the NAS, register `nomad/vonk-volume.hcl`.
4. Write `the service's Vault path` and `the poll job's Vault path` in Vault; `vonk.example.internal`
   is covered by the internal `*.example.internal` wildcard.
5. Copy `nomad/vonk.hcl` and `nomad/vonk-poll.hcl` to `<cluster-share>/`, set the image
   tag, run `nomad job run`.
6. Paste `nomad/prometheus-vonk.rules.yml` into `infrastructure/prometheus.hcl` as a rule
   template; redeploy Prometheus and check `/api/v1/rules`.
7. Verify with a real call (`curl -H 'Host: vonk.example.internal' .../health`,
   `nomad job periodic force vonk-poll`), first against `vonk-test`, then `gezin`; add the
   Argus entries.

## Privacy

The repository is public. The vonk PDFs contain names and phone numbers of leiding and are
never committed (`tests/fixtures/pdf/` is ignored; `scripts/fetch_fixtures.py` verifies
sources by URL and sha256). Only redacted `pdftotext` output is committed. Child names live
only in Vault (`child_names`) and are never logged or echoed in error messages.
