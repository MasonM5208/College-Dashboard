# Architecture

For a developer — quite possibly Mason in a year, having forgotten all of this.

`SPEC.md` says what to build and why. This file says how it was built, and records
the decisions that are not obvious from reading the code, including the ones that
were rejected. A rejected alternative documented is a rejected alternative nobody
has to re-investigate.

Current state: **M0 through M5 built.** SPEC §12's useful core, the chat with all
four of its tools, and the archive they read. M6 — the real capacity model, the
start/stop timer, estimate calibration and overload mode — is not started, so
everything is still ranked against a flat four productive hours a weekday.

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
Canvas ingestion required, `0003_chat.sql` the two chat tables, `0004` the
reminder ladders, `0005` the kept flag on conversations, `0006` the documents,
their provenance, their links and the FTS5 index, and `0007` the review queue for
collected mail. Capacity, the timer and calibration arrive with M6. Each table
lands
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
an installed application. Web push, the other thing a service worker is for, is
not used at all — SPEC §8 sends deadline reminders through Apple Reminders
instead, for reasons set out under Reminders below.

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

## Reminders (M3)

`app/reminders.py` decides when to nudge; `app/caldav_push.py` gets those moments
onto the phone. SPEC §12 judges this milestone on one sentence — a reminder fires
on the iPhone with the laptop closed.

### One to-do carrying many alerts

Each assignment becomes a single `VTODO` with one `VALARM` per rung, rather than
one to-do per rung. Two consequences, and the second is the important one:

- The Reminders list stays one line per piece of work. Twelve Canvas assignments
  would otherwise have become about thirty separate to-dos, four of them the same
  exam.
- **iOS does the firing.** The scheduled job is therefore not "send a reminder at
  the right moment" but "keep Apple's copy in step with ours". If the server is
  down at 7am on a Saturday, the alert still fires — it was pushed days earlier and
  lives on the phone. A design where the server must be up at the moment of the
  nudge would fail exactly when it is least noticed.

SPEC §5's "materialize every reminder as its own row" still holds: one row per
rung here, regardless of how they are packaged for Apple. That is what makes a
moved deadline coherent.

### Ladders are data

`0004_reminder_defaults.sql` seeds SPEC §8's ladders into `reminder_rules`, so
tuning them later is a form rather than a deploy. SPEC gives no ladder for `other`;
it gets the worksheet one, on the grounds that silence is the worse default.

### Quiet hours

22:30–07:30, per SPEC §8, with a `due_by` shifting earlier and a `start_by` later.
The case worth writing down: "earlier" for something landing at 03:00 means 22:30
*the previous evening*, because 22:30 the same day is still ahead of it. Rungs
already past are dropped, so an assignment entered three days out does not fire its
ten-day nudge on save.

### A stable UID, deliberately

The to-do's UID is derived from the assignment id, so a re-push overwrites. A
generated UID would add a second copy every sync, and a Reminders list filling with
duplicates is the failure that would make him turn the feature off rather than
report it.

### `sent` means "on the phone", which changes what a moved deadline does

A moved deadline supersedes reminders already marked `sent`, not only `pending`
ones — because under this design `sent` means the alarm is sitting inside the
to-do on the phone, and leaving it would fire an alert at the old time. Superseded
rows are kept, so SPEC §5's auditability is intact. This changed an M1 test that
had encoded the weaker rule before M3 existed.

### Rejected: the `caldav` package

It pulls in seven dependencies including lxml, a C extension, to perform three
verbs against one server: PROPFIND to find the list, PUT to write a to-do, DELETE
to remove one. Written against `urllib` and `xml.etree` instead — the same call
made for the Canvas fetch in M1, and it matters more on a 1 vCPU box where every
dependency is something that breaks on upgrade at a moment nobody chose.

Discovery is four requests, cached in `sync_state.cursor` — a column SPEC §5
defined in M0 that nothing had used until now.

### The probe exists because I could not test this

There is no Apple app-specific password on a development machine, so the code
reached the server unproven against iCloud. `python -m app.caldav_push --probe`
walks discovery step by step and writes nothing, turning "reminders do not work"
into "discovery reached the calendar home and then found no list accepting
to-dos", which is a fixable sentence. `--dry-run` prints the exact to-do that would
be sent.

### Completion does not flow back

SPEC §8 defers this and the reason is cost, not oversight: reading state back needs
a CalDAV poll loop. Ticking a reminder off in iOS does not mark the work done here.
`DAILY_USE.md` states it plainly, because the failure mode — the dashboard still
counting hours for work already finished — is quiet and would erode trust in the
ranking.

## The chat layer (M5)

`app/claude_chat.py`. SPEC §10 settles the shape: no intent classifier, tools plus
a model that chooses among them. A mixed question — "when is my bio lab due and
can you explain the assay" — is one turn through one code path.

### Built before M3 and M4, and what that cost

Pulled forward at Mason's request, and it shipped for a fortnight with two of
SPEC §10's four tools missing, because both read `documents`. The gap had to be
designed around rather than discovered: asked about an email it has no tool to
look up, a model reconstructs one. So the prompt stated outright that no archive
existed and forbade guessing, and a test asserted that text was present.

M4 removed that paragraph and replaced it with the citation rules. The
replacement matters as much as the removal — a prompt that still claimed the
archive did not exist would make the model refuse to use its own tools.

### One conversation at a time

The first cut opened the most recently used thread whenever `/chat` was visited
and posted every question into it, so the whole term became one transcript. That
is bad to read and it is also billed for: `_history_for_model` replays a thread in
full on every turn, so an unrelated question a week later pays to re-send
everything before it.

`/chat` with no `?thread=` now starts a new conversation, and `?thread=<id>`
continues a named one. `0005_chat_threads.sql` adds a single `pinned` column so a
handful of conversations can sort above the rest; that is the entire filing
system, deliberately. Tags or folders need tending, and anything needing tending
stops being tended in November.

Deleting is a real `DELETE` rather than a flag, behind a confirmation rendered in
place on the list. `chat_messages` is deleted explicitly rather than left to the
foreign key's `ON DELETE CASCADE`, which is silently a no-op on any connection
where `PRAGMA foreign_keys` was not set — true of `sqlite3` at a shell prompt.
Recovery is the nightly backup, and `OPERATIONS.md` has a read-only procedure for
pulling one conversation out of a restored copy without touching the live file.

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

The instruction block was around 475 tokens against a 512-token minimum, so it
likely did not cache at all — silently, with a zero in `cache_read_tokens`. It was
left alone rather than padded: caching it would have saved about a hundredth of a
cent per message.

M4's archive and citation rules took it past 800 tokens, so the breakpoint that
was doing nothing is now doing what it was put there for. This is the
argument for placing a cache breakpoint before it pays: it costs nothing while it
is useless, and moving one later means finding every place the prompt is
assembled.

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

## The document archive (M4)

`app/archive.py` and `migrations/0006_documents.sql`. SPEC §7 gives four ways in
and one place they land; this is the one place.

### Every route converges on one function, not one endpoint

SPEC §7 says the four paths "converge on one ingest endpoint and one `documents`
insert". They converge on `archive.ingest()`. The distinction is deliberate:
`POST /ingest` carries a bearer token for the Shortcut, and making a browser form
carry that token would be worse than having two thin adapters over one function.
"Build the pipeline once; adapters are thin" is the sentence that matters, and it
holds — neither route touches `documents` itself, and a mail poller would be a
third caller of the same function.

### The hash is of a body that is never stored

`documents.body` is verbatim. `documents.body_sha256` is the SHA-256 of a
*normalised* copy that exists only long enough to be hashed: quoted reply chains
gone, signature block gone, "Sent from my iPhone" gone, curly quotes folded to
straight ones, whitespace collapsed, case folded.

That split is the whole dedup design. Hashing what is stored would file two copies
of everything that arrives twice, which is exactly what SPEC §7 says will happen —
"Canvas conversations arrive both in Canvas and by email. You will double-ingest."
A test asserts that the same message arriving via Gmail's quoting, Outlook's
header block and an iPhone's footer produces one fingerprint, using real-shaped
mail rather than tidy fixtures.

`UNIQUE` on the hash column means a bug in the normaliser cannot quietly produce
the duplicate the normaliser existed to prevent.

### Immutability is enforced twice

SPEC §5 asks for the application layer and a schema comment. There is also a
`BEFORE UPDATE OF body, body_sha256` trigger that raises. Three lines, and it
still holds in M6, in M7, and at a `sqlite3` prompt at one in the morning — which
application-level enforcement does not.

Everything *around* the body stays editable: a subject can be corrected, a sender
filled in, a kind reclassified, and the FTS5 index follows. Only the record of
what was said is frozen. Deleting a whole document is allowed, because a mis-paste
has to be removable; what cannot happen is a message being quietly rewritten.

### Two things the search had to get right

**Hostile input.** FTS5's `MATCH` takes a query language, not a phrase. An
unbalanced quote is a syntax error and `AND`, `OR`, `NOT`, `NEAR` and `*` are
operators — all of which are ordinary things to type into a search box. Every word
is extracted with `\w+` and re-quoted before it reaches `MATCH`, so searching
`NEAR` finds documents instead of raising a 500.

**Snippets.** `snippet()` wraps hits in markers, and those markers have to become
`<mark>` without the surrounding message text becoming markup. The escape happens
first and the markers are control characters (`\x02`, `\x03`) that cannot occur
in a real message, so a body containing the literal text `<mark>` or `<script>` is
rendered as text either way.

### Course links are manual, and the columns for automatic ones exist anyway

Nothing guesses which course a message belongs to. `document_links` still carries
SPEC's `confidence` and `created_by ('auto'|'manual')` columns, because adding a
column to a populated table later is the destructive `ALTER` CLAUDE.md says to ask
before doing, and because the argument against guessing is a product decision
rather than a schema one.

### Citations are enforced in two independent places

SPEC §10 requires both that citations be enforced in the system prompt and that
unsourced archive claims be "a visible UI state". A prompt is an instruction, not
a guarantee, so the page checks separately: a reply whose stored `tool_calls`
include `search_archive` or `get_document` but whose text contains no
`/archive/<id>` link renders with a red **No citation** strip.

It is computed at render time from data already stored, so there is no new column
and no future edit to the prompt can switch the check off by accident.

---

## SPEC §13's open items, resolved

Both were to be settled before M4, and both now are.

1. **Gmail "Check mail from other accounts"** — unavailable. Gmail offers no such
   pull, so the path SPEC named does not exist.
2. **Mac Mail bridge** — not needed. It was the fallback for exactly this, and it
   required the MacBook awake and online, which the arrangement below does not.

The path that works was not on SPEC's list: IU's Outlook can auto-forward out,
even though it blocks the forwarding SPEC §2 recorded as unavailable. The two are
different permissions, and only one of them is switched off.

---

## Collecting mail (M4, added after the fact)

`app/mailbox.py`. SPEC §7 lists a Gmail POP poll as the automatic path and marks
it **Test first**; §13 says to resolve it before M4. It resolved negatively —
Gmail offers no such pull — and M4 shipped manual-only. Then Mason found that IU's
Outlook can auto-forward, which reopens the same door from the other side: forward
to a mailbox used for nothing else, and read that mailbox over IMAP.

### A queue, not a pipe

Collected mail lands in `inbound_messages` and waits. Nothing here writes to
`documents`.

The reason is the same one SPEC §7 uses to reject vectors: keyword search works
because the archive is "curated rather than exhaustive — roughly the few dozen
messages per semester that actually matter, not thousands including every listserv
blast". A whole university account piped into `documents` would remove that
premise, and the failure would be gradual and hard to name — searches quietly
getting worse over a term.

It is also the same shape as M1's review queue for Canvas events whose course
cannot be identified (SPEC §6.4). Nothing is discarded by the machine, and nothing
is admitted by it either.

**Discarded rows are kept**, in the `discarded` state. Deleting them would mean
the next poll saw the message as new and asked again, which is the fastest way to
train someone to ignore a queue.

### The normaliser bug this found

A forwarded message puts the *entire* body below a quote marker. The original
`normalize` read that as "a reply with nothing above the quote", stripped
everything, and produced an empty string — so `ingest` refused every forwarded
message outright. Automatic collection would have collected nothing, and the tests
that existed all passed, because every fixture was a reply.

`_content_lines` now falls back: when nothing survives above the marker, the
wrapper and its header block are dropped and what is below is kept. The second
effect is the more valuable one — Outlook's `-----Original Message-----`, Gmail's
`---------- Forwarded message ---------`, Apple Mail's `Begin forwarded message:`
and plain `>` quoting all now reduce to the same fingerprint as the message saved
by hand from the share sheet. A forwarded copy of something already archived is
therefore recorded as another provenance row rather than queued a second time,
which is what stops the queue asking about things Mason has already dealt with.

### Details that will look arbitrary later

- **`UIDVALIDITY` is part of the cursor.** A mailbox that is rebuilt reissues UIDs
  from 1; without it, message 1 of the new mailbox looks like message 1 of the old
  one and is skipped forever. When it changes, the mailbox is re-read from the
  start, which is safe only because the hash check catches everything already
  decided on — and that is asserted by a test.
- **The folder is selected read-only.** Collecting must not mark mail as read in a
  mailbox somebody may also be looking at.
- **`MAX_PER_POLL` is 40.** A first run against a term's backlog should not hold a
  connection open for minutes or produce a queue of four hundred items in one go.
- **The provenance value is `gmail_poll`.** SPEC §5 fixes four values in a CHECK
  constraint and that is its name for "collected from a mailbox", from when the
  expected provider was Gmail. Widening a CHECK means rebuilding the table, which
  is the destructive migration CLAUDE.md says to ask before writing. It reads as
  "forwarded email" in the interface.
- **HTML mail is converted by a 30-line `HTMLParser` subclass**, not a library.
  It is not a renderer and does not try to be: drop `script`/`style`/`head`, turn
  block tags into newlines, keep the text. The result only has to be readable and
  searchable.

### Rejected: filtering here instead of at the forwarding end

An obvious optimisation is a rule that only queues mail from known instructors.
It is not built, and should not be: a filter here decides what matters on Mason's
behalf, silently, and the whole queue exists so that decision stays his. If the
volume becomes unbearable the fix belongs in IU's Outlook, where the rule is
visible and he wrote it.

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
