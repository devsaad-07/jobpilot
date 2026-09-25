"""Static HTML dashboard: data/report.html (open locally; links point at proof folders)."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from .config import Settings
from .db import DB

CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--mute:#78716c;--line:#e7e5e4;--card:#fff;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--acc:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--mute:#a8a29e;--line:#44403c;--card:#292524;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--acc:#93c5fd}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:1200px;margin:auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.mute{color:var(--mute)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.tile b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--mute)}
.s-confirmed,.s-submitted{color:var(--ok)}.s-needs_review,.s-unconfirmed,.s-verified{color:var(--warn)}.s-fill_failed,.s-submit_failed{color:var(--bad)}
a{color:var(--acc)}.wrap{overflow-x:auto}
"""


def build(s: Settings, db: DB) -> Path:
    jobs = db.jobs()
    apps = [dict(a) for a in db.apps()]
    tri = Counter(j["triage_status"] or "untriaged" for j in jobs)
    st = Counter(a["state"] for a in apps)
    reasons, only = Counter(), Counter()
    for j in jobs:
        if j["triage_status"] == "rejected":
            fails = [g["gate"] for g in json.loads(j["triage_reasons"] or "[]") if g["verdict"] == "fail"]
            reasons.update(fails)
            if len(fails) == 1:
                only[fails[0]] += 1       # jobs that would pass if only this gate were loosened
    by_src: dict[str, Counter] = defaultdict(Counter)
    by_resume: dict[str, Counter] = defaultdict(Counter)
    for a in apps:
        sent = a["state"] in ("submitted", "confirmed", "unconfirmed")
        for bucket, k in ((by_src, a["source"]), (by_resume, a["resume_variant"])):
            bucket[k]["sent"] += sent
            bucket[k]["interview"] += a["outcome"] == "interview"
            bucket[k]["rejected"] += a["outcome"] == "rejected"
    unverified = [a for a in apps if a["state"] in ("submitted", "confirmed", "unconfirmed")]
    band_rows = db.conn.execute("SELECT company, COUNT(*) n FROM jobs WHERE salary_basis='company_band' AND cluster_winner=1 GROUP BY company").fetchall()
    e = html.escape
    tiles = "".join(f'<div class="tile"><span class="mute">{e(k)}</span><b>{v}</b></div>' for k, v in [
        ("jobs seen", len(jobs)), ("eligible", tri.get("eligible", 0)), ("review", tri.get("review", 0)),
        ("rejected", tri.get("rejected", 0)), ("queued", st.get("queued", 0)), ("needs review", st.get("needs_review", 0)),
        ("submitted", st.get("submitted", 0) + st.get("unconfirmed", 0)), ("confirmed", st.get("confirmed", 0))])
    rej = "".join(f"<tr><td>{e(k)}</td><td>{v}</td><td>{only.get(k, 0)}</td></tr>" for k, v in reasons.most_common())

    def stat_rows(d):
        return "".join(f"<tr><td>{e(str(k))}</td><td>{c['sent']}</td><td>{c['interview']}</td><td>{c['rejected']}</td>"
                       f"<td>{(c['interview'] / c['sent'] * 100 if c['sent'] else 0):.0f}%</td></tr>" for k, c in sorted(d.items()))
    rows = []
    for a in sorted(apps, key=lambda x: x["updated_at"], reverse=True):
        pd = a["proof_dir"]
        link = f'<a href="file://{e(pd)}">proofs</a>' if pd else ""
        rr = "; ".join(json.loads(a["review_reasons"] or "[]"))[:220]
        rows.append(f'<tr><td>{a["id"]}</td><td class="s-{a["state"]}">{e(a["state"])}</td><td>{e(a["company"])}</td>'
                    f'<td><a href="{e(a["url"])}">{e(a["title"])}</a></td><td>{e(a["source"])}</td><td>{e(a["resume_variant"] or "")}</td>'
                    f'<td>{(a["fit_score"] or 0):.2f}</td><td>{e((a["submitted_at"] or "")[:16])}</td><td>{e(a["outcome"] or "")}</td>'
                    f'<td>{link}</td><td class="mute">{e(rr)}</td></tr>')
    orows = db.conn.execute(
        "SELECT channel, kind, SUM(state='drafted') d, SUM(sent_at IS NOT NULL) s, SUM(accepted_at IS NOT NULL) a, "
        "SUM(replied_at IS NOT NULL) r FROM outreach GROUP BY channel, kind").fetchall()
    outreach_rows = "".join(f"<tr><td>{e(r['channel'])}</td><td>{e(r['kind'])}</td><td>{r['d']}</td><td>{r['s']}</td>"
                            f"<td>{r['a']}</td><td>{r['r']}</td></tr>" for r in orows) or "<tr><td colspan=6>none yet</td></tr>"
    from .models import utcnow
    held = db.conn.execute("SELECT COUNT(*) FROM applications WHERE hold_until>?", (utcnow().isoformat(),)).fetchone()[0]
    bands = "".join(f"<li>{e(r['company'])} ({r['n']})</li>" for r in band_rows)
    body = f"""<main><h1>jobpilot</h1><div class="mute">mode: <b>{e(s.mode)}</b> · generated from {e(str(db.path))}</div>
<h2>Funnel</h2><div class="grid">{tiles}</div>
<h2>Why jobs were rejected</h2><div class="wrap"><table><tr><th>gate</th><th>jobs failing it</th><th>failing only this gate</th></tr>{rej}</table></div>
<h2>Response rate by portal</h2><div class="wrap"><table><tr><th>portal</th><th>sent</th><th>interview</th><th>rejected</th><th>interview rate</th></tr>{stat_rows(by_src)}</table></div>
<h2>Response rate by resume</h2><div class="wrap"><table><tr><th>resume</th><th>sent</th><th>interview</th><th>rejected</th><th>interview rate</th></tr>{stat_rows(by_resume)}</table></div>
<h2>Salary gate decided by company band</h2><p class="mute">Check these bands in config/companies.yaml (set band_verified: true once checked).</p><ul>{bands or '<li>none</li>'}</ul>
<h2>Outreach</h2><div class="wrap"><table><tr><th>channel</th><th>kind</th><th>drafted</th><th>sent</th><th>accepted</th><th>replied / referred</th></tr>{outreach_rows}</table></div>
<p class="mute">{held} application(s) waiting on a referral hold.</p>
<h2>Applications</h2><div class="wrap"><table><tr><th>#</th><th>state</th><th>company</th><th>role</th><th>via</th><th>resume</th><th>fit</th><th>submitted</th><th>outcome</th><th></th><th>notes</th></tr>{''.join(rows)}</table></div>
</main>"""
    out = s.path("data_dir") / "report.html"
    out.write_text(f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                   f"<title>jobpilot report</title><style>{CSS}</style></head><body>{body}</body></html>")
    return out
