"""Run-level decisions: parse verdicts, alert delivery and the dead-man heartbeat."""
from datetime import datetime, timedelta, timezone
import pytest
from crawlers.careers import health, notify, main as poller
from crawlers.careers.models import FetchResult
from conftest import job

def test_parse_verdict_clean_listing():
    assert poller.parse_verdict(FetchResult([job("a")]), 0.05) == (None, True, "")

def test_parse_verdict_few_skips_stay_complete():
    err, complete, note = poller.parse_verdict(FetchResult([job(str(i)) for i in range(99)], skipped=1,
                                                           skip_error="KeyError: 'title'"), 0.05)
    assert err is None and complete and "1/100" in note

def test_parse_verdict_many_skips_pause_closures():
    err, complete, _ = poller.parse_verdict(FetchResult([job("a")] * 9, skipped=1), 0.05)
    assert err is None and not complete

def test_parse_verdict_all_bad_is_a_failure():
    err, complete, note = poller.parse_verdict(FetchResult([], skipped=4, skip_error="KeyError: 'title'"), 0.05)
    assert isinstance(err, ValueError) and "all 4/4" in str(err) and not complete

def test_alerts_go_through_outbox(store):
    # transition() records the new state at once; the message must not be lost if sending fails
    msg = poller.transition(store, "Acme", "failing", fails=3, error="HTTP 500")
    notify.post(store, msg, dry_run=False, now="t")
    assert [m["text"] for m in store.outbox()] == [msg]

@pytest.fixture
def pings(monkeypatch):
    got = []
    monkeypatch.setattr(health, "ping", lambda status="", body="", url=None: got.append((status, body)))
    return got

def test_heartbeat_ok(store, pings):
    poller.heartbeat(store, datetime.now(timezone.utc), 128, 1, 2)
    assert pings == [("", "128 companies, 1 failed, 2 notified")]

def test_heartbeat_fails_when_outbox_is_stuck(store, pings):
    now = datetime.now(timezone.utc)
    store.enqueue("x", now=(now - timedelta(hours=5)).isoformat())
    poller.heartbeat(store, now, 128, 0, 0)
    assert pings[0][0] == "fail" and "undelivered" in pings[0][1]

def test_heartbeat_ok_while_outbox_is_fresh(store, pings):
    now = datetime.now(timezone.utc)
    store.enqueue("x", now=(now - timedelta(minutes=30)).isoformat())
    poller.heartbeat(store, now, 128, 0, 0)
    assert pings[0][0] == ""

def test_crash_pings_fail_and_reraises(monkeypatch, pings):
    monkeypatch.setattr(poller, "parse_args", lambda: type("A", (), {"dry_run": False, "only": None})())
    monkeypatch.setattr(poller, "run", lambda args: (_ for _ in ()).throw(RuntimeError("db locked")))
    with pytest.raises(RuntimeError):
        poller.main()
    assert pings == [("fail", "crashed: RuntimeError: db locked")]

def test_dry_run_crash_doesnt_ping(monkeypatch, pings):
    monkeypatch.setattr(poller, "parse_args", lambda: type("A", (), {"dry_run": True, "only": None})())
    monkeypatch.setattr(poller, "run", lambda args: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        poller.main()
    assert pings == []

def test_ping_without_url_is_a_noop():
    assert health.ping(url="") is False
