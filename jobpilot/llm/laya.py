"""Laya-MLX typed decisions (choice / score / noul) for bulk, local classification.

Laya answers constrained questions in one forward pass — ideal for triage (hundreds of JDs, all
local, ~10 ms each). Context is 512–1,024 tokens, so callers pass a *trimmed* JD: title,
location, and the requirements section (see `trim_jd`). When Laya isn't installed (e.g. not on
Apple Silicon) `available()` is False and triage falls back to keyword heuristics.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any, Optional

log = logging.getLogger(__name__)
_MAX_CHARS = 3200  # ≈ 800 tokens for ModernBERT; leaves room for questions and options


@lru_cache(maxsize=1)
def _agent(model: str, dtype: str):
    try:
        import laya_mlx as laya  # type: ignore
    except Exception as e:  # pragma: no cover - platform dependent
        log.info("laya-mlx unavailable (%s); using heuristics", e)
        return None
    try:
        return laya.load(model, dtype=dtype, batch_size=16)
    except Exception as e:  # pragma: no cover
        log.warning("laya-mlx load failed: %s", e)
        return None


class Laya:
    def __init__(self, model: str, dtype: str = "float16", enabled: bool = True):
        self.model, self.dtype, self.enabled = model, dtype, enabled

    def available(self) -> bool:
        return self.enabled and _agent(self.model, self.dtype) is not None

    def predict(self, state: Any, questions: dict[str, dict]) -> Optional[dict]:
        if not self.available():
            return None
        if isinstance(state, str) and len(state) > _MAX_CHARS:
            state = state[:_MAX_CHARS]
        try:
            return _agent(self.model, self.dtype).predict(state, questions)["answers"]
        except Exception as e:  # pragma: no cover
            log.warning("laya predict failed: %s", e)
            return None


_REQ_HEAD = re.compile(r"(requirements|qualifications|what you.?ll need|what we.?re looking for|who you are|you have|"
                       r"must have|minimum qualifications|about you|skills|experience)", re.I)


def trim_jd(title: str, location: str, description: str, limit: int = _MAX_CHARS) -> str:
    """Title + location + the requirements section (falls back to the head of the JD)."""
    d = re.sub(r"\s+", " ", description or "")
    m = _REQ_HEAD.search(d)
    body = d[m.start(): m.start() + limit] if m and m.start() > 300 else d[:limit]
    return f"Role: {title}\nLocation: {location}\n{body}"[:limit]


def answer_choice(ans: Optional[dict], key: str) -> tuple[Optional[str], float]:
    """Normalise a Laya choice answer to (label, probability) regardless of minor schema drift."""
    if not ans or key not in ans:
        return None, 0.0
    a = ans[key]
    if isinstance(a, dict):
        label = a.get("choice") or a.get("label") or a.get("answer")
        probs = a.get("probabilities") or a.get("probs") or {}
        if label is None and probs:
            label = max(probs, key=probs.get)
        return label, float(probs.get(label, a.get("confidence", 0.0)) if probs else a.get("confidence", 0.0))
    return str(a), 1.0


def answer_score(ans: Optional[dict], key: str, levels: int) -> Optional[float]:
    """Expected rubric level normalised to 0..1."""
    if not ans or key not in ans:
        return None
    a = ans[key]
    v = a.get("expected_score", a.get("score", a.get("expected"))) if isinstance(a, dict) else a
    try:
        return max(0.0, min(1.0, float(v) / (levels - 1)))
    except (TypeError, ValueError):
        return None


def answer_noul(ans: Optional[dict], key: str) -> Optional[float]:
    if not ans or key not in ans:
        return None
    a = ans[key]
    v = a.get("noul", a.get("p_true")) if isinstance(a, dict) else a
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
