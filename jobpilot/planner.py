"""Dedup + ranking + queueing.

Role-level dedup: the same role reaches us from several portals (Greenhouse board, a LinkedIn
repost, a Naukri copy). Jobs at the same canonical company whose canonical titles are near-equal,
or which point at the same apply URL, form one *cluster*; exactly one member is applied to.

Winner inside a cluster, per your rule — recency first, then portal reputation:
  1. recency tier: postings within `recency_bucket_hours` of the freshest member tie
     (reposts a day apart shouldn't beat the original on a worse portal)
  2. portal_rank (direct ATS > LinkedIn > Instahyre > Naukri > ...)
  3. exact posted_at, then fit score

Company-level dedup: a company applied to within `company_cooldown_days` is skipped; at most
`max_applications_per_company_per_run` new clusters per company are queued (best fit first).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .db import DB
from .models import utcnow

log = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz

    def _sim(a: str, b: str) -> float:
        return fuzz.token_set_ratio(a, b) / 100.0
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _sim(a: str, b: str) -> float:
        return SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()


def _ts(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _seniority(t: str) -> str:
    return "lead" if "lead" in t else ("senior" if "senior" in t or " 3" in f" {t}" else "mid")


def same_role(a: dict, b: dict, threshold: float) -> bool:
    if a["company_canon"] != b["company_canon"]:
        return bool(a["apply_url_canon"]) and a["apply_url_canon"] in (b["apply_url_canon"], b["url_canon"])
    for x, y in ((a["apply_url_canon"], b["apply_url_canon"]), (a["url_canon"], b["apply_url_canon"]),
                 (a["apply_url_canon"], b["url_canon"])):
        if x and x == y:
            return True
    if _seniority(a["title_canon"]) != _seniority(b["title_canon"]):
        return False
    return _sim(a["title_canon"], b["title_canon"]) >= threshold


def cluster_jobs(rows: list[dict], threshold: float) -> dict[str, list[dict]]:
    """Union-find over jobs; clusters keep an existing cluster_id when any member already has one."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_company: dict[str, list[int]] = defaultdict(list)
    by_url: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_company[r["company_canon"]].append(i)
        for u in {r["url_canon"], r["apply_url_canon"]} - {"", None}:
            by_url[u].append(i)
    for idxs in list(by_company.values()) + list(by_url.values()):
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                if find(i) != find(j) and same_role(rows[i], rows[j], threshold):
                    parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[find(i)].append(r)
    out: dict[str, list[dict]] = {}
    for members in groups.values():
        existing = sorted({m["cluster_id"] for m in members if m.get("cluster_id")})
        cid = existing[0] if existing else "c_" + hashlib.sha1(
            (members[0]["company_canon"] + "|" + min(m["title_canon"] for m in members)).encode()).hexdigest()[:12]
        out[cid] = members
    return out


def pick_winner(members: list[dict], portal_rank: dict[str, int], bucket_hours: float) -> list[dict]:
    """Members sorted best-first."""
    dated = [_ts(m["posted_at"]) for m in members if m["posted_at"]]
    freshest = max(dated) if dated else None

    def key(m: dict):
        p = _ts(m["posted_at"])
        tier = 99 if (p is None or freshest is None) else int((freshest - p).total_seconds() // (bucket_hours * 3600))
        return (tier, portal_rank.get(m["source"], 50), -(p.timestamp() if p else 0), -(m["fit_score"] or 0))

    return sorted(members, key=key)


def _maybe_hold_for_referral(s: Settings, db: DB, app_id: int, job: dict, company: str) -> None:
    """High-pay company + someone who could refer you → wait N days before applying cold (some ATSs can't
    attach a referral to an application that already exists)."""
    rh = (s.cfg.get("outreach") or {}).get("referral_hold") or {}
    if not ((s.cfg.get("outreach") or {}).get("enabled") and rh.get("enabled")):
        return
    if (job.get("salary_lpa_max") or 0) < rh.get("min_band_lpa", 60):
        return
    n = db.conn.execute("SELECT COUNT(*) FROM contacts WHERE company_canon=? AND persona IN ('connection','engineer')",
                        (company,)).fetchone()[0]
    if n:
        until = (utcnow() + timedelta(days=rh.get("days", 7))).isoformat()
        db.update_app(app_id, hold_until=until, hold_reason=f"waiting up to {rh.get('days', 7)}d for a referral ({n} possible referrer(s))")
        db.event(app_id, job["key"], "referral_hold", {"until": until, "referrers": n})


def plan(s: Settings, db: DB) -> dict[str, Any]:
    d = s.cfg["dedup"]
    rank = s.cfg["portal_rank"]
    stats = defaultdict(int)
    # jobs re-triaged as rejected (tighter filters, `triage --all`) leave the queue
    for a in db.conn.execute("SELECT a.id, j.triage_reasons FROM applications a JOIN jobs j ON j.key = a.job_key "
                             "WHERE a.state = 'queued' AND j.triage_status = 'rejected'").fetchall():
        fails = [g["reason"] for g in json.loads(a["triage_reasons"] or "[]") if g["verdict"] == "fail"]
        db.transition(a["id"], "skipped", error=f"re-triaged as rejected: {'; '.join(fails)}"[:500])
        stats["dequeued_rejected"] += 1
    rows = [dict(r) for r in db.jobs("triage_status IN ('eligible','review')")]
    clusters = cluster_jobs(rows, d["title_similarity"])

    # persist cluster ids + winners (all members, so the report can show what each won against)
    per_company: dict[str, list[tuple[str, list[dict]]]] = defaultdict(list)
    for cid, members in clusters.items():
        ordered = pick_winner(members, rank, d["recency_bucket_hours"])
        for i, m in enumerate(ordered):
            db.set_triage(m["key"], cluster_id=cid, cluster_winner=int(i == 0))
        stats["clusters"] += 1
        stats["duplicates_collapsed"] += len(members) - 1
        per_company[ordered[0]["company_canon"]].append((cid, ordered))

    applied_clusters = {r["cluster_id"] for r in db.conn.execute(
        "SELECT cluster_id FROM applications WHERE state <> 'fill_failed'").fetchall()}
    tried_jobs = {r["job_key"] for r in db.conn.execute("SELECT job_key FROM applications").fetchall()}
    cooldown = s.cfg["exclusions"]["company_cooldown_days"]
    max_per = d["max_applications_per_company_per_run"]
    queued = []
    for company, cl in per_company.items():
        cl = [c for c in cl if c[0] not in applied_clusters]
        if not cl:
            continue
        recent = db.company_recently_applied(company, cooldown)
        if recent and not s.cfg["exclusions"].get("cooldown_allow_different_team"):
            stats["company_cooldown_skips"] += len(cl)
            for cid, ordered in cl:
                db.event(None, ordered[0]["key"], "skip_company_cooldown",
                         {"company": company, "last_applied": recent["submitted_at"], "last_title": recent["title"]})
            continue
        if db.company_has_open_app(company):
            stats["company_open_app_skips"] += len(cl)
            continue
        # best clusters for this company: eligible before review, then fit, then recency
        cl.sort(key=lambda c: (c[1][0]["triage_status"] != "eligible", -(c[1][0]["fit_score"] or 0),
                               -(_ts(c[1][0]["posted_at"]).timestamp() if c[1][0]["posted_at"] else 0)))
        for cid, ordered in cl[:max_per]:
            # runner-up takes over when the winner already failed mechanically (e.g. Easy Apply broken)
            remaining = [m for m in ordered if m["key"] not in tried_jobs]
            if not remaining:
                continue
            w = remaining[0]
            app_id = db.enqueue(w["key"], cid, company, w["resume_variant"])
            if app_id is None:
                continue
            _maybe_hold_for_referral(s, db, app_id, w, company)
            if w["triage_status"] == "review":
                reasons = [f"{g['gate']}: {g['reason']}" for g in json.loads(w["triage_reasons"]) if g["verdict"] == "unknown"]
                db.update_app(app_id, review_reasons=reasons)
            db.event(app_id, w["key"], "cluster_winner", {
                "cluster": cid, "beat": [{"source": m["source"], "posted_at": m["posted_at"], "url": m["url"]} for m in ordered if m is not w]})
            queued.append(app_id)
        stats["company_extra_clusters_deferred"] += max(0, len(cl) - max_per)
    stats["queued"] = len(queued)
    return dict(stats)
