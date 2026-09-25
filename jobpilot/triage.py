"""Fit validation: every job gets a verdict per gate plus an overall status.

    eligible  — all gates pass
    review    — no gate failed, but at least one couldn't be decided (goes to the review queue)
    rejected  — any gate failed

Gates run cheapest-first; LLM calls (Laya) only fill in what regexes couldn't decide.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Optional

from .config import Settings
from .filters.experience import experience_verdict, parse_experience
from .filters.location import location_verdict
from .filters.role import is_staffing, title_verdict
from .filters.salary import parse_salary, salary_verdict
from .llm.laya import Laya, answer_choice, answer_noul, answer_score, trim_jd
from .models import utcnow
from .normalize import build_alias_map, canon_company

log = logging.getLogger(__name__)

RESUMES = {
    "trading": {
        "file": "Dev_Saad_Senior_Trading_Platform_Engineer.pdf",
        "describe": "trading systems, exchanges, brokers, order management, market data, market making, options, low latency",
        "keywords": r"trading|exchange|order management|\boms\b|market[- ]mak|market data|broker|hft|low[- ]latency|options|"
                    r"derivatives|futures|crypto|matching engine|quant|capital markets|equities|fix protocol|execution",
    },
    "platform": {
        "file": "Dev_Saad_Senior_Platform_Engineer.pdf",
        "describe": "platform and infrastructure: messaging, deployment, observability, Kubernetes, developer platform, reliability",
        "keywords": r"platform|infrastructure|messaging|kafka|nats|pub/?sub|observability|prometheus|grafana|kubernetes|k8s|"
                    r"developer (experience|productivity)|internal tools|reliability|control plane|service mesh|ci/cd|terraform",
    },
    "general": {
        "file": "Dev_Saad_Senior_Software_Engineer.pdf",
        "describe": "general backend / product engineering: APIs, microservices, distributed systems, payments, product features",
        "keywords": r"microservices|api|product|backend|distributed systems|payments|fintech|saas|scalab",
    },
}

CANDIDATE_BRIEF = ("Candidate: backend engineer, 5 years, 4 in Go. Built options market-making quoting engine and "
                   "hedging system (NATS JetStream, PostgreSQL), equities broker order management (250K orders/day), "
                   "config service at 1.2M req/min, WebSocket layer 400K events/s, Redis, Kubernetes, Prometheus. "
                   "Mentors engineers, led 8-engineer migration. Also TypeScript, Java, C++, Python.")

_SKILL_WEIGHTS = {
    r"\bgo(lang)?\b": 3, r"distributed systems?": 2, r"microservices?": 1.5, r"\bkafka|nats|rabbitmq|message (queue|broker)": 1.5,
    r"postgres|mysql|sql\b": 1, r"redis": 1, r"kubernetes|k8s|docker": 1, r"aws|gcp|cloud": 0.5, r"grpc|protobuf": 1,
    r"low[- ]latency|high[- ]throughput|scal(e|ability)": 1, r"trading|exchange|fintech|payments?": 1.5,
    r"system design|architecture": 1, r"mentor": 0.5, r"java\b|c\+\+|typescript|node": 0.5,
}
_OFF_STACK = r"\b(php|ruby on rails|\.net|c#|salesforce|sap|mainframe|cobol|drupal|wordpress|magento)\b"


def keyword_fit(text: str) -> float:
    t = text.lower()
    s = sum(w for p, w in _SKILL_WEIGHTS.items() if re.search(p, t))
    if re.search(_OFF_STACK, t):
        s -= 3
    return max(0.0, min(1.0, s / 10.0))


def pick_resume_keywords(text: str) -> tuple[str, dict[str, int]]:
    counts = {k: len(re.findall(v["keywords"], text, re.I)) for k, v in RESUMES.items()}
    # Trading and platform need a clear signal; otherwise the general resume is safest.
    if counts["trading"] >= 3 and counts["trading"] >= counts["platform"]:
        return "trading", counts
    if counts["platform"] >= 4 and counts["platform"] > counts["trading"]:
        return "platform", counts
    return "general", counts


class Triage:
    def __init__(self, s: Settings):
        self.s = s
        f = s.cfg["fit"]
        self.f = f
        self.alias = build_alias_map(s.companies)
        self.excluded = {canon_company(c, self.alias) for c in s.cfg["exclusions"]["companies"]}
        self.excluded |= {canon_company(d) for d in s.cfg["exclusions"].get("domains", [])}
        self.bands = {canon_company(c["name"], self.alias): c for c in s.companies}
        lc = s.cfg["llm"]["laya"]
        self.laya = Laya(lc["model"], lc.get("dtype", "float16"), lc.get("enabled", True))

    def company_band(self, company_canon: str) -> Optional[dict]:
        return self.bands.get(company_canon)

    def evaluate(self, job: dict[str, Any]) -> dict[str, Any]:
        """job: a row from the jobs table (dict-like). Returns fields for DB.set_triage."""
        gates: list[dict] = []

        def gate(name: str, verdict: str, reason: str, **extra):
            gates.append({"gate": name, "verdict": verdict, "reason": reason, **extra})

        title, desc, loc = job["title"], job["description"] or "", job["location"] or ""
        cc = job["company_canon"]
        full = f"{title}\n{loc}\n{desc}"

        # 1. exclusions
        if cc in self.excluded or any(e and e in (desc.lower()[:400]) for e in self.s.cfg["exclusions"].get("domains", [])):
            gate("exclusion", "fail", f"company '{job['company']}' is excluded")
        else:
            gate("exclusion", "pass", "not excluded")
        if not self.f.get("allow_staffing_agencies", True) and is_staffing(job["company"], desc):
            gate("staffing", "fail", "staffing/consultancy posting")

        # 2. freshness
        max_age = self.s.cfg["discovery"]["max_posting_age_days"]
        if job["posted_at"]:
            from datetime import datetime
            age = utcnow() - datetime.fromisoformat(job["posted_at"])
            if age > timedelta(days=max_age):
                gate("freshness", "fail", f"posted {age.days}d ago (> {max_age}d)")
            else:
                gate("freshness", "pass", f"posted {age.days}d ago")
        else:
            gate("freshness", "pass", "posting date unknown")

        # 3. title
        v, r = title_verdict(title, self.f["title_allow"], self.f["title_deny"])
        gate("title", v, r)

        # 4. experience
        raw_exp = (job.get("raw") or "")
        lo, hi, ev = parse_experience(desc)
        if lo is None and hi is None and "experience_range" in raw_exp:
            lo, hi, ev = parse_experience(raw_exp)          # Naukri gives "5-10 Yrs" as metadata
        ev_cfg = self.f["experience"]
        v, r = experience_verdict(lo, hi, ev_cfg["max_min_required"], ev_cfg["min_max_required"])
        if v == "unknown":
            p = answer_noul(self.laya.predict(trim_jd(title, loc, desc),
                                              {"senior7": {"type": "noul", "instructions": "The role requires 7 or more years of professional experience."}}),
                            "senior7")
            if p is not None:
                v, r = ("fail", f"Laya: likely needs 7+ yrs (p={p:.2f})") if p >= 0.7 else \
                       (("pass", f"Laya: 7+ yrs unlikely (p={p:.2f})") if p <= 0.3 else ("unknown", r))
            if v == "unknown":
                v = {"accept": "pass", "reject": "fail"}.get(ev_cfg["unknown_policy"], "unknown")
        gate("experience", v, r, evidence=ev)

        # 5. location
        v, r = location_verdict(loc, desc, None if job["remote"] is None else bool(job["remote"]), title)
        if v == "unknown":
            p = answer_noul(self.laya.predict(trim_jd(title, loc, desc),
                                              {"india": {"type": "noul", "instructions": "Someone living and working from India can be hired for this role."}}),
                            "india")
            if p is not None and p >= 0.75:
                v, r = "pass", f"Laya: India-eligible (p={p:.2f})"
            elif p is not None and p <= 0.25:
                v, r = "fail", f"Laya: not India-eligible (p={p:.2f})"
        gate("location", v, r)

        # 6. salary
        sc = self.f["salary"]
        slo, shi, sev = parse_salary(f"{job.get('salary_text') or ''}\n{desc}", sc.get("usd_to_inr", 84))
        basis = "posted"
        v, r = salary_verdict(slo, shi, sc["min_lpa"])
        if v == "unknown":
            band = self.company_band(cc)
            if band and band.get("band_lpa"):
                slo, shi = band["band_lpa"]
                basis = "company_band"
                v, r = salary_verdict(slo, shi, sc["min_lpa"])
                r = f"{r} (company band{'' if band.get('band_verified') else ', UNVERIFIED estimate'})"
            else:
                basis = "unknown"
                v, r = "unknown", "no posted salary and no company band"
        gate("salary", v, r, evidence=sev, basis=basis)

        # 7. fit score + resume
        jd = trim_jd(title, loc, desc)
        ans = self.laya.predict(f"{CANDIDATE_BRIEF}\n\n{jd}", {
            "fit": {"type": "score", "instructions": "How well does the candidate's background match this role's requirements?",
                    "criteria": ["poor match", "partial match", "good match", "strong match"]},
            "resume": {"type": "choice", "instructions": "Which focus best describes this role?",
                       "criteria": {k: v["describe"] for k, v in RESUMES.items()}},
        })
        fit = answer_score(ans, "fit", 4)
        kfit = keyword_fit(full)
        fit_score = round(0.7 * fit + 0.3 * kfit, 3) if fit is not None else round(kfit, 3)
        kw_variant, counts = pick_resume_keywords(full)
        lv, lp = answer_choice(ans, "resume")
        variant = lv if (lv in RESUMES and lp >= 0.5) else kw_variant
        umf = sc.get("unknown_min_fit")
        if basis == "unknown" and umf is not None and fit_score < umf:
            for g in gates:
                if g["gate"] == "salary":
                    g.update(verdict="fail", reason=f"no posted salary, no company band, and fit {fit_score:.2f} < {umf}")
        if len(desc) < 300:
            gate("fit", "unknown", "description too short to score (fetch JD or review)")
        else:
            gate("fit", "pass" if fit_score >= self.f["min_fit_score"] else "fail",
                 f"fit {fit_score:.2f} ({'laya+kw' if fit is not None else 'keywords'})")

        verdicts = [g["verdict"] for g in gates]
        status = "rejected" if "fail" in verdicts else ("review" if "unknown" in verdicts else "eligible")
        return {
            "triage_status": status, "triage_reasons": gates, "fit_score": fit_score, "resume_variant": variant,
            "exp_min": lo, "exp_max": hi, "salary_lpa_min": slo, "salary_lpa_max": shi, "salary_basis": basis,
        }
