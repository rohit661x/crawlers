"""Outbox: messages survive send failures and are retried next run instead of being lost."""
import pytest
from crawlers.careers import notify
from conftest import job

NOW = "2026-10-06T12:00:00+00:00"

@pytest.fixture
def sender(monkeypatch):
    """Fake hermes: records texts; fails for any text in `fail`."""
    class Sender:
        sent, fail = [], set()
        def __call__(self, text):
            if text in self.fail or "*" in self.fail:
                return "exit 1: telegram down"
            self.sent.append(text)
            return None
    s = Sender(); s.sent, s.fail = [], set()
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
