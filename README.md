# crawlers

Python + Playwright crawlers for the `base` droplet.

- `common/`   shared browser factory (proxy from `.env`), JSONL sink, logging
- `crawlers/<name>/main.py`  one module per crawler; run with `python -m crawlers.<name>.main`
- `systemd/`  `crawler@<name>` service template, `crawl-sync` timer -> `spaces:rohit-base/crawls/`
- output: `~/data/crawls/<name>/<YYYY-MM-DD>.jsonl`

New crawler: copy `crawlers/example`, edit TARGETS/parse logic, then
`systemctl --user enable --now crawler@<name>` for long-running, or a timer for scheduled.
Proxy: set `PROXY_URL` in `.env`; per-crawler override by passing `proxy_url=` to `browser_context()`.

## careers poller (`crawlers/careers/`)

Polls company job boards hourly and pings Telegram (via `hermes send`) about new jobs that pass the filters.

- `config.toml`  companies (`ats` = greenhouse | lever | ashby | smartrecruiters | workday | amazon | eightfold | google | apple | meta | custom) and `[filters]`
- `fetchers/`    one fetcher per source -> normalized `Job` (`models.py`): `ats.py` standard boards, `bigtech.py` own backends, `browser.py` Playwright (meta, custom)
- `store.py`     SQLite at `$CAREERS_DB` (default `~/data/state/careers.db`): first/last seen, closed, notify state.
                 A company's first poll is a silent baseline plus one "now tracking" summary.
- events (`new`, `closed`, `error`) also go to `~/data/crawls/careers/<day>.jsonl`

Add companies: `python -m crawlers.careers.discover "Stripe" "Notion" [--ats greenhouse,ashby] [--add]`
finds the ATS + slug (careers-page links, then slug guesses verified by board name) and appends confident
matches to `config.toml`. Check the reported board name: a guessed domain can be another company.

Reliability (`[http]`, `[alerts]` in config): per-host pacing + retry/backoff on 429/5xx/network errors
(honours Retry-After), per-company timeout, run lock. Guards against broken fetchers: a listing that shrinks
below 20% of what's on record pauses closures (accepted as real after 6 runs); a burst of "new" jobs is absorbed
as a re-baseline. Failing / empty / shrunk / recovered each send one Telegram alert on change, and the daily
health summary (first run after `[health].hour`) doubles as a dead-man's switch.

Test without sending: `python -m crawlers.careers.main --dry-run [--only NAME]` (in-memory DB).
Schedule: `systemctl --user enable --now poll@careers.timer` (one-shot `poll@.service`, hourly `poll@.timer`).
