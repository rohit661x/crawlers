"""Normalized job record every fetcher produces, regardless of ATS."""
from dataclasses import dataclass, field

@dataclass
class Job:
    company: str
    ats: str
    job_id: str               # stable per company; primary key with `company`
    title: str
    url: str
    location: str = ""
    remote: bool | None = None  # None = ATS didn't say
    department: str = ""
    posted_at: str | None = None  # ISO8601 when the ATS gives one

@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    # False when we stopped early (max_jobs cap), so missing jobs must not be marked closed.
    complete: bool = True
    skipped: int = 0          # records that failed to parse (see fetchers.base.RecordGuard)
    skip_error: str = ""      # the first such error, for logs/alerts
