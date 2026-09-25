"""We Work Remotely, Workday and Weekday support."""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from jobpilot.appliers.engine import Engine
from jobpilot.browser import session
from jobpilot.planner import plan
from jobpilot.sources import workday, wwr
from jobpilot.triage import Triage

from .conftest import add_job

FIX = Path(__file__).parent / "fixtures"


def test_wwr_feed_parse():
    jobs = wwr.parse_feed((FIX / "wwr.rss").read_text())
    assert len(jobs) == 2
    j = jobs[0]
    assert (j.company, j.title, j.source) == ("Acme Payments", "Senior Backend Engineer (Go)", "weworkremotely")
    assert j.remote and "Anywhere in the World" in j.location
    assert "5+ years" in j.description and j.posted_at.year == 2026
    assert j.source_job_id == "acme-payments-senior-backend-engineer-go"


def test_wwr_region_gate(settings, db):
    from jobpilot.filters.location import location_verdict
    a, b = wwr.parse_feed((FIX / "wwr.rss").read_text())
    assert location_verdict(a.location, a.description, a.remote)[0] == "pass"
    assert location_verdict(b.location, b.description, b.remote)[0] == "fail"


def test_workday_parse_site_and_dates():
    assert workday.parse_site("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/x") == \
        {"tenant": "nvidia", "host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"}
    assert workday.parse_site("https://example.com/careers") is None
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert workday.posted_from_text("Posted Today", now) == now
    assert workday.posted_from_text("Posted 3 Days Ago", now).day == 21
    assert workday.posted_from_text("Posted 30+ Days Ago", now).month == 8


def test_workday_list_pagination_and_mapping():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.method == "POST":
            body = json.loads(req.content)
            off = body["offset"]
            assert body["limit"] == 20
            posts = [{"title": f"Senior Software Engineer {off + i}", "externalPath": f"/job/Bengaluru/SSE_{off + i}",
                      "locationsText": "Bengaluru, India", "postedOn": "Posted 2 Days Ago"} for i in range(20 if off < 20 else 5)]
            return httpx.Response(200, json={"total": 25 if off == 0 else 0, "jobPostings": posts})
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>5+ years Go</p>", "startDate": "2026-09-20",
                                                           "jobReqId": "R123", "location": "Bengaluru"}})

    site = workday.parse_site("https://acme.wd5.myworkdayjobs.com/External")
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        posts = workday.list_jobs(c, site, "software engineer", 5)
        assert len(posts) == 25                  # kept paging although page 2 reports total=0
        detail = c.get("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Bengaluru/SSE_0").json()
    j = workday.to_job(site, "Acme", posts[0], detail)
    assert j.url == "https://acme.wd5.myworkdayjobs.com/External/job/Bengaluru/SSE_0"
    assert j.source_job_id == "acme:R123" and "5+ years Go" in j.description
    assert j.posted_at == datetime(2026, 9, 20, tzinfo=timezone.utc)


def _setup(settings, db, url, source="workday"):
    add_job(db, settings, source, "wd1", "NVIDIA", "Senior Software Engineer", url=url, apply_url=url)
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    return db.apps()[0]["id"]


def _run(settings, db, app_id):
    return Engine(settings, db).process(app_id, lambda har: session(settings, har=har, headless=True))


def test_workday_signin_stops_for_human(settings, db):
    settings.cfg["mode"] = "live"
    app_id = _setup(settings, db, (FIX / "workday.html").as_uri() + "#signin")
    assert _run(settings, db, app_id) == "needs_review"
    assert "Workday sign-in" in " ".join(json.loads(db.app(app_id)["review_reasons"]))
    assert db.app(app_id)["submitted_at"] is None


def test_workday_flow_fills_keeps_autofill_and_needs_review_then_submits(settings, db):
    settings.cfg["mode"] = "live"
    app_id = _setup(settings, db, (FIX / "workday.html").as_uri())
    assert _run(settings, db, app_id) == "needs_review"          # Workday is never auto-submitted
    a = db.app(app_id)
    reasons = " ".join(json.loads(a["review_reasons"]))
    assert "requires review" in reasons and "autofilled" in reasons
    fields = {f["field"]["label"].replace(" *", "*"): f for f in json.loads((Path(a["proof_dir"]) / "fields.json").read_text())}
    assert fields["Country*"]["actual"] == "India"
    assert fields["Country Phone Code*"]["actual"] == "India (+91)"
    assert fields["Phone Device Type*"]["actual"] == "Mobile"
    assert fields["How Did You Hear About Us?*"]["actual"] == "Company Website"
    assert fields["What is your notice period?*"]["actual"] == "60 days"
    assert fields["Job Title*"]["provenance"] == "portal_prefill"
    assert fields["Job Title*"]["actual"] == "Software Development Engineer II"   # not overwritten
    ver = json.loads((Path(a["proof_dir"]) / "verification.json").read_text())
    assert ver["status"] == "review" and not ver["blocks"], ver["blocks"]
    # human approves → re-fill, re-verify, submit
    db.transition(app_id, "approved")
    db.update_app(app_id, approved=1)
    assert _run(settings, db, app_id) == "submitted"
    assert "application has been submitted" in db.app(app_id)["confirmation"].lower() or \
           "congratulations" in db.app(app_id)["confirmation"].lower()


def test_weekday_and_wwr_routes_and_ranks(settings):
    eng = Engine(settings, None)
    assert eng._route({"source": "weekday"}) == "oneclick"
    assert eng._route({"source": "weworkremotely"}) == "aggregator"
    for src in ("remoteok", "remotive", "himalayas"):
        assert eng._route({"source": src}) == "aggregator"
    assert eng._route({"source": "workday"}) == "workday"
    r = settings.cfg["portal_rank"]
    assert r["workday"] == r["greenhouse"] < r["linkedin"] < r["weworkremotely"] < r["instahyre"]
    assert r["cutshort"] < r["weekday"] < r["hirist"]
    import re
    pat = re.compile(settings.portals["portals"]["weekday"]["job_link_pattern"])
    assert pat.search("https://jobs.weekday.works/weave-senior-backend-engineer---india-(golang-%2B-distributed-systems)")
    assert not pat.search("https://jobs.weekday.works/sign-in")
