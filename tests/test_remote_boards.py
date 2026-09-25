"""Remote OK, Remotive, Himalayas: parsing, India gating, Remotive call budget, link-out apply."""
import json
from pathlib import Path

from jobpilot.appliers.engine import Engine
from jobpilot.browser import session
from jobpilot.filters.location import location_verdict
from jobpilot.filters.salary import parse_salary
from jobpilot.planner import plan
from jobpilot.sources import remote_boards as rb
from jobpilot.triage import Triage

from .conftest import add_job

FIX = Path(__file__).parent / "fixtures"
DESC = "<p>Senior backend role. 5+ years of experience with Go, distributed systems, PostgreSQL, Kafka.</p>"

REMOTEOK = [
    {"last_updated": 1790208007, "legal": "API Terms of Service: Please link back ..."},
    {"id": "1137427", "slug": "remote-senior-backend-engineer-acme-1137427", "epoch": 1790172002, "company": "Acme",
     "position": "Senior Backend Engineer", "tags": ["golang", "backend"], "description": DESC, "location": "Worldwide",
     "salary_min": 90000, "salary_max": 130000, "url": "https://remoteOK.com/remote-jobs/remote-senior-backend-engineer-acme-1137427"},
    {"id": "2", "position": "Senior Sales Manager", "tags": ["sales"], "company": "X", "description": "", "location": "US"},
]
REMOTIVE = {"0-legal-notice": "...", "job-count": 2, "jobs": [
    {"id": 1, "url": "https://remotive.com/remote-jobs/software-dev/senior-go-engineer-1", "title": "Senior Go Engineer",
     "company_name": "Beta", "job_type": "full_time", "publication_date": "2026-09-20T10:00:05",
     "candidate_required_location": "USA Only", "salary": "$150k - $180k", "description": DESC},
    {"id": 2, "url": "https://remotive.com/remote-jobs/software-dev/backend-engineer-2", "title": "Senior Backend Engineer",
     "company_name": "Gamma", "job_type": "full_time", "publication_date": "2026-09-21T10:00:05",
     "candidate_required_location": "India", "salary": "", "description": DESC}]}
HIMALAYAS = {"totalCount": 3, "nextCursor": None, "jobs": [
    {"title": "Senior Backend Engineer", "companyName": "Delta", "minSalary": 6000000, "maxSalary": 8000000, "currency": "INR",
     "locationRestrictions": ["India"], "timezoneRestrictions": [5.5], "pubDate": 1790093957, "description": DESC,
     "applicationLink": "https://jobs.lever.co/delta/abc", "guid": "https://himalayas.app/companies/delta/jobs/senior-backend"},
    {"title": "Senior Software Engineer", "companyName": "Epsilon", "locationRestrictions": [], "timezoneRestrictions": [-8, -7, -6, -5],
     "pubDate": 1790093957, "description": DESC, "applicationLink": "https://himalayas.app/x", "guid": "https://himalayas.app/companies/eps/jobs/sse"},
    {"title": "Senior Platform Engineer", "companyName": "Zeta", "locationRestrictions": [], "timezoneRestrictions": [],
     "pubDate": 1790093957, "description": DESC, "applicationLink": "https://himalayas.app/companies/zeta/jobs/spe/apply",
     "guid": "https://himalayas.app/companies/zeta/jobs/spe"}]}


def test_remoteok_parse():
    jobs = rb.parse_remoteok(REMOTEOK)
    assert len(jobs) == 1                                 # legal notice and non-dev role dropped
    j = jobs[0]
    assert (j.source, j.company, j.title) == ("remoteok", "Acme", "Senior Backend Engineer")
    assert location_verdict(j.location, j.description, j.remote)[0] == "pass"
    lo, hi, _ = parse_salary(j.salary_text, 84)
    assert round(lo, 1) == 75.6 and round(hi, 1) == 109.2     # $90k-$130k → LPA


def test_remotive_parse_and_location_gate():
    a, b = rb.parse_remotive(REMOTIVE)
    assert location_verdict(a.location, a.description, a.remote)[0] == "fail"   # USA Only
    assert location_verdict(b.location, b.description, b.remote)[0] == "pass"   # India
    assert a.posted_at.day == 20


def test_remotive_daily_budget(tmp_path):
    assert all(rb._remotive_budget_ok(tmp_path) for _ in range(4))
    assert rb._remotive_budget_ok(tmp_path) is False


def test_himalayas_parse_timezones_salary_direct_apply():
    jobs = rb.parse_himalayas(HIMALAYAS)
    assert [j.company for j in jobs] == ["Delta", "Zeta"]      # US-only timezone job dropped
    d = jobs[0]
    assert d.apply_url == "https://jobs.lever.co/delta/abc"   # direct ATS link kept
    assert parse_salary(d.salary_text, 84)[:2] == (60.0, 80.0)
    assert jobs[1].apply_url == ""                            # Himalayas-hosted page → follow at apply time


def test_aggregator_links_out_to_company_form(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    form = (FIX / "ats_form.html").read_text().replace("MASK", "").replace("WHY", "")
    (tmp_path / "form.html").write_text(form)
    (tmp_path / "listing.html").write_text(f'<h1>Senior Backend Engineer</h1><a href="{(tmp_path / "form.html").as_uri()}" '
                                           f'target="_blank">Apply now</a>')
    add_job(db, settings, "remoteok", "ro1", "Stripe", "Senior Backend Engineer", url=(tmp_path / "listing.html").as_uri(),
            location="Remote / Worldwide")
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    app_id = db.apps()[0]["id"]
    res = Engine(settings, db).process(app_id, lambda har: session(settings, har=har, headless=True))
    assert res == "needs_review"                                # aggregator sources are review-only
    a = db.app(app_id)
    assert "source 'remoteok' requires review" in " ".join(json.loads(a["review_reasons"]))
    assert json.loads((Path(a["proof_dir"]) / "verification.json").read_text())["status"] == "pass"
    assert db.jobs("key=?", (a["job_key"],))[0]["apply_url"].endswith("form.html")   # resolved company form recorded
