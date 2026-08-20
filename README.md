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
running monthly total is shown. Pulled ahead of M3 and M4 by request, and
completed by M4: all four of SPEC §10's tools are present, and any claim drawn
from a saved message carries a tappable link to the verbatim original.
Conversations are kept separate, and can be named, kept and deleted.

**M3 — Reminders: built, and blocked by iCloud.** Each assignment becomes
one item in Apple Reminders carrying its whole ladder of alerts, pushed over
CalDAV. Because the alerts live on the phone, they fire whether or not the server
is running. Quiet hours are respected, and a moved deadline retires its old
reminders and rebuilds them rather than moving them.

Every part of that works up to the point Apple takes delivery: discovery finds the
reminders list, the writes are accepted, and reading the collection back returns
the twelve to-dos. They then appear in the Reminders app on no device, and not on
iCloud.com either — including the items in that list which the dashboard did not
put there. The likeliest explanation is that Apple's Reminders app and its CalDAV
endpoint no longer read the same store. **Deferred rather than fixed**;
`docs/OPERATIONS.md` records what was established and what to try next, including
the calendar-event fallback that would satisfy SPEC §12's criterion.

So **M0–M2 and M4–M6 are working; M3 is built but not delivering.**

**M4 — Document archive: built, awaiting its first real save.** A verbatim record
of the messages that matter, saved from the iPhone share sheet in two taps or
pasted in on the laptop, and searched with FTS5 and BM25. What is stored is never
edited — enforced by a database trigger, not only by the code. Deduplication runs
before insert on a normalised copy, so one message arriving by two routes is kept
once with a note of both. Course links are manual; nothing is guessed.

**Automatic collection, with a review queue.** IU's Outlook can auto-forward, so
mail is forwarded to a mailbox used for nothing else and read over IMAP every
fifteen minutes. What arrives waits to be kept or discarded rather than entering
the archive — SPEC §7's case for keyword search rests on the archive being
curated rather than exhaustive, and a whole university account is exhaustive. A
forwarded copy of something already saved by hand is recognised as the same
message and recorded as another route, not queued again.

**M6 — Capacity, calibration and overload: built, awaiting a real week.** The
flat four-hours-a-weekday constant is replaced by a per-weekday budget, fixed
weekly commitments, and a practice target that is subtracted before anything is
ranked — because practice has no due date and would otherwise lose every
comparison in a deadline-driven sort, invisibly, for about a month.

A start/stop timer logs what work actually takes, and after three finished pieces
of a kind the dashboard reports how far off the estimates for that kind are.
It never rewrites a number Mason typed; it reports the multiplier and leaves the
applying to him.

**Overload mode** is the piece SPEC calls the highest-value feature in the
document after reminders. When the next seven days need more hours than exist, the
top of Today says so in plain hours and names what is cheapest to let slide, with
why each one is cheap. No toggle, no softening. Unestimated work is excluded from
the total rather than assumed free, so a shortfall is a floor.

The seeded defaults reproduce M2's constant exactly, so nothing re-ranks until
Mason describes his actual week on **Your week**.

Next: **M7** — grade tracking with a "what do I need on the final" calculator,
workload forecasting two to three weeks out, and practice-hours trending. Before
that, M3's reminders are worth another attempt; `docs/OPERATIONS.md` records where
that got to.

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
