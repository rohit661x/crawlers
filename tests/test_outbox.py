"""Outbox: messages survive send failures and are retried next run instead of being lost."""
import httpx, pytest
from crawlers.careers import notify
from conftest import job

NOW = "2026-10-06T12:00:00+00:00"

DM, CHANNEL = "telegram", "telegram:-100123"

@pytest.fixture
def sender(monkeypatch):
    """Fake hermes: records (target, text) in `routed`; fails for any text or target in `fail`.
    Destinations: jobs -> the owner's DM and a channel, ops -> the DM only."""
    class Sender:
        def __call__(self, text, target):
            if {text, target, "*"} & self.fail:
                return "exit 1: telegram down"
            self.sent.append(text)
            self.routed.append((target, text))
            return None
    s = Sender(); s.sent, s.routed, s.fail = [], [], set()
    monkeypatch.setattr(notify, "send", s)
    monkeypatch.setattr(notify, "TARGETS", {"jobs": [DM, CHANNEL], "ops": [DM]})
    return s

def pending_job(store, job_id):
    store.sync("Acme", [job("seed")], True, "2026-10-06T09:00:00+00:00")       # baseline run
    store.sync("Acme", [job("seed"), job(job_id)], True, "2026-10-06T10:00:00+00:00")
    return [r for r in store.pending() if r["job_id"] == job_id]

def state(store, job_id):
    return store.db.execute("SELECT notify_state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]

def test_queued_jobs_are_not_pending_again(store):
    rows = pending_job(store, "j1")
    store.enqueue("new job", rows, NOW)
    assert state(store, "j1") == "queued" and store.pending() == []

def test_delivered_message_marks_jobs_sent(store, sender):
    store.enqueue("new job", pending_job(store, "j1"), NOW)
    assert notify.flush(store, NOW) == 1
    assert sender.sent == ["new job"] and store.outbox() == [] and state(store, "j1") == "sent"

def test_failed_send_keeps_message_for_next_run(store, sender):
    store.enqueue("alert A", now=NOW)
    store.enqueue("alert B", now=NOW)
    sender.fail = {"*"}
    assert notify.flush(store, NOW) == 0
    left = store.outbox()
    assert [m["text"] for m in left] == ["alert B", "alert A"]  # A failed once, so it goes after B
    assert left[1]["attempts"] == 1 and "telegram down" in left[1]["last_error"]
    assert left[0]["attempts"] == 0  # stopped after the first failure; B wasn't even tried
    sender.fail = set()
    notify.flush(store, NOW)
    assert sender.sent == ["alert B", "alert A"] and store.outbox() == []

def test_poisoned_message_doesnt_block_others(store, sender):
    store.enqueue("bad", now=NOW)
    sender.fail = {"bad"}
    notify.flush(store, NOW)                  # bad: attempts 1
    store.enqueue("good", now=NOW)
    notify.flush(store, NOW)                  # good goes first (0 attempts), then bad fails again
    assert sender.sent == ["good"]

def test_message_dropped_after_max_attempts(store, sender):
    store.enqueue("bad", pending_job(store, "j1"), NOW)
    sender.fail = {"bad"}
    for _ in range(3):
        notify.flush(store, NOW, max_attempts=3)
    assert store.outbox() == [] and state(store, "j1") == "dropped"

def test_oldest_undelivered(store):
    assert store.oldest_undelivered() is None
    store.enqueue("x", now="2026-10-06T01:00:00+00:00")
    store.enqueue("y", now="2026-10-06T05:00:00+00:00")
    assert store.oldest_undelivered() == "2026-10-06T01:00:00+00:00"

def post_job(store, job_id="j1", text="🆕 1 new job"):
    notify.post(store, text, pending_job(store, job_id), route="jobs", dry_run=False, now=NOW)

def test_jobs_fan_out_to_every_destination_ops_only_to_owner(store, sender):
    post_job(store)
    notify.post(store, "alert", route="ops", dry_run=False, now=NOW)
    assert sorted((m["route"], m["target"]) for m in store.outbox()) == sorted([("jobs", CHANNEL), ("jobs", DM), ("ops", DM)])
    assert notify.flush(store, NOW) == 1  # one job, counted once though it went to two places
    assert sorted(sender.routed) == sorted([(DM, "🆕 1 new job"), (CHANNEL, "🆕 1 new job"), (DM, "alert")])
    assert state(store, "j1") == "sent"

def test_down_channel_doesnt_block_dm(store, sender):
    # e.g. the bot was removed from the channel: the owner still gets jobs and alerts
    post_job(store, "j1", "job 1")
    post_job(store, "j2", "job 2")
    sender.fail = {CHANNEL}
    notify.flush(store, NOW)
    assert (DM, "job 1") in sender.routed and (DM, "job 2") in sender.routed
    assert all(t == DM for t, _ in sender.routed)
    left = {m["text"]: m["attempts"] for m in store.outbox()}
    assert left == {"job 1": 1, "job 2": 0}  # the channel stopped at its first failure
    assert state(store, "j1") == "sent"

def test_down_dm_doesnt_block_channel(store, sender):
    post_job(store)
    sender.fail = {DM}
    notify.flush(store, NOW)
    assert sender.routed == [(CHANNEL, "🆕 1 new job")]

def test_sent_is_final_when_another_copy_is_dropped(store, sender):
    post_job(store)
    sender.fail = {CHANNEL}
    notify.flush(store, NOW, max_attempts=1)  # DM delivered, channel copy dropped at once
    assert state(store, "j1") == "sent" and store.outbox() == []

def test_dropped_everywhere_is_dropped(store, sender):
    post_job(store)
    sender.fail = {"*"}
    notify.flush(store, NOW, max_attempts=1)
    assert state(store, "j1") == "dropped"

def test_nothing_new_posts_nothing():
    assert notify.chunk("🆕 0 new jobs", []) == []

def test_long_summary_splits_into_few_messages_not_one_per_job():
    items = [(n, "Acme — Software Engineer Intern\n📍 Toronto, ON\nhttps://example.com/" + "x" * 60) for n in range(80)]
    msgs = notify.chunk("🆕 80 new jobs", items)
    assert 1 < len(msgs) < 10 and all(len(m) <= notify.MAX_CHARS for _, m in msgs)
    assert sum(len(rows) for rows, _ in msgs) == 80

def test_targets_from_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_JOBS_TARGETS", " telegram , telegram:-100123,")
    assert notify._targets("NOTIFY_JOBS_TARGETS") == ["telegram", "telegram:-100123"]
    monkeypatch.delenv("NOTIFY_JOBS_TARGETS")
    assert notify._targets("NOTIFY_JOBS_TARGETS") == [notify.NOTIFY_TARGET]

@pytest.mark.parametrize("err, wait", [
    ("exit 1: flood_control:37.0", 37.0),
    ("exit 1: Too Many Requests: retry after 12", 12.0),
    ("exit 1: Flood control exceeded. Retry in 5 seconds", 5.0),
    ("exit 1: telegram down", None),
])
def test_retry_after_parsing(err, wait):
    assert notify.retry_after(err) == wait

@pytest.fixture
def fake_hermes(monkeypatch):
    """_send_once replays `replies`; sleeps are recorded, not slept."""
    calls, sleeps = [], []
    def once(text, target):
        calls.append(target)
        return fake_hermes.replies.pop(0)
    fake_hermes = type("F", (), {"calls": calls, "sleeps": sleeps, "replies": []})
    monkeypatch.setattr(notify, "_send_once", once)
    monkeypatch.setattr(notify.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(notify, "SEND_GAP_S", 0)
    return fake_hermes

def test_429_waits_as_long_as_telegram_asks(fake_hermes):
    fake_hermes.replies = [(False, "exit 1: Flood control exceeded. Retry in 7 seconds"), (True, "")]
    assert notify.send("x", CHANNEL) is None
    assert fake_hermes.calls == [CHANNEL, CHANNEL] and 7.0 in fake_hermes.sleeps

def test_long_429_penalty_waits_for_next_run(fake_hermes):
    fake_hermes.replies = [(False, "exit 1: flood_control:3600")]
    assert "flood_control" in notify.send("x", CHANNEL)
    assert len(fake_hermes.calls) == 1 and max(fake_hermes.sleeps, default=0) < notify.MAX_RETRY_AFTER_S

def test_post_rejects_unknown_route(store):
    with pytest.raises(ValueError):
        notify.post(store, "x", route="findings", dry_run=False, now=NOW)

def test_dry_run_shows_each_destination(store, sender, capsys):
    notify.post(store, "hello", route="jobs", dry_run=True, now=NOW)
    out = capsys.readouterr().out
    assert f"[jobs -> {DM}]\nhello" in out and f"[jobs -> {CHANNEL}]\nhello" in out
    assert store.outbox() == []

def test_old_outbox_rows_go_to_the_owner(tmp_path, sender):
    import sqlite3
    from crawlers.careers.store import Store
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                " route TEXT NOT NULL DEFAULT 'ops', text TEXT NOT NULL, jobs TEXT,"
                " attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    old.execute("INSERT INTO outbox (created_at, text) VALUES (?, ?)", (NOW, "queued before routing"))
    old.execute("INSERT INTO outbox (created_at, route, text) VALUES (?, 'findings', ?)", (NOW, "old group digest"))
    old.commit(); old.close()
    s = Store(db)
    assert {m["route"] for m in s.outbox()} == {"ops"} and {m["target"] for m in s.outbox()} == {None}
    notify.flush(s, NOW)
    assert all(t == DM for t, _ in sender.routed)  # never the channel

def test_failing_destination_alerts_owner_once_then_recovery(store, sender):
    post_job(store)
    sender.fail = {CHANNEL}
    notify.flush(store, NOW)
    notify.flush(store, NOW)  # still down: no second alert
    alerts = [t for _, t in sender.routed if "Can't deliver" in t]
    assert len(alerts) == 1 and CHANNEL in alerts[0] and "telegram down" in alerts[0]
    assert all(t == DM for t, _ in sender.routed)
    sender.fail = set()
    notify.flush(store, NOW)
    assert (CHANNEL, "🆕 1 new job") in sender.routed
    assert sender.routed[-1][0] == DM and "again" in sender.routed[-1][1]
    n = len(sender.routed)
    notify.flush(store, NOW)  # nothing new to say
    assert len(sender.routed) == n

def test_destination_alert_queued_when_owner_also_down(store, sender):
    post_job(store)
    sender.fail = {"*"}
    notify.flush(store, NOW)
    queued = [m for m in store.outbox() if "Can't deliver" in m["text"]]
    assert [(m["route"], m["target"]) for m in queued] == [("ops", DM)]

def test_emptied_queue_is_not_recovery(store, sender):
    post_job(store)
    sender.fail = {CHANNEL}
    notify.flush(store, NOW, max_attempts=1)  # channel copy dropped on its first failure
    sender.fail = set()
    notify.flush(store, NOW)
    assert not any("again" in t for _, t in sender.routed)

TOKEN = "123456:SECRET-test-token"

@pytest.fixture
def bot_api(monkeypatch):
    """Fake Bot API: records (url, payload); answers with `bot_api.reply` (status, json) or raises it."""
    class Api:
        calls, reply = [], (200, {"ok": True, "result": {"message_id": 1}})
    def post(url, json, timeout):
        Api.calls.append((url, json))
        if isinstance(Api.reply, Exception):
            raise Api.reply
        status, body = Api.reply
        return httpx.Response(status, json=body)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "8093975156")
    monkeypatch.setattr(notify.httpx, "post", post)
    Api.calls = []
    return Api

def test_sends_plain_text_to_home_channel_and_explicit_chats(bot_api):
    assert notify._send_once("hi *there*", "telegram") == (True, "")
    assert notify._send_once("hi", "telegram:-1003936325725") == (True, "")
    assert notify._send_once("hi", "telegram:-1001:17") == (True, "")
    (url, first), (_, second), (_, third) = bot_api.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert first == {"chat_id": "8093975156", "text": "hi *there*"}  # no parse_mode: sent verbatim
    assert second["chat_id"] == "-1003936325725" and third["message_thread_id"] == 17

def test_429_reply_feeds_retry_after(bot_api):
    bot_api.reply = (429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 12",
                           "parameters": {"retry_after": 12}})
    ok, err = notify._send_once("x", "telegram")
    assert not ok and notify.retry_after(err) == 12

def test_errors_never_leak_the_token(bot_api):
    bot_api.reply = httpx.ConnectError(f"failed: https://api.telegram.org/bot{TOKEN}/sendMessage")
    ok, err = notify._send_once("x", "telegram")
    assert not ok and TOKEN not in err and "<token>" in err

def test_rejected_post_reports_telegram_reason(bot_api):
    bot_api.reply = (403, {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member of the channel chat"})
    ok, err = notify._send_once("x", "telegram:-1003936325725")
    assert not ok and "not a member" in err and "403" in err

def test_unsupported_or_missing_target(bot_api, monkeypatch):
    assert "unsupported target" in notify._send_once("x", "discord:#ops")[1]
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL")
    monkeypatch.setattr(notify, "HERMES_ENV", notify.Path("/nonexistent/.env"))
    assert "no chat id" in notify._send_once("x", "telegram")[1]
    assert bot_api.calls == []

def test_token_read_from_hermes_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('OTHER=1\nTELEGRAM_BOT_TOKEN="abc:def"\n')
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(notify, "HERMES_ENV", env)
    assert notify._hermes_env("TELEGRAM_BOT_TOKEN") == "abc:def"

def test_titles_go_out_verbatim():
    row = {"company": "Acme", "title": "C_Sharp [Backend]   Intern", "location": "Toronto", "remote": 0,
           "url": "https://example.com/1"}
    assert notify.format_job(row).splitlines()[0] == "Acme — C_Sharp [Backend] Intern"
