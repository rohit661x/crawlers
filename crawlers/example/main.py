"""Placeholder crawler: proves browser, proxy, output and sync all work.
Run: python -m crawlers.example.main
"""
import asyncio
from common.browser import browser_context
from common.output import JsonlSink, setup_logging

NAME = "example"
log = setup_logging(NAME)
TARGETS = ["https://httpbin.org/ip", "https://example.com"]

async def run():
    sink = JsonlSink(NAME)
    async with browser_context() as ctx:
        page = await ctx.new_page()
        for url in TARGETS:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded")
                sink.write({
                    "crawler": NAME,
                    "url": url,
                    "status": resp.status if resp else None,
                    "title": await page.title(),
                    "text": (await page.inner_text("body"))[:500],
                })
                log.info("ok %s", url)
            except Exception as e:
                sink.write({"crawler": NAME, "url": url, "error": repr(e)})
                log.warning("fail %s: %r", url, e)
    sink.close()
    log.info("wrote %s", sink.path)

if __name__ == "__main__":
    asyncio.run(run())
