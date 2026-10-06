"""Every open job that matches your filters, grouped by company.

Run:  python -m crawlers.careers.report [--company NAME ...] [--days N] [--unsent] [--send [--yes]]
  --company  only these companies
  --days N   only jobs first seen in the last N days
  --unsent   only jobs never delivered to Telegram (e.g. openings a company had when first tracked)
  --send     queue the list to Telegram through the outbox instead of printing it
             (over 100 jobs needs --yes; jobs sent this way are marked 'sent')
"""
import argparse, sys, tomllib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .filters import build_filters
from .main import DEFAULT_DB, HERE, acquire_lock
from .store import Store
from . import digest, notify

SEND_CONFIRM_OVER = 100

def main(argv=None):
    ap = argparse.ArgumentParser(description="List open jobs that match your filters.")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--company", nargs="+")
    ap.add_argument("--days", type=float)
    ap.add_argument("--unsent", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    cfg = tomllib.loads(Path(args.config).read_text())
    global_filter, per_company = build_filters(cfg)
    store = Store(args.db)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).isoformat() if args.days else None
    rows = digest.matching(store.open_jobs(companies=args.company, since=since, unsent=args.unsent),
                           global_filter, per_company)
    if not rows:
        print("No matching open jobs.")
        return 0

    if args.send:
        if len(rows) > SEND_CONFIRM_OVER and not args.yes:
            print(f"{len(rows)} jobs (~{len(rows) // 15 + 1} Telegram messages). Re-run with --yes to send, "
                  "or narrow it with --company / --days / --unsent.")
            return 1
        if (lock := acquire_lock(args.db)) is None:  # the poller is mid-run; it flushes the outbox too
            print("The poller is running right now; try again in a couple of minutes.")
            return 1
        n = len(rows)
        for chunk_rows, msg in notify.chunk(f"📋 {n} open matching job{'s' * (n != 1)}",
                                            [(r, notify.format_job(r)) for r in rows]):
            notify.post(store, msg, chunk_rows, dry_run=False, now=now.isoformat())
        sent = notify.flush(store, now.isoformat())
        print(f"Sent {sent} of {n} jobs." + ("" if sent == n else " The rest stay queued; the next poll retries."))
        return 0

    by_company = defaultdict(list)
    for r in rows:
        by_company[r["company"]].append(r)
    for company, jobs in sorted(by_company.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"\n{company} ({len(jobs)})")
        for r in jobs:
            where = r["location"] or ("Remote" if r["remote"] else "")
            mark = "" if r["notify_state"] in ("sent", "queued") else "  [not sent]"
            print(f"  {r['title']}" + (f" | {where}" if where else "") + mark)
            print(f"    {r['url']}")
    unsent = sum(r["notify_state"] not in ("sent", "queued") for r in rows)
    print(f"\n{len(rows)} matching open jobs at {len(by_company)} companies; {unsent} never sent to Telegram.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
