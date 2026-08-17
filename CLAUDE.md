# CLAUDE.md

Read by Claude Code at the start of every session in this repo. Keep this short — anything long belongs in SPEC.md or docs/, not here.

## Project

Personal academic dashboard for Mason: Canvas ICS ingest, CalDAV reminder ladders, a verbatim message archive, and a Claude-powered chat layer. Single user, forever.

**SPEC.md is authoritative** on product decisions. This file governs workflow and environment. Read SPEC.md in full, every session, before writing code.

## Start of every session

1. Read `SPEC.md` in full.
2. Run `git log` and `git status` to see what's already built.
3. State which milestone (M0–M7) you're starting or continuing, and restate your plan before writing any code.
4. If the plan touches the schema, the reminder engine, or anything under "Ask first" below, use plan mode and wait for confirmation before acting.

One milestone per session unless told otherwise. Don't start M(n+1) while M(n) is unfinished or untested.

## Environment reality — overrides SPEC.md §4 on hardware

- VPS: Vultr, Shared CPU, Regular Performance, **1 vCPU, 1–2GB RAM**. Not 4GB. Don't size caches, builds, or containers assuming more memory than this exists.
- OS: Debian 12. Non-root user `mason`. Docker installed.
- Reachable only via Tailscale. The app binds to the tailnet interface, never `0.0.0.0`. No public ports, ever — don't open one to make testing easier.
- Connection details (tailnet IP, hostname) are not committed anywhere, including here. Ask if you need them.

## Hard stack constraints — do not substitute or "improve"

- SQLite only, WAL mode, FTS5 compiled in. No Postgres, no MySQL.
- No Redis, no task queue, no Celery. Cron or an in-process scheduler only.
- No vector database, no embeddings. Retrieval is FTS5 + BM25 — see SPEC.md §7.
- Chat is Claude API only. No local LLM, no provider abstraction for it.
- No auto-scheduling into calendar blocks, no pomodoro timers, no streaks, no gamification — rejected explicitly, see SPEC.md non-goals.

If one of these seems wrong, say so and explain why. Don't silently work around it.

## Secrets

Never write a real secret value into anything this repo tracks — not in code, comments, doc examples, or commit messages. Real values live only in the server's root-owned `600` env file, outside the repo. Reference secrets by name (`CLAUDE_API_KEY`, `CANVAS_ICS_URL`), never by value. If you're about to paste something that looks like a live key, a token-bearing URL, or a password, stop.

## Documentation

Per SPEC.md §0: write `docs/SETUP.md` in the same session as the feature it documents, not after the fact. Doc quality is part of each milestone's done-criteria, not cleanup. Assume the reader has never used a terminal.

## Git

- Small commits, one logical change each, at every working state.
- Never leave `main` in a broken state — branch if pausing mid-feature.
- Commit messages say what changed and why, not just which file.

## Ask first, don't guess

- Any destructive operation: migrations that drop or alter columns, deleting data, disabling auth.
- Any product decision not already settled in SPEC.md.
- Before marking a milestone done, re-check its "Done when" line in SPEC.md §12 against what was actually built, not just that code exists.

## Verify, don't assume

- M1: confirm against Mason's real Canvas feed, not a synthetic one.
- M3: confirm a real reminder fires on his phone with the laptop closed before calling it done.
- M4: confirm dedup against an actual Canvas-conversation-that's-also-an-email before trusting it.
