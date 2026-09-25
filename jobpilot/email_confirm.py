"""Match confirmation / outcome emails to applications over IMAP (Gmail app password).

- "application received" mails confirm submitted/unconfirmed applications (strongest proof;
  catches silent failures when nothing arrives)
- rejection / interview mails set `outcome`, feeding the per-portal / per-resume response stats
Every matched message is saved into the application's proof folder as .eml and the manifest resealed.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

from .config import Settings
from .db import DB
from .llm.laya import Laya, answer_choice
from .normalize import canon_company, fold
from .proof import seal

log = logging.getLogger(__name__)

CONFIRM = re.compile(r"thank(s| you) for (applying|your application|your interest|submitting)|application (was |has been )?"
                     r"(received|submitted)|we(.ve| have) received your application|successfully applied|your application (to|for)", re.I)
REJECT = re.compile(r"unfortunately|not (to )?(move|moving) forward|decided to (pursue|proceed with) other|other candidates|"
                    r"regret to inform|position has been filled|will not be progressing", re.I)
INTERVIEW = re.compile(r"\binterview\b|schedule (a|some) (time|call|chat)|next steps|your availability|calendly\.com|"
                       r"assessment|coding (test|challenge)|hackerrank|codesignal", re.I)
ATS_SENDERS = re.compile(r"greenhouse|lever\.co|ashbyhq|workable|myworkday|smartrecruiters|naukri|linkedin|instahyre|"
                         r"cutshort|wellfound|hirist|weekday|weworkremotely|remoteok|remotive|himalayas|icims|jobvite|recruitee", re.I)


def _h(v: Optional[str]) -> str:
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _body(msg: email.message.Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    out = []
    for p in parts:
        if p.get_content_type() in ("text/plain", "text/html") and not p.get_filename():
            try:
                t = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            out.append(re.sub(r"<[^>]+>", " ", t) if p.get_content_type() == "text/html" else t)
    return re.sub(r"\s+", " ", " ".join(out))[:20000]


def classify(subject: str, body: str, laya: Optional[Laya] = None) -> str:
    text = f"{subject}\n{body[:3000]}"
    r, i, c = bool(REJECT.search(text)), bool(INTERVIEW.search(text)), bool(CONFIRM.search(text))
    if r:
        return "rejected"
    if i and not c:
        return "interview"
    if c:
        return "confirmation"
    if laya and laya.available():
        lab, p = answer_choice(laya.predict(text, {"kind": {"type": "choice", "instructions": "What is this email about a job application?",
                                                             "criteria": ["confirmation", "rejected", "interview", "unrelated"]}}), "kind")
        if lab and p >= 0.6:
            return lab
    return "unrelated"


def run(s: Settings, db: DB) -> dict:
    ec = s.cfg["email"]
    user, pw = os.environ.get(ec["imap_user_env"]), os.environ.get(ec["imap_password_env"])
    if not (user and pw):
        return {"skipped": f"set {ec['imap_user_env']} and {ec['imap_password_env']} in .env"}
    apps = [dict(a) for a in db.apps("a.state IN ('submitted','unconfirmed','confirmed')")]
    if not apps:
        return {"apps": 0}
    lc = s.cfg["llm"]["laya"]
    laya = Laya(lc["model"], lc.get("dtype", "float16"), lc.get("enabled", True))
    since = (datetime.now(timezone.utc) - timedelta(days=ec["lookback_days"])).strftime("%d-%b-%Y")
    stats = {"scanned": 0, "confirmed": 0, "outcomes": 0}
    M = imaplib.IMAP4_SSL(ec["imap_host"])
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"' if "gmail" in ec["imap_host"] else "INBOX", readonly=True)
    _, data = M.search(None, f'(SINCE "{since}")')
    ids = data[0].split()
    seen_file = s.path("data_dir") / "email_seen.txt"
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    for num in ids[-2000:]:
        _, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
        h = email.message_from_bytes(hdr[0][1])
        mid = (h.get("Message-ID") or num.decode()).strip()
        if mid in seen:
            continue
        frm, subj = _h(h.get("From")), _h(h.get("Subject"))
        if not (ATS_SENDERS.search(frm) or re.search(r"application|applying|candidate|interview|position|role", subj, re.I)):
            continue
        _, full = M.fetch(num, "(BODY.PEEK[])")
        raw = full[0][1]
        msg = email.message_from_bytes(raw)
        body = _body(msg)
        stats["scanned"] += 1
        try:
            when = parsedate_to_datetime(h.get("Date")).astimezone(timezone.utc)
        except Exception:
            when = datetime.now(timezone.utc)
        kind = classify(subj, body, laya)
        if kind == "unrelated":
            continue
        app = _match(apps, frm, subj, body, when, ec["match_window_hours"] if kind == "confirmation" else 24 * 90)
        if not app:
            continue
        d = Path(app["proof_dir"])
        name = "confirmation.eml" if kind == "confirmation" else f"{kind}-{when:%Y%m%d%H%M}.eml"
        (d / name).write_bytes(raw)
        if kind == "confirmation" and app["state"] in ("submitted", "unconfirmed"):
            db.transition(app["id"], "confirmed", confirmed_at=when.isoformat(),
                          confirmation=f"email: {subj[:150]}")
            app["state"] = "confirmed"
            stats["confirmed"] += 1
        elif kind in ("rejected", "interview"):
            db.update_app(app["id"], outcome=kind)
            stats["outcomes"] += 1
        seal(d, {"app_id": app["id"], "resealed_for": name})
        seen.add(mid)
    M.logout()
    seen_file.write_text("\n".join(seen))
    return stats


def _match(apps: list[dict], frm: str, subj: str, body: str, when: datetime, window_h: float) -> Optional[dict]:
    name, addr = parseaddr(frm)
    hay = fold(f"{name} {addr} {subj} {body[:4000]}")
    hay_c = re.sub(r"[^a-z0-9]", "", hay)
    best, best_score = None, 0.0
    for a in apps:
        sub = datetime.fromisoformat(a["submitted_at"]) if a["submitted_at"] else None
        if sub and not (sub - timedelta(hours=1) <= when <= sub + timedelta(hours=window_h)):
            continue
        cc = a["company_canon"]
        score = 0.0
        if cc and len(cc) >= 3 and cc in hay_c:
            score += 2
        if cc and cc in re.sub(r"[^a-z0-9]", "", addr.split("@")[-1].lower()):
            score += 1
        t = fold(a["title"])
        if t and t in hay:
            score += 1.5
        elif t and sum(w in hay for w in t.split()) >= max(2, len(t.split()) - 1):
            score += 0.75
        if score > best_score:
            best, best_score = a, score
    return best if best_score >= 2 else None
