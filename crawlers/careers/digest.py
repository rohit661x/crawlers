"""Digests of open jobs that match the filters: what a newly tracked company already has
open, and jobs that start matching after a filter change. (report.py lists them on demand.)

Config:
  [digest]
  baseline_max = 50    # matching jobs to send per newly tracked company (rest: see report.py)
  refilter_max = 100   # newly matching jobs to send after a filter change
"""
import json, logging
from .filters import build_filters
from . import notify

log = logging.getLogger("careers.digest")
REPORT_CMD = "python -m crawlers.careers.report"

def filter_spec(cfg: dict) -> str:
    """Every filter setting (global + per-company) as canonical JSON; changes when you edit them."""
    spec = {"filters": cfg.get("filters", {}),
            "companies": {co["name"]: co["filters"] for co in cfg.get("companies", []) if "filters" in co}}
    return json.dumps(spec, sort_keys=True, ensure_ascii=False)

def _filters_from_spec(spec: str):
    s = json.loads(spec)
    return build_filters({"filters": s["filters"],
                          "companies": [{"name": n, "filters": f} for n, f in s["companies"].items()]})

def matching(rows, global_filter, per_company) -> list:
    return [r for r in rows if per_company.get(r["company"], global_filter).matches(r)]

def job_items(rows, cap: int, more_hint: str) -> list[tuple[object, str]]:
    """(row, text) items for notify.chunk: up to `cap` jobs, then one line pointing at the rest."""
    items = [(r, notify.format_job(r)) for r in rows[:cap]]
    if len(rows) > cap:
        items.append((None, f"…and {len(rows) - cap} more: {more_hint}"))
    return items

def baselines(store, baselines, global_filter, per_company, *, cap: int, dry_run: bool, now: str):
    """Announce newly tracked companies with every job that already matches (up to `cap` per
    company). Those jobs go through the outbox like new ones, so they end up marked 'sent'."""
    found, quiet = [], []
    for co, jobs in baselines:
        f = per_company.get(co["name"], global_filter)
        hits = [vars(j) for j in jobs if f.matches(j)]
        (found if hits else quiet).append((co, len(jobs), hits))
    items = []
    for co, total, hits in sorted(found, key=lambda x: -len(x[2])):
        items.append((None, f"👀 {co['name']} ({co['ats']}): {total} open, {len(hits)} match your filters"))
        items += job_items(hits, cap, f'{REPORT_CMD} --company "{co["name"]}"')
    if quiet:
        items.append((None, f"No current matches ({len(quiet)}): " + ", ".join(sorted(c["name"] for c, _, _ in quiet))))
    n = len(baselines)
    for rows, msg in notify.chunk(f"📋 Now tracking {n} new compan{'ies' if n != 1 else 'y'}", items):
        notify.post(store, msg, rows, dry_run=dry_run, now=now)

def refilter(store, cfg: dict, global_filter, per_company, *, cap: int, dry_run: bool, now: str) -> int:
    """When the filters changed since the last run, send open, never-sent jobs that match the
    new filters but not the old ones. Returns how many. Jobs that matched all along (e.g. a
    company's openings when first tracked) aren't "new" here: report.py --unsent lists those.
    The first run only records the filters."""
    new_spec, old_spec = filter_spec(cfg), store.get("filter_spec")
    if new_spec == old_spec:
        return 0
    hits = []
    if old_spec is not None:
        old_global, old_per_company = _filters_from_spec(old_spec)
        hits = [r for r in matching(store.open_jobs(states=("filtered", "baseline")), global_filter, per_company)
                if not old_per_company.get(r["company"], old_global).matches(r)]
        log.info("filters changed: %d open jobs newly match", len(hits))
        n = len(hits)
        header = f"🔎 Filters changed: {n} open job{'s' * (n != 1)} now match{'es' * (n == 1)}"
        for rows, msg in notify.chunk(header, job_items(hits, cap, f"{REPORT_CMD} --unsent")):
            notify.post(store, msg, rows, dry_run=dry_run, now=now)
    if not dry_run:
        store.set("filter_spec", new_spec)
    return len(hits)
