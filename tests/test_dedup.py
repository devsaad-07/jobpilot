from jobpilot.planner import plan
from jobpilot.triage import Triage

from .conftest import add_job


def triage_all(settings, db):
    t = Triage(settings)
    for r in db.jobs():
        db.set_triage(r["key"], **t.evaluate(dict(r)))


def winner(db, company_canon):
    rows = db.conn.execute("SELECT j.source, j.key FROM applications a JOIN jobs j ON j.key=a.job_key WHERE a.company_canon=?",
                           (company_canon,)).fetchall()
    return [r["source"] for r in rows]


def test_same_role_across_portals_prefers_ats_within_recency_bucket(settings, db):
    add_job(db, settings, "naukri", "n1", "Stripe India Pvt Ltd", "Sr. Backend Engineer", hours_ago=2)
    add_job(db, settings, "linkedin", "l1", "Stripe", "Senior Backend Engineer (Remote)", hours_ago=10)
    add_job(db, settings, "greenhouse", "g1", "Stripe", "Senior Backend Engineer", hours_ago=30)
    triage_all(settings, db)
    stats = plan(settings, db)
    assert stats["duplicates_collapsed"] == 2
    assert winner(db, "stripe") == ["greenhouse"]


def test_recency_beats_portal_outside_bucket(settings, db):
    add_job(db, settings, "linkedin", "l2", "Datadog", "Senior Software Engineer", hours_ago=3)
    add_job(db, settings, "greenhouse", "g2", "Datadog", "Senior Software Engineer", hours_ago=24 * 9)
    triage_all(settings, db)
    plan(settings, db)
    assert winner(db, "datadog") == ["linkedin"]


def test_same_apply_url_is_same_role_even_with_different_titles(settings, db):
    add_job(db, settings, "linkedin", "l3", "Rubrik", "Software Engineer - Backend Platform", hours_ago=1,
            apply_url="https://job-boards.greenhouse.io/rubrik/jobs/555?gh_src=linkedin")
    add_job(db, settings, "greenhouse", "g3", "Rubrik", "Senior Software Engineer, Platform", hours_ago=2,
            url="https://job-boards.greenhouse.io/rubrik/jobs/555")
    triage_all(settings, db)
    stats = plan(settings, db)
    assert stats["duplicates_collapsed"] == 1
    assert winner(db, "rubrik") == ["greenhouse"]


def test_one_application_per_company_per_run(settings, db):
    add_job(db, settings, "greenhouse", "a", "Samsara", "Senior Backend Engineer")
    add_job(db, settings, "greenhouse", "b", "Samsara", "Senior Platform Engineer")
    triage_all(settings, db)
    stats = plan(settings, db)
    assert stats["queued"] == 1 and stats["company_extra_clusters_deferred"] == 1
    # re-planning doesn't queue the second while the first is open
    assert plan(settings, db)["queued"] == 0


def test_company_cooldown(settings, db):
    add_job(db, settings, "greenhouse", "c1", "Okta", "Senior Software Engineer")
    triage_all(settings, db)
    plan(settings, db)
    app = db.apps()[0]
    for st in ("filling", "verified", "submitting", "submitted"):
        db.transition(app["id"], st, **({"submitted_at": "2099-01-01T00:00:00+00:00"} if st == "submitting" else {}))
    add_job(db, settings, "lever", "c2", "Okta Inc", "Senior Backend Engineer")
    triage_all(settings, db)
    stats = plan(settings, db)
    assert stats["queued"] == 0 and stats["company_cooldown_skips"] == 1


def test_excluded_companies_rejected(settings, db):
    for c in ["CoinSwitch", "Sahi", "alpaca.markets", "ImageKit.io", "Clearfeed", "NeoSapiens", "Lemonn"]:
        add_job(db, settings, "linkedin", c, c, "Senior Backend Engineer")
    triage_all(settings, db)
    rows = db.jobs()
    assert all(r["triage_status"] == "rejected" for r in rows), [(r["company"], r["triage_status"]) for r in rows]
    assert plan(settings, db)["queued"] == 0


def test_rejects_high_experience_and_low_salary(settings, db):
    add_job(db, settings, "greenhouse", "x1", "Postman", "Senior Backend Engineer",
            desc="Requirements: 8+ years of backend experience in Go and distributed systems. " * 5)
    add_job(db, settings, "greenhouse", "x2", "Juspay", "Senior Backend Engineer")  # band 40-70 → passes (top ≥ 60)
    add_job(db, settings, "greenhouse", "x3", "Unknownco", "Senior Backend Engineer",
            desc="We pay 20-30 LPA. 5+ years experience in Go, microservices, distributed systems. " * 5)
    triage_all(settings, db)
    st = {r["source_job_id"]: r["triage_status"] for r in db.jobs()}
    assert st == {"x1": "rejected", "x2": "eligible", "x3": "rejected"}


def test_idempotent_enqueue(settings, db):
    add_job(db, settings, "greenhouse", "i1", "Cloudflare", "Senior Software Engineer")
    triage_all(settings, db)
    plan(settings, db)
    plan(settings, db)
    assert len(db.apps()) == 1


def test_retriaged_rejected_job_leaves_queue(settings, db):
    import json
    add_job(db, settings, "greenhouse", "g9", "Stripe", "Senior Backend Engineer", hours_ago=5)
    triage_all(settings, db)
    assert plan(settings, db)["queued"] == 1
    key = db.jobs()[0]["key"]
    db.set_triage(key, triage_status="rejected",
                  triage_reasons=[{"gate": "title", "verdict": "fail", "reason": "title denied"}])
    stats = plan(settings, db)
    assert stats["dequeued_rejected"] == 1
    a = db.conn.execute("SELECT state, error FROM applications").fetchone()
    assert a["state"] == "skipped" and "title denied" in a["error"]
