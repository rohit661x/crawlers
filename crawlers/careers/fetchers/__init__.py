"""Fetcher registry: config `ats` value -> fetcher. HTTP fetchers take an httpx client,
browser fetchers a Playwright context. Each returns a FetchResult of normalized Jobs."""
from .ats import greenhouse, lever, ashby, smartrecruiters, workday
from .bigtech import amazon, eightfold, google, apple, janestreet
from .browser import meta, custom

HTTP_FETCHERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
    "amazon": amazon,
    "eightfold": eightfold,
    "google": google,
    "apple": apple,
    "janestreet": janestreet,
}
BROWSER_FETCHERS = {"meta": meta, "custom": custom}
