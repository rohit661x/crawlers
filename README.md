# crawlers

Python + Playwright crawlers for the `base` droplet.

- `common/`   shared browser factory (proxy from `.env`), JSONL sink, logging
- `crawlers/<name>/main.py`  one module per crawler; run with `python -m crawlers.<name>.main`
- `systemd/`  `crawler@<name>` service template, `crawl-sync` timer -> `spaces:rohit-base/crawls/`
- output: `~/data/crawls/<name>/<YYYY-MM-DD>.jsonl`

New crawler: copy `crawlers/example`, edit TARGETS/parse logic, then
`systemctl --user enable --now crawler@<name>` for long-running, or a timer for scheduled.
Proxy: set `PROXY_URL` in `.env`; per-crawler override by passing `proxy_url=` to `browser_context()`.
Dependencies are pinned in `requirements.txt`; after bumping `playwright`, run `python -m playwright install chromium`
(each Playwright release needs its own browser build).

## careers poller (`crawlers/careers/`)

Polls company job boards hourly and pings Telegram about new jobs that pass the filters. It posts through the
Bot API with the hermes bot's token (read from `~/.hermes/.env`), not `hermes send`, which would also copy every
alert into the owner's Hermes conversation and make each later DM to Hermes re-read all of them.

Messages have two routes, each with a list of destinations (hermes targets, set in `.env`):
- **jobs** -> `NOTIFY_JOBS_TARGETS` (comma-separated, e.g. `telegram,telegram:-1003936325725`: the owner's DM
  and the "Crawler Alerts" channel): one "🆕 N new jobs" summary per run per destination, split into a few
  messages only past Telegram's size limit, and nothing at all when nothing new matched.
  🎯 HITs: new matches that also pass the finer `[hits]` filter in `config.toml` (the target audience:
  internships/co-ops, bachelor's level, new grad / entry level; never PhD, MS, MBA, postdoc or residency)
  go first, in their own "🎯 HIT" message (bold, numbered, an "Apply ↗" link and an Apply button per job, no
  link previews; if Telegram ever rejects the formatting it's resent as plain text with the buttons); the 🆕
  summary then lists only the rest, as plain text like before. Remove `[hits]` to turn it off.
- **ops** -> `NOTIFY_OPS_TARGET`, the owner only: poller alerts (failing / empty / shrunk / recovered), the daily
  health summary, "now tracking" and filter-change digests, `report --send`, `notify-failure` log tails.
Both default to `NOTIFY_TARGET` (`telegram` = hermes' home channel, the owner's DM).

Every destination gets its own outbox row, so each retries, fails and recovers independently: a channel that
rejects posts never holds up the DM, or the other way round, and the owner gets one ⚠️ message when a
destination starts failing (and a ✅ once it delivers again). A job counts as sent once any destination has
it. Sends are spaced 3.1 s apart (20 messages/min per group or channel), and a Telegram 429 waits the
`retry_after` it asks for (up to 2 min; longer penalties leave the message queued for the next run).

Adding a channel: make the bot an admin with only "Post Messages", check the id with the Bot API's `getChat`
(channel ids are `-100…`), and append `telegram:<id>` to `NOTIFY_JOBS_TARGETS`. The bot needs no read access,
so Telegram's privacy mode stays on.

- `config.toml`  ~136 companies (`ats` = greenhouse | lever | ashby | smartrecruiters | workable | workday | amazon |
                 eightfold | google | apple | janestreet | meta | custom) and `[filters]`. Per-company options:
                 `searches`, `max_jobs`, `timeout` (slow boards, e.g. RBC = 600), `disabled`, a `[companies.filters]`
                 override, and `content = true` (greenhouse only: adds departments, ~12x larger responses)
- `fetchers/`    one fetcher per source -> normalized `Job` (`models.py`): `ats.py` standard boards, `bigtech.py` own backends, `browser.py` Playwright (meta, custom)
- `store.py`     SQLite at `$CAREERS_DB` (default `~/data/state/careers.db`): first/last seen, closed, notify state.
                 A company's first poll is a silent baseline plus one "now tracking" summary.
- events (`new`, `closed`, `error`) also go to `~/data/crawls/careers/<day>.jsonl`

Matching jobs you haven't seen: `python -m crawlers.careers.report [--company NAME] [--days N] [--unsent] [--send]`
lists every open job that passes the filters (`--send` queues them to the owner). A newly tracked company's
announcement includes all its matching jobs (up to `[digest].baseline_max`), and after you edit `[filters]`
the next run sends open jobs that match the new filters but didn't match the old ones (`refilter_max`).

Add companies: `python -m crawlers.careers.discover "Stripe" "Notion" [--ats greenhouse,ashby] [--add]`
finds the ATS + slug (careers-page links, then slug guesses verified by board name) and appends confident
matches to `config.toml`. Check the reported board name: a guessed domain can be another company.
It only finds slug-based boards; Workday (`url` = the board, e.g. `https://intel.wd1.myworkdayjobs.com/External`,
check `POST <host>/wday/cxs/<tenant>/<site>/jobs` returns a `total`) and Eightfold (`host` + `domain`, e.g.
Qualcomm's `careers.qualcomm.com`; `filter_seniority = "Intern"` / `"Entry"` narrow it) are added by hand.
Workday's `searchText` is fuzzy ("co-op" can match the whole board): when a search returns most of the board,
drop `searches` and fetch everything; the filters do the narrowing.

Reliability (`[http]`, `[alerts]` in config): per-host pacing + retry/backoff on 429/5xx/network errors
(honours Retry-After), per-company timeout, run lock. Guards against broken fetchers: a listing that shrinks
below 20% of what's on record pauses closures (accepted as real after 6 runs); a burst of "new" jobs is absorbed
as a re-baseline. Failing / empty / shrunk / recovered each send one Telegram alert on change, and the daily
health summary (first run after `[health].hour`) doubles as a dead-man's switch.

Malformed records are skipped rather than failing the company (over 5% skipped pauses closures; all
skipped counts as a failure). A job with a blank title counts as malformed (it could never match the filters), and a
listing that is mostly blank titles fails the run (`fail_threshold` failed runs => the ❌ alert): that is how a page
redesign shows up. The `custom` browser fetcher falls back from its selectors to each link's aria-labelledby /
aria-describedby, its first heading, then a title guessed from the URL slug, and a job only closes after missing from 3 complete listings in a row, so
search results shifting between pages don't close and reopen it. A Workday posting that arrives without its path
(no id, no URL) still keeps its job open via the requisition id in `bulletFields`. Every Telegram message goes through an
outbox table: a failed send is retried next run (dropped after 24 failed runs) instead of lost.

Dead-man switches: each full run pings `HEALTHCHECK_URL` (e.g. a healthchecks.io check, period 1h, grace 2h),
with `/fail` on a crash or when the outbox has been stuck for 3h; a crash also triggers
`OnFailure=notify-failure@%n` (Telegram message to the ops target with the log tail). `crawl-backup.timer` snapshots
`careers.db` daily to `spaces:rohit-base/state/careers-<weekday>.db.gz` (7 days kept).
`crawl-sync` and `crawl-backup` report to their own checks (`HEALTHCHECK_SYNC_URL`, `HEALTHCHECK_BACKUP_URL`)
via `hc-ping.sh` in `ExecStopPost=`, so a sync or backup that silently stops running is caught too.

Tests: `pip install -r requirements-dev.txt && python -m pytest`.
Test without sending: `python -m crawlers.careers.main --dry-run [--only NAME]` (in-memory DB).
Schedule: `systemctl --user enable --now poll@careers.timer` (one-shot `poll@.service`, hourly `poll@.timer`).
