"""End-to-end against local fixture forms with a real (headless) Chromium."""
import json
from pathlib import Path

import pytest

from jobpilot.appliers.engine import Engine
from jobpilot.browser import session
from jobpilot.planner import plan
from jobpilot.triage import Triage

from .conftest import add_job

FIX = Path(__file__).parent / "fixtures"


def make_form(tmp_path, mask="", why=""):
    html = (FIX / "ats_form.html").read_text().replace("MASK", mask).replace("WHY", why)
    p = tmp_path / "form.html"
    p.write_text(html)
    return p.as_uri()


def setup_app(settings, db, url):
    add_job(db, settings, "greenhouse", "e2e", "Stripe", "Senior Backend Engineer", url=url, apply_url=url)
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    return db.apps()[0]["id"]


def run(settings, db, app_id):
    eng = Engine(settings, db)
    return eng.process(app_id, lambda har: session(settings, har=har, headless=True))


def test_shadow_fills_verifies_and_never_submits(settings, db, tmp_path):
    settings.cfg["mode"] = "shadow"
    app_id = setup_app(settings, db, make_form(tmp_path))
    assert run(settings, db, app_id) == "verified"
    a = db.app(app_id)
    assert a["state"] == "verified"
    d = Path(a["proof_dir"])
    decision = json.loads((d / "decision.json").read_text())
    assert decision["auto_ok"] is True and decision["gate_now"] is False
    fields = {f["field"]["label"]: f for f in json.loads((d / "fields.json").read_text())}
    assert fields["Email *"]["actual"] == "test.user@example.com"
    assert fields["Years of experience *"]["actual"] == "3-5 years"
    assert fields["Are you legally authorized to work in India? *"]["actual"] == "Yes"
    assert fields["Will you now or in the future require visa sponsorship? *"]["actual"] == "No"
    assert fields["Gender"]["actual"] == "Decline to self-identify"
    assert fields["Expected CTC (LPA) *"]["actual"] == "65"
    assert "Dev_Saad" in fields["Resume/CV *"]["actual"]
    assert not (d / "post_submit.png").exists()
    assert "<h1>Thank you" not in (d / "pre_submit.html").read_text().split("<script>")[0]


def test_live_submits_and_seals_proofs(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    app_id = setup_app(settings, db, make_form(tmp_path))
    assert run(settings, db, app_id) == "submitted"
    a = db.app(app_id)
    assert a["state"] == "submitted" and "Thank you" in a["confirmation"]
    d = Path(a["proof_dir"])
    man = json.loads((d / "manifest.json").read_text())
    for f in ["pre_submit.png", "post_submit.png", "fields.json", "verification.json", "job.json", "jd.txt", "resume.pdf", "network.har"]:
        assert f in man["files"], f
    assert "Thank you for applying" in (d / "post_submit.html").read_text()


def test_input_mask_mismatch_blocks(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    url = make_form(tmp_path, mask='oninput="this.value=this.value.slice(0,5)"')
    app_id = setup_app(settings, db, url)
    assert run(settings, db, app_id) == "needs_review"
    reasons = " ".join(json.loads(db.app(app_id)["review_reasons"]))
    assert "Phone" in reasons
    assert db.app(app_id)["submitted_at"] is None


def test_required_freetext_without_source_blocks(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    why = '<label for="why">Why do you want to join us? *</label><textarea id="why" name="why" required></textarea>'
    app_id = setup_app(settings, db, make_form(tmp_path, why=why))
    assert run(settings, db, app_id) == "needs_review"
    assert "Why do you want to join us" in " ".join(json.loads(db.app(app_id)["review_reasons"]))


def test_unfilled_profile_placeholder_blocks(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    settings.profile["work"]["expected_ctc_lpa"] = "FILL_ME"
    app_id = setup_app(settings, db, make_form(tmp_path))
    assert run(settings, db, app_id) == "needs_review"
    assert "placeholder" in " ".join(json.loads(db.app(app_id)["review_reasons"]))


def test_review_override_then_approved_submit(settings, db, tmp_path):
    settings.cfg["mode"] = "live"
    why = '<label for="why">Why do you want to join us? *</label><textarea id="why" name="why" required></textarea>'
    app_id = setup_app(settings, db, make_form(tmp_path, why=why))
    assert run(settings, db, app_id) == "needs_review"
    ans = json.loads(db.app(app_id)["answers"])
    key = next(k for k, v in ans.items() if v["label"].startswith("Why do you want"))
    db.update_app(app_id, overrides={key: "I build low-latency Go systems and want to work on payments infrastructure."}, approved=1)
    db.transition(app_id, "approved")
    assert run(settings, db, app_id) == "submitted"


def test_multistep_easy_apply(settings, db, tmp_path):
    from jobpilot.answers import Answerer
    from jobpilot.llm.codex import Codex
    from jobpilot.verify import Verifier
    resume = settings.path("resumes_dir") / "Dev_Saad_Senior_Software_Engineer.pdf"
    eng = Engine(settings, db)
    job = {"company": "Acme", "title": "Senior Software Engineer", "location": "Bengaluru"}
    ans = Answerer(settings, Codex(enabled=False), resume, "", job)
    with session(settings, headless=True) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "easy_apply.html").as_uri())
        pc = settings.portals["portals"]["linkedin"]
        planned, submit, errs = eng._fill_steps(page, page.main_frame, pc["modal"], ans, tmp_path, pc["next_texts"], pc["submit_texts"])
        assert submit is not None and not errs
        vals = {p.field.label: p.actual for p in planned if p.actual not in ("", None)}
        assert vals["Email address"] == "test.user@example.com"
        assert vals["How many years of work experience do you have with Go?"] == "4"
        assert vals["How many years of experience do you have?"] == "5"
        rep = Verifier(settings, Codex(enabled=False), "").check(planned, errs, resume)
        assert rep["status"] == "pass", rep["blocks"]
        submit.click()
        assert "application was sent" in page.inner_text("body")
