"""`codex exec` wrapper with JSON-schema-constrained output.

Used for the two jobs that need real reasoning: answering screening questions that the answer
bank doesn't cover (with provenance), and the semantic verification judge over a filled form.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class Codex:
    def __init__(self, binary: str = "codex", timeout_s: int = 180, extra_args: list[str] | None = None, enabled: bool = True):
        self.binary, self.timeout_s, self.extra_args, self.enabled = binary, timeout_s, extra_args or [], enabled

    def available(self) -> bool:
        return self.enabled and shutil.which(self.binary) is not None

    def run(self, prompt: str, schema: dict, images: list[Path] | None = None) -> Optional[dict]:
        if not self.available():
            return None
        with tempfile.TemporaryDirectory() as td:
            sp, op = Path(td) / "schema.json", Path(td) / "out.json"
            sp.write_text(json.dumps(schema))
            cmd = [self.binary, "exec", *self.extra_args, "--output-schema", str(sp), "-o", str(op)]
            for img in images or []:
                cmd += ["-i", str(img)]
            cmd.append("-")
            try:
                subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=self.timeout_s, cwd=td, check=True)
                return json.loads(op.read_text())
            except subprocess.CalledProcessError as e:
                log.warning("codex exec failed rc=%s: %s", e.returncode, (e.stderr or "")[-500:])
            except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
                log.warning("codex exec error: %s", e)
        return None


ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["answer", "provenance", "confidence", "evidence"],
    "properties": {
        "answer": {"type": "string"},
        "provenance": {"type": "string", "enum": ["profile", "resume", "inferred", "cannot_answer"]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
}

JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["ok", "issues"],
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["field", "severity", "problem"],
            "properties": {"field": {"type": "string"}, "severity": {"type": "string", "enum": ["block", "warn"]},
                           "problem": {"type": "string"}}}},
    },
}


def answer_prompt(question: str, field_type: str, options: list[str], profile: dict, resume_text: str, job: dict[str, Any]) -> str:
    opts = f"\nAllowed options (answer must be exactly one of these): {json.dumps(options)}" if options else ""
    return f"""You fill job application forms for the candidate below. Answer ONE form question.

Rules:
- Use only facts from PROFILE or RESUME. Never invent employers, numbers, dates, skills or credentials.
- provenance = "profile" or "resume" when the answer is directly stated there; "inferred" when you
  reasoned from them (e.g. a short motivation sentence); "cannot_answer" when the facts aren't there.
- For yes/no about skills: "Yes" only if the resume shows it.
- Free-text answers: at most 3 sentences, first person, plain, no hype, specific to the role.
- evidence: quote the profile key or resume phrase you used.

JOB: {job.get('company')} — {job.get('title')} ({job.get('location')})
QUESTION: {question}
FIELD TYPE: {field_type}{opts}

PROFILE (YAML-as-JSON):
{json.dumps(profile, default=str)[:6000]}

RESUME:
{resume_text[:9000]}
"""


def judge_prompt(fields: list[dict], profile: dict, job: dict[str, Any]) -> str:
    return f"""You are the final checker before a job application is submitted. Compare every filled field
with the candidate's PROFILE. Flag as "block": wrong identity/contact data, wrong numbers (experience,
CTC, notice), an answer contradicting the profile, an answer that is a placeholder/empty for a required
field, a fabricated claim, answers in the wrong field, or wrong resume file. Flag "warn" for awkward but
harmless answers. ok=true only if there are no "block" issues. A screenshot of the form may be attached.

JOB: {job.get('company')} — {job.get('title')}
FILLED FIELDS (label, value read back from the page, provenance):
{json.dumps(fields, default=str)[:12000]}

PROFILE:
{json.dumps(profile, default=str)[:6000]}
"""
