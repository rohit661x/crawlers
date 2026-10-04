"""Standard ATS job boards with public JSON APIs.

Config keys: slug (greenhouse/lever/ashby/smartrecruiters), region = "eu" (lever),
url + searches (workday), max_jobs (paginated sources).
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from .base import Job, FetchResult, DEFAULT_MAX_JOBS, remote_hint as _remote_hint, get_json as _get_json, searches, dedup

async def greenhouse(client, co) -> FetchResult:
    slug = co["slug"]
    data = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                           params={"content": "true"})
    jobs = []
    for j in data["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        loc_type = next((m.get("value") for m in j.get("metadata") or []
                         if (m.get("name") or "").lower() == "location type"), None)
        jobs.append(Job(
            company=co["name"], ats="greenhouse", job_id=str(j["id"]), title=j["title"],
            url=j["absolute_url"], location=loc,
            remote=_remote_hint(loc, loc_type if isinstance(loc_type, str) else None),
            department=", ".join(d["name"] for d in j.get("departments") or []),
            posted_at=j.get("first_published") or j.get("updated_at"),
        ))
    return FetchResult(jobs)

async def lever(client, co) -> FetchResult:
    host = "api.eu.lever.co" if co.get("region") == "eu" else "api.lever.co"
    data = await _get_json(client, f"https://{host}/v0/postings/{co['slug']}", params={"mode": "json"})
    jobs = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or ([cat["location"]] if cat.get("location") else [])
        wt = (j.get("workplaceType") or "").lower()
        jobs.append(Job(
            company=co["name"], ats="lever", job_id=j["id"], title=j["text"], url=j["hostedUrl"],
            location="; ".join(locs),
            remote=True if wt == "remote" else False if wt in ("onsite", "hybrid") else _remote_hint(*locs),
            department=cat.get("department") or cat.get("team") or "",
            posted_at=datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat()
                      if j.get("createdAt") else None,
        ))
    return FetchResult(jobs)

async def ashby(client, co) -> FetchResult:
    data = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{co['slug']}")
    jobs = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        locs = [l for l in locs if l]
        wt = (j.get("workplaceType") or "").lower()
        remote = j.get("isRemote")
        if remote is None:
            remote = True if wt == "remote" else _remote_hint(*locs)
        jobs.append(Job(
            company=co["name"], ats="ashby", job_id=j["id"], title=j["title"], url=j["jobUrl"],
            location="; ".join(locs), remote=remote,
            department=j.get("department") or j.get("team") or "", posted_at=j.get("publishedAt"),
        ))
    return FetchResult(jobs)

async def smartrecruiters(client, co) -> FetchResult:
    slug, cap = co["slug"], co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, offset, total = [], 0, None
    while total is None or offset < total:
        if offset >= cap:
            return FetchResult(jobs, complete=False)
        data = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                               params={"limit": 100, "offset": offset})
        total = data.get("totalFound", 0)
        page = data.get("content") or []
        if not page:
            break
        for j in page:
            loc = j.get("location") or {}
            jobs.append(Job(
                company=co["name"], ats="smartrecruiters", job_id=str(j["id"]), title=j["name"],
                url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                location=loc.get("fullLocation") or loc.get("city") or "",
                remote=bool(loc["remote"]) if "remote" in loc else None,
                department=(j.get("department") or {}).get("label", ""),
                posted_at=j.get("releasedDate"),
            ))
        offset += len(page)
    return FetchResult(jobs)

WORKDAY_TOTAL_CEILING = 2000  # Workday never reports/serves more than this per search
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_WD_REQ = re.compile(r"_([A-Za-z0-9-]+)$")

async def workday(client, co) -> FetchResult:
    """url: the public careers page, e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
    searches: list of search strings (default: everything)."""
    u = urlparse(co["url"])
    tenant = u.hostname.split(".")[0]
    site = [p for p in u.path.split("/") if p and not _LOCALE.match(p)][-1]
    endpoint = f"https://{u.hostname}/wday/cxs/{tenant}/{site}/jobs"
    cap = co.get("max_jobs", DEFAULT_MAX_JOBS)
    jobs, complete = [], True
    for q in searches(co, ""):
        offset, total = 0, None
        while total is None or offset < total:
            if offset >= cap:
                complete = False
                break
            r = await client.post(endpoint, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                                  "searchText": q})
            r.raise_for_status()
            data = r.json()
            if total is None:  # only the first page reports the real total
                total = data.get("total", 0)
            page = data.get("jobPostings") or []
            if not page:
                break
            for j in page:
                path = j.get("externalPath", "")
                m = _WD_REQ.search(path.rsplit("/", 1)[-1])
                loc = j.get("locationsText", "")
                jobs.append(Job(
                    company=co["name"], ats="workday", job_id=m.group(1) if m else path, title=j["title"],
                    url=f"https://{u.hostname}/{site}{path}", location=loc,
                    remote=True if "remote" in (j.get("remoteType") or "").lower() else _remote_hint(loc),
                ))
            offset += len(page)
        # at the ceiling the listing is likely cut off; narrow it with `searches` in config
        complete = complete and (total or 0) < WORKDAY_TOTAL_CEILING
    return FetchResult(dedup(jobs), complete)
