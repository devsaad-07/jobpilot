"""Remote-job aggregators with public JSON APIs (no login): Remote OK, Remotive, Himalayas.

All three link out to the company's own application page, so applying goes through the
external route (with the late ATS dedup check) and they are review-only sources.

Terms honoured:
  Remote OK  — "link back to the URL on Remote OK and mention Remote OK as a source": every job keeps
               its Remote OK URL (shown in the report) and source name.
  Remotive   — "max. 4 times a day"; more than 2 requests/minute gets blocked; jobs are 24h delayed;
               link back + mention Remotive. jobpilot makes ONE request per run and at most 4 per day.
  Himalayas  — limit is 20 per request, cursor pagination, data refreshed every 24h, 429 when over the
               rate limit; link back + mention Himalayas. Searches with country=IN so every result is
               hireable from India.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from ..models import Job, utcnow
from .ats import html_to_text

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobpilot/1.0 (personal job search)",
      "Accept": "application/json"}
DEV_TAGS = re.compile(r"\b(backend|back-end|golang|go|engineer|developer|dev|full ?stack|platform|infrastructure|devops|sre|"
                      r"distributed|api|python|java|node|typescript)\b", re.I)


def _money(lo, hi, currency: str | None) -> str:
    try:
        lo, hi = int(lo or 0), int(hi or 0)
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    sym = {"USD": "$", "INR": "₹"}.get((currency or "USD").upper())
    if not sym:
        return ""                      # EUR/GBP etc.: not converted; the company band decides
    return f"{sym}{lo or hi:,} - {sym}{hi or lo:,}"


# ------------------------------------------------------------------ Remote OK
def parse_remoteok(data: list) -> list[Job]:
    out = []
    for j in data:
        if not isinstance(j, dict) or "id" not in j or "position" not in j:
            continue                   # first element is the legal notice
        tags = j.get("tags") or []
        if tags and not any(DEV_TAGS.search(t) for t in tags) and not DEV_TAGS.search(j.get("position", "")):
            continue
        loc = (j.get("location") or "").strip() or "Worldwide"
        posted = None
        if j.get("epoch"):
            posted = datetime.fromtimestamp(int(j["epoch"]), tz=timezone.utc)
        out.append(Job(
            source="remoteok", source_job_id=str(j["id"]), company=j.get("company", ""), title=j.get("position", ""),
            url=j.get("url", ""), apply_url="", location=f"Remote / {loc}", remote=True,
            description=html_to_text(j.get("description", "")), posted_at=posted,
            salary_text=_money(j.get("salary_min"), j.get("salary_max"), "USD"),
            raw={"tags": tags, "attribution": "Remote OK", "apply_via": j.get("apply_url")},
        ))
    return out


def remoteok(c: httpx.Client) -> list[Job]:
    r = c.get("https://remoteok.com/api")
    r.raise_for_status()
    return parse_remoteok(r.json())


# ------------------------------------------------------------------ Remotive
def _remotive_budget_ok(data_dir: Path, per_day: int = 4) -> bool:
    p = data_dir / "remotive_calls.json"
    today = datetime.now(timezone.utc).date().isoformat()
    calls = json.loads(p.read_text()) if p.exists() else {}
    if calls.get(today, 0) >= per_day:
        return False
    p.write_text(json.dumps({today: calls.get(today, 0) + 1}))
    return True


def parse_remotive(data: dict) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("candidate_required_location") or "").strip() or "Worldwide"
        posted = None
        if j.get("publication_date"):
            try:
                posted = datetime.fromisoformat(j["publication_date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                posted = None
        out.append(Job(
            source="remotive", source_job_id=str(j["id"]), company=j.get("company_name", ""), title=j.get("title", ""),
            url=j.get("url", ""), apply_url="", location=f"Remote / {loc}", remote=True,
            description=html_to_text(j.get("description", "")), posted_at=posted, salary_text=j.get("salary") or "",
            raw={"job_type": j.get("job_type"), "tags": j.get("tags"), "attribution": "Remotive"},
        ))
    return out


def remotive(c: httpx.Client, data_dir: Path) -> list[Job]:
    if not _remotive_budget_ok(data_dir):
        log.info("Remotive: daily budget (4 calls) used; skipping")
        return []
    r = c.get("https://remotive.com/api/remote-jobs", params={"category": "software-dev"})
    r.raise_for_status()
    return parse_remotive(r.json())


# ------------------------------------------------------------------ Himalayas
def _tz_ok(tzs: list, ist: float = 5.5, max_gap_h: float = 2.5) -> bool:
    """Empty = any timezone. Otherwise at least one allowed offset must be within ~2.5h of IST."""
    return not tzs or any(abs(float(t) - ist) <= max_gap_h for t in tzs)


def parse_himalayas(data: dict) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        if not _tz_ok(j.get("timezoneRestrictions") or []):
            continue
        locs = j.get("locationRestrictions") or []
        loc = " / ".join(locs) if locs else "Worldwide"
        guid = j.get("guid") or j.get("applicationLink") or ""
        posted = datetime.fromtimestamp(int(j["pubDate"]), tz=timezone.utc) if j.get("pubDate") else None
        link = j.get("applicationLink") or ""
        out.append(Job(
            source="himalayas", source_job_id=re.sub(r"^https?://", "", guid)[-160:], company=j.get("companyName", ""),
            title=j.get("title", ""), url=guid, apply_url=link if "himalayas.app" not in link else "",
            location=f"Remote / {loc}", remote=True, description=html_to_text(j.get("description") or j.get("excerpt", "")),
            posted_at=posted, salary_text=_money(j.get("minSalary"), j.get("maxSalary"), j.get("currency")),
            raw={"seniority": j.get("seniority"), "employmentType": j.get("employmentType"),
                 "timezones": j.get("timezoneRestrictions"), "attribution": "Himalayas",
                 "application_link": link},
        ))
    return out


def himalayas(c: httpx.Client, terms: list[str], max_pages: int = 2) -> list[Job]:
    jobs: list[Job] = []
    for q in terms:
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params = {"q": q, "country": "IN", "seniority": "Senior", "sort": "recent", "limit": 20}
            if cursor:
                params["cursor"] = cursor
            r = c.get("https://himalayas.app/jobs/api/search", params=params)
            if r.status_code == 429:
                log.warning("Himalayas rate limit hit; stopping for this run")
                return jobs
            r.raise_for_status()
            d = r.json()
            jobs.extend(parse_himalayas(d))
            cursor = d.get("nextCursor")
            if not cursor:
                break
            time.sleep(1.5)
    return jobs


# ------------------------------------------------------------------ entry
def discover(cfg: dict, data_dir: Path) -> list[Job]:
    rc = cfg["discovery"].get("remote_boards") or {}
    sources = rc.get("sources", ["remoteok", "remotive", "himalayas"])
    jobs: list[Job] = []
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as c:
        for name in sources:
            try:
                if name == "remoteok":
                    jobs += remoteok(c)
                elif name == "remotive":
                    jobs += remotive(c, data_dir)
                elif name == "himalayas":
                    jobs += himalayas(c, rc.get("himalayas_terms") or ["backend engineer", "software engineer", "golang"],
                                      rc.get("himalayas_pages", 2))
            except Exception as e:
                log.warning("%s discovery failed: %s", name, e)
    return jobs
