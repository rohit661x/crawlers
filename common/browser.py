"""Playwright browser factory. Proxy comes from env so crawlers don't hardcode it."""
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from . import config

def _proxy_settings(url: str | None):
    if not url:
        return None
    u = urlparse(url)
    server = f"{u.scheme}://{u.hostname}:{u.port}"
    out = {"server": server}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out

@asynccontextmanager
async def browser_context(proxy_url: str | None = config.PROXY_URL, **ctx_kwargs):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=config.HEADLESS,
            proxy=_proxy_settings(proxy_url),
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            locale="en-CA",
            **ctx_kwargs,
        )
        ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        try:
            yield ctx
        finally:
            await ctx.close()
            await browser.close()
