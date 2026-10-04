"""Career-page poller: fetch every company's open jobs, diff against SQLite, ping Telegram
about new jobs that pass the filters. One-shot; scheduled by systemd/poll@careers.timer.

Run:  python -m crawlers.careers.main [--dry-run] [--only NAME ...] [--config PATH] [--db PATH]
--dry-run prints messages instead of sending and uses an in-memory DB unless --db is given.
"""
import argparse, asyncio, logging, os, tomllib
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import httpx
from common import config
from common.browser import browser_context
from common.output import JsonlSink, setup_logging
from .fetchers import HTTP_FETCHERS, BROWSER_FETCHERS
from .filters import build_filters
from .store import Store
from . import health, notify

NAME = "careers"
log = setup_logging(NAME)
logging.getLogger("httpx").setLevel(logging.WARNING)
HERE = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.getenv("CAREERS_DB", str(Path.home() / "data" / "state" / "careers.db")))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
BASELINE_PREVIEW = 5  # matching jobs to show when a company is first tracked

async def fetch_all(companies: list[dict], concurrency: int) -> list[tuple[dict, object]]:
    """Returns [(company_cfg, FetchResult | Exception)]; one company failing never stops the rest."""
    sem = asyncio.Semaphore(concurrency)
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": UA, "Accept": "application/json"}))
        ctx, browser_lock = None, asyncio.Lock()  # one page at a time keeps chromium memory flat
        if any(co["ats"] in BROWSER_FETCHERS for co in companies):
            ctx = await stack.enter_async_context(browser_context())

        async def one(co):
            try:
                if co["ats"] in HTTP_FETCHERS:
                    async with sem:
                        return co, await HTTP_FETCHERS[co["ats"]](client, co)
                if co["ats"] in BROWSER_FETCHERS:
                    async with browser_lock:
                        return co, await BROWSER_FETCHERS[co["ats"]](ctx, co)
                raise ValueError(f"unknown ats {co['ats']!r}")
            except Exception as e:
                return co, e

        return await asyncio.gather(*(one(co) for co in companies))

def notify_pending(store, global_filter, per_company, dry_run, now) -> int:
    pending = store.pending()
    matched, filtered = [], []
    for r in pending:
        (matched if per_company.get(r["company"], global_filter).matches(r) else filtered).append(r)
    store.mark(filtered, "filtered", now)
    sent = 0
    n = len(matched)
    for rows, msg in notify.chunk(f"🆕 {n} new job{'s' * (n != 1)}",
                                  [(r, notify.format_job(r)) for r in matched]):
        if dry_run:
            print(msg, end="\n\n")
        elif notify.send(msg):
            store.mark(rows, "sent", now)
            sent += len(rows)
        # on failure rows stay pending and are retried next run
    return sent

def notify_baselines(baselines, global_filter, per_company, dry_run):
    # many companies at once (bulk add) -> compact: one line each, fewer preview jobs
    compact = len(baselines) > 5
    preview = 3 if compact else BASELINE_PREVIEW
    items = []
    for co, jobs in baselines:
        f = per_company.get(co["name"], global_filter)
        hits = [asdict(j) for j in jobs if f.matches(j)]
        text = f"👀 Now tracking {co['name']} ({co['ats']}): {len(jobs)} open, {len(hits)} match your filters"
        if hits:
            text += "\n\n" + "\n\n".join(notify.format_job(h) for h in hits[:preview])
            if len(hits) > preview:
                text += f"\n\n…and {len(hits) - preview} more"
        items.append((len(hits), co, text))
    if compact:
        quiet = sorted(c["name"] for n, c, _ in items if n == 0)
        items = [(n, c, t) for n, c, t in sorted(items, key=lambda x: -x[0]) if n]
        if quiet:
            items.append((0, None, f"No current matches ({len(quiet)}): " + ", ".join(quiet)))
    header = f"📋 Now tracking {len(baselines)} new compan{'ies' if len(baselines) != 1 else 'y'}"
    for _, msg in notify.chunk(header, [(c, t) for _, c, t in items]):
        print(msg, end="\n\n") if dry_run else notify.send(msg)

def short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    return f"{type(e).__name__}: {str(e)[:150]}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--db")
    ap.add_argument("--only", nargs="*", help="company names to poll")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--health", action="store_true", help="send the health summary now, regardless of hour")
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text())
    companies = [co for co in cfg.get("companies", []) if not co.get("disabled")]
    if args.only:
        companies = [co for co in companies if co["name"] in args.only]
    global_filter, per_company = build_filters(cfg)
    store = Store(args.db or (":memory:" if args.dry_run else DEFAULT_DB))
    sink = JsonlSink(NAME)
    started = datetime.now(timezone.utc)
    now = started.isoformat()
    if store.get("first_run") is None:
        store.set("first_run", now)

    results = asyncio.run(fetch_all(companies, cfg.get("concurrency", 4)))
    baselines, failures, new_jobs = [], 0, 0
    for co, res in results:
        if isinstance(res, Exception):
            failures += 1
            log.warning("fail %s (%s): %r", co["name"], co["ats"], res)
            sink.write({"crawler": NAME, "event": "error", "company": co["name"], "ats": co["ats"],
                        "error": repr(res)})
            store.record_fail(co["name"], short_error(res), now)
            continue
        if not res.jobs and store.open_count(co["name"]):
            log.warning("%s returned 0 jobs but has open jobs on record; not marking closed", co["name"])
        ch = store.sync(co["name"], res.jobs, res.complete, now)
        store.record_ok(co["name"], len(res.jobs), now)
        new_jobs += 0 if ch["baseline"] else len(ch["new"])
        log.info("ok %s: %d jobs, %d new, %d closed%s%s", co["name"], len(res.jobs), len(ch["new"]),
                 len(ch["closed"]), " (baseline)" if ch["baseline"] else "",
                 "" if res.complete else " (listing incomplete; closures skipped)")
        if ch["baseline"]:
            baselines.append((co, res.jobs))
        else:
            for j in ch["new"]:
                sink.write({"crawler": NAME, "event": "new", **asdict(j)})
        for job_id in ch["closed"]:
            sink.write({"crawler": NAME, "event": "closed", "company": co["name"], "job_id": job_id})

    if baselines:
        notify_baselines(baselines, global_filter, per_company, args.dry_run)
    sent = notify_pending(store, global_filter, per_company, args.dry_run, now)
    sink.close()
    finished = datetime.now(timezone.utc)
    if not args.only:  # partial runs would skew the health stats
        store.record_run(now, finished.isoformat(), len(companies), failures, new_jobs, sent)
    log.info("done: %d companies, %d failed, %d notified", len(companies), failures, sent)

    health_cfg = cfg.get("health", {})
    if args.health or (not args.only and not args.dry_run and health.due(store, health_cfg, finished)):
        msg = health.build(store, companies, health_cfg, finished)
        if args.dry_run:
            print(msg)
        elif notify.send(msg):
            health.mark_sent(store, finished)
            log.info("health summary sent")

if __name__ == "__main__":
    main()
