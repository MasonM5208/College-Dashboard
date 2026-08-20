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


def test_the_prompt_demands_a_citation_for_anything_from_the_archive(conn):
    """SPEC §10: "Enforce this in the system prompt."

    An uncited claim about a message is the failure mode that makes the whole
    archive untrustworthy — if Mason cannot check it in one tap, a confident
    sentence about a deadline is worse than no sentence at all.
    """
    instructions = claude_chat.INSTRUCTIONS.lower()

    assert "cite" in instructions
    assert "/archive/" in instructions
    # And still forbidden to invent one.
    assert "never reconstruct" in instructions
    # The pre-M4 disclaimer must be gone, or it will refuse to use its own tools.
    assert "there is no archive" not in instructions

    blocks = claude_chat.system_blocks(conn, INDIANA)
    assert blocks[0]["text"] == claude_chat.INSTRUCTIONS


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


# --- how replies are displayed ----------------------------------------------


def test_markdown_is_rendered_not_shown_as_asterisks():
    """Replies come back as Markdown; printing it raw shows the syntax."""
    html = claude_chat.render_markdown("**Key idea:** limits\n\n- one\n- two")
    assert "<strong>Key idea:</strong>" in html
    assert "<li>one</li>" in html
    assert "**" not in html


def test_an_indented_expression_becomes_a_scrollable_block():
    """Where displayed maths lands, now that LaTeX is out."""
    html = claude_chat.render_markdown("Therefore:\n\n    lim(x→1) (x²−1)/(x−1) = 2\n")
    assert "<pre>" in html
    assert "lim(x→1)" in html


def test_raw_html_in_a_reply_is_escaped_not_executed():
    html = claude_chat.render_markdown("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_dangerous_link_never_becomes_a_link():
    """The renderer refuses the href entirely and leaves the text."""
    for source in ("[x](javascript:alert(1))", "[x](JaVaScRiPt:alert(1))"):
        html = claude_chat.render_markdown(source)
        assert "<a href" not in html
    assert '<a href="https://example.com">' in claude_chat.render_markdown(
        "[ok](https://example.com)"
    )


def test_the_prompt_forbids_latex_because_nothing_can_render_it():
    """Current models default to LaTeX for maths; the page shows it literally."""
    instructions = claude_chat.INSTRUCTIONS
    assert "never as LaTeX" in instructions
    assert "→" in instructions and "²" in instructions


def test_a_question_is_shown_as_typed_and_an_answer_is_rendered(client, conn):
    conn.execute("INSERT INTO chat_threads (id, title) VALUES (1, 'x')")
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content) "
        "VALUES (1, 'user', 'what is 2**3 in python?')"
    )
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content, model) "
        "VALUES (1, 'assistant', '**Eight.** Use `2**3`.', 'claude-opus-5')"
    )

    body = client.get("/chat?thread=1").text

    # The question keeps its asterisks; the answer has them turned into markup.
    assert "what is 2**3 in python?" in body
    assert "<strong>Eight.</strong>" in body
    assert "<code>2**3</code>" in body


# --- the archive tools (M4) -------------------------------------------------


def saved(conn, body, **kwargs):
    from app import archive
    return archive.ingest(conn, body, source="paste", **kwargs).document_id


def test_search_archive_returns_extracts_not_bodies(conn):
    """Extracts keep the tool result small; get_document is where the text is."""
    document_id = saved(
        conn,
        "The makeup exam is on 12 October. Bring a calculator and nothing else.",
        subject="Makeup exam",
    )

    output = json.loads(claude_chat.run_tool(conn, "search_archive",
                                             {"query": "makeup exam"}, INDIANA))

    assert [item["id"] for item in output["documents"]] == [document_id]
    assert "calculator" in output["documents"][0]["extract"]
    # No control characters from the search engine's highlighting.
    assert "\x02" not in output["documents"][0]["extract"]
    assert "cite" in output["note"].lower()


def test_search_archive_can_be_narrowed_to_a_course(conn):
    from app import archive
    wanted = saved(conn, "The lab report is due Friday.", subject="Bio")
    saved(conn, "A lab section for something else.", subject="Other")
    archive.link_course(conn, wanted, 1)

    output = json.loads(claude_chat.run_tool(
        conn, "search_archive", {"query": "lab", "course_id": 1}, INDIANA
    ))

    assert [item["id"] for item in output["documents"]] == [wanted]


def test_search_archive_survives_a_query_full_of_operators(conn):
    saved(conn, "Something about the midterm.")
    output = json.loads(claude_chat.run_tool(
        conn, "search_archive", {"query": 'NEAR "unbalanced *'}, INDIANA
    ))
    assert "documents" in output


def test_get_document_returns_the_body_verbatim(conn):
    body = "Line one.\n\n  indented line\n\n**not markdown**"
    document_id = saved(conn, body, subject="Verbatim")

    output = json.loads(claude_chat.run_tool(conn, "get_document",
                                             {"id": document_id}, INDIANA))

    assert output["body"] == body
    assert output["cite_as"] == f"/archive/{document_id}"


def test_get_document_says_so_when_the_id_is_wrong(conn):
    """A model that invented an id must be told, not handed an empty document."""
    output = json.loads(claude_chat.run_tool(conn, "get_document", {"id": 999}, INDIANA))
    assert "error" in output


def test_the_four_tools_spec_asks_for_are_all_present():
    """SPEC §10 names four. get_workload is a fifth, added with M6.

    SPEC's list is what the chat must be able to do, not a cap on what it may
    have. The capacity model answers a question the other four cannot — "does
    this week fit" — and leaving it out would mean the chat reassuring him about
    a week the dashboard already knows is impossible.
    """
    names = {tool["name"] for tool in claude_chat.TOOLS}
    assert {"get_assignments", "get_courses", "search_archive",
            "get_document"} <= names
    assert "get_workload" in names


# --- unsourced archive claims are visible (SPEC §10) ------------------------


def store_reply(conn, content, tool_calls):
    conn.execute("INSERT INTO chat_threads (id, title) VALUES (1, 'x')")
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content) VALUES (1,'user','q')"
    )
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content, tool_calls, model, "
        "output_tokens) VALUES (1,'assistant',?,?,'claude-opus-5',10)",
        (content, json.dumps(tool_calls)),
    )


def test_an_archive_answer_without_a_link_is_flagged(client, conn):
    store_reply(conn, "She said the exam moved to the 12th.",
                [{"name": "search_archive", "input": {"query": "exam"}}])

    body = client.get("/chat?thread=1").text

    assert "No citation" in body


def test_an_archive_answer_with_a_link_is_not_flagged(client, conn):
    store_reply(conn, "She said the exam moved to the 12th — [1 Sep](/archive/7).",
                [{"name": "get_document", "input": {"id": 7}}])

    body = client.get("/chat?thread=1").text

    assert "No citation" not in body


def test_an_answer_that_never_touched_the_archive_is_not_flagged(client, conn):
    """A schedule question has nothing to cite, so the warning would be noise."""
    store_reply(conn, "Your next deadline is Friday.",
                [{"name": "get_assignments", "input": {}}])

    assert "No citation" not in client.get("/chat?thread=1").text


# --- the workload tool (M6) -------------------------------------------------


def test_get_workload_reports_a_week_that_does_not_fit(conn):
    conn.execute("UPDATE capacity_settings SET productive_hours = 1.0")
    due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for index in range(4):
        conn.execute(
            "INSERT INTO assignments (course_id,title,type,due_at,est_hours,"
            "est_hours_remaining,status,source,points_possible) "
            "VALUES (1,?,'worksheet',?,8,8,'not_started','manual',100)",
            (f"Item {index}", due),
        )

    output = json.loads(claude_chat.run_tool(conn, "get_workload", {}, INDIANA))

    assert output["overloaded"] is True
    # At least the 32 hours added here; the fixture has work of its own.
    assert output["hours_of_work"] >= 32.0
    assert output["shortfall_hours"] > 0
    assert output["cheapest_to_drop"]
    assert all(item["why_it_is_cheap"] for item in output["cheapest_to_drop"])


def test_get_workload_reports_the_days_and_what_was_taken_out_of_them(conn):
    conn.execute(
        "INSERT INTO commitments (term_id,label,kind,weekday,start_time,end_time) "
        "VALUES (1,'Wind Ensemble','ensemble',2,'08:00','20:00')"
    )
    output = json.loads(claude_chat.run_tool(conn, "get_workload", {}, INDIANA))

    assert len(output["days"]) == 7
    assert any(day["hours_committed"] > 0 for day in output["days"])
    # The note is what stops the model reading "available" as "hours in the day".
    assert "practice" in output["note"]


def test_get_workload_is_honest_that_a_shortfall_is_a_floor(conn):
    """Unestimated work is not counted, so the real gap can only be larger."""
    output = json.loads(claude_chat.run_tool(conn, "get_workload", {}, INDIANA))
    assert "floor" in output["note"]


def test_the_prompt_tells_it_not_to_reassure_about_an_unchecked_week(conn):
    instructions = claude_chat.INSTRUCTIONS.lower()
    assert "get_workload" in instructions
    assert "do not \nreassure" in instructions or "not reassure" in instructions.replace("\n", " ")
