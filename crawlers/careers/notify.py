"""Telegram via `hermes send` (reuses the gateway's bot token; gateway needn't be running).

Messages aren't sent directly: callers queue them in the store's outbox and flush() delivers
them, so a Telegram/hermes outage delays messages instead of losing them.

Each message has a route, and each route a list of destinations (hermes targets):
- "jobs": the new-jobs summary, to every NOTIFY_JOBS_TARGETS destination (the owner's DM and the
  alerts channel; later, per-subscriber chats).
- "ops": everything only the owner should see (poller alerts, health summary, digests, reports).
post() queues one outbox row per destination, so each destination retries, fails and recovers
on its own: a broken channel never holds up the DM, and vice versa."""
import json, logging, os, re, shutil, subprocess, time
from pathlib import Path

log = logging.getLogger("careers.notify")

HERMES_BIN = os.getenv("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "telegram")  # hermes target: telegram = its home channel

def _targets(var: str) -> list[str]:
    """Comma-separated hermes targets from `var`, e.g. "telegram,telegram:-1001234567890"."""
    return [t.strip() for t in os.getenv(var, "").split(",") if t.strip()] or [NOTIFY_TARGET]

TARGETS = {  # route -> destinations
    "jobs": _targets("NOTIFY_JOBS_TARGETS"),
    "ops": _targets("NOTIFY_OPS_TARGET"),
}
MAX_CHARS = 3500  # Telegram caps at 4096; leave room for hermes' markdown escaping

SEND_GAP_S = 3.1             # Telegram flood control: groups/channels allow 20 msgs/min
RETRY_DELAYS_S = (5, 20)
MAX_RETRY_AFTER_S = 120      # longer Telegram flood penalties wait for the next run instead
MAX_ATTEMPTS = 24            # runs a message may fail in before it's dropped (~1 day hourly)
_last_send = 0.0
# hermes reports a flood wait as "flood_control:37.0" or Telegram's "Retry in 37 seconds"
_RETRY_AFTER = re.compile(r"(?:flood_control:|retry[ _-]?after\D{0,3}|retry in )(\d+(?:\.\d+)?)", re.I)

def retry_after(err: str) -> float | None:
    """Seconds Telegram asked us to wait (HTTP 429), if the error says."""
    m = _RETRY_AFTER.search(err or "")
    return float(m.group(1)) if m else None

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

def send(text: str, target: str) -> str | None:
    """Send to one destination with retries; a 429 waits as long as Telegram asks (up to
    MAX_RETRY_AFTER_S; a longer penalty gives up so the message waits for the next run).
    Returns None on success, else the last error."""
    global _last_send
    for attempt in range(1 + len(RETRY_DELAYS_S)):
        time.sleep(max(_last_send + SEND_GAP_S - time.monotonic(), 0))
        ok, err = _send_once(text, target)
        _last_send = time.monotonic()
        if ok:
            return None
        log.warning("hermes send to %s failed (attempt %d/%d): %s",
                    target, attempt + 1, 1 + len(RETRY_DELAYS_S), err)
        if attempt == len(RETRY_DELAYS_S):
            break
        wait = retry_after(err)
        if wait is not None and wait > MAX_RETRY_AFTER_S:
            log.warning("%s: Telegram asks to wait %.0fs; leaving it queued for the next run", target, wait)
            break
        time.sleep(wait if wait is not None else RETRY_DELAYS_S[attempt])
    return err

def post(store, text: str, rows=(), *, route: str, dry_run: bool, now: str):
    """Queue a message for every destination of `route` (dry run: print it, queue nothing).
    `rows` are the jobs it carries (None entries, e.g. header lines from chunk(), are ignored)."""
    if route not in TARGETS:
        raise ValueError(f"unknown route {route!r}")
    for target in TARGETS[route]:
        if dry_run:
            print(f"[{route} -> {target}]\n{text}", end="\n\n")
        else:
            store.enqueue(text, [r for r in rows if r is not None], now, route=route, target=target)

def flush(store, now: str, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Deliver queued messages. A destination stops at its first failure (Telegram, hermes or
    that chat is probably unavailable; the rest stay queued for the next run), but the others
    carry on. A message that has failed in max_attempts runs is dropped with an error log, so
    one bad message can't retry forever. Returns the number of jobs delivered."""
    sent, down, up = 0, {}, set()
    for msg in store.outbox():
        target = msg["target"] or TARGETS[msg["route"]][0]  # rows queued before per-destination rows
        if target in down:
            continue
        err = send(msg["text"], target)
        if err is None:
            sent += store.delivered(msg, now)
            up.add(target)
            continue
        if msg["attempts"] + 1 >= max_attempts:
            n = store.dropped(msg, now)
            log.error("dropping message %d to %s after %d failed runs (%d jobs): %s",
                      msg["id"], target, max_attempts, n, err)
        else:
            store.send_failed(msg, err)
        down[target] = err
    _destination_alerts(store, down, up, now)
    return sent

def _destination_alerts(store, down: dict, up: set, now: str):
    """Tell the owner (ops) once when another destination starts failing and once when it
    delivers again, e.g. the bot removed from the channel. Otherwise the channel just goes
    quiet. Recovery needs a real delivery: a destination whose queue merely emptied (messages
    dropped) hasn't recovered."""
    ops = set(TARGETS["ops"])
    for target in sorted((set(down) | up) - ops):  # only destinations tried this run can change state
        key = f"target_down:{target}"
        was_down = bool(store.get(key))
        if target in down and not was_down:
            store.set(key, now)
            text = (f"⚠️ Can't deliver to {target}: {down[target][:300]}\n"
                    f"Messages for it stay queued and retry every run (dropped after {MAX_ATTEMPTS} failed runs); "
                    "other destinations aren't affected.")
        elif target in up and was_down:
            store.set(key, "")
            text = f"✅ Messages are reaching {target} again"
        else:
            continue
        for ops_target in TARGETS["ops"]:
            if send(text, ops_target) is not None:  # ops down too: queue it, the next run retries
                store.enqueue(text, now=now, route="ops", target=ops_target)

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
