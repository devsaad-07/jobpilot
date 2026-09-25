"""Assisted outreach for referrals and hiring-manager contact.

jobpilot finds people, drafts messages from resume facts only, checks them, and enforces limits.
**You** press Send: on LinkedIn it opens the profile in your normal browser with the note on your
clipboard; for email it puts a draft (resume attached) in your Gmail Drafts. The script never
sends a LinkedIn invite or message itself, because that is exactly what LinkedIn's User
Agreement (section 8.2) bans and what gets accounts restricted.

People come from:
  connections_csv  LinkedIn's official data export (Settings → Data privacy → Get a copy of your data
                   → Connections). Warm first-degree contacts at target companies.
  hiring_team      the "Meet the hiring team" people on a LinkedIn job page jobpilot already opens.
  apollo_csv       an export from Apollo (or any CSV with name/title/company/email/linkedin columns).
"""
from __future__ import annotations

import csv
import difflib
import email.message
import imaplib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import FILL_ME, Settings
from .db import DB
from .llm.codex import Codex
from .models import utcnow
from .normalize import build_alias_map, canon_company, fold

log = logging.getLogger(__name__)

PERSONAS = [
    ("recruiter", r"recruit|talent|sourc(er|ing)|\bhr\b|human resources|people (partner|operations)|hiring (partner|specialist|manager - ta)"),
    ("founder", r"co-?founder|\bfounder\b|\bceo\b|\bcto\b|chief (technology|executive|technical) officer"),
    ("eng_manager", r"engineering manager|\bem\b|head of (engineering|platform|backend|technology)|director[^,]*engineering|"
                    r"vp[^,]*engineering|vice president[^,]*engineering|engineering lead|manager[^,]*(backend|platform|software|engineering)"),
    ("engineer", r"engineer|developer|\bsde\b|\bswe\b|architect|member of technical staff|programmer|tech lead"),
]
KIND_FOR = {"connection": "referral_ask", "engineer": "referral_ask", "hiring_team": "hiring_manager",
            "eng_manager": "hiring_manager", "founder": "founder", "recruiter": "recruiter"}
URL_RX = re.compile(r"https?://|www\.|linkedin\.com/|\.(com|io|ai|in)\b/", re.I)


def persona_of(title: str) -> str:
    t = (title or "").lower()
    for name, rx in PERSONAS:
        if re.search(rx, t):
            return name
    return "other"


# ------------------------------------------------------------------ contacts
def _canon(s: Settings, company: str) -> str:
    return canon_company(company or "", build_alias_map(s.companies)) if company else ""


def add_contact(db: DB, s: Settings, *, name: str, company: str, title: str, source: str, linkedin_url: str = "",
                email_addr: str = "", is_connection: bool = False, connected_on: str = "") -> Optional[int]:
    name = " ".join((name or "").split())
    if not name:
        return None
    li = re.sub(r"[?#].*$", "", (linkedin_url or "").strip()).rstrip("/") or None
    em = (email_addr or "").strip().lower() or None
    row = None
    if li:
        row = db.conn.execute("SELECT * FROM contacts WHERE linkedin_url=?", (li,)).fetchone()
    if row is None and em:
        row = db.conn.execute("SELECT * FROM contacts WHERE email=?", (em,)).fetchone()
    persona = "connection" if is_connection else ("hiring_team" if source == "hiring_team" else persona_of(title))
    if row:  # merge: keep the warmest persona, fill missing fields
        warm = row["is_connection"] or is_connection
        db.conn.execute(
            "UPDATE contacts SET title=COALESCE(NULLIF(?,''),title), company=COALESCE(NULLIF(?,''),company), "
            "company_canon=COALESCE(NULLIF(?,''),company_canon), email=COALESCE(email,?), linkedin_url=COALESCE(linkedin_url,?), "
            "is_connection=?, persona=? WHERE id=?",
            (title, company, _canon(s, company), em, li, int(warm), "connection" if warm else row["persona"], row["id"]))
        return row["id"]
    cur = db.conn.execute(
        "INSERT INTO contacts(name,company,company_canon,title,persona,linkedin_url,email,source,is_connection,connected_on,added_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, company, _canon(s, company), title, persona, li, em, source, int(is_connection), connected_on, utcnow().isoformat()))
    return cur.lastrowid


def import_connections_csv(db: DB, s: Settings, path: Path) -> dict:
    """LinkedIn export: a few 'Notes:' lines, then First Name,Last Name,URL,Email Address,Company,Position,Connected On."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lower().startswith("first name")), None)
    if start is None:
        raise ValueError("no 'First Name,...' header found — is this LinkedIn's Connections.csv?")
    n = 0
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        cid = add_contact(db, s, name=f"{r.get('First Name', '')} {r.get('Last Name', '')}", company=r.get("Company", ""),
                          title=r.get("Position", ""), source="connections_csv", linkedin_url=r.get("URL", ""),
                          email_addr=r.get("Email Address", ""), is_connection=True, connected_on=r.get("Connected On", ""))
        n += cid is not None
    return {"imported": n}


_COLS = {
    "first": ["first name", "first_name", "firstname"], "last": ["last name", "last_name", "lastname"],
    "name": ["name", "full name", "full_name"], "title": ["title", "job title", "position"],
    "company": ["company", "company name", "organization", "account name"],
    "email": ["email", "email address", "work email", "business email"],
    "linkedin": ["person linkedin url", "linkedin url", "linkedin", "linkedin_url", "url", "profile url"],
}


def import_contacts_csv(db: DB, s: Settings, path: Path, source: str = "apollo_csv") -> dict:
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig", errors="replace"))))
    if not rows:
        return {"imported": 0}
    keys = {k.lower().strip(): k for k in rows[0].keys()}

    def col(r, what):
        for c in _COLS[what]:
            if c in keys and r.get(keys[c]):
                return r[keys[c]].strip()
        return ""
    n = 0
    for r in rows:
        name = col(r, "name") or f"{col(r, 'first')} {col(r, 'last')}"
        cid = add_contact(db, s, name=name, company=col(r, "company"), title=col(r, "title"), source=source,
                          linkedin_url=col(r, "linkedin"), email_addr=col(r, "email"))
        n += cid is not None
    return {"imported": n}


HIRING_TEAM_JS = r"""
() => {
  const txt = e => (e && (e.innerText || '').replace(/\s+/g, ' ').trim()) || '';
  const heads = [...document.querySelectorAll('h2, h3, span, div')].filter(e =>
      /^(meet the hiring team|people you can reach out to|hiring team)$/i.test(txt(e)));
  const out = [], seen = new Set();
  for (const h of heads) {
    let sec = h.closest('section, div[class*="hirer"], div[class*="people-who-can-help"]') || h.parentElement;
    for (let i = 0; i < 3 && sec && sec.querySelectorAll('a[href*="/in/"]').length === 0; i++) sec = sec.parentElement;
    if (!sec) continue;
    for (const a of sec.querySelectorAll('a[href*="/in/"]')) {
      const url = a.href.split('?')[0];
      if (seen.has(url)) continue;
      const card = a.closest('li, div[class*="hirer-card"], div[class*="entity"]') || a.parentElement;
      const lines = txt(card).split(/ · | • |\n/).map(x => x.trim()).filter(Boolean);
      const name = txt(a).split(/ · |\n/)[0] || lines[0] || '';
      const title = lines.find(l => l !== name && l.length > 3 && !/^(\d(st|nd|rd)|message|connect|follow|job poster)/i.test(l)) || '';
      if (name) { seen.add(url); out.push({ name, title, url }); }
    }
  }
  return out.slice(0, 6);
}
"""


def capture_hiring_team(page, db: DB, s: Settings, job: dict) -> int:
    try:
        people = page.evaluate(HIRING_TEAM_JS)
    except Exception as e:
        log.info("hiring team read failed: %s", e)
        return 0
    n = 0
    for p in people:
        n += add_contact(db, s, name=p["name"], company=job["company"], title=p.get("title", ""),
                         source="hiring_team", linkedin_url=p["url"]) is not None
    if n:
        db.event(None, job["key"], "hiring_team_captured", {"n": n})
    return n


# ------------------------------------------------------------------ limits
def _count(db: DB, where: str, params: tuple) -> int:
    return db.conn.execute(f"SELECT COUNT(*) FROM outreach WHERE {where}", params).fetchone()[0]


def linkedin_plan(s: Settings) -> tuple[str, int]:
    """-> (plan, note char limit), falling back to free when the Premium trial date has passed."""
    lc = s.cfg["outreach"]["linkedin"]
    plan = lc.get("plan", "free")
    until = str(lc.get("premium_until") or "")
    if plan == "premium" and until and until != FILL_ME:
        try:
            if date.fromisoformat(until) < date.today():
                plan = "free"
        except ValueError:
            pass
    return plan, lc["note_chars_premium"] if plan == "premium" else lc["note_chars_free"]


def limits_status(s: Settings, db: DB) -> dict:
    L = s.cfg["outreach"]["limits"]
    now = utcnow()
    day, week, month = (now - timedelta(days=1)).isoformat(), (now - timedelta(days=7)).isoformat(), (now - timedelta(days=30)).isoformat()
    li = "channel='linkedin' AND sent_at IS NOT NULL AND sent_at>=?"
    invites_day = _count(db, li + " AND state<>'messaged'", (day,))
    invites_week = _count(db, li + " AND state<>'messaged'", (week,))
    notes_month = _count(db, li + " AND note IS NOT NULL AND note<>''", (month,))
    emails_day = _count(db, "channel='email' AND sent_at>=?", (day,))
    sent_total = _count(db, "channel='linkedin' AND sent_at IS NOT NULL", ())
    accepted = _count(db, "channel='linkedin' AND accepted_at IS NOT NULL", ())
    rate = accepted / sent_total if sent_total else None
    paused = bool(rate is not None and sent_total >= L["acceptance_min_sample"] and rate < L["pause_if_acceptance_below"])
    plan, chars = linkedin_plan(s)
    return {"plan": plan, "note_chars": chars, "invites_today": invites_day, "invites_week": invites_week,
            "notes_this_month": notes_month, "emails_today": emails_day, "acceptance_rate": rate, "paused": paused,
            "can_invite": (not paused) and invites_day < L["invites_per_day"] and invites_week < L["invites_per_week"],
            "can_note": plan == "premium" or notes_month < s.cfg["outreach"]["linkedin"]["free_notes_per_month"],
            "can_email": emails_day < L["emails_per_day"]}


# ------------------------------------------------------------------ targeting
def pick_targets(s: Settings, db: DB) -> list[dict]:
    """For every company with a live or held application, choose up to N people not contacted recently."""
    oc = s.cfg["outreach"]
    L = oc["limits"]
    prio = {p: i for i, p in enumerate(oc["persona_priority"])}
    since = (utcnow() - timedelta(days=L["recontact_after_days"])).isoformat()
    apps = db.conn.execute(
        "SELECT a.company_canon, a.job_key, j.company, j.title FROM applications a JOIN jobs j ON j.key=a.job_key "
        "WHERE a.state NOT IN ('skipped','fill_failed','submit_failed') ORDER BY a.id").fetchall()
    out, seen = [], set()
    for a in apps:
        cc = a["company_canon"]
        if cc in seen:
            continue
        seen.add(cc)
        already = _count(db, "company_canon=? AND state<>'skipped'", (cc,))
        room = L["contacts_per_company"] - already
        if room <= 0:
            continue
        people = db.conn.execute(
            "SELECT c.* FROM contacts c WHERE c.company_canon=? AND c.persona<>'other' AND NOT EXISTS "
            "(SELECT 1 FROM outreach o WHERE o.contact_id=c.id AND o.created_at>=?)", (cc, since)).fetchall()
        people = sorted(people, key=lambda c: (prio.get(c["persona"], 99), c["name"]))
        for c in people[:room]:
            out.append({"contact": dict(c), "job_key": a["job_key"], "company": a["company"], "title": a["title"]})
    return out


# ------------------------------------------------------------------ drafting
DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["note", "message", "email_subject", "email_body", "facts_used"],
    "properties": {"note": {"type": "string"}, "message": {"type": "string"}, "email_subject": {"type": "string"},
                   "email_body": {"type": "string"}, "facts_used": {"type": "array", "items": {"type": "string"}}},
}
KIND_GUIDE = {
    "referral_ask": "Ask for a referral for the specific role. Mention one relevant thing from the resume. Low pressure; offer to send details.",
    "hiring_manager": "Say you applied or are applying for the role and why your background fits their team. Ask if they'd be open to a quick look.",
    "founder": "Short, direct note to a founder/CTO about the role: one concrete overlap between your work and what they build.",
    "recruiter": "Say you're interested in the role, give the one-line fit, and ask about the process/right person.",
}


def draft_prompt(s: Settings, kind: str, contact: dict, company: str, title: str, jd: str, resume_text: str,
                 note_chars: int, resume_link: str, with_note: bool) -> str:
    lo, hi = s.cfg["outreach"]["linkedin"]["note_target_chars"]
    first = contact["name"].split()[0]
    return f"""Draft outreach from a job seeker ({s.profile['identity']['full_name']}) to {contact['name']} ({contact.get('title') or 'unknown title'}) at {company},
about the role "{title}". Purpose: {KIND_GUIDE[kind]}

Hard rules:
- Use ONLY facts from RESUME. Never invent shared history, mutual friends, numbers, or claims about {company}.
- If you mention something about the company, take it from the JOB DESCRIPTION only.
- note: LinkedIn connection note. {"Between " + str(lo) + " and " + str(min(hi, note_chars)) + " characters (hard max " + str(note_chars) + ")." if with_note else "Return an empty string (the invite goes out without a note)."} No links, no emojis. Start with "Hi {first},".
- message: follow-up after they accept, ≤ 600 characters, includes this resume link exactly once: {resume_link or "(no link available: don't include one)"}
- email_subject: ≤ 70 characters, mentions the role. email_body: ≤ 900 characters, plain text, ends with the signature "{s.profile.get('outreach', {}).get('signature') or s.profile['identity']['full_name']}".
- facts_used: the resume phrases you relied on.
- Plain, specific, first person. No flattery, no "I hope this finds you well", no "urgent".

JOB DESCRIPTION (excerpt):
{(jd or '')[:2500]}

RESUME:
{resume_text[:6000]}
"""


def template_draft(s: Settings, kind: str, contact: dict, company: str, title: str, note_chars: int, resume_link: str,
                   with_note: bool) -> dict:
    """Deterministic fallback when Codex isn't available. Uses only resume headline facts."""
    first = contact["name"].split()[0]
    fit = "backend engineer, ~5 years (4 in Go), building trading and order-management systems"
    asks = {
        "referral_ask": f"Hi {first}, I'm a {fit}. I'm applying for {title} at {company}; would you be open to referring me?",
        "hiring_manager": f"Hi {first}, I'm a {fit}. I'm applying for {title} on your team and would value a quick look.",
        "founder": f"Hi {first}, I'm a {fit}. The {title} role at {company} fits my work; happy to share details.",
        "recruiter": f"Hi {first}, I'm a {fit}, interested in {title} at {company}. Who's the right person to talk to?",
    }
    note = asks[kind][:note_chars] if with_note else ""
    link = f" My resume: {resume_link}" if resume_link else ""
    message = (f"Thanks for connecting, {first}. I'm interested in {title} at {company}. Recently I built an options "
               f"market maker's quoting engine and hedging system in Go, and designed an equities broker's order management "
               f"architecture.{link}")
    sig = s.profile.get("outreach", {}).get("signature") or s.profile["identity"]["full_name"]
    body = (f"Hi {first},\n\nI'm a {fit}. I'm interested in the {title} role at {company}. Recent work: the quoting "
            f"engine and hedging system behind an in-house options market maker, and the Go order-management architecture "
            f"for an equities broker.{(' Resume: ' + resume_link) if resume_link else ''}\n\nWould you be open to a quick chat, "
            f"or pointing me to the right person?\n\n{sig}")
    return {"note": note, "message": message, "email_subject": f"{title} at {company}: backend engineer (Go)",
            "email_body": body, "facts_used": ["headline", "quoting engine", "hedging system", "order management"]}


def check_draft(d: dict, *, note_chars: int, company: str, first_name: str, known_numbers: set[str],
                resume_link: str, others: list[str], with_note: bool) -> dict:
    blocks, warns = [], []
    note, msg, body = d.get("note", ""), d.get("message", ""), d.get("email_body", "")
    if with_note:
        if not note:
            blocks.append("empty connection note")
        if len(note) > note_chars:
            blocks.append(f"note is {len(note)} chars (> {note_chars})")
        if URL_RX.search(note):
            blocks.append("link in connection note (hurts acceptance)")
        if note and not note.lower().startswith(f"hi {first_name.lower()}"):
            warns.append("note doesn't open with the person's first name")
    for label, text in (("note", note), ("message", msg), ("email", body), ("subject", d.get("email_subject", ""))):
        if FILL_ME in text or re.search(r"\{[\w.]+\}|\[(name|company|role)\]", text, re.I):
            blocks.append(f"{label}: placeholder left in text")
        stray = [n for n in re.findall(r"\d+(?:\.\d+)?", text) if n not in known_numbers]
        if stray:
            blocks.append(f"{label}: numbers not in resume/job ({stray})")
        if re.search(r"[\U0001F300-\U0001FAFF]", text):
            warns.append(f"{label}: emoji")
    if company.lower() not in (note + msg + body).lower():
        warns.append("company name not mentioned")
    if resume_link and resume_link != FILL_ME and resume_link not in msg + body:
        warns.append("resume link missing from message/email")
    if len(msg) > 600:
        blocks.append(f"message is {len(msg)} chars (> 600)")
    for o in others:
        if o and note and difflib.SequenceMatcher(None, o, note).ratio() > 0.9:
            blocks.append("note is near-identical to another draft (looks automated)")
            break
    return {"status": "block" if blocks else "pass", "blocks": blocks, "warns": warns}


def plan_and_draft(s: Settings, db: DB, codex: Codex, limit: int = 30) -> dict:
    from .triage import RESUMES
    targets = pick_targets(s, db)[:limit]
    plan, note_chars = linkedin_plan(s)
    st = limits_status(s, db)
    links = s.profile.get("outreach", {}).get("resume_links", {})
    stats = {"targets": len(targets), "drafted": 0, "blocked": 0}
    drafted_notes = [r["note"] for r in db.conn.execute("SELECT note FROM outreach WHERE note IS NOT NULL").fetchall()]
    log.info("outreach: drafting for %d contacts", len(targets))
    for i, t in enumerate(targets, 1):
        c, job = t["contact"], dict(db.jobs("key=?", (t["job_key"],))[0])
        log.info("outreach [%d/%d]: %s (%s) at %s", i, len(targets), c["name"], c["persona"], job["company"])
        kind = KIND_FOR.get(c["persona"], "recruiter")
        channel = "email" if (c.get("email") and not c["is_connection"] and c["persona"] in ("founder", "eng_manager", "recruiter", "hiring_team")) else "linkedin"
        variant = job.get("resume_variant") or "general"
        resume = s.path("resumes_dir") / RESUMES[variant]["file"]
        resume_text = resume.with_suffix(".txt").read_text() if resume.with_suffix(".txt").exists() else ""
        link = links.get(variant, "")
        link = "" if link == FILL_ME else link
        with_note = channel == "linkedin" and not c["is_connection"] and st["can_note"]
        d = None
        if codex.available():
            d = codex.run(draft_prompt(s, kind, c, job["company"], job["title"], job.get("description", ""), resume_text,
                                       note_chars, link, with_note), DRAFT_SCHEMA)
        d = d or template_draft(s, kind, c, job["company"], job["title"], note_chars, link, with_note)
        if c["is_connection"]:
            d["note"] = ""   # already connected: straight to a message
        known = set(re.findall(r"\d+(?:\.\d+)?", resume_text + " " + json.dumps(s.profile, default=str) + " " +
                               job["title"] + " " + (job.get("description") or "")[:4000] + " " + link))
        chk = check_draft(d, note_chars=note_chars, company=job["company"], first_name=c["name"].split()[0],
                          known_numbers=known, resume_link=link, others=drafted_notes, with_note=with_note)
        now = utcnow().isoformat()
        db.conn.execute(
            "INSERT INTO outreach(contact_id,company_canon,job_key,channel,kind,state,note,message,email_subject,email_body,checks,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["company_canon"], job["key"], channel, kind, "drafted", d["note"], d["message"], d["email_subject"],
             d["email_body"], json.dumps({**chk, "facts_used": d.get("facts_used", [])}), now, now))
        drafted_notes.append(d["note"])
        stats["drafted" if chk["status"] == "pass" else "blocked"] += 1
    return stats


# ------------------------------------------------------------------ assisted sending
def _copy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"] if sys.platform == "darwin" else ["xclip", "-selection", "clipboard"], input=text, text=True, check=True)
    except Exception:
        print("  (clipboard unavailable, copy the text above)")


def _open(url: str) -> None:
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gmail_draft(s: Settings, to: str, subject: str, body: str, attachment: Optional[Path]) -> bool:
    """Put a draft (with the resume attached) into Gmail Drafts over IMAP. Nothing is sent."""
    ec = s.cfg["email"]
    user, pw = os.environ.get(ec["imap_user_env"]), os.environ.get(ec["imap_password_env"])
    if not (user and pw):
        print(f"  set {ec['imap_user_env']}/{ec['imap_password_env']} in .env to create Gmail drafts")
        return False
    msg = build_email(user, to, subject, body, attachment)
    M = imaplib.IMAP4_SSL(ec["imap_host"])
    M.login(user, pw)
    M.append('"[Gmail]/Drafts"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    M.logout()
    return True


def build_email(frm: str, to: str, subject: str, body: str, attachment: Optional[Path]) -> email.message.EmailMessage:
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m.set_content(body)
    if attachment and attachment.exists():
        m.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    return m


def send_queue(s: Settings, db: DB) -> None:
    from .triage import RESUMES
    rows = db.conn.execute(
        "SELECT o.*, c.name, c.title AS ctitle, c.linkedin_url, c.email, c.persona, c.is_connection, j.title AS jtitle, j.company, "
        "j.resume_variant FROM outreach o JOIN contacts c ON c.id=o.contact_id JOIN jobs j ON j.key=o.job_key "
        "WHERE o.state='drafted' ORDER BY o.id").fetchall()
    if not rows:
        print("No drafts waiting. Run `jobpilot outreach plan` first.")
        return
    print("o=open profile + copy note/message  g=Gmail draft (email)  s=I sent it  b=sent a blank invite  e=edit note  k=skip  q=quit")
    for r in rows:
        st = limits_status(s, db)
        chk = json.loads(r["checks"] or "{}")
        print("\n" + "=" * 96)
        print(f"#{r['id']} {r['name']} ({r['ctitle'] or '?'}) @ {r['company']}  [{r['persona']}, {r['channel']}, {r['kind']}]  role: {r['jtitle']}")
        for b in chk.get("blocks", []):
            print(f"  ✗ {b}")
        for w in chk.get("warns", []):
            print(f"  ~ {w}")
        if r["channel"] == "linkedin":
            if st["paused"]:
                print(f"  PAUSED: acceptance rate {st['acceptance_rate']:.0%} is below the threshold; fix targeting/notes first.")
                return
            if not st["can_invite"] and not r["is_connection"]:
                print(f"  Daily/weekly invite limit reached ({st['invites_today']} today, {st['invites_week']} this week). Stopping.")
                return
            text = r["message"] if r["is_connection"] else (r["note"] or "")
            print(("  MESSAGE:\n  " if r["is_connection"] else "  NOTE (" + str(len(text)) + " chars):\n  ") + (text or "(blank invite)"))
        else:
            if not st["can_email"]:
                print("  Daily email limit reached. Stopping.")
                return
            print(f"  TO: {r['email']}\n  SUBJECT: {r['email_subject']}\n  {r['email_body']}")
        while True:
            c = input("  > ").strip().lower()
            now = utcnow().isoformat()
            if c == "o" and r["channel"] == "linkedin":
                if r["linkedin_url"]:
                    _open(r["linkedin_url"])
                _copy(r["message"] if r["is_connection"] else (r["note"] or ""))
                print("  opened profile; text is on your clipboard. Send it yourself, then press s (or b if you sent a blank invite).")
            elif c == "g" and r["channel"] == "email":
                if chk.get("status") == "block":
                    print("  blocked by checks; edit first")
                    continue
                pdf = s.path("resumes_dir") / RESUMES.get(r["resume_variant"] or "general", RESUMES["general"])["file"]
                if gmail_draft(s, r["email"], r["email_subject"], r["email_body"], pdf):
                    db.conn.execute("UPDATE outreach SET state='email_drafted', updated_at=? WHERE id=?", (now, r["id"]))
                    print("  draft (with resume attached) is in Gmail Drafts; review and send it there.")
                    break
            elif c in ("s", "b"):
                if chk.get("status") == "block" and c == "s":
                    print("  this draft failed checks; press e to fix it, or b if you sent a blank invite instead")
                    continue
                state = "messaged" if r["is_connection"] else "invited"
                db.conn.execute("UPDATE outreach SET state=?, sent_at=?, note=CASE WHEN ?='b' THEN '' ELSE note END, updated_at=? WHERE id=?",
                                (state, now, c, now, r["id"]))
                break
            elif c == "e":
                new = input("  new note/message text: ").strip()
                col = "message" if r["is_connection"] else "note"
                db.conn.execute(f"UPDATE outreach SET {col}=?, checks=?, updated_at=? WHERE id=?",
                                (new, json.dumps({"status": "pass", "blocks": [], "warns": ["edited by you"]}), now, r["id"]))
                chk = {"status": "pass"}
                print("  saved.")
            elif c == "k":
                db.conn.execute("UPDATE outreach SET state='skipped', updated_at=? WHERE id=?", (now, r["id"]))
                break
            elif c == "q":
                return


def followups(s: Settings, db: DB) -> dict:
    """What needs a human next: accepted → send message with resume link; silent → one follow-up; stale invites → withdraw."""
    L = s.cfg["outreach"]["limits"]
    now = utcnow()
    q = ("SELECT o.id, o.state, o.message, o.sent_at, o.accepted_at, c.name, c.linkedin_url, j.company, j.title FROM outreach o "
         "JOIN contacts c ON c.id=o.contact_id JOIN jobs j ON j.key=o.job_key WHERE ")
    accepted = db.conn.execute(q + "o.state='accepted'").fetchall()
    silent = db.conn.execute(q + "o.state='messaged' AND o.followup_at IS NULL AND o.replied_at IS NULL AND o.sent_at<=?",
                             ((now - timedelta(days=L["followup_after_days"])).isoformat(),)).fetchall()
    stale = db.conn.execute(q + "o.state='invited' AND o.sent_at<=?",
                            ((now - timedelta(days=L["withdraw_pending_after_days"])).isoformat(),)).fetchall()
    return {"send_message_with_resume": [dict(r) for r in accepted], "one_followup": [dict(r) for r in silent],
            "withdraw_invite": [dict(r) for r in stale]}


def mark(db: DB, outreach_id: int, what: str) -> None:
    now = utcnow().isoformat()
    col = {"accepted": "accepted_at", "replied": "replied_at", "referred": "replied_at", "followed_up": "followup_at",
           "messaged": "sent_at", "withdrawn": "updated_at"}[what]
    state = {"followed_up": "messaged"}.get(what, what)
    db.conn.execute(f"UPDATE outreach SET state=?, {col}=COALESCE({col},?), updated_at=? WHERE id=?", (state, now, now, outreach_id))
    if what == "referred":
        r = db.conn.execute("SELECT o.company_canon, c.name FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE o.id=?",
                            (outreach_id,)).fetchone()
        # release the referral hold and record the referrer for "Referred by" form fields
        db.conn.execute("UPDATE applications SET referrer=?, hold_until=NULL, hold_reason='referral received' "
                        "WHERE company_canon=? AND state IN ('queued','approved','needs_review','verified')", (r["name"], r["company_canon"]))


# ------------------------------------------------------------------ notification sync (LinkedIn emails you about these)
ACCEPT_RX = re.compile(r"^(.+?) (has )?accepted your invitation", re.I)
MSG_RX = re.compile(r"^(?:new message from |)(.+?) (sent you a (new )?message|just messaged you|replied)", re.I)


def parse_notification(subject: str) -> Optional[tuple[str, str]]:
    s = " ".join((subject or "").split())
    if m := ACCEPT_RX.search(s):
        return "accepted", m.group(1).strip()
    if m := MSG_RX.search(s):
        return "replied", m.group(1).strip()
    if m := re.match(r"^New message from (.+)$", s, re.I):
        return "replied", m.group(1).strip()
    return None


def apply_notification(db: DB, kind: str, name: str, when: datetime) -> Optional[int]:
    row = db.conn.execute(
        "SELECT o.id FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE lower(c.name)=lower(?) AND "
        "o.state IN ('invited','messaged','accepted') ORDER BY o.id DESC LIMIT 1", (name,)).fetchone()
    if not row:
        return None
    col = "accepted_at" if kind == "accepted" else "replied_at"
    new_state = "accepted" if kind == "accepted" else "replied"
    db.conn.execute(f"UPDATE outreach SET state=CASE WHEN state='messaged' AND ?='accepted' THEN state ELSE ? END, "
                    f"{col}=COALESCE({col},?), updated_at=? WHERE id=?", (kind, new_state, when.isoformat(), utcnow().isoformat(), row["id"]))
    return row["id"]


def sync_notifications(s: Settings, db: DB, lookback_days: int = 14) -> dict:
    ec = s.cfg["email"]
    user, pw = os.environ.get(ec["imap_user_env"]), os.environ.get(ec["imap_password_env"])
    if not (user and pw):
        return {"skipped": "no IMAP credentials"}
    from email.header import decode_header, make_header
    from email.utils import parsedate_to_datetime
    M = imaplib.IMAP4_SSL(ec["imap_host"])
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    _, data = M.search(None, f'(SINCE "{since}" FROM "linkedin.com")')
    n = 0
    for num in data[0].split()[-500:]:
        _, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
        h = email.message_from_bytes(hdr[0][1])
        subj = str(make_header(decode_header(h.get("Subject") or "")))
        hit = parse_notification(subj)
        if hit:
            try:
                when = parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc)
            except Exception:
                when = utcnow()
            n += apply_notification(db, hit[0], hit[1], when) is not None
    M.logout()
    return {"updated": n}


def status(s: Settings, db: DB) -> dict:
    by = {f"{r['channel']}:{r['state']}": r["n"] for r in db.conn.execute(
        "SELECT channel, state, COUNT(*) n FROM outreach GROUP BY channel, state")}
    contacts = {r["persona"]: r["n"] for r in db.conn.execute("SELECT persona, COUNT(*) n FROM contacts GROUP BY persona")}
    held = db.conn.execute("SELECT COUNT(*) FROM applications WHERE hold_until IS NOT NULL AND hold_until>?",
                           (utcnow().isoformat(),)).fetchone()[0]
    return {"limits": limits_status(s, db), "outreach": by, "contacts": contacts, "applications_on_referral_hold": held}
