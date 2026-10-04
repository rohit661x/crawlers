"""Companies with their own careers backends (no standard ATS). All take `searches`:
a list of query-param dicts (strings for amazon), results unioned, newest first."""
import json, re
from datetime import datetime, timezone
from .base import Job, FetchResult, DEFAULT_MAX_JOBS, remote_hint, get_json, searches, dedup

def _ts(epoch) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

async def amazon(client, co) -> FetchResult:
    """amazon.jobs search.json. searches: base_query strings, e.g. ["intern", "entry level"]."""
    cap = co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, complete = [], True
    for q in searches(co, ""):
        offset, total = 0, None
        while total is None or offset < total:
            if offset >= cap:
                complete = False
                break
            d = await get_json(client, "https://www.amazon.jobs/en/search.json", params={
                "base_query": q, "sort": "recent", "result_limit": 100, "offset": offset})
            total, page = d.get("hits", 0), d.get("jobs") or []
            if not page:
                break
            for j in page:
                try:
                    posted = datetime.strptime(" ".join(j["posted_date"].split()), "%B %d, %Y").date().isoformat()
                except (KeyError, ValueError):
                    posted = None
                loc = j.get("normalized_location") or j.get("location") or ""
                jobs.append(Job(
                    company=co["name"], ats="amazon", job_id=str(j["id_icims"]), title=j["title"],
                    url="https://www.amazon.jobs" + j["job_path"], location=loc, remote=remote_hint(loc),
                    department=j.get("job_category") or "", posted_at=posted,
                ))
            offset += len(page)
    return FetchResult(dedup(jobs), complete)

async def eightfold(client, co) -> FetchResult:
    """Eightfold PCSX (Microsoft and others). host + domain from config;
    searches: param dicts, e.g. {filter_seniority = "Intern"}."""
    host, domain = co.get("host", "apply.careers.microsoft.com"), co.get("domain", "microsoft.com")
    cap = co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, complete = [], True
    for params in searches(co, {}):
        start, total = 0, None
        while total is None or start < total:
            if start >= cap:
                complete = False
                break
            d = (await get_json(client, f"https://{host}/api/pcsx/search", params={
                "domain": domain, "query": "", "location": "", "sort_by": "timestamp",
                "start": start, **params}))["data"]
            total, page = d.get("count", 0), d.get("positions") or []
            if not page:
                break
            for j in page:
                locs = j.get("standardizedLocations") or j.get("locations") or []
                wl = (j.get("workLocationOption") or "").lower()
                jobs.append(Job(
                    company=co["name"], ats="eightfold", job_id=str(j.get("displayJobId") or j["id"]),
                    title=j["name"], url=f"https://{host}{j['positionUrl']}", location="; ".join(locs),
                    remote=True if "remote" in wl else False if wl == "onsite" else remote_hint(*locs),
                    department=j.get("department") or "", posted_at=_ts(j.get("postedTs")),
                ))
            start += len(page)
    return FetchResult(dedup(jobs), complete)

_GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", re.S)

async def google(client, co) -> FetchResult:
    """Parses the job list embedded in the careers results page (no public API).
    searches: param dicts, e.g. {target_level = "INTERN_AND_APPRENTICE"}."""
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    cap = co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, complete = [], True
    for params in searches(co, {}):
        page_no, seen, total = 1, 0, None
        while total is None or seen < total:
            if seen >= cap:
                complete = False
                break
            r = await client.get(base, params={"sort_by": "date", "page": page_no, **params},
                                 headers={"Accept": "text/html"})
            r.raise_for_status()
            m = _GOOGLE_DATA.search(r.text)
            if not m:
                raise ValueError("google: job data block not found (page format changed?)")
            d = json.loads(m.group(1))
            page, total = d[0] or [], d[2] if len(d) > 2 and d[2] is not None else 0
            if not page:
                break
            for j in page:
                locs = [l[0] for l in (j[9] or []) if l]
                jobs.append(Job(
                    company=co["name"], ats="google", job_id=str(j[0]), title=j[1],
                    url=f"{base}{j[0]}-{_slug(j[1])}", location="; ".join(locs), remote=remote_hint(*locs),
                    posted_at=_ts((j[12] or [None])[0]),
                ))
            seen += len(page)
            page_no += 1
    return FetchResult(dedup(jobs), complete)

_APPLE_DATA = re.compile(r'window\.__staticRouterHydrationData = JSON\.parse\("(.*?)"\);', re.S)

async def apple(client, co) -> FetchResult:
    """Parses the hydration JSON in jobs.apple.com search pages.
    searches: param dicts, e.g. {team = "internships-STDNT-INTRN"}."""
    cap = co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, complete = [], True
    for params in searches(co, {}):
        page_no, seen, total = 1, 0, None
        while total is None or seen < total:
            if seen >= cap:
                complete = False
                break
            r = await client.get("https://jobs.apple.com/en-us/search",
                                 params={"sort": "newest", "page": page_no, **params},
                                 headers={"Accept": "text/html"})
            r.raise_for_status()
            m = _APPLE_DATA.search(r.text)
            if not m:
                raise ValueError("apple: hydration data not found (page format changed?)")
            s = json.loads(json.loads(f'"{m.group(1)}"'))["loaderData"]["search"]
            page, total = s.get("searchResults") or [], s.get("totalRecords", 0)
            if not page:
                break
            for j in page:
                locs = [", ".join(x for x in (l.get("name"), l.get("countryName")) if x)
                        for l in j.get("locations") or []]
                jobs.append(Job(
                    company=co["name"], ats="apple", job_id=str(j.get("reqId") or j["id"]),
                    title=j["postingTitle"],
                    url=f"https://jobs.apple.com/en-us/details/{j['positionId']}/{j.get('transformedPostingTitle', '')}",
                    location="; ".join(locs), remote=True if j.get("homeOffice") else remote_hint(*locs),
                    department=(j.get("team") or {}).get("teamName", ""), posted_at=j.get("postDateInGMT"),
                ))
            seen += len(page)
            page_no += 1
    return FetchResult(dedup(jobs), complete)
