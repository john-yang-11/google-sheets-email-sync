"""Scan the community repos for new Summer-2027 US software internships.

Runs daily ahead of sheet_sync.py, and doubles as the backfill tool: the window
is just a parameter. The feeds carry date_posted, so "what appeared since
yesterday" and "what appeared in the last three weeks" are the same query with a
different --days.

Applies exactly the filters the watcher alerts on -- watchlist, target season, US
location, active+visible -- plus is_swe_intern, because those feeds match on
company name alone and a watchlist company's marketing and PM postings are noise
in a spreadsheet even when they are a fine Discord ping.

Writes only to the found log. Whether a row is actually warranted is
sheet_sync.py's decision, made against the spreadsheet as it stands. Re-running
is safe: entries already logged are skipped by content key.

    python scan_listings.py              # last 2 days (what the workflow runs)
    python scan_listings.py --days 21    # backfill three weeks
    python scan_listings.py --dry-run    # say what it would add, write nothing
"""

import json
import sys
import time

import requests

from listings import (FOUND_LOG, SOURCES, STATE_DIR, content_key, is_swe_intern,
                      is_target_season, is_us_location, load_watchlist, matches)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_DAYS = 2      # the workflow runs daily; 2 gives an overlap


def arg(flag: str, default: int) -> int:
    return int(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


def main() -> None:
    days = arg("--days", DEFAULT_DAYS)
    dry_run = "--dry-run" in sys.argv
    cutoff = time.time() - days * 86400

    keywords = load_watchlist()
    if not keywords:
        raise SystemExit("watchlist is empty -- nothing would match")
    print(f"{len(keywords)} watchlist keywords, window = last {days} days")

    existing = []
    if FOUND_LOG.exists():
        existing = json.loads(FOUND_LOG.read_text(encoding="utf-8"))
    # Re-running must not double up. The found log stores company/title, which is
    # exactly what content_key hashes, so old entries can be keyed the same way.
    have = {content_key({"company_name": e.get("company", ""),
                         "title": e.get("title", "")}) for e in existing}

    fresh: list[dict] = []
    for name, url in SOURCES:
        try:
            data = requests.get(url, timeout=90).json()
        except Exception as e:
            print(f"WARNING: {name} fetch failed ({e}), skipping", file=sys.stderr)
            continue
        n = 0
        for l in data:
            if not (l.get("active") and l.get("is_visible", True)):
                continue
            if (l.get("date_posted") or 0) < cutoff:
                continue
            if not matches(l, keywords) or not is_target_season(l):
                continue
            # Same rule the live logger uses: company-name matching alone lets a
            # watchlist company's marketing and PM postings through, which is
            # noise in a spreadsheet even when it is a fine Discord ping.
            if not is_swe_intern(l.get("title", "")):
                continue
            if not is_us_location(l.get("locations") or []):
                continue
            key = content_key(l)
            if key in have:
                continue
            have.add(key)
            fresh.append({
                "company": l.get("company_name", ""),
                "title": l.get("title", ""),
                "url": l.get("url", ""),
                "source": "scan",
                # The real post date, not now: the found log is a record of when a
                # listing appeared, and stamping a three-week-old post with today
                # would throw that away for every row this adds.
                "found_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                          time.gmtime(l.get("date_posted") or 0)),
            })
            n += 1
        print(f"  {name}: {n} match(es) in window")

    fresh.sort(key=lambda e: (e["found_at"], e["company"].lower()))
    companies = {e["company"] for e in fresh}
    print(f"\n{len(fresh)} new log entr(ies) across {len(companies)} companies")
    if dry_run:
        for e in fresh:
            print(f"  {e['found_at'][:10]}  {e['company']:<24} {e['title'][:56]}")
        print("\n[dry-run] found_log.json not written")
        return

    STATE_DIR.mkdir(exist_ok=True)
    FOUND_LOG.write_text(json.dumps(existing + fresh, indent=1), encoding="utf-8")
    print(f"found_log.json now holds {len(existing) + len(fresh)} entr(ies)")
    print("run sheet_sync.py --dry-run to see which of them warrant a row")


if __name__ == "__main__":
    main()
