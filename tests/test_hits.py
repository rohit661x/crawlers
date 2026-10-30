"""🎯 HITs: the finer [hits] filter on top of [filters] (real config, real titles)."""
import tomllib
from pathlib import Path
import pytest
from crawlers.careers import main as poller, notify
from crawlers.careers.filters import JobFilter
from conftest import job

CFG = tomllib.loads((Path(__file__).parents[1] / "crawlers/careers/config.toml").read_text())
HITS = JobFilter(CFG["hits"])

@pytest.mark.parametrize("title", [
    "2027 - Software Engineering Intern, AMER",
    "ASIC Verification Intern - Summer 2027",
    "Quantitative Developer Intern",
    "Junior Technical Writer co-op",
    "Power Architect - New College Grad 2027",
    "New Graduate Engineer, Infrastructure",
    "2027 University Graduate - Technical Support Consultant",
    "Software Engineer I - Full Stack",
    "Information Security Engineer Undergraduate Intern",
    "Software Engineer Intern (BS/MS)",                 # bachelor's level still qualifies
    "Entry-Level Data Scientist",
])
def test_target_roles_are_hits(title):
    assert HITS.matches(job("x", title=title))

@pytest.mark.parametrize("title", [
    "Research Scientist Intern, Integrity AI (PhD)",
    "2027 Summer Intern, MS/PhD, Software Engineer, ML Frameworks and Efficiency",
    "AI Data Science / Software Engineering PhD Intern New 2027",
    "Product Marketing Management MBA Intern - Summer 2027",
    "Compiler Engineering Graduate Intern",              # Intel: grad-student internship
    "Machine Learning Intern (MSc)",
    "Postdoctoral Researcher, Robotics",
    "Anthropic Research Resident",
    "Associate Manager, Marketing Product Ops",          # matches [filters], not the target level
    "Software Engineer II",
])
def test_non_target_roles_are_not_hits(title):
    assert not HITS.matches(job("x", title=title))

T0 = "2026-10-06T10:00:00+00:00"

@pytest.fixture(autouse=True)
def one_destination(monkeypatch):
    # independent of the server's .env (which lists the DM and the channel): one outbox row per message
    monkeypatch.setattr(notify, "TARGETS", {"jobs": ["telegram"], "ops": ["telegram"]})

def queued(store, ids_titles, hits=HITS):
    """Baseline, then the given jobs arrive new; returns (count, [(header line, titles)]) as queued."""
    store.sync("Acme", [job("seed")], True, "2026-10-06T08:00:00+00:00")
    store.sync("Acme", [job("seed")] + [job(i, title=t) for i, t in ids_titles], True, "2026-10-06T09:00:00+00:00")
    n = poller.notify_pending(store, JobFilter(CFG["filters"]), {}, False, T0, hits=hits)
    return n, [(m["text"].splitlines()[0], m["text"]) for m in store.outbox()]

def test_hits_go_first_in_their_own_message_and_are_not_repeated(store):
    n, msgs = queued(store, [("a", "Research Scientist Intern, Audio AI (PhD)"),
                             ("b", "Software Engineering Intern"),
                             ("c", "Data Science Intern")])
    assert n == 3 and [h for h, _ in msgs] == ["<b>🎯 HIT · 2 target roles</b>", "🆕 1 new job"]
    hit_text, rest_text = msgs[0][1], msgs[1][1]
    assert "Software Engineering Intern" in hit_text and "Data Science Intern" in hit_text
    assert "(PhD)" in rest_text and "Software Engineering Intern" not in rest_text

def test_no_hits_leaves_the_standard_message_unchanged(store):
    n, msgs = queued(store, [("a", "Research Scientist Intern, Audio AI (PhD)")])
    assert n == 1 and [h for h, _ in msgs] == ["🆕 1 new job"]

def test_only_hits_sends_no_empty_standard_message(store):
    n, msgs = queued(store, [("a", "Software Engineering Intern")])
    assert [h for h, _ in msgs] == ["<b>🎯 HIT · 1 target role</b>"]

def test_without_hits_config_everything_is_standard(store):
    n, msgs = queued(store, [("a", "Software Engineering Intern"), ("b", "Research Intern (PhD)")], hits=None)
    assert [h for h, _ in msgs] == ["🆕 2 new jobs"]

def queued_rows(store, ids_titles, **job_kw):
    store.sync("Acme", [job("seed")], True, "2026-10-06T08:00:00+00:00")
    store.sync("Acme", [job("seed")] + [job(i, title=t, **job_kw) for i, t in ids_titles], True, "2026-10-06T09:00:00+00:00")
    poller.notify_pending(store, JobFilter(CFG["filters"]), {}, False, T0, hits=HITS)
    return store.outbox()

def test_hit_message_is_style_b_and_standard_stays_plain(store):
    import json
    hit, rest = queued_rows(store, [("a", "Software Engineering Intern"), ("b", "Data Science Intern"),
                                    ("c", "Research Scientist Intern, Audio AI (PhD)")])
    opts = json.loads(hit["options"])
    assert opts["parse_mode"] == "HTML" and opts["link_preview_options"] == {"is_disabled": True}
    buttons = [row[0] for row in opts["reply_markup"]["inline_keyboard"]]
    assert [b["text"] for b in buttons] == ["Apply 1 · Acme", "Apply 2 · Acme"]
    assert [b["url"] for b in buttons] == ["https://example.com/a", "https://example.com/b"]
    assert "1. <b>Acme</b> — Software Engineering Intern" in hit["text"] and '<a href="https://example.com/a">Apply ↗</a>' in hit["text"]
    assert rest["options"] is None and rest["text"].startswith("🆕 1 new job")   # the standard message: unchanged

def test_hit_text_is_html_escaped(store):
    (hit,) = queued_rows(store, [("a", "R&D <Firmware> Intern")])
    assert "R&amp;D &lt;Firmware&gt; Intern" in hit["text"] and "<Firmware>" not in hit["text"]

def test_many_hits_split_with_buttons_matching_each_part(store):
    import json
    msgs = queued_rows(store, [(f"j{i}", f"Software Engineering Intern, Team {i} " + "x" * 120) for i in range(40)])
    assert len(msgs) > 1
    for m in msgs:
        urls = {b[0]["url"] for b in json.loads(m["options"])["reply_markup"]["inline_keyboard"]}
        assert urls and all(f'href="{u}"' in m["text"] for u in urls)   # each part's buttons are its own jobs
    assert sum(len(json.loads(m["options"])["reply_markup"]["inline_keyboard"]) for m in msgs) == 40
