"""Telegram via the Bot API, with the hermes bot's token (one token, rotated in one place).
Not `hermes send`: that also copies every message into the owner's Hermes conversation, so each
later DM to Hermes would re-read every alert ever sent (119k input tokens for one "ping").

Messages aren't sent directly: callers queue them in the store's outbox and flush() delivers
them, so a Telegram outage delays messages instead of losing them.

Each message has a route, and each route a list of destinations (targets like "telegram:<chat_id>"):
- "jobs": the new-jobs summary, to every NOTIFY_JOBS_TARGETS destination (the owner's DM and the
  alerts channel; later, per-subscriber chats).
- "ops": everything only the owner should see (poller alerts, health summary, digests, reports).
post() queues one outbox row per destination, so each destination retries, fails and recovers
on its own: a broken channel never holds up the DM, and vice versa."""
import html, json, logging, os, re, time
from pathlib import Path
import httpx

log = logging.getLogger("careers.notify")

NOTIFY_TARGET = os.getenv("NOTIFY_TARGET", "telegram")  # target: "telegram" = the owner's DM (hermes' home channel)
HERMES_ENV = Path(os.getenv("HERMES_ENV") or Path.home() / ".hermes/.env")
TELEGRAM_API = "https://api.telegram.org"

def _hermes_env(key: str) -> str:
    """Our own environment first, then hermes' .env (where the bot token and home channel live)."""
    if value := os.getenv(key):
        return value
    try:
        for line in HERMES_ENV.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""

def _resolve(target: str) -> tuple[str, int | None]:
    """"telegram" -> the home channel; "telegram:<chat_id>[:<thread_id>]" -> that chat (and topic)."""
    platform, _, rest = target.partition(":")
    if platform != "telegram":
        raise ValueError(f"unsupported target {target!r}: only telegram[:<chat_id>[:<thread_id>]]")
    chat, _, thread = rest.partition(":")
    chat = chat or _hermes_env("TELEGRAM_HOME_CHANNEL")
    if not chat:
        raise ValueError("no chat id: set TELEGRAM_HOME_CHANNEL or use telegram:<chat_id>")
    return chat, int(thread) if thread else None

def _targets(var: str) -> list[str]:
    """Comma-separated targets from `var`, e.g. "telegram,telegram:-1001234567890"."""
    return [t.strip() for t in os.getenv(var, "").split(",") if t.strip()] or [NOTIFY_TARGET]

TARGETS = {  # route -> destinations
    "jobs": _targets("NOTIFY_JOBS_TARGETS"),
    "ops": _targets("NOTIFY_OPS_TARGET"),
}
MAX_CHARS = 3500  # Telegram caps a message at 4096 characters

SEND_GAP_S = 3.1             # Telegram flood control: groups/channels allow 20 msgs/min
RETRY_DELAYS_S = (5, 20)
MAX_RETRY_AFTER_S = 120      # longer Telegram flood penalties wait for the next run instead
MAX_ATTEMPTS = 24            # runs a message may fail in before it's dropped (~1 day hourly)
_last_send = 0.0
# a flood wait: "retry_after=37" (ours), "Too Many Requests: retry after 37" (Telegram), "flood_control:37"
_RETRY_AFTER = re.compile(r"(?:flood_control:|retry[ _-]?after\D{0,3}|retry in )(\d+(?:\.\d+)?)", re.I)

def retry_after(err: str) -> float | None:
    """Seconds Telegram asked us to wait (HTTP 429), if the error says."""
    m = _RETRY_AFTER.search(err or "")
    return float(m.group(1)) if m else None

def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))

def _send_once(text: str, target: str, options: dict | None = None) -> tuple[bool, str]:
    """One sendMessage call: plain text unless `options` (sendMessage extras: parse_mode,
    reply_markup, ...) says otherwise. If Telegram can't parse the formatting, it's sent once
    more as plain text (buttons kept), so a formatting slip never loses a message.
    Errors never contain the token; a 429's retry_after is kept for send()."""
    token = _hermes_env("TELEGRAM_BOT_TOKEN")
    try:
        chat, thread = _resolve(target)
        if not token:
            return False, f"no TELEGRAM_BOT_TOKEN (environment or {HERMES_ENV})"
        payload = {"chat_id": chat, "text": text, **({"message_thread_id": thread} if thread is not None else {}),
                   **(options or {})}
        r = httpx.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload, timeout=30)
        if r.status_code == 400 and "parse_mode" in payload and "can't parse entities" in r.text:
            log.warning("%s: Telegram rejected the formatting; sending as plain text", target)
            payload = {k: v for k, v in payload.items() if k != "parse_mode"} | {"text": _plain(text)}
            r = httpx.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload, timeout=30)
    except (ValueError, httpx.HTTPError) as e:
        err = f"{type(e).__name__}: {e}"
        return False, (err.replace(token, "<token>") if token else err)[:500]
    try:
        reply = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code}: non-JSON reply"
    if reply.get("ok"):
        return True, ""
    wait = (reply.get("parameters") or {}).get("retry_after")
    return False, f"HTTP {r.status_code}: {reply.get('description', '')[:400]}" + (f" (retry_after={wait})" if wait else "")

def send(text: str, target: str, options: dict | None = None) -> str | None:
    """Send to one destination with retries; a 429 waits as long as Telegram asks (up to
    MAX_RETRY_AFTER_S; a longer penalty gives up so the message waits for the next run).
    Returns None on success, else the last error."""
    global _last_send
    for attempt in range(1 + len(RETRY_DELAYS_S)):
        time.sleep(max(_last_send + SEND_GAP_S - time.monotonic(), 0))
        ok, err = _send_once(text, target, options)
        _last_send = time.monotonic()
        if ok:
            return None
        log.warning("send to %s failed (attempt %d/%d): %s",
                    target, attempt + 1, 1 + len(RETRY_DELAYS_S), err)
        if attempt == len(RETRY_DELAYS_S):
            break
        wait = retry_after(err)
        if wait is not None and wait > MAX_RETRY_AFTER_S:
            log.warning("%s: Telegram asks to wait %.0fs; leaving it queued for the next run", target, wait)
            break
        time.sleep(wait if wait is not None else RETRY_DELAYS_S[attempt])
    return err

def post(store, text: str, rows=(), *, route: str, dry_run: bool, now: str, options: dict | None = None):
    """Queue a message for every destination of `route` (dry run: print it, queue nothing).
    `rows` are the jobs it carries (None entries, e.g. header lines from chunk(), are ignored)."""
    if route not in TARGETS:
        raise ValueError(f"unknown route {route!r}")
    for target in TARGETS[route]:
        if dry_run:
            print(f"[{route} -> {target}]\n{text}", end="\n\n")
        else:
            store.enqueue(text, [r for r in rows if r is not None], now, route=route, target=target, options=options)

def flush(store, now: str, max_attempts: int = MAX_ATTEMPTS) -> int:
    """Deliver queued messages. A destination stops at its first failure (Telegram or
    that chat is probably unavailable; the rest stay queued for the next run), but the others
    carry on. A message that has failed in max_attempts runs is dropped with an error log, so
    one bad message can't retry forever. Returns the number of jobs delivered."""
    sent, down, up = 0, {}, set()
    for msg in store.outbox():
        target = msg["target"] or TARGETS[msg["route"]][0]  # rows queued before per-destination rows
        if target in down:
            continue
        err = send(msg["text"], target, json.loads(msg["options"]) if msg["options"] else None)
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
    # messages go out as plain text, so titles need no escaping; just tidy whitespace
    return " ".join((s or "").split())

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

HIT_TAGLINE = "intern · new grad · entry level"

def _where(j) -> str:
    where = [_clean(j["location"])] if j["location"] else []
    if j["remote"] and "remote" not in (j["location"] or "").lower():
        where.append("Remote")
    return " · ".join(dict.fromkeys(where))

def hit_messages(rows) -> list[tuple[list, str, dict]]:
    """🎯 HIT messages (style B): bold HTML, numbered jobs, an Apply link and an Apply button per
    job, no link previews. Returns [(rows, html_text, sendMessage options)], split like chunk()."""
    n, e = len(rows), html.escape
    number = {id(r): i for i, r in enumerate(rows, 1)}
    items = []
    for r in rows:
        where = _where(r)
        items.append((r, f"{number[id(r)]}. <b>{e(_clean(r['company']))}</b> — {e(_clean(r['title']))}\n"
                         + (f"📍 {e(where)} · " if where else "") + f'<a href="{e(r["url"])}">Apply ↗</a>'))
    header = f"<b>🎯 HIT · {n} target role{'s' * (n != 1)}</b>\n<i>{HIT_TAGLINE}</i>"
    out = []
    for chunk_rows, text in chunk(header, items):
        buttons = [[{"text": f"Apply {number[id(r)]} · {_clean(r['company'])}"[:60], "url": r["url"]}] for r in chunk_rows]
        out.append((chunk_rows, text, {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                                       "reply_markup": {"inline_keyboard": buttons}}))
    return out
