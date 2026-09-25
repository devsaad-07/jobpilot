"""Submission policy: may this verified application be submitted right now, without a human?"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .db import DB

IST = timezone(timedelta(hours=5, minutes=30))


def _undecided(job: Any, accepted: frozenset = frozenset()) -> list[str]:
    """Triage gates left 'unknown', minus the ones config says to accept as unknown."""
    try:
        gates = json.loads(job["triage_reasons"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return ["triage left gates undecided"]
    unknown = [g for g in gates if g.get("verdict") == "unknown"]
    if not unknown:
        return [] if gates else ["triage left gates undecided"]
    return [f"triage undecided — {g['gate']}: {g['reason']}" for g in unknown if g["gate"] not in accepted]


def accepted_unknowns(s: Settings) -> frozenset:
    a = s.cfg["autosubmit"]
    return frozenset(g for g, flag in (("experience", "allow_unknown_experience"), ("salary", "allow_unknown_salary"))
                     if a.get(flag))


def gate_now(s: Settings, db: DB, source: str) -> tuple[bool, str]:
    """Global gates that apply even to approved applications."""
    if s.mode == "shadow":
        return False, "shadow mode: never submits"
    if (s.root / s.cfg.get("kill_switch_file", "STOP")).exists():
        return False, "kill switch file present"
    lo, hi = s.cfg["pacing"]["active_hours_ist"]
    h = datetime.now(IST).hour
    if not (lo <= h < hi):
        return False, f"outside active hours {lo}-{hi} IST"
    cap = s.cfg["caps_per_day"].get(source, 10)
    if db.submits_today(source) >= cap:
        return False, f"daily cap reached for {source} ({cap})"
    return True, ""


def auto_ok(s: Settings, db: DB, app: Any, job: Any, report: dict) -> tuple[bool, list[str]]:
    """Tiered auto-submit rules. Returns (ok, reasons-it-needs-review)."""
    a = s.cfg["autosubmit"]
    why: list[str] = []
    if job["source"] not in a["sources"]:
        why.append(f"source '{job['source']}' requires review")
    if (job["fit_score"] or 0) < a["min_fit_score"]:
        why.append(f"fit {job['fit_score']:.2f} < {a['min_fit_score']}")
    accepted = accepted_unknowns(s)
    if a.get("require_salary_known_or_banded") and job["salary_basis"] not in ("posted", "company_band") \
            and "salary" not in accepted:
        why.append("salary unknown")
    if a.get("require_verified_band") and job["salary_basis"] == "company_band" and "salary" not in accepted \
            and "UNVERIFIED" in (job["triage_reasons"] or ""):
        why.append("salary from an unverified company band estimate")
    llm = [f for f in report["fields"] if str(f["provenance"]).startswith("llm:")]
    if len(llm) > a.get("max_llm_answers", 0):
        why.append(f"{len(llm)} LLM-generated answer(s)")
    if not a.get("allow_llm_freetext_answers") and any(f["field"]["type"] in ("text", "textarea") for f in llm):
        why.append("LLM free-text answer present")
    if any(f["provenance"] == "portal_prefill" for f in report["fields"]):
        why.append("portal autofilled sections (e.g. Workday work history) need a look")
    if report["status"] != "pass":
        why.append(f"verification: {report['status']}")
    if job["triage_status"] != "eligible":
        why.extend(_undecided(job, accepted))
    if s.mode == "canary" and db.submits_today(auto_only=True) >= s.cfg.get("canary_daily_auto_submits", 3):
        why.append("canary auto-submit budget used for today")
    return (not why), why
