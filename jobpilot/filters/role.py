from __future__ import annotations

import re


def title_verdict(title: str, allow: list[str], deny: list[str]) -> tuple[str, str]:
    t = " ".join((title or "").split())
    for d in deny:
        if m := re.search(d, t, re.I):
            return "fail", f"title denied by '{m.group(0)}'"
    for a in allow:
        if re.search(a, t, re.I):
            return "pass", "title matches target roles"
    return "fail", "title not in target role family"


STAFFING = re.compile(
    r"\b(our client|for (one of )?our (esteemed )?clients?|on behalf of (our|a) client|staffing|consultancy|"
    r"third[- ]party payroll|c2h|contract to hire|body ?shopping)\b", re.I)


def is_staffing(company: str, description: str) -> bool:
    return bool(STAFFING.search(description or "")) or bool(
        re.search(r"\b(consult(ants|ancy|ing)|staffing|manpower|recruit(ers|ment)|hr solutions|placements?)\b", company or "", re.I))
