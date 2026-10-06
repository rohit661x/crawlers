"""Closure hysteresis: a job closes only after missing from several complete listings."""
import sqlite3
from crawlers.careers.store import Store
from conftest import job

def runs(store, *listings, complete=True, close_after=3):
    """Sync each listing (a list of job ids) as one hourly run; returns the last change set."""
    ch = None
    for i, ids in enumerate(listings):
        ch = store.sync("Acme", [job(x) for x in ids], complete, f"2026-10-06T{10 + i:02d}:00:00+00:00",
                        close_after=close_after)
    return ch

def open_ids(store):
    return {r[0] for r in store.db.execute("SELECT job_id FROM jobs WHERE closed_at IS NULL")}

def test_first_listing_is_baseline(store):
    ch = runs(store, ["a", "b"])
    states = {r[0] for r in store.db.execute("SELECT notify_state FROM jobs")}
    assert ch["baseline"] and states == {"baseline"}

def test_two_misses_dont_close(store):
    ch = runs(store, ["a", "b"], ["a"], ["a"])
    assert ch["closed"] == [] and open_ids(store) == {"a", "b"}

def test_three_consecutive_misses_close(store):
    ch = runs(store, ["a", "b"], ["a"], ["a"], ["a"])
    assert ch["closed"] == ["b"]
    assert open_ids(store) == {"a"}

def test_reappearing_resets_the_count(store):
    # the flapping seen on Workday/Google: gone, gone, back, gone, gone => never closed
    runs(store, ["a", "b"], ["a"], ["a"], ["a", "b"], ["a"], ["a"])
    assert open_ids(store) == {"a", "b"}
    assert store.db.execute("SELECT missed FROM jobs WHERE job_id = 'b'").fetchone()[0] == 2

def test_incomplete_listings_dont_count(store):
    runs(store, ["a", "b"])
    for h in (11, 12, 13, 14):
        store.sync("Acme", [job("a")], False, f"2026-10-06T{h}:00:00+00:00")
    assert open_ids(store) == {"a", "b"}
    assert store.db.execute("SELECT missed FROM jobs WHERE job_id = 'b'").fetchone()[0] == 0

def test_close_after_one_closes_immediately(store):
    # used when a shrunk listing is accepted as real after shrink_accept_runs
    ch = runs(store, ["a", "b"], ["a"], close_after=1)
    assert ch["closed"] == ["b"]

def test_empty_listing_never_closes_without_allow_empty(store):
    runs(store, ["a"], [], [], [], [])
    assert open_ids(store) == {"a"}
    ch = store.sync("Acme", [], True, "2026-10-06T20:00:00+00:00", allow_empty=True, close_after=1)
    assert ch["closed"] == ["a"]

def test_reopened_job(store):
    ch = runs(store, ["a", "b"], ["a"], ["a"], ["a"], ["a", "b"])
    assert [j.job_id for j in ch["reopened"]] == ["b"] and open_ids(store) == {"a", "b"}

def test_migrates_old_db_without_missed_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (company TEXT NOT NULL, job_id TEXT NOT NULL, ats TEXT, title TEXT, url TEXT,"
               " location TEXT, remote INTEGER, department TEXT, posted_at TEXT, first_seen TEXT NOT NULL,"
               " last_seen TEXT NOT NULL, closed_at TEXT, notify_state TEXT, notified_at TEXT,"
               " PRIMARY KEY (company, job_id))")
    db.execute("INSERT INTO jobs (company, job_id, first_seen, last_seen) VALUES ('Acme', 'a', 't', 't')")
    db.commit(); db.close()
    store = Store(path)
    assert store.db.execute("SELECT missed FROM jobs").fetchone()[0] == 0
