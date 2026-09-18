# Google Sheets email sync

Keeps the **main internship companies** spreadsheet on johnyang1032@gmail.com
filling itself in, so a tracker that used to be retyped by hand from Discord
alerts stays current on its own.

Once a day (13:10 UTC, ~9am ET) GitHub Actions scans the two community
internship repos for newly-posted Summer-2027 US software internships, then adds
the ones the sheet doesn't already track.

```
scan_listings.py  -> state/found_log.json -> sheet_sync.py -> the spreadsheet
```

Companion to [job-alert](https://github.com/john-yang-11/job-alert), which
watches the same feeds and sends the Discord/Poke alerts. That repo is untouched
by this one; `listings.py` is a copy of the parts of its `check.py` that decide
what counts as a Summer-2027 US software internship.

## What earns a row

| found | sheet already has | result |
| --- | --- | --- |
| Figma, "SWE Intern" | no Figma row | new row, `Figma` in col A |
| Amazon, "SDE Intern" | `Amazon` row | nothing — that row covers it |
| Amazon, "SDE Intern, Robotics" | `Amazon` row | new row, `Amazon` + role in col B |
| Amazon, "Propel Program" | `Amazon Propel program` row | nothing — already tracked |

"Distinct role or the same generic req" is `role_key()`: strip every word that
only says *software internship* (`intern`, `2027`, `summer`, `engineer`, ...)
plus the company's own name, and whatever survives is the distinguishing part.
Nothing surviving means the company's own row already covers it.

Rows are **appended at the bottom**, on the next empty line — the sheet is only
sorted down to a point, and below that it's chronological, which is where recent
finds belong. Only columns A, B and *Expected 2027 Apps Open* are written; every
other column is hand-maintained and left alone.

The target row is computed from the fetched rows and written with
`values.update`, **not** `values.append`. Append re-derives the "table" from the
sheet's contents, and given `A:Q` on this sheet it anchored on column Q and put a
30-row batch into `Q:AG`. The last used row is already known — nothing is left to
infer.

Only **software** roles reach the sheet (`is_swe_intern`). The feeds match on
company name alone, so a watchlist company's marketing, PM and electrical
postings match too; they outnumbered the software ones two to one.

### Two things stop it running away with the sheet

* `MAX_PER_COMPANY` / `MAX_PER_RUN` — Amazon alone posts dozens of team-specific
  reqs a cycle and each is a "distinct role". Anything over the cap is *not*
  marked pushed, so it returns tomorrow rather than being dropped; a burst lands
  over several days.
* `state/sheet_pushed.json` — keys already written. The sheet is the main dedup,
  but it can't be the only one, or deleting a row you rejected would invite it
  straight back next morning.

Both state files are committed by the workflow. Losing them means re-adding rows
the sheet already has.

## Setup

The Drive/Sheets connector can read a sheet but not write cells, so this uses a
**service account**: no OAuth consent screen, no browser step, no token that
expires after 7 days.

1. console.cloud.google.com → new project. Check it says **Organization: "No
   organization"** — a Workspace org (e.g. a university account) blocks service
   account key creation outright.
2. APIs & Services → Library → enable **Google Sheets API**.
3. IAM & Admin → Service Accounts → Create. No roles needed; project roles govern
   Google Cloud, and this only ever touches one sheet.
4. Keys → Add key → Create new key → **JSON**. Downloads once.
5. In the sheet: Share → paste the account's `client_email` → **Editor**.
   Skipping this is the usual first failure; the sync prints the fix on a 403.
6. Repo Settings → Secrets and variables → Actions → add
   `GOOGLE_SERVICE_ACCOUNT_JSON`, the entire file. Optionally
   `WATCHLIST_CSV_URL` (a published-to-web CSV of companies to watch; falls back
   to `watchlist.txt`).

The sheet id is pinned in the workflow rather than stored as a secret — it's just
the id from the URL of a sheet only the owner can open. `sheet_sync.py` itself
refuses to run without one, with no fallback: two near-identical copies of this
sheet exist, and a default in the script would mean one missing value quietly
files a month of rows into the stale one.

## Running it by hand

```bash
python scan_listings.py --days 21 --dry-run   # what a 3-week window would log
python scan_listings.py --days 21             # log it
python sheet_sync.py --dry-run                # which rows that warrants
python sheet_sync.py --no-cap                 # write them all in one run
```

`--no-cap` is for draining a backfill window. Never use it on the schedule; the
caps are what stop one busy day burying the sheet.

## What this does NOT do

The `applied?` / `when` / `result` columns are filled from Gmail — application
confirmations and rejection emails — and that **cannot run here**. The service
account has no access to anyone's mail, and giving it some means separate Gmail
OAuth. Those columns are updated by hand.
