"""SQLite state: which jobs we've seen, when they closed, and whether we've notified."""
import sqlite3
from dataclasses import asdict
from pathlib import Path
from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    company     TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    ats         TEXT,
    title       TEXT,
    url         TEXT,
    location    TEXT,
    remote      INTEGER,
    department  TEXT,
    posted_at   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    closed_at   TEXT,
    -- NULL = pending, 'sent', 'filtered' (didn't match), 'baseline' (existed on first poll)
    notify_state TEXT,
    notified_at  TEXT,
    PRIMARY KEY (company, job_id)
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(company) WHERE notify_state IS NULL;
CREATE TABLE IF NOT EXISTS company_status (
    company     TEXT PRIMARY KEY,
    last_ok     TEXT,
    last_count  INTEGER,
    last_error_at TEXT,
    last_error  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failing_since TEXT,
    alert_state TEXT      -- NULL = fine; 'failing' | 'empty' | 'shrunk' once alerted
);
CREATE TABLE IF NOT EXISTS runs (
    started_at  TEXT PRIMARY KEY,
    finished_at TEXT,
    companies   INTEGER,
    failed      INTEGER,
    new_jobs    INTEGER,
    notified    INTEGER
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_FIELDS = ("ats", "title", "url", "location", "remote", "department", "posted_at")

class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(company_status)")}
        if "alert_state" not in cols:
            self.db.execute("ALTER TABLE company_status ADD COLUMN alert_state TEXT")

    def sync(self, company: str, jobs: list[Job], complete: bool, now: str, allow_empty: bool = False) -> dict:
        """Upsert one company's current listing. Returns {baseline, new, reopened, closed}.

        The first time a company is seen, its jobs are stored as 'baseline' so we don't
        announce hundreds of existing postings. Jobs absent from a complete, non-empty
        listing are marked closed (an empty listing only closes with allow_empty).
        """
        existing = {r["job_id"]: r["closed_at"] for r in self.db.execute(
            "SELECT job_id, closed_at FROM jobs WHERE company = ?", (company,))}
        baseline = not existing
        new, reopened = [], []
        with self.db:
            for j in jobs:
                vals = {k: asdict(j)[k] for k in _FIELDS}
                if j.job_id not in existing:
                    self.db.execute(
                        f"INSERT INTO jobs (company, job_id, {', '.join(_FIELDS)}, first_seen, last_seen, notify_state)"
                        f" VALUES (?, ?, {', '.join('?' * len(_FIELDS))}, ?, ?, ?)",
                        (company, j.job_id, *vals.values(), now, now, "baseline" if baseline else None))
                    existing[j.job_id] = None
                    new.append(j)
                else:
                    if existing[j.job_id] is not None:
                        reopened.append(j)
                    self.db.execute(
                        f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in _FIELDS)}, last_seen = ?, closed_at = NULL"
                        " WHERE company = ? AND job_id = ?",
                        (*vals.values(), now, company, j.job_id))
            closed = []
            if complete and (jobs or allow_empty):
                closed = [r["job_id"] for r in self.db.execute(
                    "SELECT job_id FROM jobs WHERE company = ? AND closed_at IS NULL AND last_seen < ?",
                    (company, now))]
                self.db.execute(
                    "UPDATE jobs SET closed_at = ? WHERE company = ? AND closed_at IS NULL AND last_seen < ?",
                    (now, company, now))
        return {"baseline": baseline, "new": new, "reopened": reopened, "closed": closed}

    def open_count(self, company: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE company = ? AND closed_at IS NULL",
                               (company,)).fetchone()[0]

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM jobs WHERE notify_state IS NULL AND closed_at IS NULL"
            " ORDER BY company, first_seen").fetchall()

    def mark(self, rows, state: str, now: str):
        with self.db:
            self.db.executemany(
                "UPDATE jobs SET notify_state = ?, notified_at = ? WHERE company = ? AND job_id = ?",
                [(state, now, r["company"], r["job_id"]) for r in rows])

    # --- health bookkeeping -------------------------------------------------

    def record_ok(self, company: str, count: int, now: str):
        with self.db:
            self.db.execute(
                "INSERT INTO company_status (company, last_ok, last_count, consecutive_failures)"
                " VALUES (?, ?, ?, 0) ON CONFLICT(company) DO UPDATE SET last_ok = excluded.last_ok,"
                " last_count = excluded.last_count, consecutive_failures = 0, failing_since = NULL",
                (company, now, count))

    def record_fail(self, company: str, error: str, now: str):
        with self.db:
            self.db.execute(
                "INSERT INTO company_status (company, last_error_at, last_error, consecutive_failures, failing_since)"
                " VALUES (?, ?, ?, 1, ?) ON CONFLICT(company) DO UPDATE SET last_error_at = excluded.last_error_at,"
                " last_error = excluded.last_error, consecutive_failures = consecutive_failures + 1,"
                " failing_since = COALESCE(failing_since, excluded.failing_since)",
                (company, now, error, now))

    def record_run(self, started: str, finished: str, companies: int, failed: int, new_jobs: int, notified: int):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                            (started, finished, companies, failed, new_jobs, notified))

    def set_alert_state(self, company: str, state: str | None) -> str | None:
        """Store the new alert state; returns the previous one."""
        row = self.db.execute("SELECT alert_state FROM company_status WHERE company = ?", (company,)).fetchone()
        prev = row[0] if row else None
        with self.db:
            self.db.execute("UPDATE company_status SET alert_state = ? WHERE company = ?", (state, company))
        return prev

    def company_status(self) -> dict[str, sqlite3.Row]:
        return {r["company"]: r for r in self.db.execute("SELECT * FROM company_status")}

    def runs_since(self, ts: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at", (ts,)).fetchall()

    def sent_since(self, ts: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE notify_state = 'sent' AND notified_at >= ?",
                               (ts,)).fetchone()[0]

    def total_open(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()[0]

    def get(self, k: str) -> str | None:
        r = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return r[0] if r else None

    def set(self, k: str, v: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, v))
