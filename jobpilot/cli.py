"""jobpilot CLI.

  jobpilot doctor                 check config, profile, tools, logins
  jobpilot login [portal ...]     open the dedicated browser profile to log in once
  jobpilot probe-ats              resolve company ATS boards + check Workday sites (companies.yaml)
  jobpilot discover               ATS boards, Workday sites, We Work Remotely RSS, Remote OK/Remotive/Himalayas APIs, JobSpy (LinkedIn/Naukri),
                                  browser portals (Instahyre/Cutshort/Wellfound/Hirist/Weekday)
  jobpilot triage                 run fit gates on new/untriaged jobs
  jobpilot plan                   dedup (role + company), rank, queue
  jobpilot apply [--limit N] [--id N] [--interactive]
  jobpilot review                 human review queue
  jobpilot confirm-emails         match confirmation/outcome emails, attach .eml proofs
  jobpilot report                 write data/report.html
  jobpilot status
  jobpilot outreach import-connections Connections.csv | import-contacts apollo.csv | find | plan |
                    send | followups | mark ID STATE | sync | status
  jobpilot run                    discover → triage → plan → apply → confirm-emails → report (for launchd/cron)
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import load_settings
from .db import DB
from .filters.role import title_verdict
from .normalize import build_alias_map, canon_company, canon_title, canon_url

log = logging.getLogger("jobpilot")


def _db(s) -> DB:
    return DB(s.path("data_dir") / "jobpilot.db")


def cmd_discover(s, db, args) -> dict:
    """Each source is saved to the DB as soon as it finishes, so Ctrl+C mid-discovery keeps what was found."""
    from .sources import ats, jobspy_src
    alias = build_alias_map(s.companies)
    allow, deny = s.cfg["fit"]["title_allow"], s.cfg["fit"]["title_deny"]
    tot = {"fetched": 0, "title_matched": 0, "new": 0}

    def save(got) -> tuple[int, int]:
        kept = new = 0
        for j in got:
            if not j.title or not j.company:
                continue
            if title_verdict(j.title, allow, deny)[0] == "fail":
                continue  # cheap pre-filter keeps the DB to plausible roles
            kept += 1
            new += db.upsert_job(j, canon_company(j.company, alias), canon_title(j.title), canon_url(j.url), canon_url(j.apply_url))
        return kept, new

    def add(name, fn):
        t0 = time.time()
        log.info("discover: %s ...", name)
        try:
            got = fn()
        except Exception as e:          # one broken source never kills the run
            log.warning("discover: %s failed: %s", name, e)
            got = []
        kept, new = save(got)
        tot["fetched"] += len(got); tot["title_matched"] += kept; tot["new"] += new
        log.info("discover: %s -> %d fetched, %d title-matched, %d new (saved) in %.0fs",
                 name, len(got), kept, new, time.time() - t0)

    if s.get("discovery.ats.enabled"):
        add("ATS boards", lambda: ats.discover(s.companies, s.path("data_dir")))
    if s.get("discovery.workday.enabled"):
        from .sources import workday
        add("Workday", lambda: workday.discover(s.companies, s.cfg))
    if s.get("discovery.wwr.enabled"):
        from .sources import wwr
        add("We Work Remotely", lambda: wwr.discover(s.cfg))
    if s.get("discovery.remote_boards.enabled"):
        from .sources import remote_boards
        add("Remote OK/Remotive/Himalayas", lambda: remote_boards.discover(s.cfg, s.path("data_dir")))
    if s.get("discovery.jobspy.enabled") and not args.no_jobspy:
        add("JobSpy (LinkedIn/Naukri)", lambda: jobspy_src.discover(s.cfg))
    if s.get("discovery.browser_portals.enabled") and not args.no_browser:
        from .browser import session
        from .sources import browser_portals
        with session(s) as ctx:
            page = ctx.new_page()
            for portal in s.get("discovery.browser_portals.portals", []):
                pc = s.portals["portals"].get(portal)
                if pc and pc.get("search_urls"):
                    add(portal, lambda: browser_portals.discover(page, portal, pc, s.get("discovery.search_terms")[:3]))
    return tot


def cmd_triage(s, db, args) -> dict:
    from .triage import Triage
    t = Triage(s)
    where = "1=1" if args.all else "triage_status IS NULL"
    c = {"eligible": 0, "review": 0, "rejected": 0}
    rows = db.jobs(where)
    log.info("triage: %d jobs to evaluate", len(rows))
    for i, row in enumerate(rows, 1):
        res = t.evaluate(dict(row))
        db.set_triage(row["key"], **res)
        c[res["triage_status"]] += 1
        if i % 25 == 0 or i == len(rows):
            log.info("triage: %d/%d %s", i, len(rows), c)
    return c


def cmd_plan(s, db, args) -> dict:
    from .planner import plan
    return plan(s, db)


def cmd_apply(s, db, args) -> dict:
    import fcntl
    lock = open(s.path("data_dir") / "apply.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one apply loop at a time (launchd vs manual run)
    except BlockingIOError:
        return {"skipped": "another `jobpilot apply` is running"}
    try:
        return _apply_locked(s, db, args)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _apply_locked(s, db, args) -> dict:
    from .appliers.engine import Engine
    from .browser import session
    eng = Engine(s, db, interactive=args.interactive)
    if args.id:
        ids = [args.id]
    else:
        states = "('queued','approved')" if s.mode == "shadow" else "('queued','approved','verified')"
        from .models import utcnow
        rows = db.apps(f"a.state IN {states} AND (a.hold_until IS NULL OR a.hold_until <= ? OR a.referrer IS NOT NULL)",
                       (utcnow().isoformat(),))
        # approved first, then best fit
        rows = sorted(rows, key=lambda r: (not r["approved"], -(r["fit_score"] or 0)))
        ids = [r["id"] for r in rows][: args.limit]
    out: dict[str, int] = {}
    pace = s.cfg["pacing"]
    log.info("apply: %d applications this run (mode=%s)", len(ids), s.mode)
    if not ids:
        st = cmd_status(s, db, args)
        hint = ("no jobs in the DB yet: run `jobpilot discover`, then `triage` and `plan`" if not st["jobs"] else
                "jobs not triaged yet: run `jobpilot triage`, then `plan`" if st["jobs"].get("untriaged") else
                "nothing queued: run `jobpilot plan` (or everything is already processed / on referral hold)")
        return {"nothing_to_apply": hint, "jobs": st["jobs"], "applications": st["applications"]}
    for i, app_id in enumerate(ids):
        if (s.root / s.cfg.get("kill_switch_file", "STOP")).exists():
            log.warning("kill switch present — stopping")
            break
        a = db.app(app_id)
        if args.id and a["state"] in ("submitting", "submitted", "unconfirmed", "confirmed"):
            log.error("app %s is %s — refusing to re-apply (check the portal / proofs)", app_id, a["state"])
            break
        if args.id and a["state"] in ("needs_review", "fill_failed", "skipped", "submit_failed"):
            db.transition(app_id, "queued")
        try:
            res = eng.process(app_id, lambda har: session(s, har=har, headless=args.headless))
        except KeyboardInterrupt:
            raise
        except Exception as e:     # one bad application never stops the loop
            log.exception("app %s crashed: %s", app_id, e)
            res = "crashed"
        out[res] = out.get(res, 0) + 1
        log.info("app %s -> %s", app_id, res)
        if res in ("submitted", "unconfirmed") and i < len(ids) - 1:
            gap = random.uniform(pace["min_gap_s"], pace["max_gap_s"])
            log.info("pacing %.0fs", gap)
            time.sleep(gap)
    return out


def cmd_doctor(s, db, args) -> dict:
    from .llm.codex import Codex
    from .llm.laya import Laya
    from .triage import RESUMES
    issues, ok = [], []
    miss = s.unfilled_profile_keys()
    (issues if miss else ok).append(f"profile.yaml FILL_ME: {miss}" if miss else "profile.yaml complete")
    for k, v in RESUMES.items():
        p = s.path("resumes_dir") / v["file"]
        (ok if p.exists() else issues).append(f"resume {k}: {p.name} {'found' if p.exists() else 'MISSING'}")
        if p.exists() and not p.with_suffix('.txt').exists():
            issues.append(f"resume text {p.with_suffix('.txt').name} missing (pdftotext {p.name})")
    c = s.cfg["llm"]["codex"]
    (ok if Codex(c["binary"]).available() else issues).append(f"codex CLI {'on PATH' if shutil.which(c['binary']) else 'NOT found (screening answers + judge disabled → review)'}")
    lc = s.cfg["llm"]["laya"]
    (ok if Laya(lc["model"], lc.get("dtype", "float16")).available() else issues).append(
        f"laya-mlx {'loaded' if Laya(lc['model']).available() else 'unavailable → keyword heuristics'}")
    try:
        import jobspy  # noqa: F401
        ok.append("python-jobspy installed")
    except ImportError:
        issues.append("python-jobspy not installed (LinkedIn/Naukri/Indeed discovery off)")
    import os
    ec = s.cfg["email"]
    (ok if os.environ.get(ec["imap_user_env"]) and os.environ.get(ec["imap_password_env"]) else issues).append(
        "IMAP credentials " + ("set" if os.environ.get(ec["imap_password_env"]) else f"missing ({ec['imap_user_env']}/{ec['imap_password_env']} in .env)"))
    res = s.path("data_dir") / "ats_resolved.json"
    (ok if res.exists() else issues).append("ATS boards resolved" if res.exists() else "run `jobpilot probe-ats`")
    ok.append(f"mode = {s.mode}")
    if args.portals:
        from .browser import session
        from .portal_auth import login_state
        dbg = s.path("data_dir") / "doctor"
        dbg.mkdir(exist_ok=True)
        with session(s) as ctx:
            page = ctx.new_page()
            for name, pc in s.portals["portals"].items():
                if not (pc.get("home") and pc.get("logged_in_check")):
                    continue   # WWR/remote boards need no login; Workday logins are per company (checked at apply time)
                try:
                    page.goto(pc["home"], wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    logged, why = login_state(page, pc)
                    (ok if logged else issues).append(f"{name}: {'logged in' if logged else 'NOT logged in'} ({why})")
                    if args.debug or not logged:
                        page.screenshot(path=str(dbg / f"{name}.png"))
                        cookies = sorted({c["name"] for c in ctx.cookies() if name.split("_")[0] in c["domain"] or
                                          urlsplit(pc["home"]).netloc.split(".")[-2] in c["domain"]})
                        print(f"  · {name}: url={page.url[:90]}  cookies={cookies[:25]}  screenshot={dbg / (name + '.png')}")
                except Exception as e:
                    issues.append(f"{name}: {e}")
    for line in ok:
        print("  ✓", line)
    for line in issues:
        print("  ✗", line)
    return {"ok": len(ok), "issues": len(issues)}


def cmd_login(s, db, args) -> dict:
    """Open jobpilot's Chrome profile as a normal Chrome window (not automated) for one-time sign-ins."""
    import subprocess
    from .browser import CHROME_MAC, login_command, profile_in_use
    names = args.portals or [n for n, pc in s.portals["portals"].items() if pc.get("home") and pc.get("logged_in_check")]
    urls = [s.portals["portals"][n]["home"] for n in names]
    profile = s.path("browser_profile")
    chrome = s.cfg["browser"].get("chrome_path") or CHROME_MAC
    if not Path(chrome).exists():
        return {"error": f"Google Chrome not found at {chrome}; set browser.chrome_path in config.yaml"}
    if profile_in_use(profile):
        return {"error": "jobpilot's Chrome window is already open; finish signing in there, then quit it with Cmd+Q"}
    proc = subprocess.Popen(login_command(profile, urls, chrome), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"A separate Chrome window opened with jobpilot's profile and tabs for: {', '.join(names)}.")
    print("Sign in to each one there (tick 'Remember me' / 'Keep me signed in').")
    print("Tip: signing in to Chrome itself in that window (profile icon → Turn on sync) brings your saved passwords,")
    print("     so each portal login is one autofill click. Your normal Chrome is not touched.")
    input("When you're done, quit that Chrome window with Cmd+Q, then press Enter here... ")
    # give Chrome time to finish quitting: it writes the cookie store on a clean exit
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        input("That Chrome window is still open. Quit it with Cmd+Q (so your logins are saved), then press Enter... ")
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()   # graceful quit signal; Chrome still flushes cookies on SIGTERM
            proc.wait(timeout=15)
    return {"portals": names, "profile": str(profile), "next": "jobpilot doctor --portals"}


def cmd_outreach(s, db, args) -> dict:
    from . import outreach as o
    from .llm.codex import Codex
    a = args.action
    if a == "import-connections":
        return o.import_connections_csv(db, s, Path(args.arg))
    if a == "import-contacts":
        return o.import_contacts_csv(db, s, Path(args.arg))
    if a == "find":
        return _find_hiring_teams(s, db, o)
    if a == "plan":
        c = s.cfg["llm"]["codex"]
        return o.plan_and_draft(s, db, Codex(c.get("binary", "codex"), c.get("timeout_s", 180), c.get("extra_args"), c.get("enabled", True)))
    if a == "send":
        o.send_queue(s, db)
        return o.status(s, db)["limits"]
    if a == "followups":
        return o.followups(s, db)
    if a == "mark":
        oid, what = args.arg.split(":", 1) if ":" in (args.arg or "") else (args.arg, args.state)
        o.mark(db, int(oid), what)
        return {"marked": oid, "as": what}
    if a == "sync":
        return o.sync_notifications(s, db)
    return o.status(s, db)


def _find_hiring_teams(s, db, o) -> dict:
    """Open LinkedIn job pages (only for companies you're applying to) to read 'Meet the hiring team'. Paced + capped."""
    from .browser import session
    cap = s.cfg["outreach"].get("hiring_team_pages_per_day", 20)
    rows = db.conn.execute(
        "SELECT DISTINCT j.* FROM applications a JOIN jobs j ON j.company_canon=a.company_canon WHERE j.source='linkedin' "
        "AND a.state NOT IN ('skipped','fill_failed') AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.company_canon=j.company_canon "
        "AND c.source='hiring_team') LIMIT ?", (cap,)).fetchall()
    n = 0
    with session(s) as ctx:
        page = ctx.new_page()
        for r in rows:
            page.goto(r["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(random.uniform(3000, 6000))
            n += o.capture_hiring_team(page, db, s, dict(r))
            time.sleep(random.uniform(8, 20))
    return {"pages": len(rows), "contacts_added": n}


def cmd_status(s, db, args) -> dict:
    rows = db.conn.execute("SELECT state, COUNT(*) n FROM applications GROUP BY state").fetchall()
    tri = db.conn.execute("SELECT COALESCE(triage_status,'untriaged') t, COUNT(*) n FROM jobs GROUP BY t").fetchall()
    return {"mode": s.mode, "jobs": {r["t"]: r["n"] for r in tri}, "applications": {r["state"]: r["n"] for r in rows},
            "submitted_today": db.submits_today()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobpilot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor"); d.add_argument("--portals", action="store_true", help="also check portal logins")
    d.add_argument("--debug", action="store_true", help="with --portals: print URL + cookie names and save a screenshot per portal")
    lg = sub.add_parser("login"); lg.add_argument("portals", nargs="*")
    sub.add_parser("probe-ats")
    ds = sub.add_parser("discover"); ds.add_argument("--no-jobspy", action="store_true"); ds.add_argument("--no-browser", action="store_true")
    tr = sub.add_parser("triage"); tr.add_argument("--all", action="store_true", help="re-triage everything")
    sub.add_parser("plan")
    a = sub.add_parser("apply")
    a.add_argument("--limit", type=int, default=25); a.add_argument("--id", type=int)
    a.add_argument("--interactive", action="store_true", help="pause for CAPTCHAs instead of queueing for review")
    a.add_argument("--headless", action="store_true", default=None)
    sub.add_parser("review")
    sub.add_parser("confirm-emails")
    sub.add_parser("report")
    sub.add_parser("status")
    o = sub.add_parser("outreach", help="assisted referral / hiring-manager outreach (you press Send)")
    o.add_argument("action", choices=["import-connections", "import-contacts", "find", "plan", "send", "followups", "mark", "sync", "status"])
    o.add_argument("arg", nargs="?", help="CSV path for imports; outreach id (or id:state) for mark")
    o.add_argument("state", nargs="?", help="for mark: accepted | replied | referred | followed_up | withdrawn")
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=25); r.add_argument("--no-jobspy", action="store_true")
    r.add_argument("--no-browser", action="store_true"); r.add_argument("--skip-apply", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s = load_settings()
    db = _db(s)
    if args.cmd == "probe-ats":
        from .sources.ats import probe
        res = probe(s.companies, s.path("data_dir"))
        import httpx
        from .sources import workday
        with httpx.Client(headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "Accept-Language": "en-US"}, timeout=40) as c:
            for co in s.companies:
                if co.get("workday"):
                    st, n = workday.probe(c, co["workday"])
                    res[co["name"]] = {"ats": "workday", "url": co["workday"], "status": st, "open_jobs": n}
        print(json.dumps({k: v for k, v in res.items()}, indent=1))
        return 0
    if args.cmd == "review":
        from .review import run as review
        review(db)
        return 0
    if args.cmd == "confirm-emails":
        from .email_confirm import run as conf
        print(json.dumps(conf(s, db), indent=1))
        return 0
    if args.cmd == "report":
        from .report import build
        print(build(s, db))
        return 0
    if args.cmd == "run":
        args.all = False
        args.interactive = False
        args.id = None
        args.headless = None
        steps = [("discover", cmd_discover), ("triage", cmd_triage), ("plan", cmd_plan)]
        if not args.skip_apply:
            steps.append(("apply", cmd_apply))
        for name, fn in steps:
            log.info("== %s", name)
            print(name, json.dumps(fn(s, db, args), default=str))
        if s.get("outreach.enabled"):
            from . import outreach as o
            from .llm.codex import Codex
            c = s.cfg["llm"]["codex"]
            try:
                print("outreach-sync", json.dumps(o.sync_notifications(s, db)))
                print("outreach-drafts", json.dumps(o.plan_and_draft(s, db, Codex(c.get("binary", "codex"), c.get("timeout_s", 180),
                                                                                  c.get("extra_args"), c.get("enabled", True)))))
            except Exception as e:
                log.warning("outreach step failed: %s", e)
        if s.get("email.enabled"):
            from .email_confirm import run as conf
            try:
                print("confirm-emails", json.dumps(conf(s, db)))
            except Exception as e:
                log.warning("email confirmation failed: %s", e)
        from .report import build
        print("report", build(s, db))
        return 0
    from .browser import ProfileInUse
    fn = {"doctor": cmd_doctor, "login": cmd_login, "discover": cmd_discover, "triage": cmd_triage, "plan": cmd_plan,
          "apply": cmd_apply, "status": cmd_status, "outreach": cmd_outreach}[args.cmd]
    try:
        print(json.dumps(fn(s, db, args), indent=1, default=str))
    except ProfileInUse as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
