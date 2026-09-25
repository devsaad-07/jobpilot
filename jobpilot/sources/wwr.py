"""We Work Remotely via its public category RSS feeds (no login, no scraping).

WWR doesn't host application forms: "Apply for this position" links to the company's own
page/ATS, so applying goes through the external-apply route (and the late ATS dedup check).
Feed items: <title>Company: Role</title>, <region>, <country>, <type>, <skills>, <pubDate>, <link>.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from ..models import Job
from .ats import UA, html_to_text

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def _t(el: ET.Element, tag: str) -> str:
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_feed(xml_text: str) -> list[Job]:
    root = ET.fromstring(xml_text)
    out: list[Job] = []
    for it in root.iter("item"):
        raw_title = _t(it, "title")
        company, _, title = raw_title.partition(": ")
        if not title:
            company, title = "", raw_title
        link = _t(it, "link") or _t(it, "guid")
        posted: Optional[object] = None
        try:
            posted = parsedate_to_datetime(_t(it, "pubDate"))
        except (TypeError, ValueError):
            posted = None
        region, country = _t(it, "region"), _t(it, "country")
        desc = html_to_text(_t(it, "description"))
        m = re.search(r"/remote-jobs/([\w-]+)", link)
        out.append(Job(
            source="weworkremotely", source_job_id=m.group(1) if m else link, company=company.strip(),
            title=title.strip(), url=link, apply_url="", location=" / ".join(x for x in ["Remote", region, country] if x),
            remote=True, description=desc, posted_at=posted,
            raw={"region": region, "country": country, "type": _t(it, "type"), "skills": _t(it, "skills"),
                 "expires_at": _t(it, "expires_at")},
        ))
    return out


def discover(cfg: dict) -> list[Job]:
    feeds = (cfg["discovery"].get("wwr") or {}).get("feeds") or DEFAULT_FEEDS
    jobs: list[Job] = []
    with httpx.Client(follow_redirects=True, headers=UA, timeout=25) as c:
        for f in feeds:
            try:
                r = c.get(f)
                r.raise_for_status()
                jobs.extend(parse_feed(r.text))
            except Exception as e:
                log.warning("WWR feed %s failed: %s", f, e)
    return jobs
