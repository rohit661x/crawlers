"""Fetchers against canned API responses: malformed records are skipped, not fatal."""
import asyncio, json
import httpx
from crawlers.careers.fetchers.ats import greenhouse, workday
from crawlers.careers.fetchers.base import RetryClient

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

def test_workday_title_from_path_when_missing():
    # the real failure: postings without `title` raised KeyError and failed all of NVIDIA / TD Bank
    postings = [wd_posting(1), {"externalPath": "/job/x/Software-Engineer--New-Grad_JR2"}, wd_posting(3)]
    res = run(workday, WD, wd_handler([{"total": 3, "jobPostings": postings}]))
    assert [j.job_id for j in res.jobs] == ["JR1", "JR2", "JR3"]
    assert res.jobs[1].title == "Software Engineer New Grad"
    assert res.skipped == 0 and res.complete

def test_workday_all_titles_missing_is_not_a_failure():
    page = {"total": 2, "jobPostings": [{"externalPath": f"/job/x/Intern_JR{n}"} for n in (1, 2)]}
    res = run(workday, WD, wd_handler([page]))
    assert [j.title for j in res.jobs] == ["Intern", "Intern"] and res.skipped == 0

def test_workday_skips_posting_without_path():
    # no externalPath means no job id; storing it under "" would merge unrelated postings
    postings = [wd_posting(1), {"title": "Orphan"}, wd_posting(3)]
    res = run(workday, WD, wd_handler([{"total": 3, "jobPostings": postings}]))
    assert [j.job_id for j in res.jobs] == ["JR1", "JR3"]
    assert res.skipped == 1 and "externalPath" in res.skip_error
    # the orphan was returned, not missing: one bad record mustn't block a company's closures
    # (the real failure: TD Bank / NVIDIA closures skipped for days); parse_verdict judges the skips
    assert res.complete

def test_workday_pathless_record_does_not_mask_pagination_drift():
    # total 22; page 2 repeats a page-1 posting, so job #20 is missing even counting the orphan
    page1 = {"total": 22, "jobPostings": [wd_posting(n) for n in range(19)] + [{"title": "Orphan"}]}
    page2 = {"total": 0, "jobPostings": [wd_posting(5), wd_posting(21)]}
    res = run(workday, WD, wd_handler([page1, page2]))
    assert res.skipped == 1 and not res.complete

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

def run_retrying(fetcher, co, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetcher(RetryClient(client, min_interval=0, backoff=0), co)
    return asyncio.run(go())

def test_workday_retries_html_200():
    # the real failure: Workday sometimes answers 200 with an HTML page -> JSONDecodeError failed the company
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, text="<html>Service Unavailable</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, json={"total": 1, "jobPostings": [wd_posting(1)]})
    res = run_retrying(workday, WD, handler)
    assert len(calls) == 2 and [j.job_id for j in res.jobs] == ["JR1"] and res.complete

def test_workday_html_200_still_fails_after_retries():
    handler = lambda request: httpx.Response(200, text="<html></html>")
    try:
        run_retrying(workday, WD, handler)
    except ValueError:  # json.JSONDecodeError
        pass
    else:
        raise AssertionError("expected a JSON error once retries run out")

def test_retry_client_retries_cloudflare_520():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(520 if len(calls) == 1 else 200, json={"ok": True})
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await RetryClient(client, min_interval=0, backoff=0).get("https://x.test/")
    assert asyncio.run(go()).status_code == 200 and len(calls) == 2

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
