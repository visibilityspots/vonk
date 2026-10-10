# expected.json — DRAFT, to be reviewed by Jan

Status: **DRAFT**. Built by hand on 2026-10-10 from the redacted text in `text/*.txt`. Nobody
has signed it off yet. Plan step 6 says Jan reviews it once. Until then, a failing golden
test can mean the draft is wrong, not the extractor.

## What it holds

For each fixture, `expected.json` lists the events in date order, after extraction **and**
normalisation (`vonk.normalize.normalize`). It holds no child names.

The golden test (`tests/golden/test_extract_golden.py`, marker `llm`) compares the events
field by field:

- `date`, `end_date`, `start`, `end`, `all_day` and `end_unknown` must match exactly.
- The normalised `title_activity` must contain one of `title_keys` (case-insensitive).
  The model chooses which verbatim words name the activity: "bosspel" and "Vandaag trekken
  we het bos in!" are both valid. An exact title match would therefore fail on harmless
  variation.
- `title_activity` is the reference title from the recorded model output in `llm/*.json`.
  The offline unit test `tests/unit/test_extract.py` checks that output against this file
  exactly.

## Choices Jan should confirm

1. **Kabouters**: the passing mention "Powell (3/10) & Powell After Work (2/10)" belongs
   to the 3/10-4/10 item and is not a separate event. Those dates come from the jaarvonk
   (scope `groep`).
2. **Jaarvonk key dates (14)**: every row of the planning table that names an activity,
   plus the kamp from the text (12–22 July 2027, all-day).
   - **Left out**: weekly rows that only give hours, the voorkamp (leiding only), the
     nakamp (givers and leiding only), and "Juni geen scouts" (no specific date).
   - **Included although not for everyone**: the "Jonggiver en Giver trophy" (18/10).
     The plan uses it as its `possible conflict` example against the jeevees vonk (17/10).
3. **Middernachtmis** (`24u` on "Woensdag 24 december"):
   - Normalised to 2026-12-25 00:00 with an unknown end (default +3h).
   - 24 December 2026 is a Thursday, so the event carries a weekday-mismatch warning.
     That is correct: the source text is wrong.
4. **"Geen scouts!" rows** (Powell party 3/10, Kampplanning 20–21/2) are recorded as
   cancellations. The normaliser then titles them `Geen scouts (<activity>)`.
5. **Open-ended times** ("10u - ...", "18u - ....", "14u - …", a lone "19u") get the
   default end of start + 3h, with `end_unknown: true`.
6. **Powell Quiz** ("Avond", no hour) is an all-day event.
