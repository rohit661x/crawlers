"""Telegram via `hermes send` (reuses the gateway's bot token; gateway needn't be running).

Messages aren't sent directly: callers queue them in the store's outbox and flush() delivers
them, so a Telegram/hermes outage delays messages instead of losing them."""
import json, logging, os, re, shutil, subprocess, time
from pathlib import Path

log = logging.getLogger("careers.notify")

HERMES_BIN = os.getenv("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "telegram")
MAX_CHARS = 3500  # Telegram caps at 4096; leave room for hermes' markdown escaping

SEND_GAP_S = 1.5             # Telegram flood control: ~1 msg/s per chat
RETRY_DELAYS_S = (5, 20)
MAX_ATTEMPTS = 24            # runs a message may fail in before it's dropped (~1 day hourly)
_last_send = 0.0

def _send_once(text: str) -> tuple[bool, str]:
    try:
        p = subprocess.run([HERMES_BIN, "send", "--to", NOTIFY_TARGET, "--json"],
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

def send(text: str) -> str | None:
    """Send with retries. Returns None on success, else the last error."""
    global _last_send
    for attempt, delay in enumerate((0, *RETRY_DELAYS_S)):
        time.sleep(max(delay, _last_send + SEND_GAP_S - time.monotonic(), 0))
        ok, err = _send_once(text)
        _last_send = time.monotonic()
        if ok:
            return None
        log.warning("hermes send failed (attempt %d/%d): %s", attempt + 1, 1 + len(RETRY_DELAYS_S), err)
    return err

def post(store, text: str, rows=(), *, dry_run: bool, now: str):
    """Queue a message in the outbox (dry run: print it, queue nothing). `rows` are the jobs it
    carries (None entries, e.g. header lines from chunk(), are ignored)."""
    if dry_run:
        print(text, end="\n\n")
    else:
        store.enqueue(text, [r for r in rows if r is not None], now)

def flush(store, now: str, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Deliver queued messages. Stops at the first failure (Telegram or hermes is probably
    down; the rest stay queued for the next run). A message that has failed in max_attempts
    runs is dropped with an error log, so one bad message can't retry forever.
    Returns the number of jobs delivered."""
    sent = 0
    for msg in store.outbox():
        err = send(msg["text"])
        if err is None:
            sent += store.delivered(msg, now)
            continue
        if msg["attempts"] + 1 >= max_attempts:
            n = store.dropped(msg, now)
            log.error("dropping message %d after %d failed runs (%d jobs): %s",
                      msg["id"], max_attempts, n, err)
        else:
            store.send_failed(msg, err)
        break
    return sent

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
