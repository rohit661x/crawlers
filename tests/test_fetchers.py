"""Fetchers against canned API responses: malformed records are skipped, not fatal."""
import asyncio, json
import httpx
from crawlers.careers.fetchers.ats import greenhouse, workday

def run(fetcher, co, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetcher(client, co)
    return asyncio.run(go())

def wd_posting(n: int, **over) -> dict:
    p = {"title": f"Engineer Intern {n}", "externalPath": f"/job/Santa-Clara/Engineer-Intern_JR{n}",
         "locationsText": "Santa Clara, CA, US"}
    p.update(over)
    return p

def wd_handler(pages: list[dict]):
    """Serve `pages` in order of the request's offset (20 per page, like Workday)."""
    def handler(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        return httpx.Response(200, json=pages[offset // 20] if offset // 20 < len(pages) else {"jobPostings": []})
    return handler

WD = {"name": "NVIDIA", "ats": "workday", "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"}

def test_workday_skips_posting_without_title():
    # the real failure: one posting without `title` raised KeyError and failed all of NVIDIA
    postings = [wd_posting(1), {"externalPath": "/job/x/No-Title_JR2"}, wd_posting(3)]
    res = run(workday, WD, wd_handler([{"total": 3, "jobPostings": postings}]))
    assert [j.job_id for j in res.jobs] == ["JR1", "JR3"]
    assert res.skipped == 1 and "KeyError" in res.skip_error
    assert res.complete

def test_workday_pagination_drift_marks_incomplete():
    # total says 21, but results shifted while paging: page 2 repeats a page-1 posting
    page1 = {"total": 21, "jobPostings": [wd_posting(n) for n in range(20)]}
    page2 = {"total": 0, "jobPostings": [wd_posting(5)]}
    res = run(workday, WD, wd_handler([page1, page2]))
    assert len(res.jobs) == 20
    assert not res.complete  # job #20 is missing from this run; it must not be closed

def test_workday_full_listing_is_complete():
    page1 = {"total": 21, "jobPostings": [wd_posting(n) for n in range(20)]}
    page2 = {"total": 0, "jobPostings": [wd_posting(20)]}
    res = run(workday, WD, wd_handler([page1, page2]))
    assert len(res.jobs) == 21 and res.complete and res.skipped == 0

def test_workday_myworkdaysite_url():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"total": 1, "jobPostings": [wd_posting(1, externalPath="/job/LA/X_R1")]})
    co = {"name": "Snap", "ats": "workday", "url": "https://wd1.myworkdaysite.com/recruiting/snapchat/snap"}
    res = run(workday, co, handler)
    assert seen[0] == "https://wd1.myworkdaysite.com/wday/cxs/snapchat/snap/jobs"
    assert res.jobs[0].url == "https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/LA/X_R1"

def test_greenhouse_skips_malformed_record():
    data = {"jobs": [
        {"id": 1, "title": "SWE Intern", "absolute_url": "https://x/1", "location": {"name": "NYC"}},
        {"title": "no id or url"},
        {"id": 3, "title": "Quant Intern", "absolute_url": "https://x/3", "location": None},
    ]}
    res = run(greenhouse, {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
              lambda request: httpx.Response(200, json=data))
    assert [j.job_id for j in res.jobs] == ["1", "3"]
    assert res.skipped == 1

def test_unexpected_errors_still_fail_the_company():
    # only parse errors are swallowed; an HTTP error must still surface as a failure
    import pytest
    with pytest.raises(httpx.HTTPStatusError):
        run(greenhouse, {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
            lambda request: httpx.Response(500, json={}))
