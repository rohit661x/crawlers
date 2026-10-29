"""Outbox: messages survive send failures and are retried next run instead of being lost."""
import pytest
from crawlers.careers import notify
from conftest import job

NOW = "2026-10-06T12:00:00+00:00"

@pytest.fixture
def sender(monkeypatch):
    """Fake hermes: records texts (and routes in `routed`); fails for any text or route in `fail`."""
    class Sender:
        def __call__(self, text, route):
            if {text, route, "*"} & self.fail:
                return "exit 1: telegram down"
            self.sent.append(text)
            self.routed.append((route, text))
            return None
    s = Sender(); s.sent, s.routed, s.fail = [], [], set()
    monkeypatch.setattr(notify, "send", s)
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

def test_messages_keep_their_route(store, sender):
    store.enqueue("new job", pending_job(store, "j1"), NOW, route="findings")
    store.enqueue("alert", now=NOW, route="ops")
    notify.flush(store, NOW)
    assert sender.routed == [("findings", "new job"), ("ops", "alert")]

def test_down_route_doesnt_block_others(store, sender):
    # e.g. the bot was removed from the group: ops alerts still reach the owner
    store.enqueue("job 1", now=NOW, route="findings")
    store.enqueue("job 2", now=NOW, route="findings")
    store.enqueue("alert", now=NOW, route="ops")
    sender.fail = {"findings"}
    notify.flush(store, NOW)
    assert sender.routed[0] == ("ops", "alert") and all(r == "ops" for r, _ in sender.routed)
    left = {m["text"]: m["attempts"] for m in store.outbox()}
    assert left == {"job 1": 1, "job 2": 0}  # findings stopped at its first failure

def test_routes_resolve_to_targets(monkeypatch):
    monkeypatch.setattr(notify, "TARGETS", {"findings": "telegram:-100123", "ops": "telegram"})
    calls = []
    monkeypatch.setattr(notify, "_send_once", lambda text, target: (calls.append(target), (True, ""))[1])
    monkeypatch.setattr(notify, "SEND_GAP_S", 0)
    assert notify.send("x", "findings") is None and notify.send("y", "ops") is None
    assert calls == ["telegram:-100123", "telegram"]
    assert "unknown route" in notify.send("z", "nope")

def test_post_rejects_unknown_route(store):
    with pytest.raises(ValueError):
        notify.post(store, "x", route="group", dry_run=False, now=NOW)

def test_dry_run_shows_route(store, capsys):
    notify.post(store, "hello", route="findings", dry_run=True, now=NOW)
    assert capsys.readouterr().out.startswith(f"[findings -> {notify.TARGETS['findings']}]\nhello")
    assert store.outbox() == []

def test_old_outbox_rows_default_to_ops(tmp_path):
    import sqlite3
    from crawlers.careers.store import Store
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                " text TEXT NOT NULL, jobs TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
    old.execute("INSERT INTO outbox (created_at, text) VALUES (?, ?)", (NOW, "queued before routing"))
    old.commit(); old.close()
    assert [m["route"] for m in Store(db).outbox()] == ["ops"]

def test_failing_route_alerts_owner_once_then_recovery(store, sender):
    store.enqueue("job 1", now=NOW, route="findings")
    sender.fail = {"findings"}
    notify.flush(store, NOW)
    notify.flush(store, NOW)  # still down: no second alert
    alerts = [t for r, t in sender.routed if r == "ops"]
    assert len(alerts) == 1 and "Can't deliver findings" in alerts[0] and "telegram down" in alerts[0]
    sender.fail = set()
    notify.flush(store, NOW)
    assert ("findings", "job 1") in sender.routed
    assert sender.routed[-1][0] == "ops" and "again" in sender.routed[-1][1]
    notify.flush(store, NOW)  # nothing new to say
    assert len([r for r, _ in sender.routed if r == "ops"]) == 2

def test_route_alert_queued_when_ops_also_down(store, sender):
    store.enqueue("job 1", now=NOW, route="findings")
    sender.fail = {"*"}
    notify.flush(store, NOW)
    assert [m["route"] for m in store.outbox() if "Can't deliver" in m["text"]] == ["ops"]

def test_emptied_queue_is_not_recovery(store, sender):
    store.enqueue("job 1", now=NOW, route="findings")
    sender.fail = {"findings"}
    notify.flush(store, NOW, max_attempts=1)  # dropped on its first failure
    sender.fail = set()
    notify.flush(store, NOW)
    assert not any("again" in t for _, t in sender.routed)
