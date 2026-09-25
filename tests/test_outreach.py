"""Assisted outreach: contact import, targeting, drafting checks, limits, referral hold, notifications."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from jobpilot import outreach as o
from jobpilot.answers import Answerer
from jobpilot.appliers.form import Field
from jobpilot.llm.codex import Codex
from jobpilot.models import utcnow
from jobpilot.planner import plan
from jobpilot.triage import Triage

from .conftest import add_job

FIX = Path(__file__).parent / "fixtures"
CONNECTIONS = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Rahul,Verma,https://www.linkedin.com/in/rahulverma,,Stripe,Senior Software Engineer,12 Mar 2024
Neha,Iyer,https://www.linkedin.com/in/nehaiyer,,Juspay,Engineering Manager,01 Jan 2025
"""
APOLLO = """First Name,Last Name,Title,Company,Email,Person Linkedin Url
Karan,Shah,Co-Founder & CTO,Stripe,karan@stripe.com,https://www.linkedin.com/in/karanshah
Rahul,Verma,Senior Software Engineer,Stripe,rahul@stripe.com,https://www.linkedin.com/in/rahulverma
Meera,Rao,Talent Acquisition Partner,Stripe,,https://www.linkedin.com/in/meerarao
"""


def _prep(settings, db, tmp_path, band_company="Stripe"):
    (tmp_path / "c.csv").write_text(CONNECTIONS)
    (tmp_path / "a.csv").write_text(APOLLO)
    assert o.import_connections_csv(db, settings, tmp_path / "c.csv") == {"imported": 2}
    assert o.import_contacts_csv(db, settings, tmp_path / "a.csv") == {"imported": 3}
    add_job(db, settings, "greenhouse", "s1", band_company, "Senior Backend Engineer")
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)


def test_persona():
    assert o.persona_of("Co-Founder & CTO") == "founder"
    assert o.persona_of("Engineering Manager, Payments") == "eng_manager"
    assert o.persona_of("Senior Technical Recruiter") == "recruiter"
    assert o.persona_of("SDE III") == "engineer"
    assert o.persona_of("Head of Engineering") == "eng_manager"
    assert o.persona_of("Account Executive") == "other"


def test_import_merges_and_keeps_warm_connection(settings, db, tmp_path):
    _prep(settings, db, tmp_path)
    rahul = db.conn.execute("SELECT * FROM contacts WHERE name='Rahul Verma'").fetchall()
    assert len(rahul) == 1                                      # merged by LinkedIn URL
    assert rahul[0]["persona"] == "connection" and rahul[0]["email"] == "rahul@stripe.com"


def test_referral_hold_and_release(settings, db, tmp_path):
    _prep(settings, db, tmp_path)                               # Stripe band 80-140 ≥ 60, has a connection
    app = db.apps()[0]
    assert app["hold_until"] and "referral" in app["hold_reason"]
    # apply loop filter excludes it
    rows = db.apps("a.state IN ('queued') AND (a.hold_until IS NULL OR a.hold_until <= ? OR a.referrer IS NOT NULL)",
                   (utcnow().isoformat(),))
    assert rows == []
    # a referral comes through → hold released, referrer recorded
    o.plan_and_draft(settings, db, Codex(enabled=False))
    oid = db.conn.execute("SELECT o.id FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.name='Rahul Verma'").fetchone()["id"]
    o.mark(db, oid, "referred")
    app = db.app(app["id"])
    assert app["referrer"] == "Rahul Verma" and app["hold_until"] is None


def test_no_hold_for_low_band(settings, db, tmp_path):
    (tmp_path / "c.csv").write_text(CONNECTIONS)
    o.import_connections_csv(db, settings, tmp_path / "c.csv")
    add_job(db, settings, "greenhouse", "j1", "Juspay", "Senior Backend Engineer")   # band 40-70: top ≥ 60 → still holds
    settings.cfg["outreach"]["referral_hold"]["min_band_lpa"] = 75
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))
    plan(settings, db)
    assert db.apps()[0]["hold_until"] is None


def test_targets_priority_and_cap(settings, db, tmp_path):
    _prep(settings, db, tmp_path)
    t = o.pick_targets(settings, db)
    names = [x["contact"]["name"] for x in t]
    assert names == ["Rahul Verma", "Karan Shah"]              # connection first, founder next; recruiter cut by cap of 2


def test_drafts_pass_checks_and_channels(settings, db, tmp_path):
    settings.profile["outreach"]["resume_links"]["platform"] = "https://drive.google.com/file/d/abc/view"
    settings.profile["outreach"]["resume_links"]["general"] = "https://drive.google.com/file/d/abc/view"
    _prep(settings, db, tmp_path)
    st = o.plan_and_draft(settings, db, Codex(enabled=False))
    assert st == {"targets": 2, "drafted": 2, "blocked": 0}
    rows = {r["name"]: r for r in db.conn.execute(
        "SELECT o.*, c.name FROM outreach o JOIN contacts c ON c.id=o.contact_id").fetchall()}
    rahul, karan = rows["Rahul Verma"], rows["Karan Shah"]
    assert rahul["channel"] == "linkedin" and rahul["note"] == "" and rahul["kind"] == "referral_ask"   # connected: message only
    assert "drive.google.com" in rahul["message"]
    assert karan["channel"] == "email" and karan["kind"] == "founder" and karan["email_subject"]
    # nothing drafted twice
    assert o.plan_and_draft(settings, db, Codex(enabled=False))["targets"] == 0


@pytest.mark.parametrize("note,problem", [
    ("Hi Karan, I have 12 years of Rust experience and want to join Stripe.", "numbers not in resume"),
    ("Hi Karan, see my resume https://x.io/cv, keen on Stripe.", "link in connection note"),
    ("Hi Karan, " + "x" * 400, "chars"),
])
def test_check_draft_blocks(note, problem):
    d = {"note": note, "message": "", "email_subject": "", "email_body": ""}
    r = o.check_draft(d, note_chars=300, company="Stripe", first_name="Karan", known_numbers={"5", "4"},
                      resume_link="", others=[], with_note=True)
    assert r["status"] == "block" and any(problem in b for b in r["blocks"]), r


def test_near_duplicate_blocked():
    n = "Hi Karan, I'm a backend engineer applying for Senior Backend Engineer at Stripe; open to referring me?"
    r = o.check_draft({"note": n, "message": "", "email_subject": "", "email_body": ""}, note_chars=300, company="Stripe",
                      first_name="Karan", known_numbers=set(), resume_link="", others=[n.replace("Karan", "Karn")], with_note=True)
    assert any("near-identical" in b for b in r["blocks"])


def test_premium_trial_expiry_falls_back_to_free(settings):
    settings.cfg["outreach"]["linkedin"]["premium_until"] = (date.today() - timedelta(days=1)).isoformat()
    assert o.linkedin_plan(settings) == ("free", 200)
    settings.cfg["outreach"]["linkedin"]["premium_until"] = (date.today() + timedelta(days=5)).isoformat()
    assert o.linkedin_plan(settings) == ("premium", 300)


def test_limits_and_acceptance_pause(settings, db, tmp_path):
    _prep(settings, db, tmp_path)
    o.plan_and_draft(settings, db, Codex(enabled=False))
    now = utcnow().isoformat()
    cid = db.conn.execute("SELECT id FROM contacts LIMIT 1").fetchone()["id"]
    for i in range(20):
        db.conn.execute("INSERT INTO outreach(contact_id,company_canon,channel,kind,state,note,created_at,sent_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (cid, "x", "linkedin", "recruiter", "invited", "n", now, now, now))
    st = o.limits_status(settings, db)
    assert st["can_invite"] is False                            # 20 today > 15/day
    assert st["paused"] is True and st["acceptance_rate"] == 0  # 0 of 20 accepted


def test_notifications():
    assert o.parse_notification("Priya Sharma accepted your invitation") == ("accepted", "Priya Sharma")
    assert o.parse_notification("Karan Shah sent you a message") == ("replied", "Karan Shah")
    assert o.parse_notification("New message from Rahul Verma") == ("replied", "Rahul Verma")
    assert o.parse_notification("Jobs you may be interested in") is None


def test_notification_updates_outreach(settings, db, tmp_path):
    _prep(settings, db, tmp_path)
    o.plan_and_draft(settings, db, Codex(enabled=False))
    oid = db.conn.execute("SELECT o.id FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.name='Karan Shah'").fetchone()["id"]
    db.conn.execute("UPDATE outreach SET state='invited', channel='linkedin', sent_at=? WHERE id=?", (utcnow().isoformat(), oid))
    assert o.apply_notification(db, "accepted", "Karan Shah", utcnow()) == oid
    assert db.conn.execute("SELECT state FROM outreach WHERE id=?", (oid,)).fetchone()["state"] == "accepted"
    assert [r["name"] for r in o.followups(settings, db)["send_message_with_resume"]] == ["Karan Shah"]


def test_referrer_answers(settings):
    a = Answerer(settings, Codex(enabled=False), Path("r.pdf"), "", {}, referrer="Rahul Verma")
    assert a.plan(Field(id="1", type="text", label="Referred by", required=False)).value == "Rahul Verma"
    p = a.plan(Field(id="2", type="radio", label="Were you referred by an employee?", required=True, options=["Yes", "No"]))
    assert p.chosen_option == "Yes"
    p = a.plan(Field(id="3", type="select", label="How did you hear about us?", required=True,
                     options=["LinkedIn", "Employee Referral", "Other"]))
    assert p.chosen_option == "Employee Referral"
    b = Answerer(settings, Codex(enabled=False), Path("r.pdf"), "", {})
    assert b.plan(Field(id="2", type="radio", label="Were you referred by an employee?", required=True, options=["Yes", "No"])).chosen_option == "No"


def test_gmail_draft_message_has_resume():
    pdf = next((Path(__file__).parent.parent / "resumes").glob("*.pdf"))
    m = o.build_email("me@x.com", "karan@stripe.com", "Senior Backend Engineer at Stripe", "Hi Karan", pdf)
    atts = [p for p in m.iter_attachments()]
    assert m["To"] == "karan@stripe.com" and atts and atts[0].get_filename() == pdf.name


def test_hiring_team_capture(settings, db):
    from jobpilot.browser import session
    with session(settings, headless=True) as ctx:
        page = ctx.new_page()
        page.goto((FIX / "linkedin_job.html").as_uri())
        n = o.capture_hiring_team(page, db, settings, {"company": "Stripe", "key": "linkedin:1"})
    rows = {r["name"]: r for r in db.conn.execute("SELECT * FROM contacts").fetchall()}
    assert n == 2 and set(rows) == {"Priya Sharma", "Arjun Mehta"}   # sidebar person ignored
    assert rows["Priya Sharma"]["persona"] == "hiring_team"
    assert rows["Priya Sharma"]["linkedin_url"] == "https://www.linkedin.com/in/priya-eng-manager"
