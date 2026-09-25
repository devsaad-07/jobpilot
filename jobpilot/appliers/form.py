"""Portal-agnostic form engine: extract fields → decide answers → fill → read back.

Works on any page/frame (ATS forms, LinkedIn's Easy Apply modal, Instahyre dialogs) because it
reads the accessibility/label structure instead of portal-specific selectors.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

EXTRACT_JS = r"""
(scopeSel) => {
  const root = (scopeSel && document.querySelector(scopeSel)) || document;
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, ' *').trim();
  const visible = el => {
    const st = getComputedStyle(el); const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && (r.width > 0 || r.height > 0);
  };
  const byId = id => id && document.getElementById(id);
  const inHiddenTree = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const st = getComputedStyle(p); if (st.display === 'none' || st.visibility === 'hidden') return true; } return false; };
  const textOf = el => el ? clean(el.innerText || el.textContent) : '';
  const labelFor = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = textOf(l); }
    if (!t && el.getAttribute('aria-labelledby')) t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => textOf(byId(i))).join(' ');
    if (!t && el.getAttribute('aria-label')) t = el.getAttribute('aria-label');
    if (!t) { const l = el.closest('label'); if (l) t = textOf(l); }
    if (!t) {
      let p = el.parentElement;
      for (let i = 0; i < 5 && p && !t; i++) {
        const cand = [...p.querySelectorAll('label, legend, [class*="label"], [class*="question"], [class*="Label"], [class*="title"]')]
          .find(x => !x.contains(el) && textOf(x).length > 1 && textOf(x).length < 400);
        if (cand) t = textOf(cand);
        p = p.parentElement;
      }
    }
    return clean(t || el.placeholder || el.name || '');
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return textOf(lg); }
    const rg = el.closest('[role="radiogroup"], [role="group"]');
    if (rg) { const t = rg.getAttribute('aria-label') || textOf(byId(rg.getAttribute('aria-labelledby'))); if (t) return clean(t); }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++) {
      const cand = [...p.querySelectorAll('label, legend, [class*="label"], [class*="question"], [class*="title"], p, span')]
        .find(x => !x.querySelector('input') && !x.contains(el) && textOf(x).length > 3 && textOf(x).length < 400 &&
                   !(x.getAttribute('for') && document.getElementById(x.getAttribute('for'))?.type === el.type));
      if (cand && p.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1) return textOf(cand);
      p = p.parentElement;
    }
    return el.name || '';
  };
  let n = document.querySelectorAll('[data-jp-id]').length;
  const tag = el => { if (!el.getAttribute('data-jp-id')) el.setAttribute('data-jp-id', 'f' + (n++)); return el.getAttribute('data-jp-id'); };
  const req = (el, lab) => !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$|\*$/.test(lab) || /required/i.test(el.className));
  const out = []; const groups = {};
  const KEEP = '[data-automation-id*="workExperience"], [data-automation-id*="education"], [data-automation-id*="certification"], ' +
               '[data-automation-id*="language"], [data-automation-id*="skills"], [data-automation-id*="websitePanel"], [data-automation-id*="socialNetwork"]';
  const sectionOf = el => { const s = el.closest(KEEP); return s ? s.getAttribute('data-automation-id') : ''; };
  const els = root.querySelectorAll('input, textarea, select, [role="combobox"], [contenteditable="true"], button[aria-haspopup="listbox"]');
  for (const el of els) {
    const type = el.tagName === 'BUTTON' ? 'listbox'
               : (el.getAttribute('data-uxi-widget-type') === 'selectinput') ? 'combobox'
               : (el.getAttribute('role') === 'combobox' && el.tagName !== 'SELECT') ? 'combobox'
               : el.isContentEditable && el.tagName !== 'INPUT' ? 'textarea'
               : el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (inHiddenTree(el)) continue;   // field belongs to a step/section that isn't shown
    if (type !== 'file' && !visible(el) && !(type === 'radio' || type === 'checkbox')) continue;
    if ((type === 'radio' || type === 'checkbox') && !visible(el) && !visible(el.closest('label') || el.parentElement)) continue;
    if (el.disabled || el.readOnly && type !== 'combobox') continue;
    if ((type === 'radio' || type === 'checkbox') && el.name) {
      const sameName = root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`);
      if (type === 'radio' || sameName.length > 1) {
        const gid = type + ':' + el.name;
        if (!groups[gid]) {
          const lab = groupLabel(el);
          groups[gid] = { id: tag(el), group: gid, type: type === 'radio' ? 'radio' : 'checkbox_group', name: el.name, label: lab,
                          required: req(el, lab) || [...sameName].some(x => x.required), options: [], option_ids: [], section: sectionOf(el) };
          out.push(groups[gid]);
        }
        let ol = labelFor(el); if (!ol || ol === groups[gid].label) ol = el.value;
        groups[gid].options.push(clean(ol)); groups[gid].option_ids.push(tag(el));
        continue;
      }
    }
    const lab = labelFor(el);
    const cur = type === 'listbox' ? clean(el.innerText) : (el.value || '');
    const f = { id: tag(el), type, name: el.name || el.id || el.getAttribute('data-automation-id') || '', label: lab, required: req(el, lab),
                placeholder: el.placeholder || '', accept: el.accept || '', maxlength: el.maxLength > 0 ? el.maxLength : null,
                section: sectionOf(el), current: /^select( one)?$|^choose/i.test(cur) ? '' : cur };
    if (type === 'select') f.options = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--|please select)/i.test(t));
    out.push(f);
  }
  return out;
}
"""

READBACK_JS = r"""
(fields) => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const res = {};
  for (const f of fields) {
    const el = document.querySelector(`[data-jp-id="${f.id}"]`);
    if (!el) { res[f.id] = null; continue; }
    if (f.type === 'radio') {
      const ids = f.option_ids || [];
      const i = ids.findIndex(id => document.querySelector(`[data-jp-id="${id}"]`)?.checked);
      res[f.id] = i >= 0 ? f.options[i] : '';
    } else if (f.type === 'checkbox_group') {
      res[f.id] = (f.option_ids || []).map((id, i) => document.querySelector(`[data-jp-id="${id}"]`)?.checked ? f.options[i] : null).filter(Boolean);
    } else if (f.type === 'checkbox') {
      res[f.id] = el.checked ? 'Yes' : 'No';
    } else if (f.type === 'select') {
      res[f.id] = el.selectedIndex >= 0 ? txt(el.options[el.selectedIndex].text) : '';
    } else if (f.type === 'file') {
      const names = [...(el.files || [])].map(x => x.name);
      if (!names.length) { // many ATS widgets clear the input and render the filename nearby
        let p = el.parentElement, t = '';
        for (let i = 0; i < 4 && p && !/\.(pdf|docx?)\b/i.test(t); i++) { t = p.innerText || ''; p = p.parentElement; }
        const m = t.match(/[\w\-. ]+\.(pdf|docx?)\b/i); if (m) names.push(m[0].trim());
      }
      res[f.id] = names.join(', ');
    } else if (f.type === 'listbox') {
      const t = txt(el.innerText);
      res[f.id] = /^select( one)?$|^choose/i.test(t) ? '' : t;
    } else if (f.type === 'combobox') {
      let p = el, t = el.value || '';
      for (let i = 0; i < 4 && p; i++) {
        const sv = p.querySelector && p.querySelector('[data-automation-id="selectedItem"], [class*="singleValue"], [class*="single-value"], [class*="selected"], [class*="chip"], [class*="multi-value"]');
        if (sv) { t = txt(sv.innerText); break; }
        p = p.parentElement;
      }
      res[f.id] = t;
    } else if (el.isContentEditable) {
      res[f.id] = txt(el.innerText);
    } else {
      res[f.id] = el.value;
    }
  }
  return res;
}
"""


@dataclass
class Field:
    id: str
    type: str
    label: str
    name: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    option_ids: list[str] = field(default_factory=list)
    placeholder: str = ""
    accept: str = ""
    maxlength: Optional[int] = None
    group: str = ""
    section: str = ""        # repeatable portal section (e.g. Workday workExperience-1) when inside one
    current: str = ""        # value already on the page before we touched it

    @property
    def key(self) -> str:
        """Stable identity across re-renders/re-fills (label + name), used for review overrides."""
        return re.sub(r"\W+", "_", f"{self.label}|{self.name}".lower()).strip("_")[:120]


@dataclass
class Planned:
    field: Field
    value: Any
    provenance: str          # profile | bank | resume | llm:profile | llm:resume | llm:inferred | override | none
    rule: str = ""
    confidence: float = 1.0
    evidence: str = ""
    chosen_option: Optional[str] = None
    actual: Any = None
    fill_error: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["field"] = {k: v for k, v in asdict(self.field).items() if k not in ("option_ids",)}
        d["key"] = self.field.key
        return d


def extract_fields(frame, scope: str | None = None) -> list[Field]:
    raw = frame.evaluate(EXTRACT_JS, scope)
    return [Field(**{k: v for k, v in r.items() if k in Field.__dataclass_fields__}) for r in raw]


# ---------------------------------------------------------------- option matching
_YES = {"yes", "y", "true", "i agree", "agree", "i accept", "accept", "i consent", "consent", "i acknowledge"}
_NO = {"no", "n", "false", "decline"}


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9+.]+", " ", str(s).lower()).strip()


def unit_kind(label: str) -> Optional[str]:
    """Which quantity a field asks about: days (notice), years (experience), lpa (compensation)."""
    l = (label or "").lower()
    if re.search(r"notice|how soon|join(ing)?|start date|availability to start", l):
        return "days"
    if re.search(r"ctc|salary|compensation|package|pay\b", l):
        return "lpa"
    if re.search(r"experience|years|yrs|yoe", l):
        return "years"
    return None


def label_unit(label: str, kind: Optional[str]) -> Optional[str]:
    """The unit the field wants its number in (from hints like '(in months)', '(INR)')."""
    l = (label or "").lower()
    if kind == "days":
        return "months" if "month" in l else ("weeks" if "week" in l else "days")
    if kind == "years":
        return "months" if "month" in l else "years"
    if kind == "lpa":
        if re.search(r"lakh|lac|lpa|\bl\b", l):
            return "lpa"
        if re.search(r"crore|\bcr\b", l):
            return "crore"
        if re.search(r"inr|rupee|₹|\brs\b|annum|absolute|per year", l):
            return "inr"
        return "lpa"
    return None


_TO_BASE = {"days": 1, "weeks": 7, "months": 30, "years": 1, "lpa": 1, "crore": 100, "inr": 1e-5}


def to_base(n: float, kind: Optional[str], unit: Optional[str]) -> float:
    if kind == "years" and unit == "months":
        return n / 12
    return n * _TO_BASE.get(unit or "", 1)


def from_base(n: float, kind: Optional[str], unit: Optional[str]) -> float:
    if kind == "years" and unit == "months":
        return n * 12
    return n / _TO_BASE.get(unit or "", 1)


def _opt_unit(text: str, kind: Optional[str]) -> Optional[str]:
    t = text.lower()
    if kind == "days":
        return "months" if "month" in t else ("weeks" if "week" in t else "days")
    if kind == "years":
        return "months" if "month" in t and "year" not in t else "years"
    if kind == "lpa":
        return "crore" if re.search(r"crore|\bcr\b", t) else ("inr" if re.search(r"\d{6,}", t.replace(",", "")) else "lpa")
    return None


def option_interval(text: str, kind: Optional[str]) -> Optional[tuple[float, float]]:
    """'3-5 years' -> (3,5); '5+ years' -> (5,inf); 'More than 3 months' (days) -> (90,inf);
    'Less than 15 days' -> (0,15); 'Immediate' (days) -> (0,0); '2 months' (days) -> (60,60)."""
    t = str(text).lower().replace(",", "").replace("–", "-").replace("—", "-")
    u = _opt_unit(t, kind)
    b = lambda x: to_base(float(x), kind, u)
    if kind == "days" and re.search(r"immediate|serving notice|(?<![\d.])0 days", t):
        return (0.0, 0.0) if "serving" not in t else (0.0, 30.0)
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", t):
        return (b(m.group(1)), b(m.group(2)))
    if m := re.search(r"(?:more than|above|over|greater than|>\s*)\s*(\d+(?:\.\d+)?)", t):
        return (b(m.group(1)) + 1e-9, float("inf"))
    if m := re.search(r"(\d+(?:\.\d+)?)\s*\+|(\d+(?:\.\d+)?)\s*(?:years?|months?|days?|lpa|lakhs?)?\s*(?:and above|or more|& above)", t):
        return (b(m.group(1) or m.group(2)), float("inf"))
    if m := re.search(r"(?:less than|under|below|upto|up to|<\s*)\s*(\d+(?:\.\d+)?)", t):
        return (0.0, b(m.group(1)))
    if m := re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?|months?|days?|weeks?|lpa|lakhs?|l)?\s*", t):
        return (b(m.group(1)), b(m.group(1)))
    return None


def choose_bucket(x: float, options: list[str], kind: Optional[str]) -> Optional[str]:
    """Most specific option whose interval contains x (x already in base units)."""
    best, best_key = None, None
    for o in options:
        iv = option_interval(o, kind)
        if not iv or not (iv[0] - 1e-9 <= x <= iv[1] + 1e-9):
            continue
        width = iv[1] - iv[0]
        # narrowest first; on ties (60 days in "30-60" and "60-90") notice prefers the bucket ending at x
        # ("can join within 60"), experience/pay prefer the one starting at x ("5-7 years", "60-80 LPA")
        key = (width, iv[0] if kind == "days" else -iv[0])
        if best_key is None or key < best_key:
            best, best_key = o, key
    return best


def choose_option(value: Any, options: list[str], kind: Optional[str] = None) -> Optional[str]:
    """Pick the option that represents `value`. Returns None if nothing matches unambiguously.
    `kind` (days|years|lpa) enables unit-aware bucket matching for numeric values."""
    if not options:
        return None
    v = _norm(value)
    norm = {o: _norm(o) for o in options}
    for o, n in norm.items():
        if n == v:
            return o
    if v in _YES or v in _NO:
        want = _YES if v in _YES else _NO
        hits = [o for o, n in norm.items() if n in want or (n.split(" ")[0] in {"yes", "no"} and n.split(" ")[0] in want)
                or (n.startswith("i agree") and v in _YES)]
        if hits:
            return hits[0]
    num = re.fullmatch(r"(\d+(?:\.\d+)?)", v)
    if num:
        pick = choose_bucket(float(num.group(1)), options, kind)
        if pick:
            return pick
    if "decline" in v or "not want" in v or "prefer not" in v:
        hits = [o for o, n in norm.items() if re.search(r"decline|prefer not|not (want|wish)|don.?t wish|rather not", n)]
        if hits:
            return hits[0]
    if num:
        return None     # never fuzzy-match numbers ("5" must not pick "15 days")
    starts = [o for o, n in norm.items() if n.startswith(v) or v.startswith(n) and len(n) > 2]
    if len(starts) == 1:
        return starts[0]
    contains = [o for o, n in norm.items() if v and (v in n or n in v) and len(n) > 2]
    if len(contains) == 1:
        return contains[0]
    return None


# ---------------------------------------------------------------- fill + readback
def fill_field(frame, p: Planned) -> None:
    f, v = p.field, p.value
    loc = frame.locator(f'[data-jp-id="{f.id}"]')
    try:
        if loc.count() == 0 and f.label:
            # React re-rendered the widget (e.g. after the phone-country picker changed) and dropped our tag:
            # find it again by its label rather than waiting 15s for an element that no longer exists.
            lab = re.sub(r"\s*\*\s*$", "", f.label).strip()
            alt = frame.get_by_label(re.compile(rf"^\s*{re.escape(lab)}\s*\*?\s*$", re.I))
            if alt.count() == 0:
                raise LookupError(f"field '{lab}' disappeared from the page (re-rendered)")
            loc = alt.first
            loc.evaluate("(e, id) => e.setAttribute('data-jp-id', id)", f.id, timeout=3000)
        if f.type in ("text", "email", "tel", "url", "number", "textarea", "password", "date"):
            if loc.evaluate("e => e.isContentEditable && e.tagName !== 'INPUT' && e.tagName !== 'TEXTAREA'"):
                loc.click()
                loc.evaluate("e => e.innerText = ''")
                loc.type(str(v), delay=15)
            else:
                loc.fill(str(v))
                loc.dispatch_event("blur")
        elif f.type == "select":
            opt = p.chosen_option or choose_option(v, f.options, unit_kind(f.label))
            if opt is None:
                raise ValueError(f"no option matches {v!r}")
            p.chosen_option = opt
            loc.select_option(label=opt)
        elif f.type == "radio":
            opt = p.chosen_option or choose_option(v, f.options, unit_kind(f.label))
            if opt is None:
                raise ValueError(f"no option matches {v!r}")
            p.chosen_option = opt
            oid = f.option_ids[f.options.index(opt)]
            _check(frame, oid)
        elif f.type == "checkbox_group":
            wanted = v if isinstance(v, list) else [v]
            chosen = [choose_option(w, f.options, unit_kind(f.label)) for w in wanted]
            if None in chosen:
                raise ValueError(f"no option matches one of {wanted!r}")
            p.chosen_option = ", ".join(chosen)  # type: ignore[arg-type]
            for c in chosen:
                _check(frame, f.option_ids[f.options.index(c)])
        elif f.type == "checkbox":
            want = _norm(v) in _YES
            if loc.is_checked() != want:
                _check(frame, f.id)
        elif f.type == "file":
            if v:
                loc.set_input_files(str(v))
        elif f.type == "listbox":
            loc.click()
            frame.wait_for_timeout(600)
            opts = frame.locator('[role="option"]:visible, [role="listbox"] li:visible')
            texts = [t.strip() for t in opts.all_inner_texts()]
            pick = p.chosen_option or next((c for c in (choose_option(x, texts, unit_kind(f.label))
                                                        for x in (v if isinstance(v, list) else [v])) if c), None)
            if pick is None or pick not in texts:
                frame.keyboard.press("Escape")
                raise ValueError(f"no listbox option matches {v!r} (options: {texts[:8]})")
            opts.nth(texts.index(pick)).click()
            p.chosen_option = pick
        elif f.type == "combobox":
            loc.click()
            if loc.evaluate("e => 'value' in e"):
                loc.fill("")
            first = v[0] if isinstance(v, list) else v
            loc.type(str(first), delay=25)
            frame.wait_for_timeout(700)
            opts = frame.locator('[role="option"]')
            texts = [t.strip() for t in opts.all_inner_texts()] if opts.count() else []
            pick = next((c for c in (choose_option(x, texts, unit_kind(f.label))
                                     for x in (v if isinstance(v, list) else [v])) if c), None) if texts else None
            if pick:
                opts.nth(texts.index(pick)).click()
                p.chosen_option = pick
            else:
                loc.press("Enter")
        else:
            raise ValueError(f"unsupported field type {f.type}")
    except Exception as e:  # recorded; verifier turns it into a block
        p.fill_error = f"{type(e).__name__}: {e}"[:300]


def _check(frame, jp_id: str) -> None:
    loc = frame.locator(f'[data-jp-id="{jp_id}"]')
    try:
        loc.check(timeout=2500)
    except Exception:
        # custom-styled inputs are often hidden; click their label instead
        frame.evaluate("""(id) => { const el = document.querySelector(`[data-jp-id="${id}"]`);
            const lab = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label') || el.parentElement;
            lab.click(); }""", jp_id)


def read_back(frame, planned: list[Planned]) -> None:
    payload = [{"id": p.field.id, "type": p.field.type, "options": p.field.options, "option_ids": p.field.option_ids}
               for p in planned]
    vals = frame.evaluate(READBACK_JS, payload)
    for p in planned:
        p.actual = vals.get(p.field.id)
