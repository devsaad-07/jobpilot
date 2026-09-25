# jobpilot: design notes and research

## Decisions (from the grill session, 2026-09-24)

| Topic | Decision |
|---|---|
| Autonomy | Tiered. Direct-ATS applications that pass every check auto-submit; the rest go to a review queue |
| Sources | Company ATS boards, Workday sites, We Work Remotely, Remote OK, Remotive, Himalayas, LinkedIn, Naukri, Instahyre, Cutshort, Wellfound, Hirist, Weekday |
| Salary ≥ 60 LPA | Posted salary if there is one, else a per-company band table, else review |
| Stack | Python + Playwright on your Mac, in a dedicated Chrome profile |
| Models | Laya-MLX for bulk typed decisions; Codex for screening answers and the judge; Antigravity for development |
| Experience | The JD's minimum must be ≤ 6 years |
| Resume | Pick one of the 3 PDFs, unchanged (nothing generated, so nothing to fact-check) |
| Proofs | Page artifacts plus Gmail IMAP confirmation matching |
| Portal rank | Direct ATS (incl. Workday) > LinkedIn > WWR > Himalayas > Remotive > Remote OK > Instahyre > Naukri > Wellfound > Cutshort > Weekday > Hirist. The new sources were slotted in with fractional ranks so your original order is unchanged |
| Remote abroad | EOR / India entity / contractor are OK; relocation is not |
| Caps/day | ATS 50 · LinkedIn 20 · Naukri 20 · others 15 |
| Exclusions | CoinSwitch, Lemonn, ImageKit, Clearfeed, Sahi, Alpaca, Neosapiens; 90-day company cooldown |

## What the research changed

1. **Public ATS APIs are the backbone.** Greenhouse (`boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`), Lever (`api.lever.co/v0/postings/{slug}?mode=json`), Ashby (`api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`) and Workable are unauthenticated JSON. They have no ban risk and give clean posting dates, which the recency rule needs. I checked the field names against live Greenhouse and Ashby responses. A known trap with these APIs is that a missing board, an empty board and a failed request all look like `[]`. The fetchers return distinct statuses, and `probe-ats` records which slug actually answers.
2. **LinkedIn and Naukri automation carries account risk.** LinkedIn's User Agreement prohibits bots and automated access, and detection is behavioural (timing, velocity, fingerprint). That's why those portals are review-only, capped, paced with random 45–180 s gaps, limited to business hours, and run in your real Chrome profile rather than headless. JobSpy discovery also gets rate-limited on LinkedIn around page 10 from one IP, so results per search term are kept modest.
3. **JobSpy's PyPI release lags its GitHub HEAD** (Naukri parsing and LinkedIn date fixes landed after the last release). `setup.sh` installs from GitHub first.
4. **Laya-MLX answers typed questions, it doesn't generate text.** `choice`, `score` and `noul` fit triage exactly: resume choice, fit rubric, and propositions like "needs 7+ years" or "India-eligible", at ~10 ms each on-device. Its context is 512–1,024 tokens, so `trim_jd()` sends only the title, location and requirements section. `noul` returns P(true); thresholds are 0.7/0.3 for experience and 0.75/0.25 for location, and anything between them goes to review.
5. **`codex exec --output-schema`** returns JSON that validates against a schema, so screening answers come back as `{answer, provenance, confidence, evidence}` and the judge as `{ok, issues[]}`. It runs with `--sandbox read-only --ephemeral`.

## Extras I added (not in your brief)

- **Shadow / canary / live modes**: your staged-canary habit applied to applications.
- **Kill switch file** (`touch STOP`).
- **A submit is never retried automatically.** The `submitting` state is written before the click, and a crash leaves the application `unconfirmed` for you to check, never re-submitted. This is the same reasoning as not replaying an order whose fill you haven't seen.
- **Dedup enforced in the database** (unique job, unique live application per cluster), on top of the in-memory planner, the way the fill dedup uses both memory and a DB index.
- **Late dedup**: when a LinkedIn/Naukri "Apply" redirects to an ATS form, the redirect URL is checked against existing applications before any filling.
- **JD snapshot at apply time.** Postings disappear, and you'll want the exact text for interview prep.
- **Stale-posting filter** (> 21 days) to avoid ghost roles.
- **One role per company per run, plus a 90-day cooldown.** Several simultaneous applications to one company read as spray-and-pray to its recruiters.
- **One CTC everywhere.** Every CTC field is checked against `profile.yaml`, because recruiters see your Naukri, Instahyre and ATS answers side by side.
- **No-fabrication check**: numbers in LLM free text must already appear in your resume or profile.
- **CAPTCHA/login handoff** instead of solving them.
- **LinkedIn "follow company" is unticked** automatically.
- **Tamper-evident proofs**: `manifest.json` records a sha256 of every artifact and is resealed when an email arrives.
- **A feedback loop**: interview/rejection emails set `outcome`, and the report shows interview rate per portal and per resume variant. After ~50 applications, adjust `portal_rank` from your own numbers instead of priors.
- **The report flags company-band salary decisions** made on unverified bands.

## Added sources (2026-09-24)

- **Workday.** Discovery uses the endpoint the career site itself calls: `POST /wday/cxs/{tenant}/{site}/jobs`, then `GET …{externalPath}` for the detail. Known traps, all handled:
  - `limit` above 20 returns nothing, so paging is fixed at 20.
  - Only page 1 carries `total`.
  - `postedOn` is text ("Posted 3 Days Ago"), so the detail's `startDate` is used for the date.
  - The backend is slow, so each request retries once.

  Title and location are filtered *before* the per-job detail call.

  Applying: `adventureButton` → *Autofill with Resume* → a sign-in gate (a human handoff; no passwords are stored) → wizard steps through `pageFooterNextButton`. Workday's Submit shares its button id with Next, so a Submit is only trusted on the Review page. Workday's custom `button[aria-haspopup=listbox]` dropdowns are supported by the form engine. The work-history and education sections parsed from the resume are kept as `portal_prefill` and always go to review.
- **We Work Remotely.** Discovery reads its public RSS feeds. Each item carries `region`/`country`, so "USA Only" roles are rejected at triage and "Anywhere in the World" roles pass. WWR hosts no forms: its apply link goes through the external route, so the late ATS dedup check and the portal-host guard still apply. Email-only listings go to review.
- **Weekday.** A browser-portal adapter (search pages → job links → one-click Apply or external link). Weekday is an AI-sourcing/recruiter intermediary, so it ranks below the other India portals. Its selectors are untested, like the other logged-in portals.
- **Remote OK / Remotive / Himalayas.** All three are public JSON APIs whose terms ask for attribution and link-back. The job URL and source are kept everywhere, and nothing is republished.
  - Remotive asks for at most 4 calls a day and blocks more than 2 a minute. jobpilot makes one call per run and keeps a per-day counter in `data/remotive_calls.json`.
  - Himalayas caps requests at 20 jobs and uses cursor pagination. It's searched with `country=IN`, so results are hireable from India. Jobs whose time-zone limits are more than 2.5h from IST are dropped.
  - All three link out to the company's form. That goes through the same external route as WWR, including the late dedup check, which now also counts skipped applications.
- **Also fixed on the way:** field extraction now skips inputs inside hidden wizard steps. Before, radio buttons and file inputs on later steps could be picked up early.

## Outreach (2026-09-24)

- **Assisted only.** The script finds people, drafts messages and enforces limits. You click Send on LinkedIn. LinkedIn's User Agreement §8.2 bans bots that automate activity, and restrictions escalate to permanent bans. Messaging and invites are policed harder than applications.
- **Finding people without scraping.** Sources are:
  - the official Connections export;
  - the "Meet the hiring team" block on job pages the script already opens (capped at 20/day, paced);
  - CSV imports (Apollo or anything similar).

  There's no LinkedIn people search.
- **Drafts.** Codex writes them from resume facts only, using the same no-fabrication rule as form answers (every number must appear in the resume, profile or job post). A deterministic template takes over when Codex isn't available.
  - Connection notes carry no links and aim for 120–180 characters, which gets better acceptance than filling the full 300.
  - The resume link goes in the post-accept message.
  - Near-identical drafts are blocked, because uniform text is a known automation signal.
- **Guardrails.** Daily and weekly invite caps, 2 contacts per company, a 60-day no-recontact window, one follow-up, reminders to withdraw stale invites, and an automatic pause when acceptance drops below 30%.
- **Referral hold.** Some ATSs can't attach a referral after the fact, and withdrawing and reapplying risks duplicate records. So high-band companies with a possible referrer wait up to 7 days before a cold apply.
- **Unverified numbers.** The LinkedIn limits (about 100–200 invites a week; free accounts get about 5 personalised notes a month at 200 characters; Premium 300) come from third-party trackers, not official LinkedIn docs. They're config values.

## Independent code review (fixed)

A separate reviewer pass looked for submit-safety bugs. These were found and fixed, each with a regression test in `tests/test_review_findings.py`:

- **Stale approvals**: an approval could cover LLM answers generated after the review. Approvals now freeze the reviewed answers and are cleared on re-review.
- **Unit and bucket errors**: "60 days" could be matched to "More than 3 months", and "CTC (in INR)" could receive 40. Bucket choice now picks the most specific option and converts units, and invariants check the unit named in the label.
- **Double claims**: self-transitions were allowed, so two runners could claim the same application. Self-transitions are now rejected, `apply` takes a lock file, and each application runs in its own try/except, including on Ctrl-C.
- **Unreviewed dialog/chat answers**: LLM answers in one-click dialogs and the Naukri chat could be sent unreviewed. They now stop for a human.
- **Number check scope**: the no-fabrication number check now covers every LLM text answer.
- **Skipped roles returning**: a role you skipped could come back through another portal. A skip is now final for the cluster, and the runner-up is used only after a mechanical fill failure.
- **Empty combobox read-back** no longer passes verification.
- **Portal "Apply" clicks**: an unknown "Apply" button on a logged-in portal could be clicked from the ATS route. That route now stops and asks you instead.

## Worth considering next (not built)

- **Referral hold**: for top-band companies, a referral usually beats a cold apply by a wide margin. Add a `referral_first: [..]` list whose jobs are held for a week while you ask your network, then applied cold.
- **Profile freshness**: Naukri and Instahyre rank recently updated profiles higher for recruiter search, so a tiny daily profile touch may bring more inbound than applying. It could be a separate `jobpilot refresh-profiles`.
- **Portal-side salary settings**: Instahyre, Wellfound and Cutshort send your stored profile on one-click apply. Keep expected CTC and notice there identical to `profile.yaml`. The verifier can't see those values.
- **Cover notes** (the option 2 you skipped): generate from resume facts only, then run them through the same number and claim check as free-text answers.

## Honest caveats

- **Only the ATS, Easy-Apply and Workday-style flows are tested, against local fixture forms.** The Workday company URLs in `companies.yaml` weren't reachable from my sandbox either; `probe-ats` flags any that are wrong. The sandbox I built in can't reach the live portals. The LinkedIn/Naukri/Instahyre/Cutshort/Wellfound/Hirist/Weekday selectors in `config/portals.yaml` are reasonable starting points but untested. Run `jobpilot doctor --portals` and a few days of `shadow` before trusting them. The Naukri chatbot flow is the least certain.
- **The comp bands in `companies.yaml` are rough estimates** so the salary gate works on day one. They aren't researched figures.
- **Laya's JSON field names** were checked against the laya-mlx source (`choice`/`probabilities`, `score`, `noul`), but the model was not run here (it needs Apple Silicon).
- **Using automation on LinkedIn and Naukri is your call.** The caps and review-only policy lower the risk but don't remove it.
