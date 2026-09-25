"""SQLite store. Idempotency lives here: unique keys on jobs, one application per job,
and a state machine where `submitting` is never retried automatically (a crash mid-submit
needs a human to check the portal — the same rule as never replaying an unconfirmed order)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import STATES, Job, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  key            TEXT PRIMARY KEY,           -- source:source_job_id
  source         TEXT NOT NULL,
  source_job_id  TEXT NOT NULL,
  company        TEXT NOT NULL,
  company_canon  TEXT NOT NULL,
  title          TEXT NOT NULL,
  title_canon    TEXT NOT NULL,
  url            TEXT NOT NULL,
  url_canon      TEXT NOT NULL,
  apply_url      TEXT,
  apply_url_canon TEXT,
  location       TEXT,
  remote         INTEGER,
  description    TEXT,
  desc_sha       TEXT,
  posted_at      TEXT,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  salary_text    TEXT,
  department     TEXT,
  raw            TEXT,
  -- triage
  triage_status  TEXT,                       -- eligible | rejected | review
  triage_reasons TEXT,                       -- json list
  fit_score      REAL,
  resume_variant TEXT,
  exp_min        REAL,
  exp_max        REAL,
  salary_lpa_min REAL,
  salary_lpa_max REAL,
  salary_basis   TEXT,                       -- posted | company_band | unknown
  cluster_id     TEXT,                       -- dedup cluster (same role across portals)
  cluster_winner INTEGER DEFAULT 0,
  triaged_at     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_canon);
CREATE INDEX IF NOT EXISTS jobs_cluster ON jobs(cluster_id);
CREATE INDEX IF NOT EXISTS jobs_urlc ON jobs(url_canon);

CREATE TABLE IF NOT EXISTS applications (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key        TEXT NOT NULL UNIQUE REFERENCES jobs(key),
  cluster_id     TEXT,
  company_canon  TEXT NOT NULL,
  state          TEXT NOT NULL,
  approved       INTEGER DEFAULT 0,
  review_reasons TEXT,                       -- json list: why it needs a human
  resume_variant TEXT,
  resume_sha256  TEXT,
  answers        TEXT,                       -- json: label -> {value, provenance}
  overrides      TEXT,                       -- json: label -> value set during review
  verification   TEXT,                       -- json report
  proof_dir      TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  submitted_at   TEXT,
  confirmed_at   TEXT,
  confirmation   TEXT,                       -- how: page_text | email | manual
  error          TEXT,
  outcome        TEXT                        -- later: rejected | interview | offer | ghosted
);
-- One live application per dedup cluster. Only a mechanical failure before any submit
-- (fill_failed) frees the slot so the runner-up portal can be tried; a human skip is final.
CREATE UNIQUE INDEX IF NOT EXISTS app_cluster ON applications(cluster_id)
  WHERE cluster_id IS NOT NULL AND state <> 'fill_failed';
CREATE INDEX IF NOT EXISTS app_company ON applications(company_canon, submitted_at);

-- ---------------- outreach (assisted: jobpilot drafts, you press Send) ----------------
CREATE TABLE IF NOT EXISTS contacts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  company        TEXT,
  company_canon  TEXT,
  title          TEXT,
  persona        TEXT,                       -- connection | hiring_team | recruiter | founder | eng_manager | engineer
  linkedin_url   TEXT,
  email          TEXT,
  source         TEXT NOT NULL,              -- connections_csv | hiring_team | apollo_csv | manual
  is_connection  INTEGER DEFAULT 0,
  connected_on   TEXT,
  added_at       TEXT NOT NULL,
  UNIQUE(linkedin_url),
  UNIQUE(email)
);
CREATE INDEX IF NOT EXISTS contacts_company ON contacts(company_canon);

CREATE TABLE IF NOT EXISTS outreach (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id     INTEGER NOT NULL REFERENCES contacts(id),
  company_canon  TEXT NOT NULL,
  job_key        TEXT REFERENCES jobs(key),
  channel        TEXT NOT NULL,              -- linkedin | email
  kind           TEXT NOT NULL,              -- referral_ask | hiring_manager | founder | recruiter
  state          TEXT NOT NULL,              -- drafted | invited | messaged | accepted | replied | referred | followed_up | skipped | withdrawn
  note           TEXT,                       -- connection note (no links)
  message        TEXT,                       -- follow-up / DM with resume link
  email_subject  TEXT,
  email_body     TEXT,
  checks         TEXT,                       -- json verification report
  created_at     TEXT NOT NULL,
  sent_at        TEXT,
  accepted_at    TEXT,
  replied_at     TEXT,
  followup_at    TEXT,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outreach_company ON outreach(company_canon);
CREATE INDEX IF NOT EXISTS outreach_state ON outreach(state, sent_at);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  app_id    INTEGER,
  job_key   TEXT,
  kind      TEXT NOT NULL,
  detail    TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(applications)")}
        for col, typ in (("hold_until", "TEXT"), ("hold_reason", "TEXT"), ("referrer", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {typ}")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---------- jobs ----------
    def upsert_job(self, job: Job, company_canon: str, title_canon: str, url_canon: str, apply_url_canon: str) -> bool:
        """Insert or refresh a job. Returns True if new."""
        now = utcnow().isoformat()
        row = job.to_row()
        cur = self.conn.execute("SELECT key FROM jobs WHERE key=?", (job.key,)).fetchone()
        if cur:
            self.conn.execute(
                "UPDATE jobs SET last_seen_at=?, description=COALESCE(NULLIF(?,''),description), "
                "apply_url=COALESCE(NULLIF(?,''),apply_url), apply_url_canon=COALESCE(NULLIF(?,''),apply_url_canon), "
                "posted_at=COALESCE(?,posted_at), salary_text=COALESCE(NULLIF(?,''),salary_text), "
                "raw=COALESCE(NULLIF(NULLIF(?,'{}'),''),raw) WHERE key=?",
                (now, row["description"], row["apply_url"], apply_url_canon, row["posted_at"], row["salary_text"], row["raw"], job.key),
            )
            return False
        self.conn.execute(
            "INSERT INTO jobs(key,source,source_job_id,company,company_canon,title,title_canon,url,url_canon,apply_url,"
            "apply_url_canon,location,remote,description,desc_sha,posted_at,first_seen_at,last_seen_at,salary_text,department,raw) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.key, job.source, job.source_job_id, job.company, company_canon, job.title, title_canon, job.url, url_canon,
             job.apply_url, apply_url_canon, job.location, None if job.remote is None else int(job.remote), job.description,
             row["desc_sha"], row["posted_at"], now, now, job.salary_text, job.department, row["raw"]),
        )
        return True

    def jobs(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM jobs WHERE {where}", tuple(params)).fetchall()

    def set_triage(self, key: str, **fields: Any) -> None:
        fields["triaged_at"] = utcnow().isoformat()
        if "triage_reasons" in fields and not isinstance(fields["triage_reasons"], str):
            fields["triage_reasons"] = json.dumps(fields["triage_reasons"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE key=?", (*fields.values(), key))

    # ---------- applications ----------
    def app(self, app_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()

    def apps(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT a.*, j.title, j.company, j.source, j.url, j.apply_url, j.fit_score, j.posted_at "
            f"FROM applications a JOIN jobs j ON j.key=a.job_key WHERE {where} ORDER BY a.id", tuple(params)
        ).fetchall()

    def enqueue(self, job_key: str, cluster_id: str, company_canon: str, resume_variant: str) -> Optional[int]:
        now = utcnow().isoformat()
        try:
            cur = self.conn.execute(
                "INSERT INTO applications(job_key,cluster_id,company_canon,state,resume_variant,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (job_key, cluster_id, company_canon, "queued", resume_variant, now, now))
        except sqlite3.IntegrityError:
            return None  # job or its cluster already has an application → dedup held at the DB layer
        self.event(cur.lastrowid, job_key, "enqueued", {"resume": resume_variant})
        return cur.lastrowid

    def transition(self, app_id: int, to: str, **fields: Any) -> None:
        with self.tx():
            row = self.conn.execute("SELECT state, job_key FROM applications WHERE id=?", (app_id,)).fetchone()
            if row is None:
                raise KeyError(app_id)
            frm = row["state"]
            if to not in STATES[frm]:   # self-transitions are illegal too: that's what stops two runners claiming one app
                raise ValueError(f"illegal transition {frm} -> {to} for app {app_id}")
            fields = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
            if to == "needs_review":
                fields["approved"] = 0   # an approval covers one reviewed version of the form, never a later one
            fields["state"] = to
            fields["updated_at"] = utcnow().isoformat()
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE applications SET {cols} WHERE id=?", (*fields.values(), app_id))
            self.conn.execute("INSERT INTO events(at,app_id,job_key,kind,detail) VALUES(?,?,?,?,?)",
                              (utcnow().isoformat(), app_id, row["job_key"], f"{frm}->{to}", None))

    def update_app(self, app_id: int, **fields: Any) -> None:
        fields = {k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
        fields["updated_at"] = utcnow().isoformat()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE applications SET {cols} WHERE id=?", (*fields.values(), app_id))

    def event(self, app_id: Optional[int], job_key: Optional[str], kind: str, detail: Any = None) -> None:
        self.conn.execute("INSERT INTO events(at,app_id,job_key,kind,detail) VALUES(?,?,?,?,?)",
                          (utcnow().isoformat(), app_id, job_key, kind, json.dumps(detail, default=str) if detail else None))

    # ---------- dedup helpers ----------
    def company_recently_applied(self, company_canon: str, days: int) -> Optional[sqlite3.Row]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        return self.conn.execute(
            "SELECT a.*, j.title FROM applications a JOIN jobs j ON j.key=a.job_key WHERE a.company_canon=? AND "
            "a.state IN ('submitting','submitted','unconfirmed','confirmed') AND COALESCE(a.submitted_at,a.updated_at)>=? "
            "ORDER BY a.submitted_at DESC LIMIT 1", (company_canon, since)).fetchone()

    def company_has_open_app(self, company_canon: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM applications WHERE company_canon=? AND state NOT IN ('skipped','fill_failed','submit_failed')",
            (company_canon,)).fetchone() is not None

    def submits_today(self, source: Optional[str] = None, auto_only: bool = False) -> int:
        ist = timezone(timedelta(hours=5, minutes=30))
        start = datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        q = ("SELECT COUNT(*) FROM applications a JOIN jobs j ON j.key=a.job_key WHERE a.submitted_at>=? "
             "AND a.state IN ('submitting','submitted','unconfirmed','confirmed')")
        params: list[Any] = [start]
        if source:
            q += " AND j.source=?"
            params.append(source)
        if auto_only:
            q += " AND a.approved=0"
        return self.conn.execute(q, params).fetchone()[0]
