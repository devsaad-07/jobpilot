"""Terminal review queue for applications that need a human.

For each one: why it's here, what would be sent (every field with its provenance), and the
pre-submit screenshot. Approving re-runs fill + verify at apply time; verification must pass
again before the submit, so an approval can't push a broken form through.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .db import DB


def _open(path: str) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print(f"  (open manually: {path})")


def show(a) -> None:
    print("\n" + "=" * 100)
    print(f"#{a['id']}  {a['company']} — {a['title']}   [{a['source']}]  fit={a['fit_score']:.2f}  posted={a['posted_at'] or '?'}")
    print(f"    {a['url']}")
    for r in json.loads(a["review_reasons"] or "[]"):
        print(f"  ! {r}")
    ver = json.loads(a["verification"] or "{}")
    for b in ver.get("blocks", []):
        print(f"  ✗ {b}")
    for w in ver.get("warns", []):
        print(f"  ~ {w}")
    ans = json.loads(a["answers"] or "{}")
    if ans:
        print("  fields (read back from the page):")
        for k, v in ans.items():
            prov = v["provenance"]
            flag = "  <-- LLM" if str(prov).startswith("llm:") else ""
            print(f"    {v['label'][:60]:60} = {str(v['value'])[:70]!r:72} [{prov}]{flag}")
            if flag:
                print(f"      key: {k}")


def run(db: DB) -> None:
    apps = db.apps("a.state = 'needs_review'")
    if not apps:
        print("Review queue is empty.")
        return
    print(f"{len(apps)} application(s) need review.  a=approve  e=edit a field  s=skip  r=requeue  p=screenshot  j=open posting  n=next  q=quit")
    for a in apps:
        show(a)
        shot = Path(a["proof_dir"] or ".") / "pre_submit.png"
        while True:
            c = input("  > ").strip().lower()
            if c == "a":
                # freeze exactly what you reviewed: LLM-written answers become overrides, so the re-fill
                # can't produce different unreviewed text (any new LLM answer sends it back to review)
                ov = json.loads(db.app(a["id"])["overrides"] or "{}")
                for k, v in json.loads(a["answers"] or "{}").items():
                    if str(v["provenance"]).startswith("llm:") and k not in ov:
                        ov[k] = v["value"]
                db.transition(a["id"], "approved")
                db.update_app(a["id"], approved=1, overrides=ov)
                print("  approved — will re-fill, re-verify and submit on the next `jobpilot apply`.")
                break
            if c == "e":
                key = input("    field key (from the list above): ").strip()
                val = input("    value: ")
                ov = json.loads(db.app(a["id"])["overrides"] or "{}")
                ov[key] = val
                db.update_app(a["id"], overrides=ov)
                print("    saved override. Approve or requeue to apply it.")
            elif c == "s":
                db.transition(a["id"], "skipped")
                break
            elif c == "r":
                db.transition(a["id"], "queued")
                print("  requeued (fix profile.yaml / answers.yaml first if verification blocked).")
                break
            elif c == "p":
                _open(str(shot)) if shot.exists() else print("  no screenshot")
            elif c == "j":
                _open(a["url"])
            elif c == "n":
                break
            elif c == "q":
                return
