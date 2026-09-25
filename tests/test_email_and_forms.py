from datetime import datetime, timedelta, timezone

from jobpilot.appliers.form import choose_option
from jobpilot.email_confirm import _match, classify


def test_classify():
    assert classify("Thank you for applying to Stripe", "We have received your application") == "confirmation"
    assert classify("Your application", "Unfortunately we have decided to move forward with other candidates") == "rejected"
    assert classify("Next steps", "We'd love to schedule a call — share your availability") == "interview"
    assert classify("Weekly newsletter", "Top 10 tips") == "unrelated"


def test_match_by_company_and_window():
    now = datetime.now(timezone.utc)
    apps = [
        {"id": 1, "company_canon": "stripe", "title": "Senior Backend Engineer", "submitted_at": (now - timedelta(hours=2)).isoformat()},
        {"id": 2, "company_canon": "datadog", "title": "Senior Software Engineer", "submitted_at": (now - timedelta(hours=3)).isoformat()},
    ]
    m = _match(apps, "Stripe Recruiting <no-reply@greenhouse.io>", "Thank you for applying to Stripe", "Senior Backend Engineer", now, 72)
    assert m["id"] == 1
    assert _match(apps, "Acme <jobs@acme.com>", "Thanks for applying", "", now, 72) is None
    # outside the confirmation window
    assert _match(apps, "Stripe <x@stripe.com>", "Thank you for applying", "", now + timedelta(days=10), 72) is None


def test_choose_option():
    assert choose_option(5, ["0-2 years", "3-5 years", "6+ years"]) == "3-5 years"
    assert choose_option(7, ["0-2 years", "3-5 years", "6+ years"]) == "6+ years"
    assert choose_option("Yes", ["Yes, I am authorized", "No"]) == "Yes, I am authorized"
    assert choose_option("Decline to self-identify", ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"
    assert choose_option("Bengaluru", ["Bengaluru, Karnataka, India", "Mumbai"]) == "Bengaluru, Karnataka, India"
    assert choose_option("Something else", ["A", "B"]) is None
