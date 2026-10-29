"""Telegram via `hermes send` (reuses the gateway's bot token; gateway needn't be running).

Messages aren't sent directly: callers queue them in the store's outbox and flush() delivers
them, so a Telegram/hermes outage delays messages instead of losing them.

Each message has a route: "findings" (job announcements, for the shared group chat) or "ops"
(poller alerts, health summary; for the owner). The route is stored with the message and
resolved to a hermes target at send time, so retargeting in .env also redirects what's queued."""
import json, logging, os, re, shutil, subprocess, time
from pathlib import Path

log = logging.getLogger("careers.notify")

HERMES_BIN = os.getenv("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "telegram")  # hermes target: telegram = its home channel
TARGETS = {  # route -> hermes target, e.g. telegram:-1001234567890 for a group
    "findings": os.getenv("NOTIFY_FINDINGS_TARGET") or NOTIFY_TARGET,
    "ops": os.getenv("NOTIFY_OPS_TARGET") or NOTIFY_TARGET,
}
MAX_CHARS = 3500  # Telegram caps at 4096; leave room for hermes' markdown escaping

SEND_GAP_S = 3.1             # Telegram flood control: groups allow 20 msgs/min
RETRY_DELAYS_S = (5, 20)
MAX_ATTEMPTS = 24            # runs a message may fail in before it's dropped (~1 day hourly)
_last_send = 0.0

def _send_once(text: str, target: str) -> tuple[bool, str]:
    try:
        p = subprocess.run([HERMES_BIN, "send", "--to", target, "--json"],
                           input=text, text=True, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, repr(e)
    if p.returncode == 0:
        return True, ""
    try:  # --json puts the real reason in the payload; --quiet would swallow it
        err = json.loads(p.stdout).get("error") or p.stdout
    except ValueError:
        err = p.stderr or p.stdout
    return False, f"exit {p.returncode}: {str(err).strip()[:500]}"

def send(text: str, route: str) -> str | None:
    """Send with retries. Returns None on success, else the last error."""
    if (target := TARGETS.get(route)) is None:
        return f"unknown route {route!r}"
    global _last_send
    for attempt, delay in enumerate((0, *RETRY_DELAYS_S)):
        time.sleep(max(delay, _last_send + SEND_GAP_S - time.monotonic(), 0))
        ok, err = _send_once(text, target)
        _last_send = time.monotonic()
        if ok:
            return None
        log.warning("hermes send to %s failed (attempt %d/%d): %s",
                    target, attempt + 1, 1 + len(RETRY_DELAYS_S), err)
    return err

def post(store, text: str, rows=(), *, route: str, dry_run: bool, now: str):
    """Queue a message for `route` in the outbox (dry run: print it, queue nothing). `rows` are
    the jobs it carries (None entries, e.g. header lines from chunk(), are ignored)."""
    if route not in TARGETS:
        raise ValueError(f"unknown route {route!r}")
    if dry_run:
        print(f"[{route} -> {TARGETS[route]}]\n{text}", end="\n\n")
    else:
        store.enqueue(text, [r for r in rows if r is not None], now, route=route)

def flush(store, now: str, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Deliver queued messages. A route stops at its first failure (Telegram, hermes or that
    chat is probably unavailable; the rest stay queued for the next run), but other routes carry
    on, so e.g. the bot being removed from the group doesn't hold up ops alerts. A message that
    has failed in max_attempts runs is dropped with an error log, so one bad message can't retry
    forever. Returns the number of jobs delivered."""
    sent, down, up = 0, {}, set()
    for msg in store.outbox():
        if msg["route"] in down:
            continue
        err = send(msg["text"], msg["route"])
        if err is None:
            sent += store.delivered(msg, now)
            up.add(msg["route"])
            continue
        if msg["attempts"] + 1 >= max_attempts:
            n = store.dropped(msg, now)
            log.error("dropping message %d after %d failed runs (%d jobs): %s",
                      msg["id"], max_attempts, n, err)
        else:
            store.send_failed(msg, err)
        down[msg["route"]] = err
    _route_alerts(store, down, up, now)
    return sent

def _route_alerts(store, down: dict, up: set, now: str):
    """Tell the owner (ops) once when another route starts failing and once when it delivers
    again, e.g. the bot removed from the group, or the group's id changed on a supergroup
    upgrade. Otherwise the group just goes quiet. Recovery needs a real delivery: a route whose
    queue merely emptied (messages dropped) hasn't recovered."""
    for route, target in TARGETS.items():
        if route == "ops":
            continue
        key, was_down = f"route_down:{route}", bool(store.get(f"route_down:{route}"))
        if route in down and not was_down:
            store.set(key, now)
            text = (f"⚠️ Can't deliver {route} messages to {target}: {down[route][:300]}\n"
                    f"They stay queued and retry every run (dropped after {MAX_ATTEMPTS} failed runs). "
                    f"If the group was upgraded to a supergroup, its chat id changed: update NOTIFY_{route.upper()}_TARGET.")
        elif route in up and was_down:
            store.set(key, "")
            text = f"✅ {route} messages are reaching {target} again"
        else:
            continue
        if send(text, "ops") is not None:  # ops down too: queue it, the next run retries
            store.enqueue(text, now=now, route="ops")

def _clean(s: str | None) -> str:
    # hermes renders the body as markdown; keep stray markup in titles from mangling it
    return re.sub(r"[*_`\[\]]", " ", s or "").strip()

def format_job(j) -> str:
    where = [_clean(j["location"])] if j["location"] else []
    if j["remote"] and "remote" not in (j["location"] or "").lower():
        where.append("Remote")
    lines = [f"{_clean(j['company'])} — {_clean(j['title'])}"]
    if where:
        lines.append("📍 " + " · ".join(dict.fromkeys(where)))
    lines.append(j["url"])
    return "\n".join(lines)

def chunk(header: str, items: list[tuple[object, str]]) -> list[tuple[list, str]]:
    """Pack (row, text) items into messages under MAX_CHARS. Returns [(rows, message)]."""
    out, rows, body = [], [], header
    for row, text in items:
        if rows and len(body) + len(text) + 2 > MAX_CHARS:
            out.append((rows, body))
            rows, body = [], header + " (cont.)"
        rows.append(row)
        body += "\n\n" + text
    if rows:
        out.append((rows, body))
    return out
