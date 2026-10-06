"""Matching-job digests: newly tracked companies, filter changes, and the report command."""
import pytest
from crawlers.careers import digest, notify, report
from crawlers.careers.filters import build_filters
from crawlers.careers.store import Store
from conftest import job

T0, T1, T2 = "2026-10-06T09:00:00+00:00", "2026-10-06T10:00:00+00:00", "2026-10-06T11:00:00+00:00"
INTERNS = {"filters": {"title_include": ["intern"]}}

def filters(cfg):
    return build_filters(cfg)

def outbox_text(store):
    return "\n".join(m["text"] for m in store.outbox())

def state(store, job_id):
    return store.db.execute("SELECT notify_state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]

# --- filter fingerprint -----------------------------------------------------------

def test_filter_spec_tracks_global_and_per_company_filters():
    base = digest.filter_spec(INTERNS)
    assert base == digest.filter_spec({"filters": {"title_include": ["intern"]}, "http": {"retries": 9}})
    assert base != digest.filter_spec({"filters": {"title_include": ["intern", "new grad"]}})
    per_co = {**INTERNS, "companies": [{"name": "Acme", "filters": {"regions": ["US"]}}]}
    assert base != digest.filter_spec(per_co)

# --- newly tracked companies -----------------------------------------------------

def test_baseline_sends_every_match_not_a_preview(store):
    jobs = [job(f"i{n}", title=f"SWE Intern {n}") for n in range(8)] + [job("s", title="Staff Engineer")]
    store.sync("Acme", jobs, True, T0)
    digest.baselines(store, [({"name": "Acme", "ats": "greenhouse"}, jobs)], *filters(INTERNS),
                     cap=50, dry_run=False, now=T0)
    text = outbox_text(store)
    assert "Acme (greenhouse): 9 open, 8 match" in text
    assert all(f"SWE Intern {n}" in text for n in range(8)) and "Staff Engineer" not in text
    assert state(store, "i0") == "queued" and state(store, "s") == "baseline"

def test_baseline_cap_points_at_report(store):
    jobs = [job(f"i{n}", title=f"SWE Intern {n}") for n in range(5)]
    store.sync("Acme", jobs, True, T0)
    digest.baselines(store, [({"name": "Acme", "ats": "lever"}, jobs)], *filters(INTERNS),
                     cap=2, dry_run=False, now=T0)
    text = outbox_text(store)
    assert '…and 3 more: python -m crawlers.careers.report --company "Acme"' in text
    queued = [r[0] for r in store.db.execute("SELECT job_id FROM jobs WHERE notify_state = 'queued'")]
    assert len(queued) == 2  # the other 3 stay 'baseline' and show up in report --unsent

def test_baseline_companies_without_matches_share_one_line(store):
    a, b = [job("x", company="A", title="Recruiter")], [job("y", company="B", title="Lawyer")]
    digest.baselines(store, [({"name": "B", "ats": "ashby"}, b), ({"name": "A", "ats": "ashby"}, a)],
                     *filters(INTERNS), cap=50, dry_run=False, now=T0)
    assert "No current matches (2): A, B" in outbox_text(store)

# --- filter changes ----------------------------------------------------------------

def seed(store):
    """Acme is tracked; a later run adds two jobs that the intern filter rejects."""
    store.sync("Acme", [job("old", title="Data Scientist")], True, T0)       # baseline
    store.sync("Acme", [job("old", title="Data Scientist"), job("new", title="Data Analyst"),
                        job("swe", title="SWE Intern")], True, T1)
    gf, pc = filters(INTERNS)
    store.mark([r for r in store.pending() if not gf.matches(r)], "filtered", T1)
    store.mark([r for r in store.pending() if gf.matches(r)], "sent", T1)

def test_first_run_only_records_the_fingerprint(store):
    seed(store)
    assert digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T2) == 0
    assert store.get("filter_spec") == digest.filter_spec(INTERNS) and store.outbox() == []

def test_filter_change_sends_jobs_that_now_match(store):
    seed(store)
    digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T2)
    wider = {"filters": {"title_include": ["intern", "data"]}}
    assert digest.refilter(store, wider, *filters(wider), cap=100, dry_run=False, now=T2) == 2
    text = outbox_text(store)
    assert "Filters changed: 2 open jobs now match" in text
    assert "Data Scientist" in text and "Data Analyst" in text  # baseline + filtered
    assert "SWE Intern" not in text                              # already sent, not repeated
    assert store.get("filter_spec") == digest.filter_spec(wider)
    # unchanged filters next run: nothing more
    assert digest.refilter(store, wider, *filters(wider), cap=100, dry_run=False, now=T2) == 0

def test_jobs_that_matched_all_along_are_not_resent(store):
    # a company's matching openings when first tracked stay 'baseline'; a filter change mustn't
    # dump them as "now match" (that's report --unsent's job)
    store.sync("Acme", [job("i", title="ML Intern"), job("d", title="Data Scientist")], True, T0)
    digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T1)
    wider = {"filters": {"title_include": ["intern", "data"]}}
    assert digest.refilter(store, wider, *filters(wider), cap=100, dry_run=False, now=T2) == 1
    assert "Data Scientist" in outbox_text(store) and "ML Intern" not in outbox_text(store)

def test_per_company_filter_change(store):
    seed(store)
    digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T2)
    cfg = {**INTERNS, "companies": [{"name": "Acme", "filters": {"title_include": ["analyst"]}}]}
    assert digest.refilter(store, cfg, *filters(cfg), cap=100, dry_run=False, now=T2) == 1
    assert "Data Analyst" in outbox_text(store)

def test_closed_jobs_are_not_resent_after_filter_change(store):
    seed(store)
    digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T2)
    store.db.execute("UPDATE jobs SET closed_at = ? WHERE job_id = 'new'", (T2,))
    wider = {"filters": {"title_include": ["intern", "data"]}}
    assert digest.refilter(store, wider, *filters(wider), cap=100, dry_run=False, now=T2) == 1

def test_dry_run_doesnt_record_the_fingerprint(store, capsys):
    seed(store)
    digest.refilter(store, INTERNS, *filters(INTERNS), cap=100, dry_run=False, now=T2)
    wider = {"filters": {"title_include": ["intern", "data"]}}
    digest.refilter(store, wider, *filters(wider), cap=100, dry_run=True, now=T2)
    assert "Filters changed" in capsys.readouterr().out
    assert store.get("filter_spec") == digest.filter_spec(INTERNS) and store.outbox() == []

# --- report command ----------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """A real DB file + config with the intern filter, for report.main()."""
    path, cfg = tmp_path / "careers.db", tmp_path / "config.toml"
    cfg.write_text('[filters]\ntitle_include = ["intern"]\n')
    s = Store(path)
    s.sync("Acme", [job("a", title="SWE Intern"), job("b", title="ML Intern"), job("c", title="Staff Eng")], True, T0)
    s.sync("Beta", [job("d", company="Beta", title="Quant Intern")], True, T0)
    s.mark([{"company": "Acme", "job_id": "a"}], "sent", T0)
    return ["--db", str(path), "--config", str(cfg)], s

def test_report_lists_matches_grouped_by_company(db, capsys):
    args, _ = db
    assert report.main(args) == 0
    out = capsys.readouterr().out
    assert "Acme (2)" in out and "Beta (1)" in out and "Staff Eng" not in out
    assert "3 matching open jobs at 2 companies; 2 never sent to Telegram." in out

def test_report_unsent_and_company(db, capsys):
    args, _ = db
    report.main(args + ["--unsent", "--company", "Acme"])
    out = capsys.readouterr().out
    assert "ML Intern" in out and "SWE Intern" not in out and "Beta" not in out

def test_report_send_marks_jobs_sent(db, monkeypatch, capsys):
    args, s = db
    texts = []
    monkeypatch.setattr(notify, "send", lambda text: texts.append(text))
    assert report.main(args + ["--unsent", "--send"]) == 0
    assert "Sent 2 of 2 jobs." in capsys.readouterr().out
    assert "ML Intern" in texts[0] and "Quant Intern" in texts[0]
    s2 = Store(args[1])
    assert {r["job_id"] for r in s2.open_jobs(unsent=True)} == {"c"}  # only the non-match remains

def test_report_send_over_limit_needs_yes(db, monkeypatch, capsys):
    args, _ = db
    monkeypatch.setattr(report, "SEND_CONFIRM_OVER", 1)
    monkeypatch.setattr(notify, "send", lambda text: pytest.fail("must not send without --yes"))
    assert report.main(args + ["--send"]) == 1
    assert "--yes" in capsys.readouterr().out
