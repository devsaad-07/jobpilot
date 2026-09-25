#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon). Re-runnable.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
pip install 'git+https://github.com/speedyapply/JobSpy' || pip install -U python-jobspy   # HEAD has newer Naukri/LinkedIn fixes
pip install laya-mlx || echo "laya-mlx not installed (needs Apple Silicon, macOS 14+, Python 3.11+) — triage falls back to keywords"
python -m playwright install chromium   # fallback browser; config uses your installed Google Chrome first
python - <<'PY'
try:
    import laya_mlx as laya
    laya.load("aac6fef/laya-typed-decisions-mlx")   # downloads the checkpoint once
    print("laya checkpoint ready")
except Exception as e:
    print("laya skipped:", e)
PY
[ -f .env ] || cp .env.example .env
# resume text used by the answerer/verifier (already shipped; regenerate if you edit the PDFs)
if command -v pdftotext >/dev/null; then for f in resumes/*.pdf; do pdftotext "$f" "${f%.pdf}.txt"; done; fi
command -v codex >/dev/null || echo "NOTE: codex CLI not on PATH — install it and run 'codex login' for screening answers + verification judge"
echo
echo "Next: fill FILL_ME in config/profile.yaml, then:"
echo "  jobpilot doctor && jobpilot probe-ats && jobpilot login"
