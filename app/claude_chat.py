"""The chat layer.

SPEC §10 settles the architecture in one instruction: *"Do not write an intent
classifier. Do not try to decide whether a question is 'about the archive' or
'general.' Give Claude tools and let it choose."* So there is one endpoint, one
loop, and a set of tools — "when is my bio lab due and can you explain the assay"
is answered in a single turn without anything here deciding what kind of question
it was.

Two of SPEC §10's four tools read the `documents` table, which arrives in M4.
Until then the system prompt says plainly that no message archive exists, because
a model asked to recall a message it has no tool to look up will otherwise invent
one — and SPEC §10 makes unsourced archive claims a visible failure.

This is a hand-written tool loop rather than the SDK's tool runner. The runner is
the better default and keeps its own conversation history, but this milestone has
to persist every turn — content, tool calls, tool results, token counts — as it
goes, and the runner does not expose the history it holds.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import anthropic
from markdown_it import MarkdownIt

from app import config, priority

log = logging.getLogger("chat")

# Room for thinking *and* the reply — max_tokens caps the two together on current
# models, so a budget sized for the answer alone truncates mid-sentence.
MAX_TOKENS = 8192

# A question needing more tool calls than this is looping rather than working.
MAX_TOOL_ROUNDS = 6

# Cost per million tokens, (input, output). Used for the running monthly estimate
# SPEC §10 asks be surfaced "so it never surprises anyone".
PRICES_PER_MTOK = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_PRICE = (5.00, 25.00)

# Cached input is billed at about a tenth of the normal rate, and writing to the
# cache at about one and a quarter times.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


# Replies come back as Markdown, so they have to be rendered rather than printed.
# `html=False` makes the renderer escape any raw HTML in the text instead of
# passing it through, which is what keeps model output from becoming markup on a
# page. Links with a javascript: or similar scheme are dropped by the renderer's
# own validation.
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False})


def render_markdown(text: str | None) -> str:
    """Turn a reply into HTML that is safe to place on the page."""
    if not text:
        return ""
    return _MARKDOWN.render(text)


class ChatUnavailable(RuntimeError):
    """The chat cannot run. The message is safe to show on a web page."""


# Claude Opus 5 ships with elevated safety classifiers that can decline a request
# outright. Server-side fallbacks re-run a declined request on another model
# inside the same call, so a false positive on an ordinary coursework question
# recovers instead of failing.
#
# It rides on a beta flag, and this code could not be tested against the real API
# before deployment. So it is attempted, and if the API rejects the beta the code
# says so once and continues without it for the life of the process. An untestable
# optional feature must not be able to take the whole chat down.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_fallbacks_available = True


# --- the tools --------------------------------------------------------------
#
# Descriptions say *when* to call, not only what the tool does: current models
# reach for tools more conservatively, and a stated trigger condition measurably
# raises the rate at which they call one that would have helped.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_assignments",
        "description": (
            "Look up Mason's assignments, with due dates, status, estimated hours "
            "remaining, and how much spare time he has before each deadline.\n\n"
            "Call this whenever the question touches what is due, what to work on, "
            "how much work is left, whether he is behind, or how a week looks — "
            "including vague ones like 'how am I doing?'. Do not answer from the "
            "summary in the system prompt when a question needs anything beyond the "
            "next 14 days, a specific course, or completed work; call this instead.\n\n"
            "Spare hours is the number that matters: hours free before the deadline "
            "minus hours of work left. A negative value means the work does not fit "
            "in the time remaining and he is already behind on that item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {
                    "type": "integer",
                    "description": "Only this course. Omit for all courses.",
                },
                "due_before": {
                    "type": "string",
                    "description": (
                        "Only work due before this date, as YYYY-MM-DD. Omit for no limit."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "not_started", "in_progress", "submitted", "graded", "dismissed",
                    ],
                    "description": (
                        "Only work in this state. Omit for everything still outstanding."
                    ),
                },
            },
        },
    },
    {
        "name": "get_courses",
        "description": (
            "List Mason's courses: name, code, instructor, when each meets, credits, "
            "late policy, and his current grade where he has recorded one.\n\n"
            "Call this when a question involves a course's schedule, its instructor, "
            "its late policy, or what he is currently taking — and before advising "
            "about what to let slide, since the late policy and current grade decide "
            "what is actually cheap to sacrifice."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def run_tool(conn: sqlite3.Connection, name: str, arguments: dict, zone: ZoneInfo) -> str:
    """Execute one tool call and return its result as JSON text."""
    if name == "get_courses":
        rows = conn.execute(
            "SELECT c.id, c.name, c.code, c.instructor, c.meeting_pattern, c.credits, "
            "       c.late_policy, c.current_grade_pct, t.name AS term "
            "FROM courses c LEFT JOIN terms t ON t.id = c.term_id ORDER BY c.name"
        ).fetchall()
        return json.dumps({"courses": [dict(row) for row in rows]}, default=str)

    if name == "get_assignments":
        sql = [
            "SELECT a.id, a.title, a.type, a.status, a.due_at, a.pinned,",
            "       a.est_hours, a.est_hours_remaining,",
            "       c.name AS course_name, c.code AS course_code",
            "FROM assignments a LEFT JOIN courses c ON c.id = a.course_id",
            "WHERE 1=1",
        ]
        params: list[Any] = []

        if arguments.get("course_id") is not None:
            sql.append("AND a.course_id = ?")
            params.append(int(arguments["course_id"]))
        if arguments.get("due_before"):
            sql.append("AND a.due_at IS NOT NULL AND a.due_at <= ?")
            params.append(f"{str(arguments['due_before'])[:10]}T23:59:59Z")
        if arguments.get("status"):
            sql.append("AND a.status = ?")
            params.append(str(arguments["status"]))

        rows = conn.execute(" ".join(sql), params).fetchall()

        # Ranked through the same code the Today view uses, so the model sees the
        # identical numbers Mason does. Two sources of truth for "how much slack
        # is there" would eventually disagree, and he would find out mid-week.
        ranked = priority.rank(rows, zone)
        return json.dumps(
            {
                "assignments": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "course": item.course_name,
                        "type": item.type,
                        "status": item.status,
                        "due_at": item.due_at,
                        "hours_of_work_left": item.hours_left,
                        "hours_free_before_due": item.hours_free,
                        "spare_hours": item.slack,
                        "already_overdue": item.overdue,
                        "needs_an_estimate": item.needs_estimate,
                    }
                    for item in ranked
                ],
                "note": (
                    "Ordered by least spare time first. Items needing an estimate "
                    "cannot be ranked until Mason supplies one."
                ),
            },
            default=str,
        )

    raise ChatUnavailable(f"The model asked for a tool that does not exist: {name}")


# --- the system prompt ------------------------------------------------------

# Stable half. This carries the cache breakpoint, so it must not contain anything
# that changes between requests — a date here would invalidate the cached prefix
# on every single message.
INSTRUCTIONS = """\
You are the assistant inside Mason's personal academic dashboard. He is a music \
performance major carrying 20 credits across 8 courses, and he is usually asking \
between rehearsals with little time to spare.

How to answer:

- Be direct and brief. He is reading this on a phone between classes. Lead with the \
answer, then the reasoning if it is needed at all.
- Use the tools rather than guessing. A summary of his next two weeks is included \
below, but anything beyond it — a specific course, older work, completed work, \
totals — needs a tool call.
- When you talk about time pressure, use the numbers the tools give you: hours of \
work left, hours free before the deadline, and spare hours. Negative spare hours \
means the work does not fit in the time left and he is already behind on that item. \
Say that plainly when it is true; it is the single most useful thing you can tell him.
- Never invent a deadline, a grade, or a course detail. If a tool does not have it, \
say you do not have it.
- He asked for honesty over encouragement. If the numbers say he cannot finish \
everything, say so and say which item is cheapest to drop, using late policies and \
current grades. Do not soften it and do not add cheerleading.
- Answer general questions — coursework concepts, theory, writing, whatever he \
asks — as fully as they deserve. You are his assistant, not only a schedule reader.

How to format an answer:

- Markdown is rendered, so **bold**, lists, headings and `code` all display \
properly. Use them lightly — he is reading on a phone.
- **Write mathematics as plain text, never as LaTeX.** There is no maths renderer \
on this page, so `$x$`, `$$...$$`, `\\frac{a}{b}` and `\\lim` appear on screen as \
literal backslashes and dollar signs. Use real characters instead: → ² ³ √ ∞ ≤ ≥ ≠ \
± ∫ Σ π θ Δ. Write `lim(x→1)` rather than a LaTeX limit, `(x²−1)/(x−1)` rather than \
a fraction macro, and `f'(x)` for a derivative. Put a displayed expression on its \
own line, indented by four spaces so it renders as a code block and keeps its \
alignment.
- Keep tables small or skip them. A wide table is unreadable on a phone.

What you do not have yet:

- **There is no archive of his messages or emails.** That is not built. If he asks \
what a professor said, what an email contained, or anything that would require \
reading his correspondence, tell him plainly that the archive does not exist yet \
and that you cannot see his messages. Never reconstruct, guess at, or imply the \
contents of a message. Never claim to remember one.
- You cannot send reminders, change anything in Canvas, or mark work as done. He \
does that in the dashboard itself.\
"""


def build_context(conn: sqlite3.Connection, zone: ZoneInfo, now: datetime | None = None) -> str:
    """The volatile half of the system prompt.

    SPEC §10 lists exactly what goes in here, on every request regardless of
    topic: today's date and weekday, the term and course list, everything due in
    the next 14 days with status, and any stale sync source. "Cheap in tokens,
    and it eliminates most confusion."

    It sits *after* the cached instructions above, because it changes every time.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(zone)

    lines = [f"Today is {local:%A %-d %B %Y}, local time {local:%-I:%M %p} ({config.TZ})."]

    term = conn.execute(
        "SELECT name, start_date, end_date FROM terms ORDER BY start_date DESC LIMIT 1"
    ).fetchone()
    if term:
        lines.append(f"Current term: {term['name']} ({term['start_date']} to {term['end_date']}).")

    courses = conn.execute(
        "SELECT name, code, meeting_pattern FROM courses ORDER BY name"
    ).fetchall()
    if courses:
        lines.append("\nHis courses:")
        for course in courses:
            bits = [course["name"]]
            if course["code"]:
                bits.append(f"({course['code']})")
            if course["meeting_pattern"]:
                bits.append(f"meets {course['meeting_pattern']}")
            lines.append(f"  - {' '.join(bits)}")
    else:
        lines.append("\nNo courses have been set up yet.")

    horizon = (now + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.type, a.status, a.due_at, a.pinned,
               a.est_hours, a.est_hours_remaining,
               c.name AS course_name, c.code AS course_code
        FROM assignments a LEFT JOIN courses c ON c.id = a.course_id
        WHERE a.due_at IS NOT NULL AND a.due_at <= ?
        """,
        (horizon,),
    ).fetchall()

    upcoming = priority.rank(rows, zone, now)
    if upcoming:
        lines.append("\nDue in the next 14 days, least spare time first:")
        for item in upcoming:
            spare = (
                "needs an estimate"
                if item.slack is None
                else f"{item.slack:+.1f}h spare"
            )
            hours = "?" if item.hours_left is None else f"{item.hours_left:g}"
            lines.append(
                f"  - {item.title} ({item.course_name or 'no course'}) "
                f"due {item.due_local:%a %-d %b %-I:%M %p} · {hours}h of work left · "
                f"{spare} · {item.status.replace('_', ' ')}"
            )
    else:
        lines.append("\nNothing is due in the next 14 days.")

    # SPEC §4 and §6: if a source is stale, say so, because otherwise an empty
    # answer reads as "nothing due" rather than "the data stopped arriving".
    from app import status as status_module

    for source in status_module.sync_sources(conn, now):
        if source["level"] != "ok":
            lines.append(
                f"\nWarning: {source['label']} is {source['level']} — last succeeded "
                f"{source['last_success_at'] or 'never'}. Assignment data may be "
                f"incomplete, and you should say so if it is relevant."
            )

    return "\n".join(lines)


def system_blocks(conn: sqlite3.Connection, zone: ZoneInfo, now: datetime | None = None):
    """The system prompt, split so the stable half can be cached.

    Caching is a prefix match, so the unchanging instructions come first and carry
    the breakpoint; today's date and deadlines follow. Reversed, every request
    would miss.

    Whether it actually caches is another matter, and worth being straight about:
    the instruction block is around 475 tokens, and this model needs 512 before it
    will cache anything. Under that it silently does nothing — no error, just a
    zero in cache_read_tokens, which the status page shows.

    That is deliberately left alone rather than padded to clear the threshold.
    Caching 475 tokens of instructions would save roughly a hundredth of a cent a
    message; writing filler to earn it would cost more in tokens than it returns.
    The breakpoint stays because it costs nothing and starts paying the moment the
    instructions grow — M4's archive rules will push it well past the minimum.
    """
    return [
        {
            "type": "text",
            "text": INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": build_context(conn, zone, now)},
    ]


# --- cost -------------------------------------------------------------------


def message_cost(row) -> float:
    """Dollars for one stored message, priced by the model that produced it."""
    price_in, price_out = PRICES_PER_MTOK.get(row["model"] or "", DEFAULT_PRICE)
    return (
        row["input_tokens"] * price_in
        + row["cache_read_tokens"] * price_in * CACHE_READ_MULTIPLIER
        + row["cache_write_tokens"] * price_in * CACHE_WRITE_MULTIPLIER
        + row["output_tokens"] * price_out
    ) / 1_000_000


def month_to_date_cost(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """What the chat has cost so far this calendar month (SPEC §10)."""
    now = now or datetime.now(timezone.utc)
    since = now.strftime("%Y-%m-01T00:00:00Z")

    rows = conn.execute(
        "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
        "       cache_write_tokens FROM chat_messages WHERE created_at >= ?",
        (since,),
    ).fetchall()

    return {
        "since": since[:10],
        "messages": len(rows),
        "dollars": round(sum(message_cost(row) for row in rows), 4),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "cache_read_tokens": sum(row["cache_read_tokens"] for row in rows),
    }


# --- the loop ---------------------------------------------------------------


@dataclass
class Turn:
    """What one exchange produced, for persisting and for showing on the page."""

    text: str = ""
    thinking: str = ""
    stop_reason: str | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    refused: bool = False


def build_client():
    """The Anthropic client, or a clear error if the key is missing."""
    try:
        key = config.require("CLAUDE_API_KEY")
    except config.MissingSetting as exc:
        raise ChatUnavailable(str(exc)) from None
    return anthropic.Anthropic(api_key=key)


def _open_stream(client, request: dict):
    """Start the request, with server-side fallbacks where the API accepts them."""
    global _fallbacks_available

    if _fallbacks_available:
        try:
            return client.beta.messages.stream(
                **request, betas=[FALLBACK_BETA], fallbacks="default"
            )
        except anthropic.BadRequestError as exc:
            _fallbacks_available = False
            log.warning(
                "This API does not accept the %s beta, so a declined request will "
                "not be retried on another model automatically. Everything else "
                "works normally. Reported: %s",
                FALLBACK_BETA,
                exc,
            )

    return client.messages.stream(**request)


def _usage(turn: Turn, usage) -> None:
    turn.input_tokens += getattr(usage, "input_tokens", 0) or 0
    turn.output_tokens += getattr(usage, "output_tokens", 0) or 0
    turn.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
    turn.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


def answer(
    conn: sqlite3.Connection,
    history: list[dict],
    zone: ZoneInfo,
    client=None,
    now: datetime | None = None,
) -> Iterator[tuple[str, Any]]:
    """Run one question to completion, yielding events as they happen.

    Yields ``("text", chunk)`` as the reply arrives, ``("thinking", chunk)`` for
    reasoning summaries, ``("tool", name)`` when a tool runs, and finally
    ``("done", Turn)``.

    The caller persists the Turn. Streaming matters for more than appearance: it
    is also what keeps a long answer from hitting an HTTP timeout.
    """
    client = client or build_client()
    turn = Turn(model=config.CHAT_MODEL)
    messages = list(history)

    for _ in range(MAX_TOOL_ROUNDS):
        request = {
            "model": config.CHAT_MODEL,
            "max_tokens": MAX_TOKENS,
            "system": system_blocks(conn, zone, now),
            "messages": messages,
            "tools": TOOLS,
            # Summarized rather than the default of omitted: with reasoning
            # hidden, a streaming reply shows nothing at all until the model has
            # finished thinking, which reads as a hang.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": config.CHAT_EFFORT},
        }

        text_parts: list[str] = []
        with _open_stream(client, request) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type == "text_delta":
                    text_parts.append(event.delta.text)
                    yield ("text", event.delta.text)
                elif event.delta.type == "thinking_delta":
                    turn.thinking += event.delta.thinking
                    yield ("thinking", event.delta.thinking)

            response = stream.get_final_message()

        _usage(turn, response.usage)
        turn.model = getattr(response, "model", config.CHAT_MODEL)
        turn.stop_reason = response.stop_reason

        # Checked before content is touched. A declined request returns HTTP 200
        # with an empty or partial content list, so indexing it blindly raises.
        if response.stop_reason == "refusal":
            turn.refused = True
            turn.text = (
                "Claude declined to answer that one. If it was an ordinary "
                "coursework question, rephrasing usually gets past it."
            )
            log.warning("Chat request was declined by the model's safety classifiers.")
            yield ("done", turn)
            return

        turn.text = "".join(text_parts)

        if response.stop_reason != "tool_use":
            yield ("done", turn)
            return

        # Keep the whole assistant turn, thinking blocks included: they have to go
        # back unchanged or the next request is rejected.
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            yield ("tool", block.name)
            turn.tool_calls.append({"name": block.name, "input": block.input})
            try:
                output = run_tool(conn, block.name, dict(block.input or {}), zone)
                is_error = False
            except Exception as exc:  # noqa: BLE001 — reported to the model, not raised
                output = f"That lookup failed: {exc}"
                is_error = True
                log.exception("Chat tool %s failed", block.name)

            turn.tool_results.append({"name": block.name, "output": output[:2000]})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                    "is_error": is_error,
                }
            )

        # All results go back in one message. Splitting them teaches the model to
        # stop asking for tools in parallel.
        messages.append({"role": "user", "content": results})

    turn.text = turn.text or (
        "That question needed more lookups than expected, so I stopped rather "
        "than keep going. Try asking it in smaller pieces."
    )
    turn.stop_reason = "tool_round_limit"
    yield ("done", turn)
