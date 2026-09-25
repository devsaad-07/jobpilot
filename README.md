# jobpilot

A personal pipeline for SDE-3 / senior backend applications. It finds postings, checks each one against your criteria, removes duplicates, fills the form, checks what was filled, submits under a tiered policy, and saves proof of every application. It runs from the command line on your Mac.

```
discover ─► triage (fit gates) ─► plan (dedup + rank + caps) ─► apply: fill ─► read back ─► verify ─► policy ─► submit ─► proof
   │                                                                                                   │
   └ ATS APIs · Workday · WWR/Remote OK/Remotive/Himalayas · JobSpy (LinkedIn/Naukri) · Instahyre/Cutshort/Wellfound/Hirist/Weekday   └► review queue
                                                                              confirm-emails (IMAP) ─► .eml proof + outcome
```

## Setup (once)

```bash
./scripts/setup.sh                 # venv, deps, Playwright, laya-mlx checkpoint, .env
git update-index --skip-worktree config/profile.yaml   # keep your real details out of commits
$EDITOR config/profile.yaml        # fill every FILL_ME (name, email, phone, LinkedIn, CTC, notice, ...)
$EDITOR config/companies.yaml      # check the comp bands (seeded values are ESTIMATES) and add target companies
$EDITOR .env                       # Gmail app password for confirmation matching
source .venv/bin/activate
jobpilot probe-ats                 # finds which ATS each company uses → data/ats_resolved.json
jobpilot login                     # opens a normal (non-automated) Chrome window on jobpilot's own profile; sign in to each portal once, then Cmd+Q
jobpilot doctor --portals          # checks config, tools, and that you're logged in to each portal
```

## Rollout (like a canary deploy)

`config.yaml → mode`:

| mode | what happens |
|---|---|
| `shadow` (default) | Everything runs, including fill, verify and proofs, but nothing is submitted. Each application's `decision.json` shows what live mode would have done. |
| `canary` | Live, but at most `canary_daily_auto_submits` (3) automatic submits a day. Approved applications still go through. |
| `live` | Tiered auto-submit, subject to the per-portal daily caps. |

Suggested path: a few days in `shadow` (read the report, open some `proofs/` folders, work through `jobpilot review`), then a week in `canary`, then `live`.

## Daily use

```bash
jobpilot run                 # discover → triage → plan → apply → confirm-emails → report
jobpilot review              # approve / edit / skip what needs you
jobpilot apply               # submits approved ones (fill and verify run again first)
open data/report.html        # funnel, rejection reasons, response rate per portal/resume, all applications
touch STOP                   # kill switch: halts any submit loop before its next click
```

To schedule it: `sed "s#__ROOT__#$PWD#g" scripts/co.peepal.jobpilot.plist > ~/Library/LaunchAgents/co.peepal.jobpilot.plist && launchctl load ~/Library/LaunchAgents/co.peepal.jobpilot.plist` (runs at 10:30, 14:30 and 18:30 IST).

If a portal shows a CAPTCHA: `jobpilot apply --id 42 --interactive` pauses so you can solve it in the browser. The script never tries to solve one itself.

## What decides an application

**Fit gates** (`triage.py`; thresholds are in `config.yaml → fit`), all of which must pass:

- **exclusions**: CoinSwitch, Lemonn, ImageKit, Clearfeed, Sahi, Alpaca, Neosapiens (matched by name and domain)
- **freshness**: posted within 21 days
- **title**: matches the senior backend / software / platform / full-stack / lead / SDE-3 family; staff, principal, manager, frontend, mobile, data, SRE, SDE-1 and similar are rejected
- **experience**: the JD's *minimum* must be ≤ 6 years ("5+", "4-8", "6+" pass; "7+", "8-12" fail). A JD whose *maximum* is under 5 ("1-3") also fails. If the regex can't find a requirement, Laya estimates P(needs 7+ years); if that's inconclusive, the job goes to review.
- **location**: an India city, or a remote role that is eligible from India. Remote roles locked to the US/EU, requiring relocation, or asking for US work authorization are rejected. EOR and contractor setups are fine.
- **salary**: ≥ 60 LPA. It uses the posted salary if there is one (LPA, crore, INR, USD→INR), otherwise the company band from `companies.yaml`, otherwise review.
- **fit score**: Laya rubric score combined with keyword overlap with your stack. If the JD is too short to score, the job goes to review.

**Resume**: Laya picks one of trading / platform / general, with keywords as a fallback, and attaches that PDF unchanged.

**Dedup** (`planner.py`):

- *Same role on several portals*: postings at the same company with near-identical titles (or the same apply URL) form one cluster, and only one is applied to. The winner is chosen by recency first. Postings within 72h of the freshest one count as equally recent, and among those the portal rank decides (direct ATS incl. Workday > LinkedIn > We Work Remotely > Himalayas > Remotive > Remote OK > Instahyre > Naukri > Wellfound > Cutshort > Weekday > Hirist). A LinkedIn "Apply" that redirects to an ATS form you've already used is caught at apply time.
- *Same company*: a company is skipped if you applied there in the last 90 days, and at most one new role per company is queued per run.
- The database enforces this too: one application per job, one live application per cluster.

**Verification** (`verify.py`) runs on the values **read back from the page** after filling. Anything below blocks the submit:

- a required field is empty, a fill errored, the attached file isn't the chosen resume, or the page shows a validation error
- name, email, phone, LinkedIn, current/expected CTC, notice or total experience differs from `profile.yaml` (bucketed options like "3-5 years" are range-checked)
- any `FILL_ME` or placeholder value
- an LLM free-text answer contains numbers that aren't in your resume or profile
- the Codex judge (when available) flags an issue from the filled fields plus a screenshot

**Auto-submit** (`policy.py`) needs *all* of these; anything else goes to review:

- the source is a direct ATS (Greenhouse / Lever / Ashby / Workable)
- fit ≥ 0.70 and salary known or banded
- every triage gate decided, verification passed, and no LLM-written answers
- within active hours (9–21 IST), under today's cap, no kill switch

LinkedIn, Naukri and the other portals always need your approval.

**Approvals** freeze what you reviewed: LLM answers you saw become fixed overrides. If the re-fill at submit time produces any new LLM answer, the application goes back to review, and approval is cleared whenever an application re-enters review.

**Proofs** (`proofs/<date>/<id>_<company>_<role>/`) hold: the JD as it was, `job.json` with every gate's verdict, the exact resume file, a screenshot of each step, the pre-submit screenshot and HTML, `fields.json` (intended vs read-back values with provenance), `verification.json`, `decision.json`, the post-submit screenshot and HTML, `network.har`, `confirmation.eml`, and `manifest.json` with a sha256 of every file.

## Models

| Tool | Used for | Without it |
|---|---|---|
| **Laya-MLX** (local, ~10 ms/decision) | experience/location checks the regexes can't settle, fit score, resume choice, email classification | keyword heuristics |
| **Codex** (`codex exec --output-schema`) | screening questions the answer bank doesn't cover (answer, provenance, evidence); the pre-submit judge | those fields go to review |
| **Antigravity** | development only: point its browser agent at a portal page to update `config/portals.yaml` selectors when `doctor --portals` reports them stale | n/a |

## Sources

| Source | How jobs are found | How it applies | Auto-submit? |
|---|---|---|---|
| Greenhouse, Lever, Ashby, Workable | public JSON job boards (`probe-ats` finds each company's board) | hosted form | yes, if every check passes |
| **Workday** (`*.myworkdayjobs.com`) | the career site's own JSON search endpoint, for companies with a `workday:` URL in `companies.yaml` | Apply → *Autofill with Resume* → multi-step wizard | never; always reviewed |
| **We Work Remotely** | public category RSS feeds (back-end, full-stack, devops) | "Apply for this position" opens the company's own form, which goes through the ATS route | never; you review it |
| **Remote OK** | public JSON API (`remoteok.com/api`, dev-tagged jobs) | its apply link opens the company's own form | never; you review it |
| **Remotive** | public API (`software-dev` category), one call per run and at most 4 per day, as its terms ask; listings arrive 24h late | apply link → company form | never; you review it |
| **Himalayas** | public search API with `country=IN` + `seniority=Senior`; jobs whose time-zone limits are > 2.5h from IST are dropped | goes straight to the company ATS when the feed gives that link, otherwise follows the listing's apply link | never; you review it |
| LinkedIn, Naukri | JobSpy search | Easy Apply / chatbot / external link | never |
| Instahyre, Cutshort, Wellfound, Hirist, **Weekday** | search pages in your logged-in browser | one-click Apply (+ optional dialog) or external link | never |

**Workday**: every company has its own candidate account, and jobpilot never stores those passwords. The first time you apply to a company it stops and asks you to sign in or create the account. Run `jobpilot apply --id N --interactive`, sign in once in the browser that opens, and the dedicated Chrome profile keeps the session for next time. Workday's *Autofill with Resume* parses your resume into the work-history and education sections. jobpilot leaves those entries as Workday filled them (so it never overwrites a past job title with your current one), lists every kept value in the review queue, and blocks the submit if a required entry came out empty.

Remote OK, Remotive and Himalayas ask for a link back and attribution when their data is shown. jobpilot keeps each job's original listing URL and source name in the report and proofs, and never republishes listings.

**Weekday** (weekday.works) is a recruiter/AI-sourcing platform. Its one-click apply sends your Weekday profile to its recruiters rather than straight to the company, which is why it ranks just above Hirist. Keep your CTC and notice period on your Weekday profile identical to `profile.yaml`.

## Outreach (referrals, hiring managers, founders)

jobpilot finds people and writes a message for each, but **you press Send**. It never sends a LinkedIn invite or message itself: LinkedIn's User Agreement (section 8.2) bans automating that, and it's the fastest way to get restricted.

```bash
jobpilot outreach import-connections ~/Downloads/Connections.csv   # LinkedIn → Settings → Data privacy → Get a copy of your data → Connections
jobpilot outreach import-contacts ~/Downloads/apollo-export.csv    # optional: Apollo (or any CSV with name/title/company/email/linkedin)
jobpilot outreach find        # reads "Meet the hiring team" on LinkedIn jobs you're applying to (≤ 20 pages/day, paced)
jobpilot outreach plan        # picks ≤ 2 people per company and drafts from resume facts only; each draft is checked
jobpilot outreach send        # opens each profile with the note on your clipboard; you send it and press s. Email → Gmail draft with resume attached
jobpilot outreach followups   # who accepted (send the message with your resume link), who needs one follow-up, which invites to withdraw
jobpilot outreach mark 12 referred   # records the referrer and releases that company's referral hold
```

- **Who gets contacted.** In priority order: your first-degree connections at the company, then the job's hiring team, then engineering managers, founders/CTOs, engineers, and last recruiters.
  - At most 2 people per company.
  - Nobody twice within 60 days.
  - The kind of message follows the person: referral ask, hiring manager, founder, or recruiter.
- **Draft checks.** A draft is blocked if it has:
  - numbers not in your resume or the job post;
  - a link in the connection note;
  - more characters than your plan allows;
  - leftover placeholders;
  - near-identical wording to another draft.

  Connection notes aim for 120–180 characters. The resume link (`outreach.resume_links` in `profile.yaml`: set view-only Drive links) goes only in the message sent after someone accepts.
- **Limits** (`config.yaml → outreach.limits`):
  - 15 invites a day and 80 a week.
  - 20 messages and 15 emails a day.
  - One follow-up after 6 days.
  - Pending invites flagged for withdrawal after 21 days.
  - `send` stops by itself if your acceptance rate falls below 30% (after at least 20 invites).
- **LinkedIn plan.** Premium gives 300-character notes with no monthly note limit. Set `outreach.linkedin.premium_until` to your trial's end date. After that date jobpilot switches to free-tier limits: 200 characters and about 5 personalised notes a month, after which invites go out blank and the message waits for acceptance.
- **Referral hold.** At companies whose pay tops 60 LPA and where a connection or engineer could refer you, the application waits up to 7 days, because some ATSs can't attach a referral to an existing application. Marking a contact `referred` fills "Referred by" / "Employee referral" on the form and applies right away. If nothing comes, it applies cold when the hold ends.
- **Replies.** `jobpilot run` reads LinkedIn's notification emails ("… accepted your invitation", "… sent you a message") over IMAP and updates each contact's status. The report has an Outreach table.
- **Apollo free tier.** It works through CSV export: search in Apollo's web app, export the contacts, and run `import-contacts`. The free plan gives roughly 900 credits a year, granted monthly, and email reveals use credits. Apollo's own docs say free accounts registered with a *personal* email can't use people search or enrichment through its API/Claude connector; a work-email account can.

## Logins

- **The script's logins live in its own Chrome profile** (`data/chrome-profile`). `jobpilot login` opens that profile as a plain Chrome window, not an automated one, so Google sign-in, passkeys and 2FA work normally. Sign in to each portal once, quit with Cmd+Q, and the sessions are saved. `jobpilot doctor --portals` tells you which sessions have expired.
- **Why not your everyday Chrome?** Since Chrome 136, Google blocks automation on the default profile, and a copied profile can't decrypt its cookies: a custom data directory uses a different encryption key. It's safer anyway: if a portal ever flags the automated profile, your normal browser and sessions are unaffected. For a faster setup, turn on Chrome sync in jobpilot's window so your saved passwords autofill each login.
- **The remote boards (WWR, Remote OK, Remotive, Himalayas) and the ATS boards need no login.** Workday is signed into per company, the first time you apply there (`--interactive`).
- **jobpilot never stores or types passwords.** Sessions made in other browsers (your normal Chrome, the Claude app's built-in browser) aren't shared with this profile.

## Where to edit

- `config/answers.yaml`: regex → answer rules for form questions. Add one whenever review shows a question it should have answered.
- `config/portals.yaml`: button texts and selectors for each portal. Fix these when a portal changes its UI.
- `config/companies.yaml`: aliases (for dedup), ATS slugs, comp bands (`band_verified: true` once you've checked a band).
- `config/config.yaml`: every threshold, cap, rank and mode.

## Tests

`pytest -q` runs 121 tests: parsers, filters, dedup and the planner, unit-aware option buckets and invariants, state-machine claims, email matching, and end-to-end fills in headless Chromium against local fixture forms (shadow never submits; live submits and seals proofs; an input mask, a missing free-text answer and a FILL_ME each block; review override → approved submit; multi-step Easy-Apply; a Workday-style flow with sign-in handoff, listbox dropdowns and kept resume autofill; WWR feed, Workday, Remote OK, Remotive and Himalayas parsing; an aggregator listing that links out to a company form; outreach import, targeting, draft checks, limits, referral hold, notifications and hiring-team capture).
