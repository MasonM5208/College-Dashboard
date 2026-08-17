# Semester Dashboard — Build Specification

**Status:** Planning complete. Ready for implementation.
**Owner:** Mason
**Target ship date:** Before fall semester classes begin.

---

## 0. PRIMARY DIRECTIVE — READ THIS FIRST

Alongside the working software, you must produce **exhaustive setup and operations documentation**. This is not a secondary deliverable. A working system with documentation the owner cannot follow is a failed build.

### The standard

Write for a reader who has **never used a terminal**, has **never heard of SSH, Docker, or a VPS**, and whose entire computing experience is a web browser and Netflix. That reader must be able to go from zero to a running system without asking anyone for help.

This reader is intelligent. They are not stupid — they are *unfamiliar*. Do not condescend, and do not skip.

### Non-negotiable documentation rules

1. **Never use the words "simply," "just," "obviously," or "trivially."** If a step feels simple enough to warrant those words, it needs more explanation, not less.
2. **Every command appears on its own line in a copyable code block, with no shell prompt characters** (`$`, `#`, `>`) that would break a copy-paste.
3. **After every command, show the expected output.** Literally: "You should see something like this:" followed by a block. If the command produces no output on success, say so explicitly — otherwise the reader will assume it failed.
4. **Define every term at first use, inline.** Not in a glossary the reader has to go find. "SSH (Secure Shell — a way to type commands into a computer that isn't in front of you)."
5. **Never assume software is installed.** Every prerequisite gets a check command, an expected result, and install instructions if missing.
6. **For every web interface, describe the actual navigation path**, including what the button looks like and roughly where it is. UIs change; describe the goal as well as the path so the reader can adapt. Example: "Look for a green button labeled *Create Server*, usually in the upper right. If you don't see it, you may be on the wrong page — the URL should end in `/projects`."
7. **Every section opens with: what this section accomplishes, why it's needed, and how long it takes.** Time estimates for a first-timer, not for you.
8. **Every section ends with a verification checkpoint.** "Before continuing, confirm X. If X is not true, see Troubleshooting §N." The reader must never be able to build three steps on top of a silent failure.
9. **Troubleshooting is inline or immediately adjacent, not exiled to an appendix.** For each step, list the two or three most likely failure modes with the exact error text the reader will see and what to do about it.
10. **Anything destructive or hard to reverse gets a warning before the command and a stated undo path.** If there is no undo, say that plainly.
11. **Explain the "why" in one sentence before the "how."** A reader who understands the purpose of a step can recover when it goes sideways. A reader following blind cannot.
12. **Assume the reader will do this over multiple sittings.** Make sections resumable — state what must already be true to start each one.
13. **Every secret, password, and token the reader creates goes in a checklist** at the point of creation, telling them where to record it and warning that it may not be retrievable later.

### Required documents

| File | Audience | Contents |
|---|---|---|
| `docs/SETUP.md` | The Netflix user, day one | Zero to running system. The flagship document. Numbered steps, checkpoints, inline troubleshooting. |
| `docs/DAILY_USE.md` | The owner, every day | How to use the app. Adding a course, entering assignments from a syllabus, marking things done, saving an email, asking the chat a question. |
| `docs/OPERATIONS.md` | The owner, monthly | Checking that syncs are running, reading logs, restarting after a reboot, applying updates, restoring from backup. Include a "the site won't load" decision tree. |
| `docs/SECRETS.md` | The owner | Template listing every credential the system needs, where it lives, and how to rotate each one. **No actual secrets committed.** |
| `docs/ARCHITECTURE.md` | A future developer (possibly the owner in a year) | How it works and why. Design decisions and their rationale, including rejected alternatives. |
| `README.md` | Anyone | What this is, and a pointer to `SETUP.md`. |

Write `SETUP.md` **as you build**, not afterward. Retroactive documentation silently omits the steps you did from muscle memory — and those are exactly the steps the reader doesn't know.

---

## 1. What this is

A personal academic dashboard for a music performance major carrying 20 credits across 8 courses. Three jobs:

1. **Never miss a deadline.** Pull assignments from Canvas automatically, remind at multiple escalating intervals before each due date, on both laptop and phone.
2. **Remember important messages verbatim, forever.** Save Canvas messages and emails permanently and exactly, then answer questions about them with citations back to the original.
3. **Be a general-purpose assistant.** The same chat interface answers any question, with the owner's schedule and deadlines always in context.

### Non-goals

Deliberately excluded. Do not build these.

- Multi-user support. One user, forever.
- A mobile app. A web app installed to the iPhone home screen is sufficient.
- Offline capability. Network required.
- Real-time sync or collaborative editing.
- Vector embeddings or semantic search. See §7.
- Rebuilding anything Canvas already does well (announcement notifications, grade display).
- Auto-scheduling work into calendar time blocks. It always loses to reality.
- Pomodoro timers, streaks, gamification, or mood tracking. Streaks in particular punish the owner during exactly the weeks they most need slack.

---

## 2. Confirmed constraints

These were investigated. Do not design around the possibility that they might change.

| Constraint | Status | Consequence |
|---|---|---|
| Canvas API personal access token | **Unavailable.** Institution has disabled it. | No API access. Calendar ICS feed is the only automated Canvas source. |
| IU email auto-forwarding | **Unavailable.** Blocked by institution. | No automatic email ingestion via forwarding. |
| Canvas Calendar ICS feed | **Confirmed working.** | The single load-bearing dependency of the entire project. |
| Local LLM | **Dropped.** Cheap VPS cannot run one. | Claude API only. No provider abstraction needed. |
| Anthropic embeddings endpoint | **Does not exist.** | Full-text search only. No vector store. |

### Unresolved — affects M4 only, does not block starting

- **Gmail "Check mail from other accounts"** (Gmail Settings → Accounts → Check mail from other accounts). This is Gmail *pulling* via POP, which is a different permission from IU forwarding *out*. It may still be enabled. **Test this before building M4.**
- **Mac Mail bridge.** A Mail rule triggering an AppleScript that POSTs new message bodies to the ingest endpoint. Requires the MacBook awake and online. Viable fallback if Gmail POP fails.

If both fail, email ingestion is manual only (share sheet and paste), which is acceptable — see §7.

---

## 3. Architecture

**One server, thin clients.**

```
┌──────────────────────────────────────────┐
│  VPS (Debian 12, Docker, ~$5/mo)         │
│                                          │
│  ┌────────────┐  ┌──────────────────┐    │
│  │ Web app    │  │ Scheduled jobs   │    │
│  │ (API + UI) │  │ ICS poll (30m)   │    │
│  └─────┬──────┘  │ Reminder sweep   │    │
│        │         │ CalDAV push      │    │
│        │         │ Nightly backup   │    │
│        │         └────────┬─────────┘    │
│        └──────┬───────────┘              │
│           ┌───▼────┐                     │
│           │ SQLite │                     │
│           └────────┘                     │
│                                          │
│  Tailscale — no public ports open        │
└──────────────────────────────────────────┘
      │                    │
      │ tailnet            │ tailnet
      ▼                    ▼
  MacBook browser     iPhone PWA
                           │
                           ▼
                    Apple Reminders
                    + Calendar (via CalDAV)
```

**All network traffic is outbound.** The server polls Canvas, calls the Claude API, and pushes to CalDAV. Nothing needs to reach in from the public internet. The web app is reachable only over Tailscale.

This is the single most important security decision in the project. It eliminates public attack surface, certificate management, and reverse-proxy configuration in one move. Do not add a public entry point without a specific reason.

### Why a VPS and not the MacBook

Reminders must fire when the laptop is closed. A laptop-hosted server can't do that.

### Why Tailscale and not a public domain

`SETUP.md` should explain this to the reader in one paragraph: a public server must be defended, and defending a server is a skill they do not have and should not need to acquire for this project. Tailscale creates a private network between their own devices. Nothing else can reach it.

---

## 4. Stack

Choose concrete versions and pin them. Recommendations, not mandates — but justify departures in `ARCHITECTURE.md`.

- **Host:** Debian 12, 2 vCPU / 4GB / 40GB. Hetzner CX22 (~€4/mo) is best value; DigitalOcean or Vultr (~$6) for US-based.
- **Runtime:** Docker + Docker Compose. Single `docker-compose.yml`.
- **Backend:** One language, one framework, boring choices. Python with FastAPI or Node with Fastify. Whatever produces the clearest code — the owner will maintain this.
- **Database:** SQLite, single file on the VPS disk, WAL mode enabled. With FTS5 compiled in (verify at startup and fail loudly if missing).
- **Scheduling:** In-process scheduler, or cron inside the container. Do not add Redis, Celery, or a message queue. The workload is a handful of jobs per hour.
- **Frontend:** Server-rendered HTML with minimal JS, or a small SPA. **Must work as an installed PWA on iOS** — needs a valid manifest, service worker, and appropriate icon sizes.
- **Networking:** Tailscale on the VPS. App binds to the tailnet interface only, never `0.0.0.0`.

### Design principles

- **Fail loudly.** A sync that silently stops is worse than one that crashes — the owner will trust stale data. Surface last-successful-sync time prominently in the UI.
- **Boring over clever.** This is maintained by one busy person between rehearsals.
- **Every scheduled job writes to `sync_state` and logs its outcome.**

---

## 5. Data model

Implement with real migrations from commit one. Never edit the schema by hand on the server.

### `terms`
`id`, `name`, `start_date`, `end_date`

### `courses`
`id`, `term_id`, `name`, `code`, `instructor`, `meeting_pattern`, `ics_summary_pattern`, `credits`, `notes`, `late_policy`, `current_grade_pct`

`ics_summary_pattern` is the string used to match this course against the suffix in ICS event titles. See §6.

`late_policy` — free text plus a structured `penalty_pct_per_day` where known. Determines what is rational to sacrifice under overload. See §9.

`current_grade_pct` — owner-maintained, nullable. Used to discount marginal grade impact in prioritization.

### `assignments`
`id`, `course_id`, `title`, `type`, `due_at`, `start_by`, `est_hours`, `est_hours_remaining`, `points_possible`, `weight_category`, `status`, `source`, `ics_uid`, `late_penalty_override`, `pinned`, `created_at`, `updated_at`

- `est_hours_remaining` — initialized to `est_hours`, decremented as work is logged, owner-adjustable. **The prioritization engine is inert without this field populated.** Prompt for it on every create; default from `type`.
- `pinned` — boolean. Forces an item to the top of the Today view regardless of computed slack.

- `type` — enum: `worksheet`, `paper`, `project`, `exam`, `quiz`, `performance`, `milestone`, `other`. Drives reminder ladders.
- `status` — enum: `not_started`, `in_progress`, `submitted`, `graded`, `dismissed`. **Owner-controlled.** The ICS feed cannot tell us this.
- `source` — enum: `ics`, `manual`, `syllabus_batch`
- `ics_uid` — nullable, unique when present. The join key for feed diffing.
- `start_by` — computed default `due_at - (est_hours × 2 days)` for papers and projects, nullable otherwise. Owner-overridable.

### `reminder_rules`
`id`, `scope` (`global` | `course` | `assignment_type`), `course_id`, `assignment_type`, `offsets_json`, `enabled`

### `reminder_instances`
`id`, `assignment_id`, `rule_id`, `kind` (`start_by` | `due_by`), `fire_at`, `channel`, `state` (`pending` | `sent` | `snoozed` | `dismissed` | `superseded`), `sent_at`, `external_id`

**Materialize every reminder as its own row.** This is what makes individual snoozing possible and keeps state coherent when a due date moves. When a due date changes, mark affected pending instances `superseded` and generate new ones — do not mutate `fire_at` in place, so the history stays auditable.

### `commitments`
`id`, `term_id`, `label`, `kind` (`class` | `ensemble` | `lesson` | `practice` | `work` | `other`), `weekday`, `start_time`, `end_time`, `course_id`, `active`

Fixed weekly obligations, subtracted from wall-clock time to yield available hours. See §9.

### `capacity_settings`
`id`, `weekday`, `productive_hours`, `practice_hours_target`

Per-weekday budget. M2 ships with a flat constant; this table replaces it in M6.

### `time_entries`
`id`, `assignment_id`, `started_at`, `ended_at`, `minutes`, `note`

Actual time logged against assignments via a start/stop timer. Feeds estimate calibration.

### `estimate_calibration`
`assignment_type`, `sample_count`, `multiplier`, `updated_at`

Derived per-type `actual ÷ estimated` ratio, recomputed as `time_entries` rows complete. Surfaced in the UI — never applied silently.

### `documents`
`id`, `body`, `body_sha256`, `subject`, `sender`, `received_at`, `ingested_at`, `kind`

**`body` is immutable.** Never updated, never summarized in place, never truncated. This table is the permanent verbatim archive and the reason the whole system is trustworthy. Enforce immutability at the application layer and document the intent in a schema comment.

### `document_sources`
`id`, `document_id`, `source` (`share_sheet` | `paste` | `mail_bridge` | `gmail_poll`), `external_id`, `raw_headers`, `ingested_at`

**Many rows per document.** A Canvas conversation that also arrives by email is *one* document with *two* provenance rows. This is the dedup design — see §7.

### `document_links`
`id`, `document_id`, `target_type` (`course` | `assignment`), `target_id`, `confidence`, `created_by` (`auto` | `manual`)

### `documents_fts`
FTS5 virtual table over `documents.subject` and `documents.body`. Kept in sync by triggers.

### `chat_threads` / `chat_messages`
Standard. `chat_messages` stores role, content, tool calls, tool results, and token counts.

### `sync_state`
`source`, `last_success_at`, `last_attempt_at`, `last_error`, `cursor`, `consecutive_failures`

Easy to omit, painful to retrofit. Build it in M1.

### `audit_log`
Append-only. Every write to `assignments`, `documents`, and `reminder_instances`. `timestamp`, `action`, `table`, `record_id`, `detail_json`.

---

## 6. Canvas ICS ingestion

The single automated data source. Get this right.

### Obtaining the feed URL

Canvas → Calendar → **Calendar Feed** button, at the bottom of the right-hand sidebar. Produces a URL with an embedded token.

**Treat this URL as a password.** It exposes the owner's full academic schedule to anyone who holds it. Env file, mode `600`, never committed, never logged, never included in an error message. It can be regenerated in Canvas if leaked — document how in `SECRETS.md`.

### Polling

Every 30 minutes. Canvas caches the feed server-side, so **expect up to an hour of staleness, sometimes more.** Two consequences to document and design for:

- Never present ICS data as authoritative for anything due within two hours.
- Show last-successful-sync time in the UI. If it exceeds three hours, show a visible warning.

### Parsing quirks — all of these are real

1. **`UID` is stable across polls.** This is what makes diffing work. Match on `ics_uid`.
2. **Diff `DTSTART` and `SUMMARY`.** A changed `DTSTART` is a moved deadline and must trigger reminder regeneration and a push notification.
3. **Items with no due date do not appear at all.** Anything a professor assigns without a Canvas due date is invisible to this system. Document this limitation prominently in `DAILY_USE.md` — the owner must know the feed is not exhaustive.
4. **Course association comes from the `SUMMARY` suffix**, typically `Assignment Title [Course Name]`. There is no clean course field. Parse the bracketed suffix and match against `courses.ics_summary_pattern`.
5. **Unmatched events go to a review queue, never silently dropped.** Surface them in the UI for one-tap course assignment. When a new pattern is confirmed, offer to save it to the course.
6. **Deletions:** an event vanishing from the feed usually means the assignment was deleted or unpublished. Do not hard-delete. Mark it and surface it for confirmation — a transient feed error must never destroy data.

### Failure handling

Increment `consecutive_failures`. After three, surface a prominent UI warning. Never let a failed poll appear as "no assignments due."

---

## 7. Document ingestion and retrieval

### The four paths

| Path | Status | Notes |
|---|---|---|
| Share sheet | Build in M4 | iOS Shortcut POSTing to `/ingest` with a bearer token. Include a step-by-step Shortcut build guide with described screenshots in `SETUP.md`. |
| Manual paste | Build in M4 | Textarea hitting the same endpoint. |
| Gmail POP poll | **Test first** | If Gmail can pull from IU, poll via Gmail API. |
| Mac Mail bridge | Fallback | Mail rule → AppleScript → POST. Requires Mac awake. |

**All four converge on one ingest endpoint and one `documents` insert.** Build the pipeline once; adapters are thin.

### Deduplication

Canvas conversations arrive both in Canvas and by email. You will double-ingest.

Before insert: normalize whitespace, strip quoted-reply chains and signature blocks, hash the normalized body. On hash match, **add a `document_sources` row to the existing document** rather than creating a new one.

Build this in M4, before the archive has volume. Retrofitting dedup across 800 documents is miserable.

### Retrieval: FTS5 only

No embeddings. No vector database. Reasoning, for `ARCHITECTURE.md`:

- Anthropic offers no embeddings endpoint, so vectors would mean a second vendor.
- Because auto-ingestion is unavailable, the archive is **curated rather than exhaustive** — roughly the few dozen messages per semester that actually matter, not thousands including every listserv blast. Signal-to-noise is high and keyword search performs well on it.
- The corpus is small enough that BM25 top-30 fits comfortably in Claude's context window.

Add vectors only if specific real queries are observed to fail. Note that possibility in `ARCHITECTURE.md`; do not build for it.

---

## 8. Reminders

### Two channels, split by kind — never overlapping

| Channel | Carries |
|---|---|
| **Apple Reminders + Calendar** (CalDAV push) | All time-based nags. The full ladder, `start_by` and `due_by`. iOS owns delivery, snooze, lock screen, Watch. |
| **Web push** (PWA) | Event-driven only. Deadline changed, new document ingested and linked, sync failing, ICS event needs course assignment. |

Never send the same reminder through both. Duplicate notifications with divergent dismissal state will train the owner to ignore both.

### Why Apple carries the important traffic

iOS web push is real but fragile: the PWA must be installed to the home screen, the permission prompt must originate from a user tap, VAPID keys and a service worker are required, and **iOS drops push registration if the PWA goes unopened for several weeks.** Apple Reminders has none of these problems. Document all of it.

### Default ladders

Tunable per course and per type once the owner knows their professors.

| Type | Offsets before due |
|---|---|
| `worksheet`, `quiz` | 24h, 3h |
| `paper`, `project` | `start_by` at `est_hours × 2` days, then 7d, 3d, 1d, morning-of |
| `exam` | 10d, 5d, 2d, night-before |
| `performance`, `milestone` | Weekly checkpoint from 4 weeks out |

**Quiet hours 22:30–07:30.** Anything landing inside shifts to the nearest edge — earlier for `due_by`, later for `start_by`.

### Completion state does not flow back

Marking a reminder done in Apple Reminders will not update the dashboard. That requires a CalDAV read loop, which is real work. **Deferred.** Document the limitation clearly; the owner needs to know the dashboard is the source of truth for status.

---

## 9. Prioritization and capacity

**Due-date ordering must not be the default sort.** It ranks a 20-minute worksheet due tomorrow above a 6-hour paper due Thursday, which is backwards and will eventually cost a grade. The owner carries 20 credits across 8 courses; ordering by deadline hides exactly the failure mode that matters.

### The primitive: slack

```
slack_hours = available_hours_before(due_at) − est_hours_remaining
```

Sort ascending. **Negative slack means the owner is already behind on that item** — it surfaces days before a due-date sort notices anything is wrong. This single formula is most of the value in this section.

`available_hours_before(due_at)` is *not* wall-clock time remaining. It is productive hours remaining after fixed commitments are removed. See the capacity model below.

### Inputs

| Input | Source | Notes |
|---|---|---|
| `est_hours_remaining` | Owner, at entry | Everything here is inert without it. Prompt on every create; default from `type`. |
| Available hours | Capacity model | Classes, rehearsals, lessons, and practice subtracted first. |
| Marginal grade impact | `points_possible × weight`, discounted by `courses.current_grade_pct` | A 25% paper matters more at 79% than at 96%. |
| Late penalty | `courses.late_policy`, per-assignment override | Determines what is rational to sacrifice. Some professors take 10% per day, some take nothing. |
| In-progress boost | `status = in_progress` | Small. Reduces context-switch churn; must never dominate slack. |
| Pin | `assignments.pinned` | Manual override, always wins. |

### Capacity model

Weekly recurring commitments from `commitments`, plus a per-weekday productive-hours budget from `capacity_settings`. Subtract commitments from wall-clock time to get available hours.

**Practice is modeled as capacity consumption, not as a task.** This is a design decision, not a preference. Practice has no due date, so in any deadline-driven ranking it silently loses every comparison — and the owner is a performance major who will not notice the degradation until roughly a month in. Subtract practice hours from available capacity *before* any assignment is ranked against it, so the priority math protects practice by default.

### Display rules — these decide whether the feature is trusted

1. **Never show a bare score or rank number.** "Due Thursday · 6h left · 4 free hours before then" is trustworthy. "Priority: 87" is not, and the first time it is visibly wrong the owner will stop opening the app.
2. **Always show the inputs alongside the position.** Every ranked item displays the numbers that put it there.
3. **The sort must be explainable in one sentence** by someone reading the UI, with no knowledge of the formula.

### Estimate calibration

Start/stop timer on assignments, logging to `time_entries`. Compute a per-`type` multiplier from `actual ÷ estimated` and apply it to future estimates of that type.

Expect the owner to underestimate papers by roughly 2× at first. Everyone does. After about a month the system corrects for their specific bias with no ongoing effort. Cheap to build, compounds across the whole semester.

Surface the multiplier in the UI. **Do not silently inflate estimates** — an unexplained change to a number the owner typed destroys trust in the entire ranking.

### Overload mode

**The highest-value feature in this specification after reminders themselves.**

Trigger when total `est_hours_remaining` for items due inside a window exceeds available capacity in that window. Behavior:

1. State the shortfall in plain numbers: "31 hours of work, 22 hours available."
2. Rank items by **cheapest to sacrifice**, using marginal grade impact and late penalty — lowest grade cost first.
3. Recommend the specific one or two items to let slide, each with its projected grade cost.

Every other academic tracker pretends the owner can do everything and responds to overload by nagging harder. An honest shortfall calculation with a ranked sacrifice list is worth more than the entire reminder system, and it is the calculation the owner would otherwise attempt at 1am under pressure with worse information.

Do not soften this. Do not hide it behind a toggle. Do not add encouragement.

### Supporting features

- **Quick capture.** One always-reachable text field. Dump anything, triage later. Entry friction is what kills systems like this.
- **Exam auto-milestones.** An exam 10 days out generates study sessions with estimated hours, which then compete for capacity like any other work.
- **Sunday weekly review.** What's coming, what slipped, re-estimate remaining hours. Five minutes. This is where calibration data actually gets used.

### What ships when

**With M2 (minimum viable):** slack sort using a flat constant of 4 productive hours per weekday, plus `est_hours_remaining` and a manual weight field. No capacity model, no calibration, no overload mode. Roughly 90% of the benefit for a small fraction of the work, and it generates the data needed to tune the rest.

**In M6 (full):** real capacity model, practice as capacity, calibration from `time_entries`, overload mode, weekly review.

Do not attempt to guess the owner's weekly rhythm in August. Ship the constant, then replace it with measured reality in October.

---

## 10. Chat layer

### One endpoint, tool-based routing

**Do not write an intent classifier.** Do not try to decide whether a question is "about the archive" or "general." Give Claude tools and let it choose:

- `search_archive(query, course_id?, date_range?)` → FTS5 search over `documents`
- `get_document(id)` → full verbatim body
- `get_assignments(course_id?, due_before?, status?)` → structured assignment data
- `get_courses()` → course list with meeting times

This handles mixed questions for free — "when's my bio lab due and can you explain the assay" — and collapses to one code path.

### Always-injected system context

Every request, regardless of topic:

- Today's date and day of week
- Current term and full course list
- Everything due in the next 14 days with status
- Last-successful-sync time per source, if any source is stale

Cheap in tokens, and it eliminates most confusion.

### Citations are mandatory

Any claim derived from the archive must cite `document_id` and `received_at`, rendered in the UI as a tappable link to the **verbatim original**. If the owner cannot verify a claim about a deadline in one tap, the feature is not trustworthy enough to use. Enforce this in the system prompt and make unsourced archive claims a visible UI state.

### Model and cost

Use a current Claude model — check `docs.claude.com` for what's available rather than relying on a hardcoded assumption. Log token counts per message to `chat_messages`. Expected spend is a few dollars a month; surface a running monthly estimate in the UI so it never surprises anyone.

---

## 11. Security and operations

- **No public ports.** Tailscale only. App binds to the tailnet interface.
- **Secrets** in a root-owned `600` env file outside the repo. Canvas ICS URL, Claude API key, CalDAV credentials, ingest bearer token, VAPID keys. Never logged, never in error messages, never in git history.
- **`unattended-upgrades`** enabled for OS security patches.
- **Backups:** nightly `sqlite3 .backup` (not a file copy — WAL mode makes naive copies unsafe), encrypted with `age`, pushed to the owner's NAS or Backblaze B2. Retain 30 dailies and 6 monthlies.
- **Restore drill:** `OPERATIONS.md` must include a tested, step-by-step restore procedure. An untested backup is not a backup. Walk through it once during M0 and document exactly what you saw.
- **The VPS is rented and can vanish.** Nothing irreplaceable may exist only there.

---

## 12. Build order

Each milestone must be independently useful and independently shippable.

### M0 — Foundation
VPS provisioned, Docker running, Tailscale joined, SQLite initialized with migrations, nightly encrypted backup running and **restore-tested**.
**Done when:** the owner can reach a "hello world" page from both MacBook and iPhone over Tailscale, and has successfully restored a backup to a scratch location.

### M1 — Canvas ICS ingestion
Feed polling, parsing, `UID` diffing, course matching with review queue, `sync_state`, failure escalation.
**Done when:** assignments appear automatically, a due-date change in Canvas is detected within one poll cycle, and an unmatched event lands in the review queue rather than disappearing.

### M2 — Today view
The default screen. What's due, what to start, one-tap status changes. Manual assignment entry. Syllabus batch entry. Quick capture field. **Slack-sorted ordering with visible reasons** — the minimum viable version described in §9, using the flat 4-hour weekday constant.
**Done when:** the owner can see their day in one glance and mark something done in one tap, the top item is the one with least slack rather than the nearest deadline, and every ranked item shows the numbers that placed it there. **If it takes more than one tap to know what to do next, this milestone is not done.**

### M3 — Reminders out
Reminder ladders, materialized instances, CalDAV push to Apple Reminders and Calendar, quiet hours, regeneration on due-date change.
**Done when:** a reminder fires on the iPhone with the laptop closed.

**M0–M3 is the useful core. Ship it before classes start.**

### M4 — Document archive
Ingest endpoint, share sheet Shortcut, paste UI, dedup, FTS5 search interface. Gmail POP or Mail bridge if viable.
**Done when:** the owner can save an email from their phone in three taps and find it by keyword.

### M5 — Chat
Claude integration, the four tools, injected context, mandatory citations, thread history.
**Done when:** a question spanning archive and general knowledge is answered correctly in one turn, with a working citation link.

### M6 — Capacity, calibration, and overload
Real capacity model from `commitments` and `capacity_settings`. Practice as capacity consumption. Start/stop timer writing to `time_entries`. Per-type estimate calibration. **Overload mode.** Sunday weekly review. Exam auto-milestones.
**Done when:** the dashboard can state a shortfall in plain hours and name the cheapest thing to sacrifice, and estimates have been corrected by at least two weeks of logged actuals.

### M7 — Extras
Grade tracking with a "what do I need on the final" calculator. Workload forecast 2–3 weeks out. Practice hours trending. Ensemble absence tracker.

---

## 13. Open items

Resolve before M4. Neither blocks M0–M3.

1. Test Gmail Settings → Accounts → **Check mail from other accounts** against the IU address.
2. If that fails, assess Mac Mail rule + AppleScript bridge viability.

---

## 14. Running cost

| Item | Monthly |
|---|---|
| VPS | ~$5 |
| Claude API | ~$3–5 |
| Tailscale | Free tier |
| Backups | ~$0–1 |
| **Total** | **Under $12** |

---

## 15. Final reminder on the directive

The person setting this up is a full-time music performance major carrying 20 credits, and they will be doing it in gaps between rehearsals and practice sessions. They may set it aside for a week and come back to it. They will not remember what they did last time.

Write the documentation for that person. Resumable sections. Explicit checkpoints. Real error messages. No skipped steps.

Documentation quality is a first-class acceptance criterion for every milestone, not a task to be done at the end.
