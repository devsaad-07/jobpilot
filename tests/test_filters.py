import pytest

from jobpilot.config import load_settings
from jobpilot.filters.experience import experience_verdict, parse_experience
from jobpilot.filters.location import location_verdict
from jobpilot.filters.role import title_verdict
from jobpilot.filters.salary import parse_salary, salary_verdict
from jobpilot.normalize import canon_company, canon_title, canon_url

S = load_settings()
ALLOW, DENY = S.cfg["fit"]["title_allow"], S.cfg["fit"]["title_deny"]


@pytest.mark.parametrize("text,lo,hi", [
    ("We need 5+ years of experience building backend systems", 5, None),
    ("Experience: 4-8 years in software development", 4, 8),
    ("minimum 7 years of professional experience", 7, None),
    ("3 to 5 yrs experience in Java", 3, 5),
    ("At least 6 years of hands-on backend development", 6, None),
    ("Our company was founded 25 years ago. You have 5+ years of experience with Go.", 5, None),
    ("3+ years with Go; 8+ years of backend engineering experience", 8, None),
    ("No explicit number here, just great engineers", None, None),
])
def test_parse_experience(text, lo, hi):
    a, b, _ = parse_experience(text)
    assert (a, b) == (lo, hi)


@pytest.mark.parametrize("lo,hi,verdict", [
    (5, None, "pass"), (6, None, "pass"), (4, 8, "pass"), (3, None, "pass"),
    (7, None, "fail"), (8, 12, "fail"), (1, 3, "fail"), (2, 4, "fail"), (None, None, "unknown"),
])
def test_experience_verdict(lo, hi, verdict):
    assert experience_verdict(lo, hi, 6, 5)[0] == verdict


@pytest.mark.parametrize("title,verdict", [
    ("Senior Software Engineer", "pass"), ("Senior Backend Engineer (Go)", "pass"), ("SDE-3", "pass"),
    ("SDE III - Payments", "pass"), ("Software Development Engineer 3", "pass"), ("Backend Engineer", "pass"),
    ("Senior Platform Engineer", "pass"), ("Lead Engineer - Backend", "pass"), ("Full Stack Engineer", "pass"),
    ("Software Engineer II", "pass"), ("Member of Technical Staff", "pass"),
    ("Staff Software Engineer", "fail"), ("Principal Engineer", "fail"), ("Engineering Manager", "fail"),
    ("Software Engineer I", "fail"), ("SDE-1", "fail"), ("Senior Frontend Engineer", "fail"),
    ("Senior Data Engineer", "fail"), ("Senior iOS Engineer", "fail"), ("Software Engineer Intern", "fail"),
    ("Senior SRE", "fail"), ("Product Manager", "fail"), ("Senior Software Engineer in Test", "fail"),
])
def test_title(title, verdict):
    assert title_verdict(title, ALLOW, DENY)[0] == verdict, title


@pytest.mark.parametrize("text,lo,hi", [
    ("CTC: 45-70 LPA", 45, 70), ("₹60L - ₹80L", 60, 80), ("INR 5,000,000 - 7,500,000", 50, 75),
    ("Up to 1.2 Cr", 120, 120), ("$120k - $160k", 100.8, 134.4), ("salary 35 lakhs", 35, 35),
    ("Compensation: competitive", None, None), ("$40 per hour", None, None),
])
def test_salary(text, lo, hi):
    a, b, _ = parse_salary(text, 84)
    if lo is None:
        assert a is None
    else:
        assert a == pytest.approx(lo) and b == pytest.approx(hi)


def test_salary_verdict():
    assert salary_verdict(45, 65, 60)[0] == "pass"
    assert salary_verdict(30, 50, 60)[0] == "fail"
    assert salary_verdict(None, None, 60)[0] == "unknown"


@pytest.mark.parametrize("loc,desc,remote,verdict", [
    ("Bengaluru, Karnataka, India", "", None, "pass"),
    ("Remote", "We hire anywhere; async-first team", True, "pass"),
    ("Remote - US", "", True, "fail"),
    ("Remote", "Candidates must be authorized to work in the US", True, "fail"),
    ("Remote", "Must be based in India", True, "pass"),
    ("Remote (APAC)", "", True, "pass"),
    ("Remote", "Join our team", True, "unknown"),
    ("London, UK", "", None, "fail"),
])
def test_location(loc, desc, remote, verdict):
    assert location_verdict(loc, desc, remote)[0] == verdict


def test_normalize():
    assert canon_company("Razorpay Software Pvt. Ltd.") == "razorpay"
    assert canon_company("sahi.com") == "sahi"
    assert canon_company("alpaca.markets") == "alpaca"
    assert canon_title("Sr. Backend Engineer (Remote)") == canon_title("Senior Back-end Engineer")
    assert canon_title("SDE-3") == "senior software engineer"
    assert canon_url("https://www.linkedin.com/jobs/view/senior-backend-engineer-at-x-4012345678/?trk=abc") == \
        canon_url("https://linkedin.com/jobs/view/4012345678")
    assert canon_url("https://jobs.lever.co/acme/abc-123/apply?lever-source=LinkedIn") == "jobs.lever.co/acme/abc-123"
