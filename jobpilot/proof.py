"""Proof capture. Every application gets a folder:

proofs/2026-09-24/0042_stripe_senior-software-engineer/
  job.json              posting metadata + triage gates (why we applied)
  jd.txt / jd.html      the JD as it was when we applied (postings disappear)
  resume.pdf            exact file uploaded
  step-01.png ...       each multi-step screen after filling
  pre_submit.png/.html  the complete filled form
  fields.json           per field: label, intended, read-back value, provenance
  verification.json     verifier report (+ judge)
  post_submit.png/.html confirmation page
  network.har           browser traffic incl. the submit request/response (when enabled)
  confirmation.eml      matched confirmation email (added later by confirm-emails)
  manifest.json         sha256 of every file + timestamps (tamper-evident record)
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n]


def proof_dir(root: Path, app_id: int, company: str, title: str) -> Path:
    d = root / datetime.now(IST).strftime("%Y-%m-%d") / f"{app_id:04d}_{slug(company, 24)}_{slug(title)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(d: Path, name: str, obj: Any) -> Path:
    p = d / name
    p.write_text(json.dumps(obj, indent=2, default=str))
    return p


def snapshot(page, d: Path, name: str) -> None:
    try:
        page.screenshot(path=str(d / f"{name}.png"), full_page=True)
    except Exception:
        page.screenshot(path=str(d / f"{name}.png"))
    try:
        (d / f"{name}.html").write_text(page.content())
    except Exception:
        pass


def save_job(d: Path, job: dict, page_text: str | None = None, page_html: str | None = None) -> None:
    write_json(d, "job.json", {k: job[k] for k in job.keys() if k not in ("description", "raw")})
    (d / "jd.txt").write_text(job.get("description") or page_text or "")
    if page_html:
        (d / "jd.html").write_text(page_html)


def copy_resume(d: Path, resume: Path) -> str:
    dst = d / f"resume{resume.suffix}"
    shutil.copy2(resume, dst)
    return hashlib.sha256(dst.read_bytes()).hexdigest()


def seal(d: Path, extra: dict | None = None) -> dict:
    files = {}
    for p in sorted(d.iterdir()):
        if p.name == "manifest.json" or p.is_dir():
            continue
        files[p.name] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
    man = {"sealed_at": datetime.now(timezone.utc).isoformat(), "files": files, **(extra or {})}
    write_json(d, "manifest.json", man)
    return man
