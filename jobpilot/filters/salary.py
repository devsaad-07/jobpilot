"""Salary parsing into lakhs-per-annum (LPA)."""
from __future__ import annotations

import re
from typing import Optional

_LAKH = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:l|lpa|lakhs?|lacs?|lac)?\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*"
    r"(\d{1,3}(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?|l\b|lac)", re.I)
_LAKH_SINGLE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?|lac)\b", re.I)
_CRORE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:cr|crore)s?\b", re.I)
_INR_ABS = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{6,})(?:\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*([\d,]{6,}))?", re.I)
_USD = re.compile(r"(?:\$|usd\s*)(\d{2,3}(?:\.\d+)?\s*k\b|\d{2,3}(?:,\d{3})+|\d{2,3})\s*(?:-|–|to)?\s*(?:\$|usd\s*)?"
                  r"(\d{2,3}(?:\.\d+)?\s*k\b|\d{2,3}(?:,\d{3})+)?", re.I)


def _usd_val(s: str) -> float:
    s = s.lower().replace(",", "").strip()
    return float(s[:-1].strip()) * 1000 if s.endswith("k") else float(s)


def parse_salary(text: str, usd_to_inr: float = 84.0) -> tuple[Optional[float], Optional[float], str]:
    """-> (min_lpa, max_lpa, evidence). Monthly/hourly USD figures are ignored (too ambiguous)."""
    if not text:
        return None, None, ""
    t = text.replace(" ", " ")
    if m := _LAKH.search(t):
        a, b = float(m.group(1)), float(m.group(2))
        return min(a, b), max(a, b), m.group(0)
    if m := _CRORE.search(t):
        v = float(m.group(1)) * 100
        return v, v, m.group(0)
    if m := _INR_ABS.search(t):
        a = int(m.group(1).replace(",", "")) / 1e5
        b = int(m.group(2).replace(",", "")) / 1e5 if m.group(2) else a
        if 3 <= a <= 1000:
            return min(a, b), max(a, b), m.group(0)
    if m := _LAKH_SINGLE.search(t):
        v = float(m.group(1))
        return v, v, m.group(0)
    for m in _USD.finditer(t):
        ctx = t[max(0, m.start() - 40): m.end() + 40].lower()
        if re.search(r"per (hour|hr|month)|/h(ou)?r|/mo|hourly|monthly", ctx):
            continue
        a = _usd_val(m.group(1))
        b = _usd_val(m.group(2)) if m.group(2) else a
        if a < 20000:        # "$50" etc. isn't a salary
            continue
        return a * usd_to_inr / 1e5, b * usd_to_inr / 1e5, m.group(0)
    return None, None, ""


def salary_verdict(lo: Optional[float], hi: Optional[float], min_lpa: float) -> tuple[str, str]:
    if lo is None and hi is None:
        return "unknown", "no salary stated"
    top = hi if hi is not None else lo
    if top is not None and top < min_lpa:
        return "fail", f"salary tops at {top:.0f} LPA (< {min_lpa:.0f})"
    return "pass", f"salary {lo:.0f}-{(hi or lo):.0f} LPA"
