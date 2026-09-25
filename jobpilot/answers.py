"""Decide a value for every form field, with provenance.

Order: review override → answer bank (answers.yaml → profile.yaml) → Codex (profile/resume only)
→ nothing. Optional fields the bank can't answer are left blank rather than sent to an LLM.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from .appliers.form import Field, Planned, choose_option, from_base, label_unit, unit_kind
from .config import FILL_ME, Settings
from .llm.codex import ANSWER_SCHEMA, Codex, answer_prompt

log = logging.getLogger(__name__)


def _norm_label(f: Field) -> str:
    return re.sub(r"\s+", " ", f"{f.label} {f.placeholder}".replace("*", " ")).strip().lower()


class Answerer:
    def __init__(self, s: Settings, codex: Codex, resume_path: Path, resume_text: str, job: dict[str, Any],
                 overrides: dict[str, Any] | None = None, referrer: str | None = None):
        self.s, self.codex, self.resume_path, self.resume_text, self.job = s, codex, resume_path, resume_text, job
        self.overrides = overrides or {}
        self.referrer = referrer or ""
        self.rules = [(re.compile(r["match"], re.I), r) for r in s.answers]

    def bank(self, f: Field) -> Optional[tuple[Any, str]]:
        label = _norm_label(f)
        hay = f"{label} {f.name.lower()}"
        if f.type == "file":
            if re.search(r"cover", hay):
                return "", "cover letter (skipped)"
            return str(self.resume_path), "resume upload"
        for rx, rule in self.rules:
            # match on the label first; fall back to the field name for label-less inputs
            if rx.search(label) or (not label.strip() and rx.search(f.name.lower())):
                if rule.get("kind") == "file" and f.type != "file":
                    continue
                if f.type in ("radio", "checkbox", "checkbox_group") and rule.get("kind") not in ("yesno", "select"):
                    continue  # e.g. "Can we email you about jobs?" must not get the email address
                try:
                    v = rule["value"]
                    if v == "{referrer}":
                        return self.referrer, rule["match"]
                    if v == "{referred_yesno}":
                        return ("Yes" if self.referrer else "No"), rule["match"]
                    if self.referrer and isinstance(v, list) and re.search(r"hear|source", rule["match"]):
                        v = ["Employee Referral", "Referral", "Referred by an employee"] + v
                    if isinstance(v, list):   # preference list: first that matches the field's options wins
                        v = [self.s.resolve_template(x, str(self.resume_path)) for x in v]
                        if f.type in ("select", "radio") and f.options:
                            for cand in v:
                                if choose_option(cand, f.options, unit_kind(f.label)):
                                    return cand, rule["match"]
                        return (v if f.type in ("listbox", "combobox") else v[0]), rule["match"]
                    return self.s.resolve_template(v, str(self.resume_path)), rule["match"]
                except KeyError as e:
                    log.warning("answers.yaml rule %s: %s", rule["match"], e)
                    return None
        return None

    def plan(self, f: Field) -> Planned:
        if f.key in self.overrides:
            return Planned(f, self.overrides[f.key], "override", rule="review override")
        hit = self.bank(f)
        if hit is not None:
            value, rule = hit
            kind = unit_kind(f.label)
            if kind and f.type in ("text", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
                unit = label_unit(f.label, kind)      # "Notice period (in months)" -> 2, "CTC (INR)" -> 4000000
                conv = from_base(float(value), kind, unit)
                value = int(conv) if float(conv).is_integer() else round(conv, 2)
            p = Planned(f, value, "bank" if rule != "resume upload" else "profile", rule=rule)
            if f.type in ("select", "radio") and value not in ("", None):   # listbox options are only known on click
                p.chosen_option = choose_option(value, f.options, kind)
                if p.chosen_option is None and f.options:
                    # bank had a value but no option represents it → let Codex map it onto the options
                    return self._llm(f, hint=str(value)) or p
            if p.value in ("", None) and f.required and f.type != "file":
                return self._llm(f) or p
            return p
        if not f.required and f.type not in ("select", "radio", "listbox"):
            return Planned(f, "", "none", rule="optional & unknown: left blank")
        return self._llm(f) or Planned(f, "", "none", rule="no answer source")

    def _llm(self, f: Field, hint: str = "") -> Optional[Planned]:
        q = f.label + (f" (candidate's value: {hint})" if hint else "")
        out = self.codex.run(answer_prompt(q, f.type, f.options, self.s.profile, self.resume_text, self.job), ANSWER_SCHEMA)
        if not out:
            return None
        if out["provenance"] == "cannot_answer":
            return Planned(f, "", "none", rule="codex: cannot answer", evidence=out.get("evidence", ""))
        p = Planned(f, out["answer"], f"llm:{out['provenance']}", rule="codex", confidence=float(out.get("confidence", 0)),
                    evidence=out.get("evidence", ""))
        if f.options:
            p.chosen_option = choose_option(out["answer"], f.options, unit_kind(f.label))
        return p


def contains_placeholder(v: Any) -> bool:
    return isinstance(v, str) and (FILL_ME in v or re.search(r"\{[\w.]+\}|lorem ipsum|\bTODO\b|\bTBD\b", v, re.I) is not None)
