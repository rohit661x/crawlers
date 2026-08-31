# crawlers

Python + Playwright crawlers for the `base` droplet.

- `common/`   shared browser factory (proxy from `.env`), JSONL sink, logging
- `crawlers/<name>/main.py`  one module per crawler; run with `python -m crawlers.<name>.main`
- `systemd/`  `crawler@<name>` service template, `crawl-sync` timer -> `spaces:rohit-base/crawls/`
- output: `~/data/crawls/<name>/<YYYY-MM-DD>.jsonl`

New crawler: copy `crawlers/example`, edit TARGETS/parse logic, then
`systemctl --user enable --now crawler@<name>` for long-running, or a timer for scheduled.
Proxy: set `PROXY_URL` in `.env`; per-crawler override by passing `proxy_url=` to `browser_context()`.
