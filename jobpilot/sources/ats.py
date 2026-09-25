"""Public ATS job-board APIs (no auth, no scraping, no ToS risk).

Each fetcher returns (status, jobs) where status distinguishes the cases the naive
"empty list" approach collapses: board missing (404), board empty, request error.
"""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from ..models import Job

log = logging.getLogger(__name__)
UA = {"User-Agent": "jobpilot/1.0 (personal job search; contact via profile email)"}


def html_to_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _ts(v) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _get(client: httpx.Client, url: str) -> tuple[str, Optional[object]]:
    try:
        r = client.get(url, headers=UA, timeout=25)
    except httpx.HTTPError as e:
        return f"error:{type(e).__name__}", None
    if r.status_code == 404:
        return "missing", None
    if r.status_code != 200:
        return f"error:http{r.status_code}", None
    try:
        return "ok", r.json()
    except json.JSONDecodeError:
        return "error:badjson", None


def greenhouse(client: httpx.Client, slug: str, company: str) -> tuple[str, list[Job]]:
    st, d = _get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if st != "ok":
        return st, []
    out = []
    for j in d.get("jobs", []):
        out.append(Job(
            source="greenhouse", source_job_id=str(j["id"]), company=company, title=j.get("title", ""),
            url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            description=html_to_text(j.get("content", "")),
            posted_at=_ts(j.get("first_published") or j.get("updated_at")),
            department=", ".join(x.get("name", "") for x in j.get("departments", []) or []),
            raw={"slug": slug, "metadata": j.get("metadata"), "updated_at": j.get("updated_at")},
        ))
    return ("ok" if out else "empty"), out


def lever(client: httpx.Client, slug: str, company: str) -> tuple[str, list[Job]]:
    st, d = _get(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if st != "ok":
        return st, []
    if not isinstance(d, list):
        return "error:shape", []
    out = []
    for j in d:
        cats = j.get("categories") or {}
        locs = [cats.get("location", "")] + list(cats.get("allLocations") or [])
        desc = "\n".join([j.get("descriptionPlain", "")] +
                         [f"{x.get('text','')}\n{html_to_text(x.get('content',''))}" for x in j.get("lists", []) or []] +
                         [j.get("additionalPlain", "")])
        sal = j.get("salaryRange") or {}
        sal_text = f"{sal.get('currency','')} {sal.get('min','')}-{sal.get('max','')} {sal.get('interval','')}" if sal else ""
        out.append(Job(
            source="lever", source_job_id=j["id"], company=company, title=j.get("text", ""),
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl") or (j.get("hostedUrl", "") + "/apply"),
            location=" / ".join(dict.fromkeys(l for l in locs if l)),
            remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
            description=desc, posted_at=_ts(j.get("createdAt")), salary_text=sal_text,
            department=cats.get("team", ""), raw={"slug": slug, "workplaceType": j.get("workplaceType"),
                                                 "commitment": cats.get("commitment")},
        ))
    return ("ok" if out else "empty"), out


def ashby(client: httpx.Client, slug: str, company: str) -> tuple[str, list[Job]]:
    st, d = _get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if st != "ok":
        return st, []
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations", []) or []]
        comp = j.get("compensation") or {}
        out.append(Job(
            source="ashby", source_job_id=j["id"], company=company, title=j.get("title", ""),
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or (j.get("jobUrl", "") + "/application"),
            location=" / ".join(dict.fromkeys(l for l in locs if l)), remote=j.get("isRemote"),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted_at=_ts(j.get("publishedAt")),
            salary_text=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or "",
            department=j.get("department", ""), raw={"slug": slug, "workplaceType": j.get("workplaceType"),
                                                     "employmentType": j.get("employmentType")},
        ))
    return ("ok" if out else "empty"), out


def workable(client: httpx.Client, slug: str, company: str) -> tuple[str, list[Job]]:
    st, d = _get(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if st != "ok":
        return st, []
    out = []
    for j in d.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        out.append(Job(
            source="workable", source_job_id=j.get("shortcode") or j.get("id", ""), company=company,
            title=j.get("title", ""), url=j.get("url") or j.get("shortlink", ""),
            apply_url=j.get("application_url") or j.get("url", ""), location=loc, remote=j.get("telecommuting"),
            description=html_to_text(j.get("description", "")), posted_at=_ts(j.get("published_on") or j.get("created_at")),
            department=j.get("department", ""), raw={"slug": slug},
        ))
    return ("ok" if out else "empty"), out


FETCHERS: dict[str, Callable[[httpx.Client, str, str], tuple[str, list[Job]]]] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
}


def resolved_path(data_dir: Path) -> Path:
    return data_dir / "ats_resolved.json"


def probe(companies: list[dict], data_dir: Path) -> dict:
    """Try each company's slugs on every ATS; persist which (ats, slug) answers."""
    found: dict[str, dict] = {}
    with httpx.Client(follow_redirects=True) as c:
        for co in companies:
            if co.get("ats") == "none":
                continue
            atss = [co["ats"]] if co.get("ats") not in (None, "auto") else list(FETCHERS)
            for slug in co.get("slugs", []):
                for ats in atss:
                    st, jobs = FETCHERS[ats](c, slug, co["name"])
                    if st in ("ok", "empty"):
                        found[co["name"]] = {"ats": ats, "slug": slug, "status": st, "open_jobs": len(jobs)}
                        break
                if co["name"] in found:
                    break
            if co["name"] not in found:
                found[co["name"]] = {"ats": None, "slug": None, "status": "not_found"}
            log.info("%s -> %s", co["name"], found[co["name"]])
    resolved_path(data_dir).write_text(json.dumps(found, indent=1))
    return found


def discover(companies: list[dict], data_dir: Path) -> list[Job]:
    p = resolved_path(data_dir)
    resolved = json.loads(p.read_text()) if p.exists() else {}
    jobs: list[Job] = []
    with httpx.Client(follow_redirects=True) as c:
        for co in companies:
            r = resolved.get(co["name"])
            if not r or r.get("ats") not in FETCHERS:
                continue
            st, js = FETCHERS[r["ats"]](c, r["slug"], co["name"])
            if st.startswith("error") or st == "missing":
                log.warning("%s board %s/%s: %s (re-run probe-ats if persistent)", co["name"], r["ats"], r["slug"], st)
            jobs.extend(js)
    return jobs
