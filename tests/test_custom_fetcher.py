"""The custom (browser) fetcher against real Chromium: titles survive markup changes."""
import asyncio, urllib.parse
import pytest
from playwright.async_api import async_playwright
from crawlers.careers.fetchers.browser import custom, slug_title
from crawlers.careers import main as poller
from crawlers.careers.models import FetchResult, Job

U = "1affb074-f055-4f5a-a97e-80d97d77da6e"
PAGE = f"""<html><body>
<!-- Shopify since Oct 2026: h3 title, location only via aria-describedby -->
<a href="https://shop.test/careers/account-executive-smb_{U}" aria-labelledby="t1" aria-describedby="l1">
  <div><h3 id="t1">Account Executive, SMB</h3><p id="l1">Remote - Americas</p></div></a>
<!-- the old markup: h4 + .location -->
<a href="https://shop.test/careers/data-engineer-intern_2bffb074-f055-4f5a-a97e-80d97d77da6e">
  <h4>Data Engineer Intern</h4><span class="location">Toronto</span></a>
<!-- nothing but an icon: title from the slug -->
<a href="https://shop.test/careers/applied-machine-learning-engineers_9f16f862-0000-4f5a-a97e-80d97d77da6e"><svg></svg></a>
</body></html>"""

def fetch(**selectors):
    co = {"name": "Shopify", "ats": "custom", "url": "data:text/html," + urllib.parse.quote(PAGE),
          "link_selector": 'a[href*="/careers/"]', **selectors}
    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                return await custom(await browser.new_context(), co)
            finally:
                await browser.close()
    return asyncio.run(go())

def by_title(res):
    return {j.title: j for j in res.jobs}

def test_titles_and_locations_without_selectors():
    jobs = by_title(fetch())
    assert jobs["Account Executive, SMB"].location == "Remote - Americas"
    assert "Data Engineer Intern" in jobs and not jobs["Data Engineer Intern"].title_guessed
    guessed = jobs["Applied Machine Learning Engineers"]
    assert guessed.title_guessed and guessed.job_id.endswith("9f16f862-0000-4f5a-a97e-80d97d77da6e")

def test_stale_selectors_fall_back_instead_of_blanking():
    # the real failure: title_selector "h4" stopped matching and every title came back ''
    jobs = by_title(fetch(title_selector="h4", location_selector=".location"))
    assert jobs["Data Engineer Intern"].location == "Toronto"           # selectors still used where they match
    assert jobs["Account Executive, SMB"].location == "Remote - Americas"  # and fall back where they don't
    assert all(j.title for j in jobs.values())

@pytest.mark.parametrize("url, title", [
    (f"https://www.shopify.com/careers/applied-machine-learning-engineers_{U}", "Applied Machine Learning Engineers"),
    ("https://x.test/jobs/software-engineer-intern_R12345/", "Software Engineer Intern"),
    ("https://x.test/jobs/", "Jobs"),
])
def test_slug_title(url, title):
    assert slug_title(url) == title

def job(title):
    return Job(company="Acme", ats="custom", job_id=title or "x", title=title, url="https://x.test")

def test_blank_titles_none():
    res = FetchResult([job("A"), job("B")])
    assert poller.blank_titles(res) is res and res.skipped == 0

def test_few_blank_titles_are_skipped_records():
    res = poller.blank_titles(FetchResult([job("A"), job("B"), job("C"), job("")]))
    assert [j.title for j in res.jobs] == ["A", "B", "C"] and res.skipped == 1 and "blank title" in res.skip_error

def test_mostly_blank_titles_fail_the_run():
    res = poller.blank_titles(FetchResult([job("A"), job(""), job(" ")]))
    assert isinstance(res, ValueError) and "2/3 jobs have no title" in str(res)
