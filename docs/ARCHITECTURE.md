# Architecture

For a developer — quite possibly Mason in a year, having forgotten all of this.

`SPEC.md` says what to build and why. This file says how it was built, and records
the decisions that are not obvious from reading the code, including the ones that
were rejected. A rejected alternative documented is a rejected alternative nobody
has to re-investigate.

Current state: **M0, M1, M2 and M5 built.** M3 (reminders) and M4 (the document
archive) are not started — M5 was pulled ahead of both at Mason's request, which
is why the chat ships with two of its four tools.

---

## The shape of it

```
   iPhone (PWA)          MacBook (browser)
        │                        │
        └────── Tailscale ───────┘
                    │
        ┌───────────▼────────────────────────┐
        │  Vultr VPS, Debian 12              │
        │                                    │
        │  Docker container "dashboard"      │
        │    entrypoint: migrate, then serve │
        │    uvicorn → FastAPI               │
        │      GET /         status page     │
        │      GET /healthz  JSON            │
        │                                    │
        │  /srv/dashboard/data/dashboard.db  │
        │      SQLite, WAL, FTS5             │
        │                                    │
        │  root cron, 03:15                  │
        │    ops/backup.sh                   │
        └────────────────┬───────────────────┘
                         │ outbound only
                         ▼
                  Backblaze B2
              (age-encrypted archives)
```

Every connection is outbound except the two devices arriving over Tailscale. There
is no inbound path from the public internet at all — not to the dashboard, and
since `SETUP.md` Section 7, not to SSH either.

---

## Stack, and why

| Layer | Choice | Reasoning |
|---|---|---|
| Language | Python 3.12 | `sqlite3` in the standard library already has FTS5; mature libraries exist for every remaining milestone (`icalendar`, `caldav`, `anthropic`) |
| Web | FastAPI + uvicorn, one worker | One process fits 1 vCPU. Jinja2 server-rendered HTML, no frontend build step |
| Database | SQLite, WAL, FTS5 | Mandated by SPEC §4. One file, which makes backups a file operation |
| Migrations | Numbered `.sql` files, custom runner | See below |
| Scheduling | Cron for backups; in-process for polling, from M1 | SPEC §4 rules out Redis and task queues |
| Container | One image, one process | Nothing here justifies a second |
| Network | Tailscale, no public ports | SPEC §3, the project's central security decision |

### Rejected: Node with Fastify

SPEC §4 offered either. Python won on two practical points: `better-sqlite3` is a
native module that compiles at image build time, which is slow on one shared vCPU
and a recurring source of breakage across upgrades; and the ICS and CalDAV
libraries needed in M1 and M3 are more mature in Python.

### Rejected: any ORM, and Alembic for migrations

The schema needs FTS5 virtual tables and the triggers that keep them in step with
`documents` (M4). Model-autogeneration tools cannot represent those, so they end up
being written as raw SQL inside migration files anyway — at which point the
framework is adding a dependency and a set of failure modes without removing any
work. A directory of numbered `.sql` files keeps the DDL literal and reviewable.

---

## Migrations

`app/migrate.py` over `migrations/NNNN_name.sql`, applied in numeric order, tracked
in a `schema_migrations` table holding each file's version, name, SHA-256 and the
time it was applied.

Four properties matter:

1. **One transaction per file**, including the bookkeeping row. A file applies
   completely or not at all, so a failure never leaves a half-migrated database.
2. **Applied files are checksummed on every start.** Editing a migration that has
   already run is refused, by name. This is the mechanical enforcement of SPEC §5's
   "never edit the schema by hand" — the same check catches a database that was
   altered outside the migration system.
3. **Forward-only.** There are no down-migrations. With one user, nightly tested
   backups, and a schema that changes a handful of times a semester, an untested
   `down` path is worse than none: it invites a rollback nobody has ever exercised,
   at the worst possible moment. Mistakes are corrected by a new numbered file, and
   recovery from a bad migration is a restore.
4. **Migration files carry no transaction control.** The runner supplies it, and
   refuses files containing a top-level `COMMIT`, `ROLLBACK` or `VACUUM`. The check
   matches only at the start of a line, so the indented `BEGIN ... END` of a trigger
   body is unaffected.

Migrations run in the container entrypoint, before uvicorn, under `set -e`. A
schema failure is therefore a container that will not start with the reason as the
last line of its log, rather than a web server answering requests against a schema
it does not understand. The application separately refuses to start if migrations
are pending, which covers someone running uvicorn by hand.

### Why the schema arrives in pieces

`0001_core.sql` creates only the tables M1–M3 need. `0002_ingest.sql` adds what
Canvas ingestion required, and `0003_chat.sql` the two chat tables. Documents and
the FTS5 index arrive with M4; capacity, the timer and calibration with M6. Each table lands
alongside the code that exercises it, so a mistake is caught by use rather than
discovered months later, when fixing it would need exactly the destructive
`ALTER`/rebuild that SPEC forbids doing casually.

Two tables break that rule deliberately: `sync_state` and `audit_log` exist from
`0001` despite belonging to later features, because SPEC §5 identifies both as
painful to retrofit.

### Deviations from SPEC §5's column lists

- **`audit_log.table_name`**, not `table` — `table` is an SQL keyword and would
  need quoting at every use.
- **`assignments.feed_missing_since`** — not in §5's list, but §6.6 requires that a
  feed event which disappears is marked and surfaced for confirmation rather than
  deleted, and that needs somewhere to record when it went missing.
- **`courses.penalty_pct_per_day`** — §5 describes it in prose ("free text plus a
  structured `penalty_pct_per_day`") without listing it as a column.
- **`assignments.course_id` is nullable** — required by §6.5's review queue for
  events whose course cannot be identified, and by §9's quick capture.

---

## Two environment files

Docker Compose treats these differently, and conflating them fails in a way that
looks like a mystery:

- **`/etc/college-dashboard/env`** — named by `env_file:`. Compose passes these
  into the container. It does **not** use them for `${...}` substitution in the
  compose file itself. Root-owned, mode 600, outside the repository. All real
  secrets (SPEC §11).
- **`<repo>/.env`** — read by Compose at interpolation time, which is the only way
  `${TAILNET_BIND_IP}` can resolve. Gitignored. Deploy values only.

Because the secrets file is readable only by root, every `docker compose` command
in this project runs under `sudo`.

---

## Networking, and the `0.0.0.0` that is not a mistake

The container's uvicorn binds `0.0.0.0:8000`. That is the container's own network
namespace, and it is not the binding SPEC §4 forbids. The host publishes the port
as `${TAILNET_BIND_IP}:8000:8000`, so the only address the outside world sees is
the tailnet one. `sudo ss -ltnp | grep 8000` on the host is the check that matters,
and `SETUP.md` makes the reader run it.

This is written down because `--host 0.0.0.0` reads like a violation, and
"fixing" it to bind the tailnet address inside the container would break the app:
that address does not exist in the container's namespace.

### Rejected: `network_mode: host`

It would let uvicorn bind the tailnet address literally, which reads better. It
also gives the container the host's entire network stack and moves the binding out
of the compose file into application configuration, where it is easier to get
wrong and harder to audit. An explicit published port is checkable with one
command.

---

## Backups

`ops/backup.sh`, from **root's** crontab at 03:15.

- `sqlite3 .backup`, not `cp`. In WAL mode the live database is spread across
  `dashboard.db` and `dashboard.db-wal`; a naive copy can capture a torn state.
  `.backup` is SQLite's online backup API and is safe while the dashboard serves.
- **`PRAGMA integrity_check` on the copy before it is encrypted.** A corrupt
  database is caught the night it happens, instead of being discovered thirty
  archives later during a real recovery.
- `age`, to a **public key**. The private key is deliberately absent from the
  server, so a break-in yields archives the machine itself cannot read.
- `rclone` to Backblaze B2, configured entirely through `RCLONE_CONFIG_B2_*`
  environment variables so that credentials stay in the one root-owned file rather
  than a second `rclone.conf`.
- Retention: the newest 30, plus the newest archive from each of the 6 most recent
  months, applied identically to the local copies and to B2.
- Outcome written to `sync_state`, so a backup that stops running appears on the
  status page (SPEC §4).

### Why root, and why it drops privileges anyway

Root, because the encryption key and B2 credentials are in a root-only file.

But every command that touches the database goes through `runuser` as the
database's owner. Root reading a WAL database creates root-owned `-wal` and `-shm`
files beside it, and the container — running as uid 1000 — would then be unable to
write to its own database. That failure would appear hours later, as a dashboard
that had mysteriously become read-only.

### Why cron rather than the application's scheduler

The backup has to keep working when the container is crash-looping, which is
exactly when last night's copy matters. Canvas polling, which is meaningless when
the app is down, goes in the in-process scheduler in M1.

### Why the restore drill runs on the MacBook

The private key is not on the server, so the drill has to happen elsewhere — and
the scenario worth rehearsing is SPEC §11's "the VPS is rented and can vanish".
Downloading an archive from B2 and restoring it on the laptop exercises the real
recovery path. A same-host restore would prove less and require weakening the key
arrangement.

### Recorded error text contains no secrets

`sync_state.last_error` gets the name of the failing stage — "backup failed during:
copying off-site to Backblaze B2" — and nothing else. rclone's own errors can
include account identifiers, and SPEC §11 forbids secrets in error messages. Full
detail goes to `/var/log/dashboard-backup.log`, which is root-only.

---

## The service worker caches nothing

`app/static/sw.js` registers, claims clients, and has a `fetch` handler that does
nothing at all. A test asserts that no caching APIs appear in the file.

Offline capability is an explicit non-goal (SPEC §1), and the dashboard is useless
offline in any case, since everything it shows lives on the server. More
importantly, a cache-first worker does exactly what SPEC §4 forbids: it would serve
yesterday's deadlines from a page that looks completely current, at the moment the
server is unreachable. Stale data presented confidently is the failure mode this
project is most concerned with.

It exists because iOS wants a registered service worker for the page to behave as
an installed application, and because M4's web push is delivered through its
`push` event.

---

## `/healthz` stays 200 when a data source is failing

`ok` is computed from the three structural checks — WAL, FTS5, migrations current
— and not from `sync_state`. A stale Canvas feed is a warning on the dashboard, not
an unhealthy container.

If sync failures marked the container unhealthy, Docker would restart the app
during a Canvas outage. Restarting fixes nothing, and it buries the actual signal
under a restart loop.

---

## Canvas ingestion (M1)

`app/ics.py` parses, `app/canvas.py` fetches and reconciles, `app/scheduler.py`
runs it every 30 minutes. The split is deliberate: the parser is pure functions
with no network and no database, which is what makes the feed's quirks cheap to
test.

### What the real feed forced

Design decisions here came from reading Mason's actual feed rather than the RFC:

- **Unfold before anything else.** The feed has 330 continuation lines, and two of
  twelve event titles wrap *inside the course code*. Line-oriented parsing yields
  `[FA26-BL-MATH-`, matches no course, and quietly queues the event for review —
  a failure that only affects events with long titles and produces no error.
- **Parameters can repeat.** Canvas emits `DTSTART;VALUE=DATE;VALUE=DATE`. They are
  kept as lists; overwriting silently would be a bug waiting for a feed that puts
  something meaningful in the second one.
- **Two date forms.** Bare `YYYYMMDD` for all-day items, `...Z` for timed ones.
- **All-day means 23:59 local**, converted to UTC for storage. Confirmed with
  Mason. It is what an assignment "due" on a date means to the person it is due
  from, and 9am would show things as overdue that are not — SPEC §9 warns that
  visibly wrong data ends trust in the whole ranking.

### Rules that look like edge cases and are not

- **Diff on UID.** Stable across polls, so a moved deadline is distinguishable from
  a new assignment. SPEC §6.1.
- **Never hard-delete.** An event that vanishes is marked with
  `feed_missing_since`. A transient feed error and an unpublished assignment look
  identical from here, and only one of them should cost data. SPEC §6.6.
- **HTML is refused, not parsed as empty.** Canvas serves a web page when the feed
  address is stale. Reading that as a calendar with zero events would mark every
  assignment as vanished in one poll — the most destructive thing this code could
  do, from the most ordinary cause.
- **An unchanged event is not written.** Keeps `updated_at` meaning that something
  changed, and keeps `audit_log` free of noise.
- **A moved due date supersedes pending reminders** rather than mutating `fire_at`.
  Nothing writes reminders until M3; doing it now means M3 does not start with
  stale rows. SPEC §5.

### The feed address is a credential

Both `HTTPError` and `URLError` render the URL they failed on, and
`sync_state.last_error` is displayed in the browser. Every error path substitutes a
fixed description and suppresses the original with `raise ... from None`. Tests
assert the token reaches neither the database nor the logs. SPEC §11.

### Courses are created, not queued

The feed identifies a course by an SIS code and carries no readable name. First
sight of a code creates the course with the code as a placeholder name and
`needs_naming = 1`, so assignments attach immediately rather than waiting in a
queue for Mason. Creating a course requires a term, since `courses.term_id` is
`NOT NULL`; the code's prefix names it (`FA26`) and its dates are seeded from the
feed's own range with `needs_dates = 1`, because a guess must be labelled as one.

### Estimated hours stay NULL at ingest

SPEC §9 says the owner supplies them and warns that unexplained numbers destroy
trust in the ranking. A per-type default at ingest would put numbers Mason never
typed into the field the entire prioritisation engine reads. M2 prompts for them,
which is where SPEC puts that interaction.

### Polling lives in the app, backups live in cron

The backup must survive a crash-looping container, because that is when last
night's copy matters. Polling Canvas is meaningless when the app is down. Hence
one in cron and one in an asyncio task, which looks inconsistent and is not.

---

## The chat layer (M5)

`app/claude_chat.py`. SPEC §10 settles the shape: no intent classifier, tools plus
a model that chooses among them. A mixed question — "when is my bio lab due and
can you explain the assay" — is one turn through one code path.

### Built before M3 and M4, and what that cost

Pulled forward at Mason's request. Two of SPEC §10's four tools read `documents`,
which is M4. The consequence needed designing around rather than discovering:
asked about an email it has no tool to look up, a model reconstructs one. The
system prompt states there is no archive and forbids guessing, and a test asserts
that text is present. Until M4 the correct answer to "what did she email me" is "I
cannot see your messages".

### A hand-written loop, not the SDK tool runner

The runner is the better default. It also keeps its own message history and does
not expose it, and this milestone persists every turn — content, tool calls, tool
results, token counts — to `chat_messages` as it happens. Owning the loop is the
requirement, not a preference.

### What the model's own documentation changed

Written after reading the current API reference rather than from memory, which
changed four things that would otherwise be wrong:

- `temperature`, `top_p` and `budget_tokens` return **400** on this model. None
  appear anywhere.
- **Thinking is on by default**, and `max_tokens` caps thinking and reply
  together — a budget sized for the answer alone truncates mid-sentence.
- `thinking.display` defaults to **omitted**, which in a streaming UI is a dead
  pause until reasoning finishes. Set to `summarized` and shown collapsed.
- Safety classifiers can decline with an **HTTP 200 and empty content**, so
  `stop_reason` is checked before `content` is ever indexed. Server-side fallbacks
  are enabled, but fail-safe: the beta could not be tested without a key, so a
  rejection logs once and continues without it. An untestable optional feature
  must not be able to take the feature down.

### Cost is recorded per message, not per model

`chat_messages.model` stores which model produced each turn. Switching from Opus
to Sonnet must not silently re-price the history, and a global constant would.
Cache reads and writes are counted separately from ordinary input because they
bill at roughly a tenth and one and a quarter times the input rate.

`CHAT_MODEL` and `CHAT_EFFORT` are settings rather than constants. SPEC §4 forbids
a provider abstraction; this is not one — same API, different model string.

### Prompt caching, and why it probably does nothing yet

The system prompt is split so the stable instructions carry the cache breakpoint
and the volatile context follows. That ordering is required: caching is a prefix
match, so today's date above the instructions would invalidate every request.

The instruction block is around 475 tokens and the minimum is 512, so it likely
does not cache at all — silently, with a zero in `cache_read_tokens`. Left alone
deliberately: caching that block would save about a hundredth of a cent per
message, and padding it to clear the threshold would cost more than it returns.
The breakpoint stays because it is free and starts paying when M4's archive rules
grow the prompt past the minimum.

### Replies are Markdown, and maths is not

Two separate problems, found by reading a real answer rather than a test.

Claude replies in Markdown, and the page rendered it as plain text — so a reply
came out full of literal `**asterisks**`. Rendered now with `markdown-it-py`,
configured with `html=False` so raw HTML in a reply is escaped rather than passed
through, and links with a `javascript:` scheme are refused outright rather than
sanitised. Questions stay plain text; only replies are rendered.

Current models also default to **LaTeX** for anything mathematical, and there is
no maths renderer here, so `$$\frac{x^2-1}{x-1}$$` reached the page as exactly
that. The prompt now asks for plain characters — `lim(x→1)`, `(x²−1)/(x−1)`,
`f'(x)` — with displayed expressions indented so they render as a scrollable block.

Rejected: bundling KaTeX or MathJax. It is a few hundred kilobytes of JavaScript
and a build step, on a 1 vCPU server, to typeset maths that reads perfectly well
as `lim(x→1) (x²−1)/(x−1) = 2` — and better than tiny typeset fractions on a
phone. Worth revisiting only if he starts asking things where notation genuinely
carries the meaning.

### One source of truth for slack

The `get_assignments` tool ranks through `app/priority.py` — the same code the
Today view uses. Two implementations of "how much spare time is there" would
eventually disagree, and Mason would find out mid-week, from a number the chat
gave him that the dashboard contradicts.

---

## Search: FTS5, no vectors

Settled in SPEC §7, restated here because it is the question most likely to be
revisited:

- Anthropic has no embeddings endpoint, so vectors mean a second vendor.
- Auto-ingestion is unavailable, so the archive is **curated, not exhaustive** —
  the few dozen messages a semester that matter, not every listserv blast.
  Signal-to-noise is high and keyword search does well on it.
- The corpus is small enough that a BM25 top-30 fits in the context window.

Add vectors only if specific real queries are observed to fail. Do not build for
the possibility.

---

## What M0 deliberately left out

- **`docs/DAILY_USE.md`** — a SPEC §0 required document, deferred to M2. There is
  nothing to use yet, and documenting an empty page would produce something nobody
  would trust later.
- **Canvas polling, the Today view, reminders** — M1, M2, M3.
- **Log rotation for the container** — Docker's `json-file` driver is capped at
  3 × 10MB in `docker-compose.yml`, which is sufficient.
- **A firewall.** SSH listens on the tailnet address only and the dashboard's port
  is published only there, so there is nothing on a public interface for a firewall
  to filter. Adding `ufw` would introduce a second place for the rules to be wrong
  and a way to lock oneself out.

---

## Things a future reader will be tempted to change, and should not

| Temptation | Why not |
|---|---|
| Bind uvicorn to the tailnet address instead of `0.0.0.0` | That address does not exist inside the container. The host-side published port is the boundary. |
| Add caching to the service worker | It would serve stale deadlines confidently. See above. |
| Make `/healthz` fail when Canvas sync fails | Docker would restart the app during someone else's outage. |
| Add down-migrations | Untested rollback paths are worse than none; restore from backup instead. |
| Edit an applied migration to fix a small typo | The runner refuses to start. Add a new file. |
| Copy the database with `cp` for a quick backup | WAL means that can produce a file that will not open. Use `sqlite3 .backup`. |
| Put the age private key on the server for convenience | It is the one thing making a server compromise survivable. |
| Open a public port "temporarily, to test something" | SPEC §3. Use Tailscale from the device you are testing with. |
