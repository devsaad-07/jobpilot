"""Config loading. All YAML lives in ./config; paths resolve relative to the project root."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("JOBPILOT_ROOT", Path(__file__).resolve().parent.parent))
FILL_ME = "FILL_ME"


def _load(name: str) -> dict:
    p = ROOT / "config" / name
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Settings:
    cfg: dict
    profile: dict
    answers: list[dict]
    companies: list[dict]
    portals: dict
    root: Path = ROOT
    _paths: dict = field(default_factory=dict)

    def path(self, key: str) -> Path:
        p = Path(self.cfg["paths"][key])
        p = p if p.is_absolute() else (self.root / p)
        p.mkdir(parents=True, exist_ok=True) if key.endswith("_dir") or key == "browser_profile" else None
        return p

    @property
    def mode(self) -> str:
        return self.cfg.get("mode", "shadow")

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def profile_value(self, dotted: str) -> Any:
        cur: Any = self.profile
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(f"profile.yaml has no '{dotted}'")
            cur = cur[part]
        return cur

    def unfilled_profile_keys(self) -> list[str]:
        out: list[str] = []

        def walk(d: Any, prefix: str) -> None:
            if isinstance(d, dict):
                for k, v in d.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif d == FILL_ME:
                out.append(prefix)

        walk(self.profile, "")
        return out

    def resolve_template(self, value: Any, resume_file: str | None = None) -> Any:
        """Expand '{a.b}' references into profile values; '{resume_file}' into the chosen resume."""
        if not isinstance(value, str):
            return value
        m = re.fullmatch(r"\{([\w.]+)\}", value.strip())
        if m:
            key = m.group(1)
            if key == "resume_file":
                return resume_file
            return self.profile_value(key)
        return re.sub(r"\{([\w.]+)\}", lambda mm: str(self.profile_value(mm.group(1))), value)


def load_settings() -> Settings:
    _load_dotenv()
    cfg = _load("config.yaml")
    comp = _load("companies.yaml")
    defaults = comp.get("defaults", {})
    companies = [{**defaults, **c} for c in comp.get("companies", [])]
    return Settings(
        cfg=cfg,
        profile=_load("profile.yaml"),
        answers=_load("answers.yaml").get("rules", []),
        companies=companies,
        portals=_load("portals.yaml"),
    )
