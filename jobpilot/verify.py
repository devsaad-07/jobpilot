"""Post-fill verification. Runs on what the PAGE reports (read-back DOM values), not on what we
intended to type, because React forms, masks, maxlengths and autocomplete silently change input.

Three layers, all must pass before a submit:
  1. mechanical   — every planned value is actually on the page; required fields non-empty;
                    no fill errors; the right resume file is attached; no visible validation errors
  2. invariants   — identity/contact/CTC/notice/experience fields equal profile.yaml exactly;
                    no placeholders; numbers in free text exist in the resume/profile
  3. semantic     — Codex judge over (label, read-back value, provenance) + screenshot (optional)

Outcome: pass | review (a human should look; nothing wrong detected) | block (must not submit).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .answers import contains_placeholder
from .appliers.form import Planned, _norm, label_unit, option_interval, to_base, unit_kind
from .config import Settings
from .llm.codex import JUDGE_SCHEMA, Codex, judge_prompt

PAGE_ERRORS_JS = r"""
(scopeSel) => {
  const root = (scopeSel && document.querySelector(scopeSel)) || document;
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
                      return st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const msgs = new Set();
  root.querySelectorAll('[aria-invalid="true"]').forEach(el => { if (vis(el)) msgs.add('invalid: ' + (el.name || el.id || el.getAttribute('data-jp-id'))); });
  root.querySelectorAll('[role="alert"], .error, .field-error, .error-message, [class*="errorMessage"], [class*="error-text"], .artdeco-inline-feedback--error')
      .forEach(el => { const t = (el.innerText || '').trim(); if (t && vis(el) && t.length < 200) msgs.add(t); });
  return [...msgs].slice(0, 20);
}
"""


def _digits(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _num(s: Any) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or "").replace(",", ""))
    return float(m.group(0)) if m else None


DIAL = {"india": ("+91", "in"), "united states": ("+1", "us"), "united kingdom": ("+44", "gb"), "singapore": ("+65", "sg"),
        "united arab emirates": ("+971", "ae")}


def _country_equiv(actual: Any, want: Any) -> bool:
    """Greenhouse/intl-tel phone-country pickers display '+91' (or 'IN +91') after 'India' is chosen."""
    a, w = _norm(actual), _norm(want)
    for name, (code, iso) in DIAL.items():
        if name in w and (a in (code, code.lstrip("+"), iso, f"{iso} {code}") or a.endswith(" " + code)):
            return True
    return False


def _eq_text(a: Any, b: Any) -> bool:
    return re.sub(r"\s+", " ", str(a or "")).strip().lower() == re.sub(r"\s+", " ", str(b or "")).strip().lower()


class Verifier:
    def __init__(self, s: Settings, codex: Codex, resume_text: str):
        self.s, self.codex, self.resume_text = s, codex, resume_text
        p = s.profile
        self.expect = [  # (label regex, expected value, comparison)
            (r"first name|given name", p["identity"]["first_name"], "text"),
            (r"last name|surname|family name", p["identity"]["last_name"], "text"),
            (r"^(full |legal )?name$|^your name|candidate name", p["identity"]["full_name"], "text"),
            (r"e-?mail(?! .*(opt|subscribe|updates))", p["identity"]["email"], "email"),
            (r"^(?!.*(device|type|code|extension)).*(phone|mobile|contact number)", p["identity"]["phone_national"], "phone"),
            (r"linkedin", p["identity"]["linkedin"], "url"),
            (r"current (ctc|salary|compensation|package)", p["work"]["current_ctc_lpa"], "lpa"),
            (r"expected (ctc|salary|compensation|package)|salary expectation|desired (salary|compensation)", p["work"]["expected_ctc_lpa"], "lpa"),
            (r"notice period", p["work"]["notice_period_days"], "days"),
            (r"^(total|overall) (work |professional |relevant )?(experience|exp)|^(how many )?years of (total |professional |work |industry |relevant )?experience( do you have)?\??( \(years\))?$|^experience( in years| \(years\))?$",
             p["work"]["total_experience_years"], "years"),
        ]
        self.known_numbers = set(re.findall(r"\d+(?:\.\d+)?", resume_text)) | set(re.findall(r"\d+(?:\.\d+)?", str(p)))

    # ---------------------------------------------------------------- layer 1 + 2
    def check(self, planned: list[Planned], page_errors: list[str], resume_path: Path) -> dict:
        blocks: list[str] = []
        warns: list[str] = []
        review: list[str] = []
        resume_seen = False
        for p in planned:
            f, lab = p.field, (p.field.label or p.field.name)
            actual = p.actual
            empty = actual in (None, "", []) or (f.type == "checkbox" and actual == "No" and f.required)
            if contains_placeholder(p.value) or contains_placeholder(actual):
                blocks.append(f"[{lab}] placeholder value would be sent ({p.value!r}) — fill profile.yaml")
                continue
            if p.fill_error:
                (blocks if f.required or p.value not in ("", None) else warns).append(f"[{lab}] fill error: {p.fill_error}")
                continue
            if f.required and empty:
                blocks.append(f"[{lab}] required but empty on page")
                continue
            if p.value in ("", None, []) and not f.required:
                continue
            ok = self._matches(p)
            if not ok:
                blocks.append(f"[{lab}] page shows {actual!r}, intended {p.chosen_option or p.value!r}")
            if f.type == "file" and p.value:
                if Path(str(p.value)).name.lower() in str(actual).lower() or Path(str(p.value)).stem.lower()[:20] in str(actual).lower():
                    resume_seen = True
                elif actual:
                    blocks.append(f"[{lab}] attached file {actual!r} is not the chosen resume {Path(str(p.value)).name!r}")
            # invariants
            text = f"{f.label} {f.placeholder}".lower().replace("*", "").strip()
            for rx, exp, kind in self.expect:
                if re.search(rx, text) and f.type not in ("file", "radio", "checkbox", "checkbox_group") and not f.section:
                    if not self._invariant(kind, actual, exp, f.label, f.type):
                        blocks.append(f"[{lab}] {kind} mismatch: page {actual!r} vs profile {exp!r}")
                    break
            if p.provenance.startswith("llm:"):
                msg = f"[{lab}] LLM answer ({p.provenance}, conf {p.confidence:.2f}): {str(actual)[:160]!r}"
                review.append(msg)
                if f.type in ("text", "textarea"):   # any LLM text: every number must exist in resume/profile
                    stray = [n for n in re.findall(r"\d+(?:\.\d+)?", str(actual)) if n not in self.known_numbers]
                    if stray:
                        blocks.append(f"[{lab}] free-text answer contains numbers not in resume/profile: {stray}")
            if p.provenance == "portal_prefill":
                review.append(f"[{lab}] kept portal autofill ({f.section}): {str(actual)[:120]!r}")
            if p.provenance == "none" and f.required:
                blocks.append(f"[{lab}] required field has no answer source")
        if any(p.field.type == "file" and p.value for p in planned) and not resume_seen:
            if not any("attached file" in b for b in blocks):
                blocks.append("resume attachment not confirmed on page")
        for e in page_errors:
            blocks.append(f"page validation: {e}")
        status = "block" if blocks else ("review" if review else "pass")
        return {"status": status, "blocks": blocks, "warns": warns, "review": review,
                "fields": [p.to_json() for p in planned], "resume": resume_path.name}

    def _matches(self, p: Planned) -> bool:
        f, a = p.field, p.actual
        if p.provenance == "portal_prefill":
            return _eq_text(a, p.value)
        if f.type in ("select", "radio", "listbox"):
            return _eq_text(a, p.chosen_option) or _country_equiv(a, p.chosen_option or p.value)
        if f.type == "checkbox_group":
            return sorted(_norm(x) for x in (a or [])) == sorted(_norm(x) for x in str(p.chosen_option).split(", "))
        if f.type == "checkbox":
            return _eq_text(a, "Yes" if _norm(p.value) in {"yes", "true", "i agree", "agree"} else "No")
        if f.type == "file":
            return bool(a)
        if f.type == "listbox" and isinstance(p.value, list) and not p.chosen_option:
            return False
        if f.type == "combobox":
            got, want = _norm(a), _norm(p.chosen_option or (p.value[0] if isinstance(p.value, list) else p.value))
            if _country_equiv(a, want) or _country_equiv(a, p.value):
                return True
            if len(got) < 3:
                return False
            return got == want or (p.chosen_option is None and want in got)
        if f.type == "tel":
            return _digits(a).endswith(_digits(p.value)[-10:])
        if f.type == "number":
            return _num(a) == _num(p.value)
        return _eq_text(a, p.value)

    @staticmethod
    def _invariant(kind: str, actual: Any, exp: Any, label: str = "", ftype: str = "text") -> bool:
        if kind in ("text", "email", "url"):
            return _eq_text(str(actual).rstrip("/").replace("https://", "").replace("www.", ""),
                            str(exp).rstrip("/").replace("https://", "").replace("www.", ""))
        if kind == "phone":
            return _digits(actual).endswith(_digits(exp)[-10:])
        e = _num(exp)
        if e is None:
            return False
        qk = {"lpa": "lpa", "days": "days", "years": "years"}[kind]
        if ftype in ("select", "radio", "combobox", "listbox"):      # bucketed option text, e.g. "3-5 years", "More than 3 months"
            iv = option_interval(str(actual or ""), qk)
            return iv is not None and iv[0] - 1e-9 <= e <= iv[1] + 1e-9
        n = _num(actual)
        if n is None:
            return False
        got = to_base(n, qk, label_unit(label, qk))      # the number as the label's unit means it
        tol = 0.01 if kind == "lpa" else (0.5 if kind == "years" else 0.0)
        return abs(got - e) <= tol + 1e-9

    # ---------------------------------------------------------------- layer 3
    def judge(self, report: dict, job: dict, screenshot: Optional[Path]) -> dict:
        fields = [{"label": f["field"]["label"], "value": f["actual"], "provenance": f["provenance"]}
                  for f in report["fields"] if f["actual"] not in (None, "", [])]
        out = self.codex.run(judge_prompt(fields, self.s.profile, job), JUDGE_SCHEMA, images=[screenshot] if screenshot else None)
        report["judge"] = out or {"skipped": True}
        if out:
            for i in out.get("issues", []):
                msg = f"[judge:{i['field']}] {i['problem']}"
                (report["blocks"] if i["severity"] == "block" else report["warns"]).append(msg)
            if report["blocks"]:
                report["status"] = "block"
        return report


def page_errors(frame, scope: str | None = None) -> list[str]:
    try:
        return frame.evaluate(PAGE_ERRORS_JS, scope)
    except Exception:
        return []
