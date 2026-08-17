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
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Checking on it, updating it, fixing it when it will not load |
| [docs/SECRETS.md](docs/SECRETS.md) | Every password and key: what it is for, and how to replace it |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works and why, for a developer |
| [SPEC.md](SPEC.md) | What is being built, and what is deliberately excluded |

`docs/DAILY_USE.md` arrives with M2, when there is something to use.

---

## Current state

**M0 — Foundation: complete.** The server, the private network, the database with
migrations, the status page, and nightly encrypted backups that have been
restore-tested.

Next: **M1**, pulling assignments from the Canvas calendar feed.

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
