"""Playwright fetchers: take a browser context instead of an httpx client."""
import asyncio, re
from urllib.parse import urljoin, urlparse
from .base import Job, FetchResult, RecordGuard, remote_hint, searches, dedup

META_RETRY_STATUS = {429, 500, 502, 503, 504}
META_RELOAD_WAITS = (15, 30)  # seconds before each reload; stays well inside the company timeout

async def meta(ctx, co) -> FetchResult:
    """metacareers.com blocks plain HTTP; load the search page and capture the GraphQL
    response the page itself makes. searches: query strings, e.g. "roles[0]=Internship"."""
    jobs, guard = [], RecordGuard()
    for q in searches(co, ""):
        page = await ctx.new_page()
        got: asyncio.Future = asyncio.get_running_loop().create_future()

        async def on_response(resp):
            if "graphql" not in resp.url or got.done():
                return
            try:
                data = (await resp.json()).get("data") or {}
            except Exception:
                return
            res = data.get("job_search_with_featured_jobs_v2") or {}
            if "all_jobs" in res and not got.done():
                got.set_result(res["all_jobs"])

        page.on("response", on_response)
        try:
            # metacareers intermittently answers the page load itself with 429; the page then
            # never queries GraphQL, so reload (same context keeps the cookies) after a pause
            for wait in (*META_RELOAD_WAITS, None):
                resp = await page.goto(f"https://www.metacareers.com/jobsearch/?sort_by_new=true&{q}",
                                       wait_until="domcontentloaded")
                status = resp.status if resp else 0
                if status not in META_RETRY_STATUS:
                    break
                if wait is None:
                    raise ValueError(f"meta: HTTP {status} loading search {q!r}")
                await asyncio.sleep(wait)
            all_jobs = await asyncio.wait_for(got, timeout=45)
        except asyncio.TimeoutError:
            raise ValueError(f"meta: no job data for search {q!r} (HTTP {status}; page format changed?)")
        finally:
            await page.close()
        for j in all_jobs:
            with guard:
                locs = j.get("locations") or []
                jobs.append(Job(
                    company=co["name"], ats="meta", job_id=str(j["id"]), title=j["title"],
                    url=f"https://www.metacareers.com/profile/job_details/{j['id']}",
                    location="; ".join(locs), remote=remote_hint(*locs),
                    department=", ".join(j.get("teams") or []),
                ))
    return guard.result(dedup(jobs))

_SLUG_ID = re.compile(r"[_-]+[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$|_[^_]*\d[^_]*$", re.I)

def slug_title(url: str) -> str:
    """Title from a job URL's last segment, minus its id: .../applied-ml-engineers_9f16f862-...
    -> "Applied Ml Engineers". Only a fallback; the job is marked title_guessed."""
    slug = _SLUG_ID.sub("", urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    return " ".join(w.capitalize() for w in re.split(r"[-_\s]+", slug) if w)

# A job link's title: title_selector, else the element its aria-labelledby names, else its first
# heading, else its first line. Location: location_selector, else aria-describedby. Page redesigns
# drop tags and classes (Shopify went h4 -> h3, lost .location); accessible names tend to survive.
_LINKS_JS = """(els, [ts, ls]) => els.map(e => {
    const text = n => n ? (n.innerText || '').trim() : '';
    const pick = s => s ? text(e.querySelector(s)) : '';
    const ref = a => (e.getAttribute(a) || '').split(/\\s+/).map(id => text(id && document.getElementById(id)))
                                                .filter(Boolean).join(' ');
    const title = pick(ts) || ref('aria-labelledby') || text(e.querySelector('h1,h2,h3,h4,h5,h6'))
                  || (e.innerText || '').trim().split('\\n')[0];
    return [e.getAttribute('href'), title, pick(ls) || ref('aria-describedby')];
})"""

async def custom(ctx, co) -> FetchResult:
    """Self-hosted career pages: every element matching link_selector is a job link. Title and
    location come from optional title_selector / location_selector inside each link, falling
    back as in _LINKS_JS, and finally to a title guessed from the URL slug."""
    page = await ctx.new_page()
    try:
        await page.goto(co["url"], wait_until="domcontentloaded")
        await page.wait_for_selector(co.get("wait_selector") or co["link_selector"], timeout=20000)
        links = await page.eval_on_selector_all(co["link_selector"], _LINKS_JS,
                                                [co.get("title_selector"), co.get("location_selector")])
    finally:
        await page.close()
    jobs, guard = [], RecordGuard()
    for href, title, loc in links:
        with guard:
            if not href:
                continue
            url = urljoin(co["url"], href)
            title, loc = " ".join((title or "").split()), " ".join((loc or "").split())
            guessed = not title
            title = title or slug_title(url)
            jobs.append(Job(company=co["name"], ats="custom", job_id=url, title=title, url=url,
                            location=loc, remote=remote_hint(title, loc), title_guessed=guessed))
    return guard.result(dedup(jobs))
