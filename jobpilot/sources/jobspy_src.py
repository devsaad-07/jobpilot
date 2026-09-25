"""LinkedIn / Naukri / Indeed discovery through python-jobspy (read-only search; applying
happens later in your own logged-in browser). Install: pip install -U python-jobspy
(the GitHub HEAD has newer Naukri/LinkedIn parsing fixes than PyPI: pip install git+https://github.com/speedyapply/JobSpy)."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from ..models import Job

log = logging.getLogger(__name__)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def discover(cfg: dict) -> list[Job]:
    try:
        from jobspy import scrape_jobs  # type: ignore
    except ImportError:
        log.warning("python-jobspy not installed; skipping LinkedIn/Naukri/Indeed discovery")
        return []
    js = cfg["discovery"]["jobspy"]
    out: list[Job] = []
    total = len(cfg["discovery"]["search_terms"]) * len(js["sites"])
    n = 0
    for term in cfg["discovery"]["search_terms"]:
        for site in js["sites"]:
            n += 1
            t0 = time.time()
            log.info("jobspy [%d/%d] %s '%s' ... (LinkedIn fetches each description; 1-4 min per search is normal)",
                     n, total, site, term)
            try:
                df = scrape_jobs(
                    site_name=[site], search_term=term, location="India",
                    results_wanted=js["results_per_term"], hours_old=js["hours_old"],
                    country_indeed=js.get("country_indeed", "India"),
                    linkedin_fetch_description=True, description_format="markdown", verbose=0,
                )
            except Exception as e:
                log.warning("jobspy %s '%s' failed: %s", site, term, e)
                continue
            log.info("jobspy [%d/%d] %s '%s' -> %d jobs in %.0fs", n, total, site, term, len(df), time.time() - t0)
            for r in df.to_dict("records"):
                r = {k: _clean(v) for k, v in r.items()}
                posted = r.get("date_posted")
                if posted is not None and not isinstance(posted, datetime):
                    try:
                        posted = datetime.fromisoformat(str(posted))
                    except ValueError:
                        posted = None
                if isinstance(posted, datetime) and posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
                sal = ""
                if r.get("min_amount") or r.get("max_amount"):
                    sal = f"{r.get('currency') or ''} {r.get('min_amount') or ''}-{r.get('max_amount') or ''} {r.get('interval') or ''}"
                out.append(Job(
                    source=str(r.get("site") or site), source_job_id=str(r.get("id") or r.get("job_url")),
                    company=str(r.get("company") or ""), title=str(r.get("title") or ""),
                    url=str(r.get("job_url") or ""), apply_url=str(r.get("job_url_direct") or ""),
                    location=str(r.get("location") or ""), remote=r.get("is_remote"),
                    description=str(r.get("description") or ""), posted_at=posted, salary_text=sal,
                    raw={"company_url": r.get("company_url"), "job_level": r.get("job_level"),
                         "experience_range": r.get("experience_range"), "skills": r.get("skills")},
                ))
            time.sleep(4)  # be gentle; LinkedIn rate-limits around the 10th page per IP
    return out
