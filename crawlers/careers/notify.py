"""Telegram via `hermes send` (reuses the gateway's bot token; gateway needn't be running)."""
import json, logging, os, re, shutil, subprocess, time
from pathlib import Path

log = logging.getLogger("careers.notify")

HERMES_BIN = os.getenv("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")
NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "telegram")
MAX_CHARS = 3500  # Telegram caps at 4096; leave room for hermes' markdown escaping

SEND_GAP_S = 1.5             # Telegram flood control: ~1 msg/s per chat
RETRY_DELAYS_S = (5, 20)
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

def send(text: str) -> bool:
    global _last_send
    for attempt, delay in enumerate((0, *RETRY_DELAYS_S)):
        time.sleep(max(delay, _last_send + SEND_GAP_S - time.monotonic(), 0))
        ok, err = _send_once(text)
        _last_send = time.monotonic()
        if ok:
            return True
        log.warning("hermes send failed (attempt %d/%d): %s", attempt + 1, 1 + len(RETRY_DELAYS_S), err)
    return False

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
