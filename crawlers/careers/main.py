"""Career-page poller: fetch every company's open jobs, diff against SQLite, ping Telegram
about new jobs that pass the filters. One-shot; scheduled by systemd/poll@careers.timer.

Run:  python -m crawlers.careers.main [--dry-run] [--only NAME ...] [--config PATH] [--db PATH]
--dry-run prints messages instead of sending and uses an in-memory DB unless --db is given.

Reliability (config [http] / [alerts]): per-host pacing + retry/backoff (fetchers.base.RetryClient),
a per-company timeout, one retry for browser fetchers, a run lock, and guards against
broken fetchers: malformed records are skipped (too many => closures paused), a job closes only
after missing from several complete listings, a sharply shrunk listing pauses closures, a burst
of "new" jobs (ids changed) is absorbed as a re-baseline, and state changes (failing / empty /
shrunk / recovered) alert once. Messages go through a SQLite outbox, so a Telegram outage
delays them rather than losing them, and each full run pings an external dead-man switch.
"""
import argparse, asyncio, fcntl, logging, os, sys, tomllib
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from common import config
from common.browser import browser_context
from common.output import JsonlSink, setup_logging
from .fetchers import HTTP_FETCHERS, BROWSER_FETCHERS
from .fetchers.base import RetryClient
from .models import FetchResult
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
STUCK_OUTBOX_HOURS = 3  # undelivered messages older than this => report the run as failed

async def fetch_all(companies: list[dict], cfg: dict) -> tuple[list[tuple[dict, object]], int]:
    """Returns ([(company_cfg, FetchResult | Exception)], http_retries).
    One company failing or hanging never stops the rest."""
    http_cfg = cfg.get("http", {})
    sem = asyncio.Semaphore(cfg.get("concurrency", 4))
    timeout = http_cfg.get("company_timeout", 300)
    async with AsyncExitStack() as stack:
        raw = await stack.enter_async_context(httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": UA, "Accept": "application/json"}))
        client = RetryClient(raw, **http_cfg)
        ctx, browser_lock = None, asyncio.Lock()  # one page at a time keeps chromium memory flat
        if any(co["ats"] in BROWSER_FETCHERS for co in companies):
            ctx = await stack.enter_async_context(browser_context())

        async def browser_fetch(co):
            for attempt in (1, 2):  # no HTTP-level retry here, so retry the whole page once
                try:
                    return await BROWSER_FETCHERS[co["ats"]](ctx, co)
                except Exception as e:
                    if attempt == 2:
                        raise
                    log.info("retry %s (browser) after: %s", co["name"], short_error(e))
                    await asyncio.sleep(10)

        async def one(co):
            try:
                if co["ats"] in HTTP_FETCHERS:
                    async with sem:
                        return co, await asyncio.wait_for(HTTP_FETCHERS[co["ats"]](client, co),
                                                          co.get("timeout", timeout))
                if co["ats"] in BROWSER_FETCHERS:
                    async with browser_lock:
                        return co, await asyncio.wait_for(browser_fetch(co), co.get("timeout", timeout))
                raise ValueError(f"unknown ats {co['ats']!r}")
            except asyncio.TimeoutError:
                return co, TimeoutError(f"no result within {co.get('timeout', timeout)}s")
            except Exception as e:
                return co, e

        results = await asyncio.gather(*(one(co) for co in companies))
        return results, client.retried

def post(store, text: str, rows=(), *, dry_run: bool, now: str):
    """Queue a message in the outbox (dry run: print it, queue nothing)."""
    if dry_run:
        print(text, end="\n\n")
    else:
        store.enqueue(text, rows, now)

def notify_pending(store, global_filter, per_company, dry_run, now) -> int:
    """Queue messages for pending jobs that match; returns how many jobs were queued."""
    pending = store.pending()
    matched, filtered = [], []
    for r in pending:
        (matched if per_company.get(r["company"], global_filter).matches(r) else filtered).append(r)
    store.mark(filtered, "filtered", now)
    n = len(matched)
    for rows, msg in notify.chunk(f"🆕 {n} new job{'s' * (n != 1)}",
                                  [(r, notify.format_job(r)) for r in matched]):
        post(store, msg, rows, dry_run=dry_run, now=now)
    return n

def notify_baselines(store, baselines, global_filter, per_company, dry_run, now):
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
        post(store, msg, dry_run=dry_run, now=now)

def short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.url.host}"
    return f"{type(e).__name__}: {str(e)[:150]}"

def parse_verdict(res: FetchResult, max_skip_ratio: float) -> tuple[Exception | None, bool, str]:
    """Judge a listing with skipped (malformed) records -> (error, complete, note).
    All records bad: the fetcher is broken, treat as a failure. More than max_skip_ratio bad:
    keep the parsed jobs but don't close anything from this listing. A few bad: fine; the
    closure hysteresis already tolerates a job briefly missing."""
    if not res.skipped:
        return None, res.complete, ""
    total = len(res.jobs) + res.skipped
    note = f"{res.skipped}/{total} records unparseable ({res.skip_error})"
    if not res.jobs:
        return ValueError(f"all {note}"), False, note
    return None, res.complete and res.skipped <= max_skip_ratio * total, note

ALERT_TEXT = {
    "failing": "❌ {name}: failing {fails} runs in a row: {error}",
    "empty": "⚠️ {name}: returned 0 jobs (had {prev}); closures paused until it recovers",
    "shrunk": "⚠️ {name}: listing shrank {prev} → {count}; closures paused until it recovers",
}

def transition(store, name: str, state: str | None, **info) -> str | None:
    """Record a company's alert state; return a message only when it changes."""
    prev = store.set_alert_state(name, state)
    if state == prev:
        return None
    if state:
        return ALERT_TEXT[state].format(name=name, **info)
    return f"✅ {name}: recovered ({prev} → ok)"

def acquire_lock(db_path) -> object | None:
    """Exclusive lock so a manual run and the timer can't both send the same alerts."""
    if str(db_path) == ":memory:":
        return True
    f = open(f"{db_path}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return f

def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--db")
    ap.add_argument("--only", nargs="*", help="company names to poll")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--health", action="store_true", help="send the health summary now, regardless of hour")
    return ap.parse_args(argv)

def main():
    """Runs the poll; a crash is reported to the dead-man switch, then re-raised so systemd
    marks the unit failed (and its OnFailure= handler alerts Telegram)."""
    args = parse_args()
    try:
        run(args)
    except Exception as e:
        if not (args.dry_run or args.only):
            health.ping("fail", f"crashed: {type(e).__name__}: {e}")
        raise

def run(args):
    cfg = tomllib.loads(Path(args.config).read_text())
    companies = [co for co in cfg.get("companies", []) if not co.get("disabled")]
    if args.only:
        companies = [co for co in companies if co["name"] in args.only]
    global_filter, per_company = build_filters(cfg)
    db_path = args.db or (":memory:" if args.dry_run else DEFAULT_DB)
    store = Store(db_path)
    lock = acquire_lock(db_path)
    if lock is None:
        log.warning("another run holds %s.lock; exiting", db_path)
        sys.exit(0)
    acfg = cfg.get("alerts", {})
    shrink_ratio, shrink_min = acfg.get("shrink_ratio", 0.2), acfg.get("shrink_min", 10)
    shrink_accept = acfg.get("shrink_accept_runs", 6)
    burst_min, burst_ratio = acfg.get("burst_min", 25), acfg.get("burst_ratio", 0.5)
    close_after, max_skip_ratio = acfg.get("close_after_runs", 3), acfg.get("max_skip_ratio", 0.05)
    fail_threshold = cfg.get("health", {}).get("fail_threshold", 3)
    alerts = []
    sink = JsonlSink(NAME)
    started = datetime.now(timezone.utc)
    now = started.isoformat()
    if store.get("first_run") is None:
        store.set("first_run", now)

    results, retries = asyncio.run(fetch_all(companies, cfg))
    baselines, failures, new_jobs = [], 0, 0
    for co, res in results:
        name = co["name"]
        if not isinstance(res, Exception) and res.skipped:
            err, res.complete, note = parse_verdict(res, max_skip_ratio)
            log.warning("%s: %s%s", name, note, "" if res.complete else "; closures skipped")
            res = err or res
        if isinstance(res, Exception):
            failures += 1
            err = short_error(res)
            log.warning("fail %s (%s): %s", name, co["ats"], err)
            sink.write({"crawler": NAME, "event": "error", "company": name, "ats": co["ats"], "error": repr(res)})
            store.record_fail(name, err, now)
            fails = store.company_status()[name]["consecutive_failures"]
            if fails >= fail_threshold:
                alerts.append(transition(store, name, "failing", fails=fails, error=err))
            continue

        # shrink guard: a fetcher that silently breaks returns far fewer jobs; don't close them
        prev_open, count = store.open_count(name), len(res.jobs)
        complete, state, accepted = res.complete, None, False
        shrunk_key = f"shrunk_runs:{name}"
        if prev_open >= shrink_min and count < prev_open * shrink_ratio:
            runs = int(store.get(shrunk_key) or 0) + 1
            store.set(shrunk_key, str(runs))
            if runs < shrink_accept:
                complete, state = False, ("empty" if count == 0 else "shrunk")
                log.warning("%s: %d jobs vs %d open on record; closures paused (%d/%d)",
                            name, count, prev_open, runs, shrink_accept)
            else:
                accepted = True
                alerts.append(f"ℹ️ {name}: listing has stayed at {count} (was {prev_open}) for {runs} runs; "
                              "accepting it as real and closing the rest")
        else:
            store.set(shrunk_key, "0")

        # an accepted shrink has already waited shrink_accept runs; close the rest now
        ch = store.sync(name, res.jobs, complete, now, allow_empty=accepted,
                        close_after=1 if accepted else close_after)
        store.record_ok(name, count, now)
        if accepted:  # the ℹ️ message above already says so; no separate "recovered"
            store.set_alert_state(name, None)
        else:
            alerts.append(transition(store, name, state, prev=prev_open, count=count))

        # burst guard: hundreds of "new" jobs at once usually means ids changed, not a hiring spree
        if not ch["baseline"] and len(ch["new"]) > max(burst_min, burst_ratio * prev_open):
            store.mark([{"company": name, "job_id": j.job_id} for j in ch["new"]], "baseline", now)
            alerts.append(f"⚠️ {name}: {len(ch['new'])} 'new' jobs at once (had {prev_open}); "
                          "treated as a re-baseline, not alerted. Job ids may have changed.")
            ch["new"] = []
        new_jobs += 0 if ch["baseline"] else len(ch["new"])
        log.info("ok %s: %d jobs, %d new, %d closed%s%s", name, count, len(ch["new"]),
                 len(ch["closed"]), " (baseline)" if ch["baseline"] else "",
                 "" if complete else " (listing incomplete; closures skipped)")
        if ch["baseline"]:
            baselines.append((co, res.jobs))
        else:
            for j in ch["new"]:
                sink.write({"crawler": NAME, "event": "new", **asdict(j)})
        for job_id in ch["closed"]:
            sink.write({"crawler": NAME, "event": "closed", "company": name, "job_id": job_id})

    alerts = [a for a in alerts if a]
    for _, msg in notify.chunk("🚨 Poller alerts", [(None, a) for a in alerts]):
        post(store, msg, dry_run=args.dry_run, now=now)
    if baselines:
        notify_baselines(store, baselines, global_filter, per_company, args.dry_run, now)
    queued = notify_pending(store, global_filter, per_company, args.dry_run, now)
    sent = queued if args.dry_run else notify.flush(store, now)
    sink.close()
    finished = datetime.now(timezone.utc)
    if not args.only:  # partial runs would skew the health stats
        store.record_run(now, finished.isoformat(), len(companies), failures, new_jobs, sent)
    log.info("done: %d companies, %d failed, %d notified, %d http retries, %d alerts",
             len(companies), failures, sent, retries, len(alerts))

    health_cfg = cfg.get("health", {})
    if args.health or (not args.only and not args.dry_run and health.due(store, health_cfg, finished)):
        post(store, health.build(store, companies, health_cfg, finished), dry_run=args.dry_run, now=now)
        if not args.dry_run:
            health.mark_sent(store, finished)  # queued; the outbox delivers it, now or next run
            notify.flush(store, now)
            log.info("health summary queued")

    if not (args.dry_run or args.only):
        heartbeat(store, finished, len(companies), failures, sent)

def heartbeat(store, now: datetime, companies: int, failures: int, sent: int):
    """Report a finished run to the dead-man switch. Messages stuck in the outbox mean
    Telegram delivery is broken, which Telegram itself can't tell you, so that's a failure."""
    oldest = store.oldest_undelivered()
    summary = f"{companies} companies, {failures} failed, {sent} notified"
    if oldest and now - datetime.fromisoformat(oldest) > timedelta(hours=STUCK_OUTBOX_HOURS):
        n = len(store.outbox())
        health.ping("fail", f"{n} Telegram message(s) undelivered since {oldest}; {summary}")
    else:
        health.ping("", summary)

if __name__ == "__main__":
    main()
