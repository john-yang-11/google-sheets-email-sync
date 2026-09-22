"""Append newly-found internships to the "main internship companies" Google Sheet.

Runs once a day (see .github/workflows/sync-sheet.yml). Reads the matches that
check.py and check_companies.py logged to state/found_log.json since the last
sync, and adds a row for anything the sheet doesn't already track:

  * a company with no row at all      -> new row, company in column A
  * a company that HAS a row, but the
    listing names a distinct program
    (Propel, STEP, Explore, Ignite..) -> new row, company in A, program in B

A plain "Software Engineer Intern" at a company already on the sheet adds
nothing -- that row exists, and re-adding it every time a req is reposted would
bury the hand-entered columns. See role_key() for how "distinct" is decided.

Rows are appended at the bottom, on the next empty line. The sheet is only sorted
down to a point -- below that it is chronological, which is where recent finds
belong and where you are actually working. Only columns A, B and the
"Expected ... Apps Open" column are written; every other column is yours and is
left empty for you to fill in.

Env vars (both required to actually write; without them it prints a dry run):
  GOOGLE_SERVICE_ACCOUNT_JSON  the whole downloaded service-account key file
  SHEET_ID                     spreadsheet id, from its URL (no default)

The sheet must be shared with the service account's client_email as Editor --
a service account has its own Drive and sees nothing of yours until you do.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from listings import FOUND_LOG, STATE_DIR

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Keys ("company|role") already pushed. The sheet itself is the main dedup, but
# it can't be the only one: if you delete a row you rejected, the next sync would
# helpfully add it straight back. This file is what makes a deletion stick.
PUSHED_FILE = STATE_DIR / "sheet_pushed.json"

# No default on purpose. Two near-identical copies of this sheet exist -- the
# original "Internships companies" on johnya@umich.edu and "main internship
# companies" on johnyang1032@gmail -- and a default would mean a missing
# SHEET_ID quietly files a month of rows into the stale one. Fail instead.

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TIMEOUT = 30

# The column holding the apps-open note is found by header text, not hardcoded to
# Q: adding one column to the sheet would otherwise start writing notes into
# whatever now sits in Q, silently, on a file with a year of hand-entered data.
NOTE_HEADER = "apps open"
# Where the mail-derived facts go. Fixed rather than looked up by header text:
# the "appilied?" header is misspelled in the sheet and "result" is too common a
# word to match on safely, so the positions are pinned and asserted instead.
APPLIED_COL = 2          # C, headed "appilied?"
RESULT_COL = 5           # F, headed "result"
APPLICATIONS = STATE_DIR / "applications.json"
# Amazon alone posts dozens of team-specific SWE intern reqs a cycle, and each
# one is a "more specific role" by the rule above -- left uncapped, one busy
# day would bury the sheet in Amazon rows. Anything over the cap is simply not
# marked as pushed, so it is reconsidered tomorrow rather than dropped: a burst
# arrives over several days instead of all at once.
MAX_PER_COMPANY = 3
MAX_PER_RUN = 25
HEADER_SCAN_ROWS = 3          # how far down to look for the header row

# Words that say "this is a software internship" rather than naming a particular
# program. Strip them and whatever survives is the distinguishing part of the
# title -- "Propel", "Ignite", "STEP", "Quantum", "Hardware". Nothing surviving
# means it's the company's generic SWE req, which the company's own row covers.
BOILERPLATE = re.compile(
    r"\b(?:20\d\d|summer|fall|autumn|winter|spring|intern|interns|internship|"
    r"internships|co-?op|software|engineer|engineering|developer|development|"
    r"swe|sde|sw|university|graduate|undergraduate|undergrad|student|students|"
    r"new\s+grad|program|programs|technology|technologies|technical|tech|"
    r"full[-\s]?stack|back[-\s]?end|front[-\s]?end|remote|hybrid|onsite|"
    r"early\s+career|career|careers|opportunity|opportunities|position|role|"
    r"us|usa|united\s+states|i+|ii+|level\s*\d+|l\d+)\b",
    re.IGNORECASE,
)


def norm_words(text: str) -> list[str]:
    """Lowercase alphanumeric word list -- the unit both dedup checks compare on."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def _has_run(haystack: list[str], needle: list[str]) -> bool:
    """True if `needle` appears as a consecutive word-run inside `haystack`."""
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i:i + len(needle)] == needle
               for i in range(len(haystack) - len(needle) + 1))


def company_matches(company: str, row_cell: str, row_text: str) -> bool:
    """True if a listing's company is the company on this sheet row.

    Both directions matter, and checking only one was a real bug. A feed name can
    be longer than the cell ("Dell Technologies" vs your "dell", "Epic Games" vs
    "epic", "PricewaterhouseCoopers (PwC)" vs "pwc") or shorter than the row text
    ("Amazon" vs the "Amazon Propel program" row). Matching is on whole word-runs
    so "clay" does not match "Clayton" either way.

    The reverse direction deliberately compares against the company cell alone,
    not the row text: column B holds free-form role notes, and letting those match
    backwards into a company name produced nonsense collisions.
    """
    cw, cell_w, text_w = norm_words(company), norm_words(row_cell), norm_words(row_text)
    if not cw:
        return False
    return _has_run(text_w, cw) or _has_run(cw, cell_w)


def pick_company_row(company: str, rows: list[list[str]]) -> int | None:
    """The one row that unambiguously *is* this company, or None.

    For writing company-level facts (applied on, rejected) onto an existing row.
    A row only qualifies if column A names the company and nothing more, and
    column B is empty -- both columns are used here to name a specific programme,
    and a programme row is a different application than a company-level email.

    Column A may be shorter than the feed's name ("dell" is the row for "Dell
    Technologies") but never longer: "Amazon Propel program" and "thrive by
    duolingo" contain the company name and are emphatically not it. Getting this
    backwards filed a Jr. Developer Program application onto the Propel row.

    Returns a 0-based index into `rows`.
    """
    cw = norm_words(company)
    hits = []
    for i, r in enumerate(rows):
        cell_a = (r[0] if r else "") or ""
        if not cell_a.strip() or ((r[1] if len(r) > 1 else "") or "").strip():
            continue
        aw = norm_words(cell_a)
        if aw == cw or _has_run(cw, aw):      # row name equal to, or a short form of
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


def lookup_application(company: str, apps: dict) -> dict | None:
    """The mail record for this company, matched the same way sheet rows are.

    Exact normalised name first, then a whole-word-run match in either direction,
    so "Dell Technologies" from a job feed finds a "Dell" mail record and vice
    versa. Ambiguous matches return nothing rather than guessing.
    """
    if not company or not apps:
        return None
    want = norm_words(company)
    if not want:
        return None
    for name, entry in apps.items():
        if norm_words(name) == want:
            return entry
    hits = [e for name, e in apps.items()
            if _has_run(norm_words(name), want) or _has_run(want, norm_words(name))]
    return hits[0] if len(hits) == 1 else None


def role_key(title: str, company: str) -> str:
    """The distinguishing part of a role title, or "" if it's a generic SWE req.

    The company's own name is stripped too, so "Amazon Software Dev Engineer
    Intern" reduces to nothing (generic, already covered by the Amazon row) while
    "Amazon Propel Program" reduces to "propel" (its own row, as you track it).
    """
    stripped = BOILERPLATE.sub(" ", title or "")
    words = [w for w in norm_words(stripped) if w not in set(norm_words(company))]
    return " ".join(words)


def pretty_role(title: str) -> str:
    """Column B text: the original title with the year and season trimmed off, so
    the cell reads "Propel Program" rather than "2027 Propel Program (Summer)"."""
    s = re.sub(r"\b(?:20\d\d|summer|fall|autumn|winter|spring)\b", " ", title or "", flags=re.I)
    s = re.sub(r"\(\s*\)", " ", s)                       # parentheses emptied by the above
    s = re.sub(r"\s{2,}", " ", s)
    # Cutting the season out of "Intern - Summer or Fall 2027 - Platform" leaves
    # debris: a stranded conjunction, or separators with nothing between them.
    s = re.sub(r"(?:\s*[-|/,]\s*){2,}", " - ", s)
    s = re.sub(r"\s*[-|/,]\s*\b(?:or|and)\b\s*(?=[-|/,]|$)", "", s, flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" ,-|/")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        print(f"WARNING: {path.name} unreadable, treating as empty", file=sys.stderr)
        return default


def credentials() -> tuple[str, str]:
    """Return (access token, client_email) from the service-account key.

    google-auth is a dependency purely because this needs an RS256 signature
    over the JWT assertion, and requests cannot sign. The key is the whole
    downloaded JSON file pasted into one secret.
    """
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    try:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    except json.JSONDecodeError:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON -- paste "
                         "the whole key file, opening and closing braces included")
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token, info.get("client_email", "?")


def api(token: str, method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{SHEETS_API}/{path}", timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {token}"}, **kw)
    if not r.ok:
        hint = ""
        # Two very different problems both answer 403, and guessing wrong costs a
        # round of hunting: a disabled API says so in the body, everything else at
        # 403/404 is the sheet not being shared with the account.
        if "has not been used in project" in r.text or "is disabled" in r.text:
            hint = ("\n  -> enable the Google Sheets API in this project, then "
                    "retry; the URL in the message above goes straight there")
        elif r.status_code in (403, 404):
            hint = ("\n  -> share the sheet with the service account's client_email "
                    "as Editor; a service account sees nothing of yours until you do")
        raise SystemExit(f"Sheets {method} {path.split('?')[0]} "
                         f"failed ({r.status_code}): {r.text[:300]}{hint}")
    return r.json()


def find_note_column(rows: list[list[str]]) -> int | None:
    for row in rows[:HEADER_SCAN_ROWS]:
        for i, cell in enumerate(row):
            if NOTE_HEADER in (cell or "").lower():
                return i
    return None


def a1_col(index: int) -> str:
    """0-based column index -> A1 letters (0 -> A, 16 -> Q, 26 -> AA)."""
    letters = ""
    while True:
        index, rem = divmod(index, 26)
        letters = chr(ord("A") + rem) + letters
        if index == 0:
            break
        index -= 1
    return letters


def plan_rows(entries: list[dict], rows: list[list[str]], pushed: set,
              max_per_company: int = MAX_PER_COMPANY,
              max_per_run: int = MAX_PER_RUN) -> list[dict]:
    """Decide which found listings need a row. Returns newest-first-wins dicts of
    {company, program, key}; `rows` is the sheet as-is, headers included.

    The caps are parameters so a one-off backfill (--no-cap) can drain a whole
    window in one run; the daily job always uses the defaults."""
    # Existing rows, as (company cell, company + program text) pairs.
    existing = [((r[0] if r else "").strip(),
                 " ".join((r[:2] if len(r) > 1 else r[:1])))
                for r in rows if r and (r[0] or "").strip()]

    planned: list[dict] = []
    seen_keys = set(pushed)
    per_company: dict[str, int] = {}
    introduced: set[str] = set()
    capped = 0
    for e in entries:
        company = (e.get("company") or "").strip()
        title = (e.get("title") or "").strip()
        if not company:
            continue
        rk = role_key(title, company)
        key = f"{' '.join(norm_words(company))}|{rk}"
        if key in seen_keys:
            continue
        cname = " ".join(norm_words(company))
        if len(planned) >= max_per_run or per_company.get(cname, 0) >= max_per_company:
            capped += 1
            continue                     # deliberately not marked seen

        matching = [text for cell, text in existing
                    if company_matches(company, cell, text)]
        if matching:
            if not rk:
                continue                     # generic req, company row covers it
            # The program might already be tracked -- "Amazon Propel program" is
            # its own row, so a Propel posting must not add a second one.
            if any(all(w in norm_words(text) for w in rk.split()) for text in matching):
                continue
            planned.append({"company": company, "program": pretty_role(title), "key": key})
        elif cname in introduced:
            # Already being introduced by an earlier listing in this same batch.
            # HP IQ arrived with six open roles; without this it earned six rows
            # at once. Leave the rest unmarked so they can come back as ordinary
            # role variants later, once the company actually has a row.
            continue
        else:
            # No row for this company at all. The program name, if any, goes in B
            # the same way you already do it for "Google" + "google step".
            planned.append({"company": company,
                            "program": pretty_role(title) if rk else "",
                            "key": key})
            introduced.add(cname)
        seen_keys.add(key)
        per_company[cname] = per_company.get(cname, 0) + 1
        # A company can arrive twice in one batch (two boards, two roles); the
        # second one has to see the first, so treat the plan as part of the sheet.
        existing.append((planned[-1]["company"],
                         f"{planned[-1]['company']} {planned[-1]['program']}"))
    if capped:
        print(f"{capped} row(s) held back by the per-run/per-company cap; "
              f"they will be reconsidered on the next sync")
    return planned


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    have_key = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        raise SystemExit("SHEET_ID is not set -- it is the long id in the sheet URL, "
                         "docs.google.com/spreadsheets/d/<THIS>/edit")

    entries = load_json(FOUND_LOG, [])
    if not entries:
        print("found_log.json is empty -- nothing found since the last sync")
        return
    pushed = set(load_json(PUSHED_FILE, []))

    # Without credentials there is no sheet to compare against, so the most this
    # can honestly do is show what was found and how each title parses.
    if not have_key:
        print("[offline] GOOGLE_SERVICE_ACCOUNT_JSON not set -- reading and writing nothing.")
        print(f"[offline] {len(entries)} logged match(es), {len(pushed)} already pushed")
        for e in entries[-20:]:
            rk = role_key(e.get("title", ""), e.get("company", ""))
            print(f"  {e.get('company', '?'):<26.26} {e.get('title', '?')[:56]:<58} "
                  f"role={rk or '(generic)'}")
        return

    token, acting_as = credentials()
    print(f"acting as {acting_as}")
    meta = api(token, "GET", f"{sheet_id}?fields=sheets.properties(title)")
    tab = meta["sheets"][0]["properties"]["title"]

    rng = quote(f"{tab}!A:ZZ", safe="")
    rows = api(token, "GET", f"{sheet_id}/values/{rng}").get("values", [])
    note_col = find_note_column(rows)
    if note_col is None:
        print(f"WARNING: no '{NOTE_HEADER}' header found in the first "
              f"{HEADER_SCAN_ROWS} rows; leaving that column blank", file=sys.stderr)

    # --no-cap is for draining a backfill window in one run. Never use it on the
    # schedule: the caps are what stop one busy day burying the sheet.
    caps = {"max_per_company": 10 ** 6, "max_per_run": 10 ** 6} \
        if "--no-cap" in sys.argv else {}
    planned = plan_rows(entries, rows, pushed, **caps)
    print(f"{len(entries)} logged match(es), {len(pushed)} already pushed, "
          f"{len(planned)} new row(s) for '{tab}'")
    if not planned:
        PUSHED_FILE.write_text(json.dumps(sorted(pushed), indent=1), encoding="utf-8")
        return

    today = datetime.now(timezone.utc)
    note = f"OPEN NOW (found {today.month}/{today.day})"
    width = max(2, max(n for n in (note_col, APPLIED_COL, RESULT_COL)
                       if n is not None) + 1)
    apps = load_json(APPLICATIONS, {})

    def as_row(p: dict) -> list[str]:
        """One sheet row: company, role, the note, and what the mail says.

        If scan_mail.py saw an application confirmation from this company, the
        date it arrived goes in `appilied?` and the outcome in `result`, so a row
        for a job you already applied to does not land looking untouched.
        """
        cells = [""] * width
        cells[0], cells[1] = p["company"], p["program"]
        if note_col is not None:
            cells[note_col] = note
        entry = lookup_application(p["company"], apps)
        if entry:
            when = entry.get("applied") or entry.get("rejected")
            if when:
                y, m, d = when.split("-")
                cells[APPLIED_COL] = f"{int(m)}/{int(d)}"
            if entry.get("rejected"):
                cells[RESULT_COL] = "rejected"
            elif entry.get("applied"):
                cells[RESULT_COL] = "applied"
        return cells

    if dry_run:
        for p in planned:
            print(f"  [dry-run] would append: {p['company']}"
                  + (f" / {p['program']}" if p["program"] else ""))
        return

    # Address the target rows outright rather than letting the API infer them.
    # values.append re-derives the "table" from the sheet's contents, and given
    # A:Q on this sheet it anchored on column Q and wrote a 30-row batch into
    # Q:AG. `rows` already tells us the last used row, so there is nothing to
    # infer: write at the next line down, in the order the listings were found.
    first = len(rows) + 1
    last = first + len(planned) - 1
    rng = quote(f"{tab}!A{first}:{a1_col(width - 1)}{last}", safe="")
    api(token, "PUT", f"{sheet_id}/values/{rng}?valueInputOption=USER_ENTERED",
        json={"values": [as_row(p) for p in planned]})
    for p in planned:
        print(f"  {p['company']}" + (f" / {p['program']}" if p["program"] else ""))
        pushed.add(p["key"])
    where = f"{tab}!A{first}:A{last}"

    STATE_DIR.mkdir(exist_ok=True)
    PUSHED_FILE.write_text(json.dumps(sorted(pushed), indent=1), encoding="utf-8")
    print(f"{len(planned)} row(s) appended at {where}")


if __name__ == "__main__":
    main()
