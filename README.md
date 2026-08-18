# Semester Dashboard

A personal academic dashboard for one music performance major carrying 20 credits
across 8 courses. It does three things:

1. **Never miss a deadline.** Assignments arrive automatically from Canvas, with
   escalating reminders on both laptop and phone.
2. **Remember important messages, exactly, forever.** Canvas messages and emails
   are saved verbatim and searchable, with every answer citing the original.
3. **Answer questions.** A chat layer with the semester's schedule and deadlines
   always in context.

It runs on a small rented server reachable only from its owner's own devices, and
costs under $12 a month.

---

## Start here

**[docs/SETUP.md](docs/SETUP.md)** — zero to a running system. Written for a reader
who has never used a terminal. Roughly three hours, in as many sittings as you
like.

## The rest of the documentation

| File | For |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Building it, the first time |
| [docs/DAILY_USE.md](docs/DAILY_USE.md) | Using it, every day |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Checking on it, updating it, fixing it when it will not load |
| [docs/SECRETS.md](docs/SECRETS.md) | Every password and key: what it is for, and how to replace it |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works and why, for a developer |
| [SPEC.md](SPEC.md) | What is being built, and what is deliberately excluded |

---

## Current state

**M0 — Foundation: complete.** The server, the private network, the database with
migrations, the status page, and nightly encrypted backups that have been
restore-tested.

**M1 — Canvas ingestion: built, awaiting confirmation against the live feed.** The
Canvas calendar feed is polled every 30 minutes, parsed, and diffed on event UID:
new assignments appear, moved deadlines are detected, events that vanish are
flagged rather than deleted, and anything whose course cannot be identified is kept
for review. Assignments are listed at `/assignments`.

Two limitations worth knowing, both inherent to the feed rather than defects:
courses arrive identified only by an enrolment code, and **only work with a due
date set in Canvas appears at all**, so courses that do not use Canvas that way are
invisible. See `SETUP.md` Section 14.

**M2 — Today view: built, awaiting daily use.** The default screen ranks work by
slack — free hours before the deadline minus hours of work left — rather than by
which deadline is nearest, with the numbers behind every position shown alongside
it. One-tap status changes and time estimates, quick capture, pinning, manual
entry, syllabus batch entry with a preview step, and course management, so the
courses Canvas cannot see are represented too. See
[docs/DAILY_USE.md](docs/DAILY_USE.md).

**M5 — Chat: built, awaiting its first real call.** One endpoint with tools over
the assignment data, per SPEC §10's instruction not to write an intent classifier.
Today's date, courses, and the next 14 days of deadlines with slack are injected
into every request. Replies stream; token cost is recorded per message and the
running monthly total is shown. Pulled ahead of M3 and M4 by request. The two
archive tools SPEC §10 lists arrive with M4 and the table they read; until then
the assistant states plainly that it has no message archive.

Next: **M3**, reminders — ladders of escalating nudges pushed to Apple Reminders
over CalDAV, so a deadline reaches the phone with the laptop shut. Then **M4**,
the document archive, which completes the chat's tool set.

Build order and the acceptance criteria for each milestone are in [SPEC.md
§12](SPEC.md).

---

## For developers

Requires Python 3.12 or newer.

```
python3 -m venv .venv
```

```
.venv/bin/pip install -r requirements-dev.txt
```

```
.venv/bin/python -m pytest
```

To run it locally against a scratch database, without Docker:

```
DB_PATH=./scratch/dashboard.db .venv/bin/python -m app.migrate
```

```
DB_PATH=./scratch/dashboard.db .venv/bin/uvicorn app.main:app --port 8000
```

Then open <http://127.0.0.1:8000>.

Read [CLAUDE.md](CLAUDE.md) before contributing — it covers the workflow, the
constraints that are not open for substitution, and the rules about secrets.
