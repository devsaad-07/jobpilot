"""Regression tests for the code-review findings (units, buckets, state machine, skip finality)."""
import pytest

from jobpilot.appliers.form import Field, Planned, choose_option
from jobpilot.config import load_settings
from jobpilot.llm.codex import Codex
from jobpilot.planner import plan
from jobpilot.triage import Triage
from jobpilot.verify import Verifier

from .conftest import add_job


@pytest.mark.parametrize("value,options,kind,want", [
    (60, ["Immediate", "15 days", "1 month", "2 months", "3 months", "More than 3 months"], "days", "2 months"),
    (60, ["0-15 days", "15-30 days", "30-60 days", "60-90 days"], "days", "30-60 days"),
    (5, ["1+ years", "3+ years", "5+ years", "8+ years"], "years", "5+ years"),
    (5, ["0-2", "2-4", "4-6", "6+"], "years", "4-6"),
    (5, ["Less than 3 years", "3 to 5 years", "More than 5 years"], "years", "3 to 5 years"),
    (65, ["< 30 LPA", "30-50 LPA", "50-70 LPA", "70+ LPA"], "lpa", "50-70 LPA"),
    (5, ["15 days", "50"], None, None),     # numbers never fuzzy-match ("5" must not pick "15 days")
])
def test_bucket_choice(value, options, kind, want):
    assert choose_option(value, options, kind) == want


def _p(label, typ, value, actual, options=None, chosen=None):
    f = Field(id="x", type=typ, label=label, required=True, options=options or [])
    p = Planned(f, value, "bank", chosen_option=chosen)
    p.actual = actual
    return p


@pytest.fixture
def verifier(settings):
    return Verifier(settings, Codex(enabled=False), "")


def test_unit_mismatch_blocks(verifier, settings):
    res = settings.path("resumes_dir") / "x.pdf"
    bad = [_p("Current CTC (in INR)", "number", 40, "40"), _p("Notice period (in months)", "text", 60, "60")]
    rep = verifier.check(bad, [], res)
    assert len([b for b in rep["blocks"] if "mismatch" in b]) == 2
    good = [_p("Current CTC (in INR)", "number", 4000000, "4000000"), _p("Notice period (in months)", "text", 2, "2")]
    rep = verifier.check(good, [], res)
    assert not [b for b in rep["blocks"] if "mismatch" in b], rep["blocks"]


def test_wrong_bucket_blocks(verifier, settings):
    res = settings.path("resumes_dir") / "x.pdf"
    p = _p("Notice period", "select", 60, "More than 3 months", ["2 months", "More than 3 months"], "More than 3 months")
    assert any("mismatch" in b for b in verifier.check([p], [], res)["blocks"])


def test_answerer_converts_units(settings):
    from jobpilot.answers import Answerer
    a = Answerer(settings, Codex(enabled=False), settings.path("resumes_dir") / "x.pdf", "", {})
    assert a.plan(Field(id="1", type="number", label="Current CTC (in INR)", required=True)).value == 4000000
    assert a.plan(Field(id="2", type="text", label="Notice period (in months)", required=True)).value == 2
    assert a.plan(Field(id="3", type="number", label="Expected CTC (LPA)", required=True)).value == 65


def test_combobox_empty_readback_fails(verifier, settings):
    p = _p("Location", "combobox", "Bengaluru", "", chosen=None)
    p.field.required = False
    assert any("page shows" in b for b in verifier.check([p], [], settings.path("resumes_dir") / "x.pdf")["blocks"])


def test_llm_resume_numbers_checked(verifier, settings):
    p = _p("Describe your Kafka experience", "textarea", "8 years of Kafka at scale", "8 years of Kafka at scale")
    p.provenance = "llm:resume"
    assert any("numbers not in resume" in b for b in verifier.check([p], [], settings.path("resumes_dir") / "x.pdf")["blocks"])


def test_self_transition_rejected(settings, db):
    add_job(db, settings, "greenhouse", "s1", "Twilio", "Senior Software Engineer")
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    app = db.apps()[0]
    db.transition(app["id"], "filling")
    with pytest.raises(ValueError):
        db.transition(app["id"], "filling")    # a second runner can't claim it


def test_skip_is_final_for_cluster(settings, db):
    add_job(db, settings, "greenhouse", "k1", "Cohesity", "Senior Backend Engineer", hours_ago=30)
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    app = db.apps()[0]
    db.transition(app["id"], "skipped")
    add_job(db, settings, "linkedin", "k2", "Cohesity", "Sr. Backend Engineer", hours_ago=1)  # repost joins the cluster
    for r in db.jobs("triage_status IS NULL"):
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    assert plan(settings, db)["queued"] == 0


def test_runner_up_after_fill_failure(settings, db):
    add_job(db, settings, "greenhouse", "r1", "Harness", "Senior Backend Engineer", hours_ago=2)
    add_job(db, settings, "linkedin", "r2", "Harness", "Senior Backend Engineer", hours_ago=1)
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    first = db.apps()[0]
    assert first["source"] == "greenhouse"
    db.transition(first["id"], "filling")
    db.transition(first["id"], "fill_failed")
    assert plan(settings, db)["queued"] == 1
    assert [a["source"] for a in db.apps()] == ["greenhouse", "linkedin"]


def test_needs_review_clears_approval(settings, db):
    add_job(db, settings, "greenhouse", "v1", "Clari", "Senior Software Engineer")
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    a = db.apps()[0]
    db.transition(a["id"], "filling")
    db.transition(a["id"], "needs_review")
    db.transition(a["id"], "approved")
    db.update_app(a["id"], approved=1)
    db.transition(a["id"], "filling")
    db.transition(a["id"], "needs_review")
    assert db.app(a["id"])["approved"] == 0


# ---- first real report (2026-09-25) ------------------------------------------------------------
def test_title_deny_additions():
    import yaml
    from jobpilot.filters.role import title_verdict
    cfg = yaml.safe_load(open("config/config.yaml"))["fit"]
    allow, deny = cfg["title_allow"], cfg["title_deny"]
    for t in ["Senior Software Engineer in Test", "SDE III - Machine Learning", "Senior Software Engineer, NLP",
              "Information Systems Software Application Engineer", "Senior System Software Engineer"]:
        assert title_verdict(t, allow, deny)[0] == "fail", t
    for t in ["Senior Software Engineer", "Software Engineer 3", "Senior Backend Engineer, ML Platform",
              "Platform Engineer, Edge & Networking", "Sr Fullstack Engineer- Advanced AI", "Senior Juju Software Engineer (Go)"]:
        assert title_verdict(t, allow, deny)[0] == "pass", t


def test_foreign_place_in_title():
    from jobpilot.filters.location import location_verdict, title_place
    assert title_place("Senior Backend Engineer - Databases - Analytics | Sweden | Remote") == "Sweden"
    assert title_place("US Payments Senior Engineer") is None
    assert title_place("Senior Software Engineer (Remote - Europe)") == "Europe"
    v, r = location_verdict("Remote", "Build databases.", True, "Senior Backend Engineer - Databases - Analytics | Sweden | Remote")
    assert v == "fail" and "Sweden" in r
    assert location_verdict("Bengaluru, India", "", False, "Senior Engineer | London")[0] == "pass"


def test_phone_country_picker_shows_dial_code():
    from jobpilot.verify import _country_equiv
    assert _country_equiv("+91", "India")
    assert _country_equiv("IN +91", "India")
    assert _country_equiv("India (+91)", "India")
    assert not _country_equiv("+1", "India")
    assert not _country_equiv("+91", "Singapore")


def test_himalayas_keeps_application_link():
    from jobpilot.sources.remote_boards import parse_himalayas
    js = parse_himalayas({"jobs": [{"title": "Senior Software Engineer (Golang)", "companyName": "Chainstack",
                                    "guid": "https://himalayas.app/companies/chainstack/jobs/sse",
                                    "applicationLink": "https://himalayas.app/companies/chainstack/jobs/sse/apply",
                                    "pubDate": 1790000000, "locationRestrictions": ["India"], "timezoneRestrictions": []}]})
    assert js[0].apply_url == "" and js[0].raw["application_link"].endswith("/apply")


def test_autosubmit_accepts_unknown_experience_and_salary(settings, db):
    import json
    from jobpilot.policy import auto_ok
    gates = [{"gate": "experience", "verdict": "unknown", "reason": "no experience requirement found"},
             {"gate": "salary", "verdict": "unknown", "reason": "no posted salary and no company band"},
             {"gate": "title", "verdict": "pass", "reason": "ok"}]
    job = {"source": "greenhouse", "fit_score": 0.8, "salary_basis": "unknown", "triage_status": "review",
           "triage_reasons": json.dumps(gates)}
    report = {"fields": [], "status": "pass"}
    a = settings.cfg["autosubmit"]
    a.update(allow_unknown_experience=True, allow_unknown_salary=True)
    ok, why = auto_ok(settings, db, None, job, report)
    assert ok, why
    a.update(allow_unknown_experience=False, allow_unknown_salary=False)
    ok, why = auto_ok(settings, db, None, job, report)
    assert not ok and any("experience" in w for w in why) and "salary unknown" in why
    # an unknown location is never auto-accepted
    a.update(allow_unknown_experience=True, allow_unknown_salary=True)
    gates.append({"gate": "location", "verdict": "unknown", "reason": "remote; India eligibility not stated"})
    job["triage_reasons"] = json.dumps(gates)
    ok, why = auto_ok(settings, db, None, job, report)
    assert not ok and any("location" in w for w in why)
