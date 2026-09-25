"""Workday career sites (`*.myworkdayjobs.com`) through the JSON endpoints the career site itself uses.

  list:   POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
          body {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "..."}
  detail: GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

Gotchas handled: `limit` > 20 returns nothing or a 400; only the first page carries `total`;
`postedOn` is text ("Posted 3 Days Ago") so the detail's `startDate` is preferred; the backend is slow,
so requests retry once. Each company's site is configured in companies.yaml as its careers URL,
e.g. `workday: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx

from ..filters.location import INDIA, REMOTE
from ..filters.role import title_verdict
from ..models import Job, utcnow
from .ats import html_to_text

log = logging.getLogger(__name__)
PAGE = 20


def parse_site(url: str) -> Optional[dict]:
    """'https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite' -> tenant/host/site."""
    p = urlsplit(url.strip())
    m = re.match(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", p.netloc)
    if not m:
        return None
    parts = [x for x in p.path.split("/") if x and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", x)]
    if not parts:
        return None
    return {"tenant": m.group(1), "host": p.netloc, "site": parts[0]}


def posted_from_text(s: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or utcnow()
    t = (s or "").lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    if m := re.search(r"(\d+)\+?\s*days?", t):
        return now - timedelta(days=int(m.group(1)))
    return None


def _req(c: httpx.Client, method: str, url: str, **kw) -> Optional[httpx.Response]:
    for attempt in range(2):
        try:
            r = c.request(method, url, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 400, 422):
                log.warning("workday %s %s -> %s", method, url, r.status_code)
                return None
        except httpx.HTTPError as e:
            log.info("workday %s retry %d: %s", url, attempt, e)
        time.sleep(1.5)
    return None


def _base(site: dict) -> str:
    return f"https://{site['host']}/wday/cxs/{site['tenant']}/{site['site']}"


def probe(c: httpx.Client, url: str) -> tuple[str, int]:
    site = parse_site(url)
    if not site:
        return "bad_url", 0
    r = _req(c, "POST", f"{_base(site)}/jobs", json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if r is None:
        return "missing", 0
    return "ok", int(r.json().get("total") or 0)


def list_jobs(c: httpx.Client, site: dict, term: str, max_pages: int) -> list[dict]:
    out, total = [], None
    for page in range(max_pages):
        r = _req(c, "POST", f"{_base(site)}/jobs",
                 json={"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": term})
        if r is None:
            break
        d = r.json()
        if total is None:
            total = int(d.get("total") or 0)   # only page 1 carries it
        posts = d.get("jobPostings") or []
        out.extend(posts)
        if not posts or (page + 1) * PAGE >= (total or 0):
            break
    return out


def to_job(site: dict, company: str, post: dict, detail: Optional[dict]) -> Job:
    info = (detail or {}).get("jobPostingInfo") or {}
    ext = post.get("externalPath", "")
    url = f"https://{site['host']}/{site['site']}{ext}"
    posted = None
    if info.get("startDate"):
        try:
            posted = datetime.fromisoformat(info["startDate"]).replace(tzinfo=timezone.utc)
        except ValueError:
            posted = None
    posted = posted or posted_from_text(post.get("postedOn") or info.get("postedOn", ""))
    locs = [post.get("locationsText") or info.get("location") or ""] + list(info.get("additionalLocations") or [])
    remote_type = (info.get("remoteType") or "").lower()
    return Job(
        source="workday", source_job_id=f"{site['tenant']}:{info.get('jobReqId') or ext}", company=company,
        title=post.get("title") or info.get("title", ""), url=url, apply_url=url,
        location=" / ".join(dict.fromkeys(l for l in locs if l)),
        remote=("remote" in remote_type) if remote_type else None,
        description=html_to_text(info.get("jobDescription", "")), posted_at=posted,
        raw={"tenant": site["tenant"], "site": site["site"], "timeType": info.get("timeType"),
             "postedOn": post.get("postedOn"), "bulletFields": post.get("bulletFields"), "country": (info.get("country") or {}).get("descriptor")},
    )


def discover(companies: list[dict], cfg: dict) -> list[Job]:
    wc = cfg["discovery"].get("workday") or {}
    terms = wc.get("search_terms") or cfg["discovery"]["search_terms"][:4]
    allow, deny = cfg["fit"]["title_allow"], cfg["fit"]["title_deny"]
    jobs: list[Job] = []
    hdr = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Language": "en-US",
           "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobpilot/1.0"}
    with httpx.Client(headers=hdr, timeout=40, follow_redirects=True) as c:
        for co in companies:
            if not co.get("workday"):
                continue
            site = parse_site(co["workday"])
            if not site:
                log.warning("%s: bad workday url %s", co["name"], co["workday"])
                continue
            seen: dict[str, dict] = {}
            for term in terms:
                for p in list_jobs(c, site, term, wc.get("max_pages_per_term", 3)):
                    seen.setdefault(p.get("externalPath", ""), p)
            for ext, p in seen.items():
                # cheap filters before the per-job detail call (Workday is slow)
                if title_verdict(p.get("title", ""), allow, deny)[0] == "fail":
                    continue
                loc = p.get("locationsText") or ""
                if loc and not (INDIA.search(loc) or REMOTE.search(loc) or re.search(r"\d+ locations", loc, re.I)):
                    continue
                r = _req(c, "GET", f"{_base(site)}{ext}")
                jobs.append(to_job(site, co["name"], p, r.json() if r is not None else None))
                time.sleep(0.4)
    return jobs
