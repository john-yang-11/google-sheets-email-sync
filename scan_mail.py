"""Read application mail and record, per company, when you applied and the outcome.

Writes state/applications.json, which sheet_sync.py reads to fill the applied?
and result columns. Never writes to the sheet itself and never invents a row: a
company it cannot identify simply contributes nothing.

This needs its own credentials. The service account that writes the spreadsheet
has no access to anyone's mail and cannot be given any, so this authenticates as
the mailbox owner with an OAuth refresh token -- see gmail_oauth_setup.py.

Env vars:
  GMAIL_CLIENT_ID      ) the three values gmail_oauth_setup.py prints
  GMAIL_CLIENT_SECRET  )
  GMAIL_REFRESH_TOKEN  )

    python scan_mail.py                # last 30 days
    python scan_mail.py --days 180     # first run, or catching up
    python scan_mail.py --dry-run      # print what it found, write nothing
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

from listings import STATE_DIR
from mailparse import parse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APPLICATIONS = STATE_DIR / "applications.json"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TIMEOUT = 30
DEFAULT_DAYS = 30
MAX_MESSAGES = 400          # a cap so a wide --days can't run for an hour

# Broad on purpose: mailparse decides what is actually an application email, and
# a query that misses a rejection costs more than one that over-fetches.
QUERY = ("(subject:(application OR applying OR applied OR candidacy OR interview"
         " OR position OR status OR interest) OR \"your application\""
         " OR \"thank you for applying\") -in:sent -in:draft")


def token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                           "GMAIL_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"not set: {', '.join(missing)} -- run gmail_oauth_setup.py")
    creds = Credentials(
        None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[SCOPE],
    )
    creds.refresh(Request())
    return creds.token


def api(tok: str, path: str, **params) -> dict:
    r = requests.get(f"{GMAIL_API}/{path}", timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {tok}"}, params=params)
    if not r.ok:
        hint = ""
        if r.status_code == 403 and "insufficient" in r.text.lower():
            hint = ("\n  -> the refresh token lacks gmail.readonly; re-run "
                    "gmail_oauth_setup.py and grant it")
        raise SystemExit(f"Gmail {path} failed ({r.status_code}): {r.text[:300]}{hint}")
    return r.json()


def message_ids(tok: str, days: int) -> list[str]:
    ids, page = [], None
    while len(ids) < MAX_MESSAGES:
        kw = {"q": f"{QUERY} newer_than:{days}d", "maxResults": 100}
        if page:
            kw["pageToken"] = page
        data = api(tok, "messages", **kw)
        ids += [m["id"] for m in data.get("messages", [])]
        page = data.get("nextPageToken")
        if not page:
            break
    return ids[:MAX_MESSAGES]


def fetch(tok: str, mid: str) -> dict | None:
    m = api(tok, f"messages/{mid}", format="metadata",
            metadataHeaders=["From", "Subject", "Date"])
    h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
    # internalDate is epoch ms and is what Gmail sorts by; the Date header can be
    # missing or wrong on bulk mail.
    when = time.strftime("%Y-%m-%d", time.gmtime(int(m.get("internalDate", 0)) / 1000))
    return parse(h.get("from", ""), h.get("subject", ""), m.get("snippet", ""), when)


def merge(records: list[dict]) -> dict:
    """Collapse to one entry per company: earliest applied, and any outcome.

    Earliest, not latest: a company that reposts a req sends a fresh confirmation
    each time, and the first one is when you actually applied. An outcome keeps
    its own date because "rejected on" is a different fact from "applied on".
    """
    out: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: x["date"]):
        e = out.setdefault(r["company"], {"applied": None, "rejected": None,
                                          "interview": None})
        k = r["status"]
        if e.get(k) is None:
            e[k] = r["date"]
    return out


def main() -> None:
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else DEFAULT_DAYS
    dry_run = "--dry-run" in sys.argv

    tok = token()
    ids = message_ids(tok, days)
    print(f"{len(ids)} message(s) to inspect from the last {days} days")

    found, skipped = [], 0
    for i, mid in enumerate(ids, 1):
        rec = fetch(tok, mid)
        if rec:
            found.append(rec)
        else:
            skipped += 1
        if i % 50 == 0:
            print(f"  ...{i}/{len(ids)}")
    print(f"{len(found)} application email(s), {skipped} unrelated")

    merged = merge(found)
    rejected = {k: v for k, v in merged.items() if v["rejected"]}
    print(f"{len(merged)} companies, {len(rejected)} with a rejection")

    if dry_run:
        for co, e in sorted(merged.items()):
            bits = " ".join(f"{k}={v}" for k, v in e.items() if v)
            print(f"  {co:<28} {bits}")
        print("\n[dry-run] applications.json not written")
        return

    # Merge with what is already on disk rather than replacing: a --days window
    # narrower than the last one would otherwise drop every older company.
    prior = {}
    if APPLICATIONS.exists():
        prior = json.loads(APPLICATIONS.read_text(encoding="utf-8"))
    for co, e in merged.items():
        cur = prior.setdefault(co, {"applied": None, "rejected": None, "interview": None})
        for k, v in e.items():
            if v and (cur.get(k) is None or v < cur[k]):
                cur[k] = v
    STATE_DIR.mkdir(exist_ok=True)
    APPLICATIONS.write_text(json.dumps(prior, indent=1, sort_keys=True), encoding="utf-8")
    print(f"applications.json now holds {len(prior)} companies")


if __name__ == "__main__":
    main()
