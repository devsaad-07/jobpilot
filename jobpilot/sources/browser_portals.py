"""Discovery on Instahyre / Cutshort / Wellfound / Hirist using your logged-in browser profile.

Deliberately selector-light: open the configured search URLs, collect anchors whose href
matches `job_link_pattern`, then open each job page and take its visible text. The JD text is
parsed by the same regex/LLM triage as every other source, so portal UI changes rarely break it.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import quote_plus

from ..models import Job, utcnow

log = logging.getLogger(__name__)

_POSTED_AGO = re.compile(r"(\d+)\s*(minute|hour|day|week|month)s?\s*ago|posted\s*(today|yesterday)", re.I)


def _posted_from_text(text: str):
    from datetime import timedelta
    m = _POSTED_AGO.search(text or "")
    if not m:
        return None
    if m.group(3):
        return utcnow() - timedelta(days=0 if m.group(3).lower() == "today" else 1)
    n, unit = int(m.group(1)), m.group(2).lower()
    mult = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}[unit]
    return utcnow() - timedelta(days=n * mult)


def _first_line(text: str, pat: str) -> str:
    m = re.search(pat, text or "", re.I | re.M)
    return m.group(1).strip() if m else ""


def discover(page, portal: str, pcfg: dict, terms: list[str], max_jobs: int = 60) -> list[Job]:
    links: dict[str, str] = {}
    pat = re.compile(pcfg["job_link_pattern"])
    for tmpl in pcfg.get("search_urls", []):
        for term in terms:
            url = tmpl.format(q=quote_plus(term))
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                for _ in range(3):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(800)
                for href in page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
                    if pat.search(href):
                        links.setdefault(href.split("?")[0], term)
            except Exception as e:
                log.warning("%s search %s failed: %s", portal, url, e)
            if "{q}" not in tmpl:
                break
            time.sleep(random.uniform(2, 5))
    jobs: list[Job] = []
    for href in list(links)[:max_jobs]:
        try:
            page.goto(href, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            title = (page.locator("h1").first.inner_text(timeout=4000) or "").strip()
            text = page.locator("body").inner_text(timeout=6000)
        except Exception as e:
            log.info("%s job %s unreadable: %s", portal, href, e)
            continue
        company = _first_line(text, r"^(?:company|about)\s*[:\-]?\s*(.+)$") or _og(page, "og:site_name") or ""
        # Most portals render "Title at Company" or "Company · Location" in the header block.
        m = re.search(r"\bat\s+([A-Z][\w&.\- ]{1,60})", title)
        if m:
            company = m.group(1).strip()
            title = title[: m.start()].strip()
        jobs.append(Job(
            source=portal, source_job_id=re.sub(r"\W+", "-", href.split("//", 1)[-1])[-120:], company=company,
            title=title, url=href, location=_first_line(text, r"^(?:location|locations)\s*[:\-]?\s*(.+)$"),
            description=text[:20000], posted_at=_posted_from_text(text), raw={"search_term": links[href]},
        ))
        time.sleep(random.uniform(1.5, 4))
    return jobs


def _og(page, prop: str) -> str:
    try:
        return page.get_attribute(f'meta[property="{prop}"]', "content", timeout=1000) or ""
    except Exception:
        return ""
