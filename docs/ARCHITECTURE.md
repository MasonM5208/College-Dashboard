# Architecture

For a developer — quite possibly Mason in a year, having forgotten all of this.

`SPEC.md` says what to build and why. This file says how it was built, and records
the decisions that are not obvious from reading the code, including the ones that
were rejected. A rejected alternative documented is a rejected alternative nobody
has to re-investigate.

Current state: **M0 complete.** M1 onward not started.

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

`0001_core.sql` creates only the tables M1–M3 need. Documents, the FTS5 index and
chat arrive in M4; capacity, the timer and calibration in M6. Each table lands
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
