"""Playwright fetchers: take a browser context instead of an httpx client."""
import asyncio
from urllib.parse import urljoin
from .base import Job, FetchResult, RecordGuard, remote_hint, searches, dedup

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
            await page.goto(f"https://www.metacareers.com/jobs?sort_by_new=true&{q}", wait_until="domcontentloaded")
            all_jobs = await asyncio.wait_for(got, timeout=45)
        except asyncio.TimeoutError:
            raise ValueError(f"meta: no job data for search {q!r} (page format changed?)")
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

async def custom(ctx, co) -> FetchResult:
    """Self-hosted career pages: every element matching link_selector is a job link.
    Optional title_selector / location_selector are looked up inside each link;
    without them the link text is the title."""
    page = await ctx.new_page()
    try:
        await page.goto(co["url"], wait_until="domcontentloaded")
        await page.wait_for_selector(co.get("wait_selector") or co["link_selector"], timeout=20000)
        links = await page.eval_on_selector_all(co["link_selector"], """(els, [ts, ls]) => els.map(e => {
            const pick = s => s ? (e.querySelector(s)?.innerText ?? '') : null;
            return [e.getAttribute('href'), pick(ts) ?? e.innerText, pick(ls) ?? ''];
        })""", [co.get("title_selector"), co.get("location_selector")])
    finally:
        await page.close()
    jobs, guard = [], RecordGuard()
    for href, title, loc in links:
        with guard:
            if not href:
                continue
            url = urljoin(co["url"], href)
            title, loc = " ".join((title or "").split()), " ".join((loc or "").split())
            jobs.append(Job(company=co["name"], ats="custom", job_id=url, title=title, url=url,
                            location=loc, remote=remote_hint(title, loc)))
    return guard.result(dedup(jobs))
