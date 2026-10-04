"""Helpers shared by all fetchers."""
import httpx
from ..models import Job, FetchResult  # noqa: F401  (re-exported for fetcher modules)

DEFAULT_MAX_JOBS = 2000  # per search, for paginated sources

def remote_hint(*texts) -> bool | None:
    return True if any(t and "remote" in t.lower() for t in texts) else None

async def get_json(client: httpx.AsyncClient, url: str, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()

def searches(co, default=None) -> list:
    """Most fetchers accept `searches` in config: one query per entry, results unioned."""
    return co.get("searches") or [default]

def dedup(jobs: list[Job]) -> list[Job]:
    seen, out = set(), []
    for j in jobs:
        if j.job_id not in seen:
            seen.add(j.job_id)
            out.append(j)
    return out
