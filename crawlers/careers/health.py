"""Daily health summary, sent by the poller itself on the first run after [health].hour
(local time). Because the poller sends it, a missing morning message means the poller
is dead; that's the point.

Config:
  [health]
  hour = 9                 # local hour to send at (first run at/after it)
  fail_threshold = 3       # consecutive failed runs before a company is flagged
  expected_runs = 20       # fewer runs than this in 24h is flagged (timer is hourly)
"""
from datetime import datetime, timedelta, timezone

def _ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    h = (now - datetime.fromisoformat(iso)).total_seconds() / 3600
    return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"

def due(store, cfg: dict, now: datetime) -> bool:
    local = now.astimezone()
    return local.hour >= cfg.get("hour", 9) and store.get("health_sent_date") != local.date().isoformat()

def mark_sent(store, now: datetime):
    store.set("health_sent_date", now.astimezone().date().isoformat())

def build(store, companies: list[dict], cfg: dict, now: datetime) -> str:
    since = (now - timedelta(hours=24)).isoformat()
    runs = store.runs_since(since)
    status = store.company_status()
    threshold = cfg.get("fail_threshold", 3)
    issues = []

    expected = cfg.get("expected_runs", 20)
    if len(runs) < expected and (store.get("first_run") or "") < since:  # skip on day one
        issues.append(f"⚠️ only {len(runs)} runs in 24h (expected ~{expected + 4}); check poll@careers.timer")

    ok = 0
    for co in companies:
        name, st = co["name"], status.get(co["name"])
        if st is None:
            issues.append(f"⚠️ {name}: never polled")
        elif st["consecutive_failures"] >= threshold:
            issues.append(f"❌ {name}: failing {st['consecutive_failures']} runs in a row "
                          f"(since {_ago(st['failing_since'], now)}; last ok {_ago(st['last_ok'], now)}): "
                          f"{st['last_error']}")
        elif st["alert_state"] in ("empty", "shrunk"):
            issues.append(f"⚠️ {name}: listing {st['alert_state']} ({st['last_count']} jobs vs "
                          f"{store.open_count(name)} on record); closures paused")
        else:
            ok += 1
            if st["consecutive_failures"]:
                issues.append(f"↻ {name}: {st['consecutive_failures']} recent failure(s): {st['last_error']}")

    failed_runs = sum(1 for r in runs if r["failed"])
    head = "✅ Career poller: all good" if not issues else f"⚠️ Career poller: {len(issues)} issue(s)"
    lines = [
        head,
        f"Last 24h: {len(runs)} runs · {failed_runs} with failures · {store.sent_since(since)} new matches sent",
        f"Companies: {ok}/{len(companies)} OK · {store.total_open():,} open jobs tracked",
    ]
    if issues:
        lines += ["", *issues]
    return "\n".join(lines)
