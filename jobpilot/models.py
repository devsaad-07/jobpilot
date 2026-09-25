from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    source: str                    # greenhouse | lever | ashby | workable | linkedin | naukri | instahyre | ...
    source_job_id: str
    company: str
    title: str
    url: str                       # posting page on the source
    apply_url: str = ""            # where the form lives (may be an ATS URL for a LinkedIn/Naukri job)
    location: str = ""
    remote: Optional[bool] = None
    description: str = ""          # plain text
    posted_at: Optional[datetime] = None
    salary_text: str = ""
    department: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_job_id}"

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["raw"] = json.dumps(self.raw, default=str)[:200_000]
        d["key"] = self.key
        d["desc_sha"] = hashlib.sha256(self.description.encode()).hexdigest()[:16]
        return d


# Application states. Transitions are enforced in db.transition().
STATES = {
    "queued":        {"filling", "skipped"},
    "filling":       {"verified", "needs_review", "fill_failed", "queued"},
    "verified":      {"submitting", "needs_review", "queued", "filling"},
    "needs_review":  {"approved", "skipped", "queued", "filling"},
    "approved":      {"filling"},
    "fill_failed":   {"queued", "skipped"},
    "submitting":    {"submitted", "unconfirmed", "submit_failed"},   # never auto-retried
    "submitted":     {"confirmed"},
    "unconfirmed":   {"confirmed", "submitted", "submit_failed"},
    "submit_failed": {"queued", "skipped"},
    "confirmed":     set(),
    "skipped":       {"queued"},
}
