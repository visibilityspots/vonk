# Vonk → calendar

## Goal

Turn the *vonk* of Scouts en Gidsen Tervant (https://scoutstervant.be/) into a calendar I can
use on my phone, and that other parents can subscribe to.

A *vonk* is the written activity calendar of the scouting movement. Today it is a PDF, which
you open, read, and copy into a calendar by hand. This tool automates that copy.

## What the source looks like (checked 2026-10-09)

The site is WordPress. The public REST API answers without authentication, so discovery
needs no browser scraping:

- `GET /wp-json/wp/v2/posts?search=vonk` lists the vonk posts.
- `GET /wp-json/wp/v2/categories` maps each post to a *tak* (age group). Per-tak categories
  exist: `Vonk kapoenen` (9), `Vonk kabouters` (10), `Vonk welpen` (11),
  `Vonk jong gidsen` (12), `Vonk jong verkenners` (13), `Vonk gidsen` (14),
  `Vonk verkenners` (15), `Vonk jins` (16). Some vonks also land in the `<tak> Leiding`
  categories instead, so the category is a hint, not a guarantee.
- Each post body is only a PDF embed (`wp-block-pdfemb-pdf-embedder-viewer`) that links to
  `/wp-content/uploads/YYYY/MM/<file>.pdf`.

Two kinds of PDF:

| Kind | Example | Format |
|------|---------|--------|
| **Jaarvonk** (year overview) | `Jaarvonk-26-27.pdf`, 21 pages, Word | Table-like "Jaarplanning" on pages 6–8: weekday, date, time, remark. Plus camp dates and camp sign-up date in prose. Group-wide; end time differs per tak. |
| **Monthly vonk** (per tak) | `vonk-<tak>-oktober-2026.pdf`, Canva / PDFium, 1 page | Free-form designed page: dates as `11/10`, `Zondag 11 oktober`, `3/10-4/10`; times as `14u-17u`, `18u - ....`; description text with emoji. No year. Layout differs per tak and per month. |

The PDFs have a real text layer (`pdftotext -layout` works); no OCR needed so far.

### Data quality — the source contradicts itself

- Jaarvonk says "Zaterdag 18 oktober" and "Zondag 18 oktober". 18 October 2026 is a Sunday.
- Jaarvonk lists an activity on Sunday 18/10; the monthly vonk of that tak says
  Saturday 17/10.
- Jaarvonk itself says: changes are possible, check the monthly vonk.

Consequence: the **monthly vonk wins** over the jaarvonk for the same tak and date. The tool
must flag weekday/date mismatches instead of guessing silently.

## Output options

1. **Public ICS feed** (per tak, `vonk-<tak>.ics`) — any parent subscribes from iOS, Android
   or Google Calendar with one URL. Needs a public HTTP endpoint.
2. **Write into our `gezin` Baikal calendar** via CalDAV — events show up next to the rest of
   the family agenda, on every device already synced to Baikal.
3. **Both** — one parse, two sinks. The ICS feed for other parents, CalDAV for us.

Preference: **option 3**, built as one pipeline with pluggable sinks. Start with the ICS feed:
it is read-only, easy to verify, and Baikal can later consume the same events.

## Proposed pipeline

```
WP REST API ──> new/changed vonk posts ──> download PDF ──> pdftotext
     ──> extract events (date, start, end, title, description, tak)
     ──> validate (weekday matches date, year inferred from school year Sept–Aug)
     ──> merge (monthly vonk overrides jaarvonk)
     ──> sinks: ICS file(s)  |  CalDAV PUT to Baikal "gezin"
```

Key design points:

- **Extraction**: the monthly vonks are free-form Canva layouts. Regex will break every month.
  Use an LLM (Claude) with a strict JSON schema on the `pdftotext` output, and keep a regex
  parser for the jaarvonk table as cross-check.
- **Stable UIDs**: derive each event UID from `tak + date + slot`, so a re-run updates the
  event instead of creating a duplicate, and a change in the vonk becomes an update on
  every phone.
- **Year inference**: scout year runs September → August; `3/10` in a 2026-2027 vonk is
  2026-10-03, `7/2` is 2027-02-07.
- **Open-ended times** (`14u - …`, `18u - ....`, `Avond`): use a default duration per tak and
  put the original text in the description.
- **Cancellations** ("Geen scouts!"): emit as an all-day event, clearly titled, not dropped —
  a parent wants to see that there is *no* scouts that day.
- **Idempotent, scheduled run**: poll the REST API (e.g. daily), process only posts whose
  `modified` date changed, keep processed PDFs + extracted JSON as an audit trail.

## Where it runs

Candidate: a periodic batch job on the Nomad cluster, ICS served through Traefik, Baikal
credentials from Vault. Alternative: a GitHub Action that commits the `.ics` to GitHub Pages
(no homelab exposure needed for other parents). To decide.

## How we build it (oh-my-claudecode)

1. `deep-interview` — settle the open questions below.
2. `ralplan` / `plan` — consensus plan with acceptance criteria.
3. `execute` — implementation via the `executor` agent.
4. `verify` / `review` — separate pass: run against the *real* October 2026 vonks and the
   jaarvonk, compare each event against the PDF by hand, subscribe on a real phone.

Playwright MCP stays available as fallback (e.g. if the site moves vonks out of the REST
API or behind JavaScript), but the REST API makes it unnecessary for now.

## Open questions

- Which takken to follow: only the configured ones for the family calendar, or all takken
  for the public feed?
- Public ICS: hosted where (homelab via Traefik, or GitHub Pages)? Leiding names and phone
  numbers in the jaarvonk must **not** end up in a public feed.
- Group-wide events (Powell party, kampmenu, minimarkt, kamp) — separate `groep.ics`, or
  merged into each tak feed?
- Which LLM runs the extraction, and is a human review step needed before an event goes
  public (at least while the extractor is new)?
- How to handle a contradiction (jaarvonk vs. monthly vonk, or a wrong weekday): publish the
  monthly version with a warning, or hold the event back?
- Baikal: confirm the `gezin` calendar URL and whether a dedicated CalDAV user with write
  access is acceptable, or the tool writes to a separate `scouts` calendar that we subscribe
  to instead (keeps the permission boundary small).
