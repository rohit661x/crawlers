"""Helpers shared by all fetchers."""
import asyncio, logging, random, time
import httpx
from ..models import Job, FetchResult  # noqa: F401  (re-exported for fetcher modules)

log = logging.getLogger("careers.http")

DEFAULT_MAX_JOBS = 2000  # per search, for paginated sources

def remote_hint(*texts) -> bool | None:
    return True if any(t and "remote" in t.lower() for t in texts) else None

async def get_json(client: httpx.AsyncClient, url: str, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()

# Lookalike letters some sites use to defeat keyword search ("ꓟachine ꓡearning"): Lisu + Cyrillic
_CONFUSABLES = str.maketrans({
    "ꓐ": "B", "ꓑ": "P", "ꓓ": "D", "ꓔ": "T", "ꓖ": "G", "ꓗ": "K", "ꓙ": "J", "ꓚ": "C", "ꓜ": "Z", "ꓝ": "F",
    "ꓟ": "M", "ꓠ": "N", "ꓡ": "L", "ꓢ": "S", "ꓣ": "R", "ꓦ": "V", "ꓧ": "H", "ꓪ": "W", "ꓫ": "X", "ꓬ": "Y",
    "ꓮ": "A", "ꓰ": "E", "ꓲ": "I", "ꓳ": "O", "ꓴ": "U",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
})

def deconfuse(s: str) -> str:
    return s.translate(_CONFUSABLES)

class RecordGuard:
    """`with guard:` around one record's parsing. A malformed record (Workday intermittently
    omits `title`) is counted and skipped instead of failing the company's whole listing;
    main.py decides whether the skips make the listing too incomplete to close jobs from."""
    ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)

    def __init__(self):
        self.skipped, self.first_error = 0, ""

    def __enter__(self):
        return self

    def __exit__(self, et, e, tb):
        if et is None or not issubclass(et, self.ERRORS):
            return False
        self.skipped += 1
        if not self.first_error:
            self.first_error = f"{et.__name__}: {e}"[:200]
        log.debug("skipped malformed record: %r", e)
        return True

    def result(self, jobs: list[Job], complete: bool = True) -> FetchResult:
        return FetchResult(jobs, complete, self.skipped, self.first_error)

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

class RetryClient:
    """Wraps httpx.AsyncClient: per-host pacing plus retries with backoff on 429/5xx and
    network errors. Fetchers use it like a client (get/post); after the last retry the
    response is returned as-is so raise_for_status() reports the real status.

    min_interval      seconds between requests to one host (default for unlisted hosts)
    host_intervals    {host or domain suffix: seconds}, e.g. {"myworkdayjobs.com": 0.5}
    retries           extra attempts after the first
    max_wait          give up instead of sleeping longer than this (e.g. Retry-After: 3600)
    """
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, client: httpx.AsyncClient, min_interval: float = 0.25,
                 host_intervals: dict | None = None, retries: int = 3, backoff: float = 2.0,
                 max_wait: float = 90, **_ignored):
        self.client = client
        self.min_interval, self.host_intervals = min_interval, host_intervals or {}
        self.retries, self.backoff, self.max_wait = retries, backoff, max_wait
        self._locks: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}
        self.retried = 0  # for logging/health

    def _interval(self, host: str) -> float:
        for key, gap in self.host_intervals.items():
            if host == key or host.endswith("." + key):
                return gap
        return self.min_interval

    async def _pace(self, host: str):
        async with self._locks.setdefault(host, asyncio.Lock()):
            wait = self._next.get(host, 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next[host] = time.monotonic() + self._interval(host)

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None and (ra := resp.headers.get("retry-after", "")).isdigit():
            return float(ra)
        return self.backoff * 2 ** attempt * random.uniform(0.8, 1.2)

    async def request(self, method: str, url, **kw) -> httpx.Response:
        host = httpx.URL(str(url)).host
        for attempt in range(self.retries + 1):
            await self._pace(host)
            resp, err = None, None
            try:
                resp = await self.client.request(method, url, **kw)
                if resp.status_code not in self.RETRY_STATUS:
                    return resp
            except httpx.TransportError as e:  # timeouts, connection resets, DNS
                err = e
            wait = self._delay(attempt, resp)
            if attempt == self.retries or wait > self.max_wait:
                if err:
                    raise err
                return resp
            # push the whole host back, so concurrent requests to it also wait
            self._next[host] = max(self._next.get(host, 0), time.monotonic() + wait)
            self.retried += 1
            log.info("retry %s %s in %.0fs (attempt %d/%d): %s", method, host, wait, attempt + 1,
                     self.retries, f"HTTP {resp.status_code}" if resp is not None else repr(err))

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)
