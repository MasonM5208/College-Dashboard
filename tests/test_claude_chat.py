"""Tests for the chat layer (SPEC §10).

There is no API key on this machine, so these drive the loop through a fake
client returning recorded response shapes. That covers the tool loop, the
injected context, refusal handling, persistence and cost arithmetic — and covers
none of authentication, real streaming, or whether the model uses the tools well.
Those are only settled by the first real call on the server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import claude_chat, config, db, migrate
from app.main import app

INDIANA = ZoneInfo("America/Indiana/Indianapolis")


# --- a fake client ----------------------------------------------------------


class FakeStream:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def text_delta(text):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def usage(input_tokens=100, output_tokens=50, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def final(stop_reason="end_turn", content=None, model="claude-opus-5", **kwargs):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content or [],
        model=model,
        usage=usage(**kwargs),
    )


class FakeClient:
    """Returns queued responses; records the requests it was given."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.requests = []
        self.messages = self
        # The fallback path is exercised separately; here it is disabled so the
        # tests read the plain call.
        self.beta = None

    def stream(self, **request):
        self.requests.append(request)
        events, response = self._turns.pop(0)
        return FakeStream(events, response)


@pytest.fixture(autouse=True)
def _no_fallback_beta(monkeypatch):
    monkeypatch.setattr(claude_chat, "_fallbacks_available", False)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    migrate.run(path)
    connection = db.connect(path)
    try:
        connection.execute(
            "INSERT INTO terms (id,name,start_date,end_date) "
            "VALUES (1,'FA26','2026-08-24','2026-12-18')"
        )
        connection.execute(
            "INSERT INTO courses (id,term_id,name,code,instructor,late_policy,current_grade_pct) "
            "VALUES (1,1,'Music Theory III','MUS-T 251','Dr Reyes','10% per day',88)"
        )
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        connection.execute(
            "INSERT INTO assignments (course_id,title,type,source,due_at,est_hours,"
            "est_hours_remaining) VALUES (1,'Species counterpoint 1','worksheet',"
            "'manual',?,2,2)",
            (soon,),
        )
        yield connection
    finally:
        connection.close()


def run(conn, client, history=None):
    events = list(
        claude_chat.answer(
            conn, history or [{"role": "user", "content": "what's due?"}], INDIANA, client
        )
    )
    turn = next(payload for kind, payload in events if kind == "done")
    return events, turn


# --- the plain path ---------------------------------------------------------


def test_a_simple_answer_streams_and_reports_tokens(conn):
    client = FakeClient([([text_delta("You have "), text_delta("one thing due.")],
                          final(input_tokens=1200, output_tokens=90))])

    events, turn = run(conn, client)

    assert [payload for kind, payload in events if kind == "text"] == [
        "You have ", "one thing due."
    ]
    assert turn.text == "You have one thing due."
    assert turn.input_tokens == 1200
    assert turn.output_tokens == 90
    assert turn.stop_reason == "end_turn"


def test_the_forbidden_parameters_are_never_sent(conn):
    """temperature, top_p and budget_tokens all return 400 on this model."""
    client = FakeClient([([], final())])
    run(conn, client)

    request = client.requests[0]
    assert "temperature" not in request
    assert "top_p" not in request
    assert "top_k" not in request
    assert "budget_tokens" not in json.dumps(request["thinking"])
    assert request["thinking"]["type"] == "adaptive"


def test_reasoning_is_asked_for_so_streaming_does_not_look_frozen(conn):
    """The default is "omitted", which shows nothing until thinking finishes."""
    client = FakeClient([([], final())])
    run(conn, client)
    assert client.requests[0]["thinking"]["display"] == "summarized"


def test_max_tokens_leaves_room_for_thinking_as_well_as_the_reply(conn):
    client = FakeClient([([], final())])
    run(conn, client)
    assert client.requests[0]["max_tokens"] >= 4096


# --- tools ------------------------------------------------------------------


def tool_use(name, tool_id="toolu_1", inputs=None):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inputs or {})


def test_a_tool_call_runs_and_its_result_goes_back(conn):
    client = FakeClient([
        ([], final(stop_reason="tool_use", content=[tool_use("get_assignments")])),
        ([text_delta("Species counterpoint 1, due Friday.")], final()),
    ])

    events, turn = run(conn, client)

    assert ("tool", "get_assignments") in events
    assert turn.tool_calls[0]["name"] == "get_assignments"
    assert "Species counterpoint 1" in turn.tool_results[0]["output"]

    # The result was handed back as a tool_result in a single user message.
    second = client.requests[1]["messages"][-1]
    assert second["role"] == "user"
    assert second["content"][0]["type"] == "tool_result"
    assert second["content"][0]["tool_use_id"] == "toolu_1"


def test_the_assignments_tool_reports_slack_not_just_dates(conn):
    """SPEC §9's number is the one worth answering "am I behind?" with."""
    output = json.loads(claude_chat.run_tool(conn, "get_assignments", {}, INDIANA))
    item = output["assignments"][0]
    assert item["title"] == "Species counterpoint 1"
    assert item["hours_of_work_left"] == 2.0
    assert item["spare_hours"] is not None
    assert "spare time" in output["note"]


def test_the_courses_tool_reports_what_overload_advice_needs(conn):
    output = json.loads(claude_chat.run_tool(conn, "get_courses", {}, INDIANA))
    course = output["courses"][0]
    assert course["name"] == "Music Theory III"
    assert course["late_policy"] == "10% per day"
    assert course["current_grade_pct"] == 88


def test_a_failing_tool_is_reported_to_the_model_not_raised(conn):
    client = FakeClient([
        ([], final(stop_reason="tool_use",
                   content=[tool_use("get_assignments", inputs={"course_id": "not-a-number"})])),
        ([text_delta("I could not look that up.")], final()),
    ])

    events, turn = run(conn, client)

    assert turn.text == "I could not look that up."
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_the_loop_stops_rather_than_calling_tools_forever(conn):
    client = FakeClient([
        ([], final(stop_reason="tool_use", content=[tool_use("get_courses", f"toolu_{i}")]))
        for i in range(claude_chat.MAX_TOOL_ROUNDS)
    ])

    _, turn = run(conn, client)

    assert turn.stop_reason == "tool_round_limit"
    assert "smaller pieces" in turn.text


# --- refusals ---------------------------------------------------------------


def test_a_refusal_is_handled_without_reading_empty_content(conn):
    """A declined request is HTTP 200 with content empty or partial.

    Code that reads content[0] unconditionally raises IndexError here.
    """
    client = FakeClient([([], final(stop_reason="refusal", content=[]))])

    _, turn = run(conn, client)

    assert turn.refused is True
    assert turn.stop_reason == "refusal"
    assert "declined" in turn.text


# --- the injected context ---------------------------------------------------


def test_the_context_carries_everything_spec_10_lists(conn):
    context = claude_chat.build_context(conn, INDIANA)
    assert "Today is" in context
    assert "FA26" in context
    assert "Music Theory III" in context
    assert "Species counterpoint 1" in context
    assert "spare" in context


def test_a_stale_source_is_flagged_in_the_context(conn):
    """SPEC §4: a stale feed must not read as "nothing due"."""
    conn.execute(
        "INSERT INTO sync_state (source, last_success_at, consecutive_failures) "
        "VALUES ('canvas_ics', '2026-01-01T00:00:00Z', 5)"
    )
    context = claude_chat.build_context(conn, INDIANA)
    assert "Canvas calendar feed" in context
    assert "incomplete" in context


def test_the_prompt_says_there_is_no_archive_yet(conn):
    """Without M4 there is no tool that could answer "what did she email me?".

    Told nothing, a model will reconstruct one. SPEC §10 makes unsourced archive
    claims a visible failure, so the prompt forbids it outright.
    """
    instructions = claude_chat.INSTRUCTIONS
    assert "no archive" in instructions.lower()
    assert "never" in instructions.lower()

    blocks = claude_chat.system_blocks(conn, INDIANA)
    assert blocks[0]["text"] == instructions


def test_the_stable_half_is_cached_and_the_volatile_half_is_not(conn):
    """Caching is a prefix match — today's date above the instructions would
    invalidate the cache on every single request."""
    blocks = claude_chat.system_blocks(conn, INDIANA)
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    assert "Today is" in blocks[1]["text"]
    assert "Today is" not in blocks[0]["text"]


# --- cost -------------------------------------------------------------------


def row(**kwargs):
    base = {"model": "claude-opus-5", "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0}
    base.update(kwargs)
    return base


def test_cost_is_priced_per_model():
    opus = claude_chat.message_cost(row(input_tokens=1_000_000))
    sonnet = claude_chat.message_cost(row(model="claude-sonnet-5", input_tokens=1_000_000))
    assert opus == pytest.approx(5.00)
    assert sonnet == pytest.approx(3.00)


def test_cached_input_is_priced_below_fresh_input():
    fresh = claude_chat.message_cost(row(input_tokens=1_000_000))
    cached = claude_chat.message_cost(row(cache_read_tokens=1_000_000))
    assert cached == pytest.approx(fresh * 0.1)


def test_output_costs_more_than_input():
    assert claude_chat.message_cost(row(output_tokens=1_000_000)) == pytest.approx(25.00)


def test_switching_model_does_not_reprice_history(conn, monkeypatch):
    """The model is stored per message for exactly this reason."""
    conn.execute(
        "INSERT INTO chat_threads (id, title) VALUES (1, 'x')"
    )
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, model, input_tokens) "
        "VALUES (1, 'assistant', 'claude-opus-5', 1000000)"
    )
    before = claude_chat.month_to_date_cost(conn)["dollars"]

    monkeypatch.setattr(config, "CHAT_MODEL", "claude-sonnet-5")
    after = claude_chat.month_to_date_cost(conn)["dollars"]

    assert before == after == pytest.approx(5.00)


def test_month_to_date_only_counts_this_month(conn):
    conn.execute("INSERT INTO chat_threads (id, title) VALUES (1, 'x')")
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, model, output_tokens, created_at) "
        "VALUES (1,'assistant','claude-opus-5',1000000,'2020-01-01T00:00:00Z')"
    )
    assert claude_chat.month_to_date_cost(conn)["dollars"] == 0.0


# --- the page ---------------------------------------------------------------


@pytest.fixture
def client(conn):
    with TestClient(app) as test_client:
        yield test_client


def test_the_chat_page_loads(client):
    body = client.get("/chat").text
    assert "Ask" in body


def test_the_page_says_when_the_key_is_missing(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    assert "Chat is not switched on" in client.get("/chat").text


def test_asking_a_question_starts_a_thread(client, conn):
    response = client.post("/chat/send", data={"question": "what's due?", "thread": ""},
                           follow_redirects=False)
    assert response.status_code == 303

    threads = conn.execute("SELECT title FROM chat_threads").fetchall()
    assert len(threads) == 1
    assert threads[0]["title"] == "what's due?"
    assert conn.execute(
        "SELECT content FROM chat_messages WHERE role='user'"
    ).fetchone()["content"] == "what's due?"


def test_an_empty_question_does_nothing(client, conn):
    client.post("/chat/send", data={"question": "   ", "thread": ""})
    assert conn.execute("SELECT COUNT(*) n FROM chat_threads").fetchone()["n"] == 0


def test_a_missing_key_is_reported_without_leaking_it(client, conn, monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    client.post("/chat/send", data={"question": "hello", "thread": ""})
    thread_id = conn.execute("SELECT id FROM chat_threads").fetchone()["id"]

    body = client.get(f"/chat/{thread_id}/stream").text

    assert "failed" in body
    assert "CLAUDE_API_KEY" in body       # names the setting
    assert "sk-ant" not in body           # never a value


def test_the_stream_does_nothing_when_there_is_no_question(client, conn):
    conn.execute("INSERT INTO chat_threads (id, title) VALUES (1, 'x')")
    body = client.get("/chat/1/stream").text
    assert "event: done" in body
