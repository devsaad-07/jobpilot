"""Apply one queued application end to end:

  open → (login/captcha check) → start application → fill steps → read back → verify →
  proof → policy → [submit → detect confirmation → proof] → seal manifest

Routes:
  ats      Greenhouse / Lever / Ashby / Workable / other career pages (generic form engine)
  linkedin Easy Apply modal (multi-step), or "Apply" → external site → ats route
  naukri   Apply → chatbot questionnaire, or "Apply on company site" → ats route
  oneclick Instahyre / Cutshort / Wellfound / Hirist: Apply button (+ optional dialog form)

Clicking the final submit — or a one-click "Apply" — only happens when policy allows.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable, Optional

from ..answers import Answerer, contains_placeholder
from ..config import Settings
from ..db import DB
from ..llm.codex import Codex
from ..normalize import ats_from_url, canon_url
from ..policy import auto_ok, gate_now
from ..proof import copy_resume, proof_dir, save_job, seal, snapshot, write_json
from ..triage import RESUMES
from ..verify import Verifier, page_errors
from .form import Field, Planned, extract_fields, fill_field, read_back

log = logging.getLogger(__name__)
ATS_SOURCES = {"greenhouse", "lever", "ashby", "workable", "company_site"}
ONECLICK = {"instahyre", "cutshort", "wellfound", "hirist", "weekday"}
AGGREGATORS = {"weworkremotely", "remoteok", "remotive", "himalayas"}   # link out to the company's own form
WORKDAY_KEEP = r"workExperience|education|certification|language|skills|websitePanel|socialNetwork"
CAPTCHA_JS = """() => [...document.querySelectorAll('iframe')].some(f => /recaptcha\\/api2\\/bframe|hcaptcha.com.*challenge|challenges.cloudflare/.test(f.src)
                 && f.getBoundingClientRect().height > 50) || /verify (that )?you are (a )?human|are you a robot/i.test(document.body.innerText)"""


class NeedsHuman(Exception):
    pass


def _texts_rx(texts: list[str]) -> re.Pattern:
    return re.compile(r"^\s*(" + "|".join(re.escape(t) for t in texts) + r")\s*$", re.I)


def find_button(frame, texts: list[str], scope: str | None = None):
    """First visible, enabled control whose text equals one of `texts`, trying texts in priority order
    (so 'Submit application' wins over a generic 'Apply' link), preferring the form we filled."""
    if not texts:
        return None
    base = frame.locator(scope) if scope else frame
    roots = [base.locator("form:has([data-jp-id])"), base]
    for t in texts:
        rx = _texts_rx([t])
        for root in roots:
            try:
                cand = root.locator("button, [role=button], input[type=submit], a").filter(has_text=rx)
                n = cand.count()
            except Exception:
                continue
            for i in range(min(n, 8)):
                el = cand.nth(i)
                try:
                    if el.is_visible() and el.is_enabled():
                        return el
                except Exception:
                    continue
            # aria-label / value fallback (LinkedIn uses aria-labels; <input type=submit value=...>)
            try:
                el = root.locator(f'button[aria-label="{t}" i], button[aria-label^="{t}" i], input[type=submit][value="{t}" i]')
                if el.count() and el.first.is_visible():
                    return el.first
            except Exception:
                continue
    return None


def best_frame(page):
    """Forms are often inside an ATS iframe on the company's own careers page."""
    best, n_best = page.main_frame, -1
    for fr in page.frames:
        try:
            n = fr.locator("input:not([type=hidden]), textarea, select").count()
        except Exception:
            continue
        if n > n_best:
            best, n_best = fr, n
    return best


class Engine:
    def __init__(self, s: Settings, db: DB, interactive: bool = False):
        self.s, self.db, self.interactive = s, db, interactive
        c = s.cfg["llm"]["codex"]
        self.codex = Codex(c.get("binary", "codex"), c.get("timeout_s", 180), c.get("extra_args"), c.get("enabled", True))
        self.portals = s.portals.get("portals", {})
        self.ats = s.portals.get("ats", {})

    # ------------------------------------------------------------------ entry
    def process(self, app_id: int, ctx_factory: Callable[[Path], Any]) -> str:
        app = self.db.app(app_id)
        job = dict(self.db.jobs("key=?", (app["job_key"],))[0])
        d: Optional[Path] = None
        try:
            variant = app["resume_variant"] or job["resume_variant"] or "general"
            resume = self.s.path("resumes_dir") / RESUMES.get(variant, RESUMES["general"])["file"]
            if not resume.exists():
                raise FileNotFoundError(f"resume missing: {resume}")
            resume_text = resume.with_suffix(".txt").read_text() if resume.with_suffix(".txt").exists() else ""
            d = Path(app["proof_dir"]) if app["proof_dir"] else proof_dir(self.s.path("proofs_dir"), app_id, job["company"], job["title"])
            # claim: queued/approved/verified/needs_review -> filling. Illegal (incl. filling->filling) if another
            # runner holds it, which raises before we touch the browser.
            self.db.transition(app_id, "filling", proof_dir=str(d), resume_sha256=copy_resume(d, resume))
        except ValueError as e:
            log.warning("app %s not claimable: %s", app_id, e)
            return "not_claimed"
        except Exception as e:
            self.db.update_app(app_id, error=f"setup: {type(e).__name__}: {e}"[:500])
            log.error("app %s setup failed: %s", app_id, e)
            return "setup_failed"
        result = "fill_failed"
        try:
            save_job(d, job)
            overrides = json.loads(app["overrides"] or "{}")
            answerer = Answerer(self.s, self.codex, resume, resume_text, job, overrides, referrer=app["referrer"])
            verifier = Verifier(self.s, self.codex, resume_text)
            route = self._route(job)
            log.info("app %s: %s — %s [%s, route=%s]", app_id, job["company"], job["title"], job["source"], route)
            with ctx_factory(d / "network.har") as ctx:
                page = ctx.new_page()
                try:
                    result = getattr(self, f"_route_{route}")(page, ctx, dict(self.db.app(app_id)), job, answerer, verifier, resume, d)
                except NeedsHuman:
                    try:
                        page.screenshot(path=str(d / "needs_human.png"), full_page=True)
                        (d / "needs_human.url").write_text(page.url)
                    except Exception:
                        pass
                    raise
        except NeedsHuman as e:
            result = self._fail_to(app_id, f"human needed: {e}", review=True)
        except BaseException as e:   # includes KeyboardInterrupt: never leave an app stuck in filling/submitting
            log.exception("app %s failed", app_id)
            result = self._fail_to(app_id, f"{type(e).__name__}: {e}")
            if not isinstance(e, Exception):
                raise
        finally:
            try:
                seal(d, {"app_id": app_id, "job_key": job["key"], "final_state": self.db.app(app_id)["state"]})
            except Exception as e:
                log.warning("seal failed for app %s: %s", app_id, e)
        return result

    def _fail_to(self, app_id: int, msg: str, review: bool = False) -> str:
        cur = self.db.app(app_id)["state"]
        if cur == "submitting":   # something went wrong after the submit click: a human must check the portal
            self.db.transition(app_id, "unconfirmed", error=f"after submit started: {msg}"[:500])
            return "unconfirmed"
        if cur in ("filling", "verified"):
            if review:
                self.db.transition(app_id, "needs_review", review_reasons=[msg])
                return "needs_review"
            self.db.transition(app_id, "fill_failed" if cur == "filling" else "queued", error=msg[:500])
            return "fill_failed" if cur == "filling" else "held"
        self.db.update_app(app_id, error=msg[:500])
        return cur

    def _route(self, job: dict) -> str:
        if job["source"] in ATS_SOURCES:
            return "ats"
        if job["source"] == "linkedin":
            return "linkedin"
        if job["source"] == "naukri":
            return "naukri"
        if job["source"] in ONECLICK:
            return "oneclick"
        if job["source"] == "workday":
            return "workday"
        if job["source"] in AGGREGATORS:
            return "aggregator"
        return "ats"  # anything else with a direct company form

    # ------------------------------------------------------------------ shared form flow
    def _fill_steps(self, page, frame, scope: str | None, answerer: Answerer, d: Path,
                    next_texts: list[str], submit_texts: list[str], max_steps: int = 10,
                    keep_sections: str | None = None, next_selector: str | None = None,
                    before_step: Callable[[], None] | None = None) -> tuple[list[Planned], Any, list[str]]:
        planned_by_key: dict[str, Planned] = {}
        errors: list[str] = []
        for step in range(1, max_steps + 1):
            frame.wait_for_timeout(700)
            if before_step:
                before_step()
            fields = [f for f in extract_fields(frame, scope) if f.type != "search"]
            planned = []
            for f in fields:
                if keep_sections and f.section and re.search(keep_sections, f.section):
                    # portal-prefilled repeatable section (e.g. Workday parsed work history): keep, verify, review
                    planned.append(Planned(f, f.current, "portal_prefill", rule=f"kept {f.section}"))
                else:
                    planned.append(answerer.plan(f))
            for p in planned:
                if p.value not in ("", None, []) and not contains_placeholder(p.value):  # never type FILL_ME into a form
                    fill_field(frame, p)
                    frame.wait_for_timeout(120)
            frame.wait_for_timeout(600)
            read_back(frame, planned)
            for p in planned:
                planned_by_key[f"{step}:{p.field.key}"] = p
            snapshot(page, d, f"step-{step:02d}")
            errs = page_errors(frame, scope)
            submit = find_button(frame, submit_texts, scope)
            nxt = find_button(frame, next_texts, scope) if next_texts else None
            if nxt is None and next_selector and submit is None:
                cand = frame.locator(next_selector)
                nxt = cand.first if cand.count() and cand.first.is_visible() else None
            if submit is not None and (nxt is None or submit == nxt):
                return list(planned_by_key.values()), submit, errs
            if nxt is None:
                errors = errs
                break
            if errs:  # don't advance past a step the portal says is invalid
                return list(planned_by_key.values()), None, errs
            nxt.click()
            page.wait_for_timeout(1200)
        return list(planned_by_key.values()), None, errors

    def _verify_and_decide(self, page, frame, scope, app, job, planned, errs, verifier, resume, d, submit_btn) -> str:
        report = verifier.check(planned, errs, resume)
        if submit_btn is None and report["status"] != "block":
            report["blocks"].append("no submit button found after filling")
            report["status"] = "block"
        snapshot(page, d, "pre_submit")
        pre = d / "pre_submit.png"
        if report["status"] != "block":
            report = verifier.judge(report, job, pre if pre.exists() else None)
        write_json(d, "fields.json", report["fields"])
        write_json(d, "verification.json", {k: v for k, v in report.items() if k != "fields"})
        answers = {f["key"]: {"label": f["field"]["label"], "value": f["actual"], "provenance": f["provenance"]}
                   for f in report["fields"]}
        return self._decide_and_submit(page, frame, app, job, report, answers, d, lambda: submit_btn.click())

    def _decide_and_submit(self, page, frame, app, job, report, answers, d, click_submit: Callable[[], None],
                           success_patterns: list[str] | None = None) -> str:
        app_id = app["id"]
        if report["status"] == "block":
            self.db.transition(app_id, "needs_review", verification=report_summary(report), answers=answers,
                               review_reasons=["verification blocked: " + b for b in report["blocks"][:12]])
            return "needs_review"
        self.db.transition(app_id, "verified", verification=report_summary(report), answers=answers)
        ok_auto, why = auto_ok(self.s, self.db, app, job, report)
        can_now, why_not = gate_now(self.s, self.db, job["source"])
        write_json(d, "decision.json", {"approved": bool(app["approved"]), "auto_ok": ok_auto, "auto_blockers": why,
                                        "gate_now": can_now, "gate_reason": why_not, "mode": self.s.mode})
        new_llm = [f["field"]["label"] for f in report["fields"] if str(f["provenance"]).startswith("llm:")]
        if app["approved"] and new_llm:
            # approval froze the reviewed answers as overrides; anything LLM-written now wasn't reviewed
            self.db.transition(app_id, "needs_review", review_reasons=[f"LLM answer(s) not covered by approval: {new_llm}"])
            return "needs_review"
        if not (app["approved"] or ok_auto):
            self.db.transition(app_id, "needs_review", review_reasons=why)
            return "needs_review"
        if not can_now:
            # shadow mode keeps the would-submit verdict visible; other gates return it to the queue
            if self.s.mode == "shadow":
                return "verified"
            self.db.transition(app_id, "queued", error=why_not)
            return "held"
        # ---------------- submit (never retried automatically) ----------------
        self.db.transition(app_id, "submitting", submitted_at=_now())
        click_submit()
        conf = self._await_confirmation(page, success_patterns or self._success_patterns(job, page.url))
        snapshot(page, d, "post_submit")
        if conf:
            self.db.transition(app_id, "submitted", confirmation=f"page_text: {conf}"[:300])
            return "submitted"
        if self.interactive and page.evaluate(CAPTCHA_JS):
            input(f"\n[app {app_id}] CAPTCHA/verification shown. Solve it in the browser, then press Enter... ")
            conf = self._await_confirmation(page, success_patterns or self._success_patterns(job, page.url))
            snapshot(page, d, "post_submit")
            if conf:
                self.db.transition(app_id, "submitted", confirmation=f"page_text after human captcha: {conf}"[:300])
                return "submitted"
        self.db.transition(app_id, "unconfirmed", error="no confirmation text after submit; check portal / email")
        return "unconfirmed"

    def _success_patterns(self, job: dict, url: str) -> list[str]:
        ats = ats_from_url(url) or job["source"]
        pats = (self.ats.get(ats) or self.portals.get(ats) or {}).get("success_patterns") or []
        return pats + self.ats["generic"]["success_patterns"]

    def _await_confirmation(self, page, patterns: list[str], timeout_s: float = 25) -> Optional[str]:
        rx = re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                page.wait_for_timeout(1000)
                texts = [fr.locator("body").inner_text(timeout=2000) for fr in page.frames if fr.url and not fr.url.startswith("about:")]
            except Exception:
                continue
            for t in texts:
                if m := rx.search(t):
                    return m.group(0)
            if re.search(r"thank|confirm|success|submitted", page.url, re.I):
                return f"url:{page.url}"
        return None

    def _check_captcha_or_login(self, page, portal: str | None) -> None:
        if page.evaluate(CAPTCHA_JS):
            if self.interactive:
                input("CAPTCHA before the form. Solve it in the browser, then press Enter... ")
            else:
                raise NeedsHuman("captcha shown before form (run `jobpilot apply --id N --interactive`)")
        if portal and self.portals.get(portal, {}).get("logged_in_check"):
            from ..portal_auth import login_state
            logged, why = login_state(page, self.portals[portal])
            if not logged:
                raise NeedsHuman(f"not logged in to {portal}: {why} (run `jobpilot login {portal}`)")

    # ------------------------------------------------------------------ routes
    def _route_ats(self, page, ctx, app, job, answerer, verifier, resume, d, url: str | None = None) -> str:
        url = url or job["apply_url"] or job["url"]
        ats = ats_from_url(url) or job["source"]
        cfg = self.ats.get(ats) or self.ats["generic"]
        if cfg.get("url_suffix") and not re.search(r"/(apply|application)/?$", url):
            url = url.rstrip("/") + cfg["url_suffix"]
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        (d / "jd.html").write_text(page.content())
        self._check_captcha_or_login(page, None)
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", page.url).split("/")[0].lower())
        if "myworkdayjobs.com" in host:
            return self._route_workday(page, ctx, app, job, answerer, verifier, resume, d, loaded=True)
        if re.search(r"linkedin\.com|naukri\.com|instahyre\.com|cutshort\.io|wellfound\.com|hirist\.tech|weekday\.works|indeed\.", host):
            # on a logged-in portal an "Apply" click can itself be the submission → never from the ATS route
            raise NeedsHuman(f"external apply landed on portal {host}; apply there manually")
        frame = best_frame(page)
        if frame.locator("input[type=email], input[type=file]").count() == 0:
            btn = find_button(frame, cfg.get("apply_texts") or self.ats["generic"]["apply_texts"])
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
                frame = best_frame(page)
        planned, submit, errs = self._fill_steps(page, frame, None, answerer, d, ["Next", "Continue"],
                                                 cfg.get("submit_texts") or self.ats["generic"]["submit_texts"])
        return self._verify_and_decide(page, frame, None, app, job, planned, errs, verifier, resume, d, submit)

    def _route_linkedin(self, page, ctx, app, job, answerer, verifier, resume, d) -> str:
        pc = self.portals["linkedin"]
        page.goto(job["url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._check_captcha_or_login(page, "linkedin")
        (d / "jd.html").write_text(page.content())
        if (self.s.cfg.get("outreach") or {}).get("enabled"):
            from ..outreach import capture_hiring_team
            capture_hiring_team(page, self.db, self.s, job)   # read-only: the page is already open
        easy = find_button(page, pc["apply_texts"])
        if easy is None:
            ext = find_button(page, pc["external_texts"])
            if ext is None:
                raise NeedsHuman("no Apply button (closed posting or already applied?)")
            return self._external(page, ctx, app, job, answerer, verifier, resume, d, ext)
        easy.click()
        page.wait_for_timeout(1500)
        planned, submit, errs = self._fill_steps(page, page.main_frame, pc["modal"], answerer, d, pc["next_texts"], pc["submit_texts"])
        # untick "follow company" so applying doesn't spam your feed
        try:
            cb = page.locator(pc["unfollow_company_checkbox"])
            if cb.count() and page.locator("#follow-company-checkbox").is_checked():
                cb.first.click()
        except Exception:
            pass
        return self._verify_and_decide(page, page.main_frame, pc["modal"], app, job, planned, errs, verifier, resume, d, submit)

    def _late_dedup(self, app, job, ext_url: str) -> Optional[str]:
        """The company form may be a role we already applied to via its ATS board or another aggregator."""
        cu = canon_url(ext_url)
        self.db.conn.execute("UPDATE jobs SET apply_url=?, apply_url_canon=? WHERE key=?", (ext_url, cu, job["key"]))
        dup = self.db.conn.execute(
            "SELECT a.id, a.state FROM applications a JOIN jobs j ON j.key=a.job_key WHERE a.id<>? AND "
            "(j.url_canon=? OR j.apply_url_canon=?) AND a.state NOT IN ('fill_failed')", (app["id"], cu, cu)).fetchone()
        if dup:
            self.db.transition(app["id"], "needs_review", review_reasons=[f"duplicate of application #{dup['id']} ({dup['state']}) via {ext_url}"])
            return "needs_review"
        return None

    def _external(self, page, ctx, app, job, answerer, verifier, resume, d, btn) -> str:
        try:
            with ctx.expect_page(timeout=8000) as newp:
                btn.click()
            target = newp.value
            target.wait_for_load_state("domcontentloaded")
        except Exception:
            target = page
            page.wait_for_timeout(3000)
        ext_url = target.url
        if (dup := self._late_dedup(app, job, ext_url)) is not None:
            return dup
        if target is not page:
            page.close()
        return self._route_ats(target, ctx, app, job, answerer, verifier, resume, d, url=ext_url)

    def _route_naukri(self, page, ctx, app, job, answerer, verifier, resume, d) -> str:
        pc = self.portals["naukri"]
        page.goto(job["url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._check_captcha_or_login(page, "naukri")
        (d / "jd.html").write_text(page.content())
        snapshot(page, d, "pre_submit")
        ext = find_button(page, pc["external_texts"])
        if ext is not None:
            return self._external(page, ctx, app, job, answerer, verifier, resume, d, ext)
        btn = find_button(page, pc["apply_texts"])
        if btn is None:
            raise NeedsHuman("no Apply button (already applied or closed)")
        # Naukri's Apply sends your Naukri profile immediately → policy decides before the click.
        report = {"status": "pass", "blocks": [], "warns": ["Naukri sends your stored Naukri profile + resume"],
                  "review": ["one-click apply with Naukri profile"], "fields": []}
        return self._decide_and_submit(page, page.main_frame, app, job, report, {}, d,
                                       lambda: self._naukri_click_and_chat(page, btn, pc, answerer, verifier, d),
                                       pc["success_patterns"])

    def _naukri_click_and_chat(self, page, btn, pc, answerer: Answerer, verifier: Verifier, d: Path) -> None:
        btn.click()
        transcript = []
        for _ in range(15):
            page.wait_for_timeout(1800)
            drawer = page.locator(pc["modal"])
            if drawer.count() == 0:
                break
            q = drawer.locator(".botMsg, [class*='botMsg']").last
            question = q.inner_text().strip() if q.count() else ""
            chips = drawer.locator(".chatbot_Chip, [class*='chip'], .ssrc__radio-btn-container label")
            options = [c.strip() for c in chips.all_inner_texts()] if chips.count() else []
            f = Field(id="chat", type="radio" if options else "text", label=question, options=options, required=True)
            p = answerer.plan(f)
            from .form import choose_option, unit_kind
            chosen = p.chosen_option or (choose_option(p.value, options, unit_kind(question)) if options else None)
            val = chosen or p.value
            one = verifier.check([_chat_planned(p, val)], [], Path("naukri"))
            one["blocks"] = [b for b in one["blocks"] if "resume attachment" not in b]
            transcript.append({"q": question, "a": val, "provenance": p.provenance, "blocks": one["blocks"]})
            write_json(d, "naukri_chat.json", transcript)
            if one["blocks"] or val in ("", None) or p.provenance.startswith("llm:") or p.provenance == "none":
                raise NeedsHuman(f"Naukri chatbot question needs you: {question!r}")
            if options:
                chips.nth(options.index(chosen)).click()
            else:
                drawer.locator(pc["chat_input"]).first.fill(str(val))
                page.locator(pc["chat_send"]).first.click()
        write_json(d, "naukri_chat.json", transcript)

    def _route_oneclick(self, page, ctx, app, job, answerer, verifier, resume, d) -> str:
        portal = job["source"]
        pc = self.portals[portal]
        page.goto(job["url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._check_captcha_or_login(page, portal)
        (d / "jd.html").write_text(page.content())
        ext = find_button(page, pc.get("external_texts") or [])
        if ext is not None:
            return self._external(page, ctx, app, job, answerer, verifier, resume, d, ext)
        btn = find_button(page, pc["apply_texts"])
        if btn is None:
            raise NeedsHuman("no Apply button (already applied or closed)")
        snapshot(page, d, "pre_submit")
        report = {"status": "pass", "blocks": [], "warns": [f"{portal} sends your stored {portal} profile"],
                  "review": [f"one-click apply on {portal}"], "fields": []}

        def click_then_dialog():
            btn.click()
            page.wait_for_timeout(2000)
            if page.locator(pc["modal"]).count():
                planned, submit, errs = self._fill_steps(page, page.main_frame, pc["modal"], answerer, d,
                                                         pc.get("next_texts", []), pc["submit_texts"])
                rep = verifier.check(planned, errs, resume)
                write_json(d, "dialog_verification.json", {k: v for k, v in rep.items() if k != "fields"})
                write_json(d, "fields.json", rep["fields"])
                if rep["status"] != "pass" or submit is None:
                    raise NeedsHuman(f"{portal} dialog needs you ({rep['status']}): {(rep['blocks'] or rep['review'])[:3]}")
                submit.click()

        return self._decide_and_submit(page, page.main_frame, app, job, report, {}, d, click_then_dialog, pc["success_patterns"])


    def _route_aggregator(self, page, ctx, app, job, answerer, verifier, resume, d) -> str:
        """We Work Remotely / Remote OK / Remotive / Himalayas: follow the listing's apply link to the company form."""
        if job["apply_url"] and ats_from_url(job["apply_url"]):
            # the feed already gave us the company's ATS URL (Himalayas often does): go straight there
            if (dup := self._late_dedup(app, job, job["apply_url"])) is not None:
                return dup
            return self._route_ats(page, ctx, app, job, answerer, verifier, resume, d, url=job["apply_url"])
        pc = self.portals[job["source"]]
        raw = json.loads(job["raw"] or "{}") if isinstance(job.get("raw"), str) else (job.get("raw") or {})
        link = raw.get("application_link") or ""
        if link and link.rstrip("/") != (job["url"] or "").rstrip("/"):
            # Himalayas' applicationLink is often a himalayas.app/.../apply URL that redirects to the company's page
            page.goto(link, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            host = urlsplit(page.url).netloc
            if job["source"] not in host.replace(".", ""):
                if (dup := self._late_dedup(app, job, page.url)) is not None:
                    return dup
                return self._route_ats(page, ctx, app, job, answerer, verifier, resume, d, url=page.url)
        page.goto(job["url"], wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        (d / "jd.html").write_text(page.content())
        self._check_captcha_or_login(page, None)
        ext = find_button(page, pc["external_texts"])
        if ext is None:
            body = page.locator("body").inner_text(timeout=5000)[:20000]
            if re.search(r"(no longer (accepting|available)|this job (has )?(expired|closed)|position (has been )?filled)", body, re.I):
                raise NeedsHuman(f"{job['source']} listing is closed/expired")
            mail = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", body)
            if mail and re.search(r"\b(send|email|mail)\b[^.]{0,80}\b(cv|resume|application)", body, re.I):
                raise NeedsHuman(f"{job['source']} listing applies by email: {mail.group(0)}")
            raise NeedsHuman(f"{job['source']} listing: no apply button found (see needs_human.png; add its text to "
                             f"portals.yaml {job['source']}.external_texts)")
        href = (ext.get_attribute("href") or "")
        if href.startswith("mailto:"):
            raise NeedsHuman(f"{job['source']} listing applies by email: {href[7:120]}")
        return self._external(page, ctx, app, job, answerer, verifier, resume, d, ext)

    def _workday_signin_gate(self, page, wc: dict) -> None:
        def visible(sel):
            loc = page.locator(sel)
            return loc.count() > 0 and loc.first.is_visible()
        if any(visible(m) for m in wc["signin_markers"]):
            host = re.sub(r"^https?://", "", page.url).split("/")[0]
            if not self.interactive:
                raise NeedsHuman(f"Workday sign-in / account needed on {host} "
                                 f"(run `jobpilot apply --id N --interactive`, sign in once; the session is kept)")
            input(f"\nSign in or create your candidate account on {host} in the browser, then press Enter... ")
            page.wait_for_timeout(2500)
            if any(visible(m) for m in wc["signin_markers"][:2]):
                raise NeedsHuman(f"still not signed in on {host}")

    def _route_workday(self, page, ctx, app, job, answerer, verifier, resume, d, loaded: bool = False) -> str:
        wc = self.portals["workday"]
        if not loaded:
            page.goto(job["apply_url"] or job["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        (d / "jd.html").write_text(page.content())
        self._check_captcha_or_login(page, None)
        try:   # Workday renders client-side; 3s is often not enough on a cold load
            page.locator(f'{wc["apply_selector"]}, [data-automation-id="legalNameSection_firstName"], '
                         f'[data-automation-id="pageFooterNextButton"], [data-automation-id="jobPostingHeader"]').first \
                .wait_for(state="visible", timeout=20000)
        except Exception:
            pass
        in_flow = page.locator('[data-automation-id="legalNameSection_firstName"]:visible, [data-automation-id="pageFooterNextButton"]:visible')
        if in_flow.count() == 0:
            btn = page.locator(wc["apply_selector"])
            btn = btn.first if btn.count() and btn.first.is_visible() else find_button(page, wc["apply_texts"])
            if btn is None:
                body = page.locator("body").inner_text(timeout=5000)
                if re.search(r"(no longer accepting applications|job (posting )?(is )?(closed|filled|no longer available)|"
                             r"page you are looking for doesn.t exist)", body, re.I):
                    raise NeedsHuman("Workday posting is closed")
                raise NeedsHuman("no Workday Apply button found (see needs_human.png)")
            btn.click()
            page.wait_for_timeout(2000)
            for sel, txt in zip(wc["start_selectors"], wc["start_texts"]):
                el = page.locator(sel)
                el = el.first if el.count() and el.first.is_visible() else find_button(page, [txt])
                if el is not None:
                    el.click()
                    page.wait_for_timeout(2500)
                    break
        self._workday_signin_gate(page, wc)
        planned, submit, errs = self._fill_steps(
            page, page.main_frame, None, answerer, d, wc["next_texts"], wc["submit_texts"], max_steps=12,
            keep_sections=WORKDAY_KEEP, next_selector=wc["next_selector"],
            before_step=lambda: self._workday_signin_gate(page, wc))
        if submit is not None and page.locator(wc["review_marker"]).filter(visible=True).count() == 0:
            # Workday's Submit shares its button id with Next; only trust it on the Review page
            errs = errs + ["Submit button seen outside the Review page"]
        kept = [p for p in planned if p.provenance == "portal_prefill" and p.actual not in ("", None)]
        result = self._verify_and_decide(page, page.main_frame, None, app, job, planned, errs, verifier, resume, d, submit)
        if kept:
            self.db.event(app["id"], job["key"], "workday_prefill", {"fields": len(kept)})
        return result


def _chat_planned(p: Planned, val: Any) -> Planned:
    p.actual = val
    if p.field.options:
        p.chosen_option = val
    return p


def report_summary(r: dict) -> dict:
    return {k: r.get(k) for k in ("status", "blocks", "warns", "review", "judge", "resume")}


def _now() -> str:
    from ..models import utcnow
    return utcnow().isoformat()
