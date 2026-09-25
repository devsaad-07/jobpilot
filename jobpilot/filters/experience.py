"""Extract the experience requirement from a JD.

Returns (min_years, max_years, evidence). Picks the requirement that reads like the *role's*
bar, not incidental mentions ("our founders have 20 years..."): phrases near
'experience' are scored, and the one with the lowest minimum among required-looking lines wins,
because screeners filter on the stated floor.
"""
from __future__ import annotations

import re
from typing import Optional

_NUM = r"(\d{1,2}(?:\.\d)?)"
_YRS = r"\s*(?:\+|plus)?\s*(?:years?|yrs?|yoe)\b"
PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM + _YRS, re.I), "range"),                 # 5-8 years, 5 to 8 yrs
    (re.compile(r"(?:minimum|min\.?|at least|atleast|over|more than)\s*(?:of\s*)?" + _NUM + _YRS, re.I), "min"),
    (re.compile(_NUM + r"\s*\+\s*(?:years?|yrs?)\b", re.I), "min"),                           # 5+ years
    (re.compile(_NUM + r"\s*(?:or more|and above)\s*(?:years?|yrs?)\b", re.I), "min"),
    (re.compile(_NUM + r"\s*(?:years?|yrs?)\s*(?:or more|and above|\+|minimum)", re.I), "min"),
    (re.compile(r"(?:up to|upto|maximum|max\.?)\s*" + _NUM + _YRS, re.I), "max"),
    (re.compile(_NUM + _YRS + r"\s*(?:of\s+)?(?:\w+\s+){0,6}?(?:experience|exp)\b", re.I), "exact"),
]
_REQ_CONTEXT = re.compile(r"experience|exp\b|background|track record|industry|professional|software|engineering|backend|"
                          r"development|building|programming|coding|hands[- ]on", re.I)
_NOISE_CONTEXT = re.compile(r"company|founded|since|our (team|founders)|history|in business|years old|anniversary|"
                            r"vesting|vest|contract (length|duration)|warranty|age", re.I)


def parse_experience(text: str) -> tuple[Optional[float], Optional[float], str]:
    if not text:
        return None, None, ""
    candidates: list[tuple[float, Optional[float], str, int]] = []
    for pat, kind in PATTERNS:
        for m in pat.finditer(text):
            s, e = max(0, m.start() - 90), min(len(text), m.end() + 90)
            ctx = text[s:e]
            if _NOISE_CONTEXT.search(ctx) and not re.search(r"experience", ctx, re.I):
                continue
            score = 2 if _REQ_CONTEXT.search(ctx) else 0
            if re.search(r"prefer|nice to have|bonus|plus\b|good to have", ctx, re.I):
                score -= 1
            g = [float(x) for x in m.groups() if x is not None]
            if kind == "range":
                lo, hi = sorted(g[:2])
                if hi - lo > 15:
                    continue
            elif kind == "max":
                lo, hi = 0.0, g[0]
            elif kind == "min":
                lo, hi = g[0], None
            else:
                lo, hi = g[0], None
            if lo > 25:
                continue
            candidates.append((lo, hi, m.group(0).strip(), score, m.span(), kind))
    # a range ("3 to 5 yrs") beats the single numbers overlapping it ("5 yrs experience")
    ranges = [c[4] for c in candidates if c[5] == "range"]
    candidates = [c for c in candidates if c[5] == "range" or not any(a < c[4][1] and c[4][0] < b for a, b in ranges)]
    if not candidates:
        return None, None, ""
    best_score = max(c[3] for c in candidates)
    strong = [c for c in candidates if c[3] == best_score]
    # Screeners filter on the stated floor. Among equally strong mentions, the HIGHEST floor is
    # the binding one ("3+ years Go; 7+ years backend" → 7): being conservative avoids applying
    # to roles that will auto-reject.
    lo, hi, ev = max(strong, key=lambda c: c[0])[:3]
    return lo, hi, ev


def experience_verdict(lo: Optional[float], hi: Optional[float], max_min_required: float, min_max_required: float) -> tuple[str, str]:
    """-> (pass|fail|unknown, reason)."""
    if lo is None and hi is None:
        return "unknown", "no experience requirement found"
    if lo is not None and lo > max_min_required:
        return "fail", f"requires min {lo:g} yrs (> {max_min_required:g})"
    if hi is not None and hi < min_max_required:
        return "fail", f"band tops out at {hi:g} yrs (< {min_max_required:g}); too junior"
    return "pass", f"requirement {lo if lo is not None else '?'}-{hi if hi is not None else '+'} yrs fits"
