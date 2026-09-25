import copy
from datetime import timedelta

import pytest

from jobpilot.config import load_settings
from jobpilot.db import DB
from jobpilot.models import Job, utcnow
from jobpilot.normalize import build_alias_map, canon_company, canon_title, canon_url

FILLED = {   # the committed profile.yaml is all FILL_ME; tests use this made-up person
    "identity.first_name": "Test",
    "identity.last_name": "User",
    "identity.full_name": "Test User",
    "identity.email": "test.user@example.com",
    "identity.phone": "+91 9000000000",
    "identity.phone_national": "9000000000",
    "identity.linkedin": "https://linkedin.com/in/test-user",
    "identity.github": "https://github.com/test-user",
    "work.current_company": "Acme Exchange",
    "work.current_title": "Software Development Engineer II",
    "education.institution": "Test Institute of Technology",
    "outreach.signature": "Test User · linkedin.com/in/test-user",
    "work.notice_period_days": 60,
    "work.current_ctc_lpa": 40,
    "work.expected_ctc_lpa": 65,
}


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s.cfg = copy.deepcopy(s.cfg)
    s.profile = copy.deepcopy(s.profile)
    s.cfg["paths"]["data_dir"] = str(tmp_path / "data")
    s.cfg["paths"]["proofs_dir"] = str(tmp_path / "proofs")
    s.cfg["paths"]["browser_profile"] = str(tmp_path / "chrome")
    s.cfg["browser"].update(channel=None, headless=True, slow_mo_ms=0, record_har=True)
    s.cfg["llm"]["laya"]["enabled"] = False
    s.cfg["llm"]["codex"]["enabled"] = False
    s.cfg["pacing"]["active_hours_ist"] = [0, 24]
    for k, v in FILLED.items():
        a, b = k.split(".")
        s.profile[a][b] = v
    return s


@pytest.fixture
def db(settings):
    return DB(settings.path("data_dir") / "t.db")


def add_job(db, settings, source, sid, company, title, *, hours_ago=1, url=None, apply_url="", desc=None, location="Bengaluru, India"):
    alias = build_alias_map(settings.companies)
    j = Job(source=source, source_job_id=sid, company=company, title=title,
            url=url or f"https://{source}.example/jobs/{sid}", apply_url=apply_url, location=location,
            description=desc or ("We are looking for a backend engineer with 5+ years of experience in Go, distributed "
                                 "systems, microservices, Kafka, PostgreSQL, Redis, Kubernetes on AWS. " * 3),
            posted_at=utcnow() - timedelta(hours=hours_ago))
    db.upsert_job(j, canon_company(company, alias), canon_title(title), canon_url(j.url), canon_url(apply_url))
    return j.key
