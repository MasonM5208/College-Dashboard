"""Tests for ops/backup.sh's privilege handling.

These exist because of a real failure: the backup ran fine by hand and failed
every night at 03:15 for three nights with "runuser is not installed", because
cron runs jobs with PATH=/usr/bin:/bin and runuser lives in /usr/sbin. Worse, the
function that records failures needed the same privilege drop, so it died too —
and the status page showed the backup as merely stale, with no error and zero
failures, which reads as "has not run lately" rather than "has failed every
night".

Only the shell functions are exercised, by sourcing the part of the script above
its first side effect. The full run needs age and rclone and a server.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "backup.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is not available"
)


def definitions() -> str:
    """The script down to its first side effect, so it can be sourced safely."""
    text = SCRIPT.read_text()
    marker = "# --- checks ---"
    assert marker in text, "backup.sh no longer has a checks section"
    return text[: text.index(marker)]


def run(snippet: str, db: Path, env=None, path="/usr/bin:/bin"):
    """Source the script's definitions, then run a snippet against them."""
    script = definitions().replace("#!/usr/bin/env bash", "")
    # DB_PATH rather than DB: the script sets DB from it, so setting DB first
    # would simply be overwritten.
    #
    # The ERR trap is cleared afterwards because these probe return codes at the
    # top level, where set -e would otherwise end the shell. In the real script
    # that trap firing is the wanted behaviour — a failed copy must abort the run.
    program = (
        f'export DB_PATH="{db}"\nENV_FILE=/nonexistent\n{script}\n'
        "trap - ERR\n"
        f"{snippet}\n"
    )
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        env={"PATH": path, "HOME": str(db.parent), **(env or {})},
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sync_state (source TEXT PRIMARY KEY, last_success_at TEXT, "
        "last_attempt_at TEXT, last_error TEXT, cursor TEXT, "
        "consecutive_failures INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    return path


def test_the_script_sets_its_own_path(db):
    """The fix for the original failure. Cron's PATH has no /usr/sbin in it."""
    result = run('echo "$PATH"', db)
    assert "/usr/sbin" in result.stdout
    assert "/usr/local/bin" in result.stdout


def test_a_missing_runner_is_reported_rather_than_fatal(db):
    """as_db_owner used to call die(), which is what silenced the recorder."""
    result = run(
        'find_runner() { return 1; }\n'
        'DB_OWNER=nobody-at-all\n'
        # `|| rc=$?` is what keeps set -e from ending the shell, which is the
        # point: the caller decides, rather than the helper exiting for it.
        'as_db_owner true && rc=0 || rc=$?\n'
        'echo "returned $rc"\n'
        'echo still-running',
        db,
    )
    assert "returned 127" in result.stdout
    assert "still-running" in result.stdout
    assert result.returncode == 0


def test_the_runner_is_found_by_absolute_path_not_by_lookup(db):
    """"Is runuser installed" and "can this shell find runuser" are different
    questions, and only the first one matters."""
    body = definitions()
    assert "/usr/sbin/runuser" in body
    assert "command -v runuser" not in body


def test_a_failure_is_recorded_even_when_privileges_cannot_be_dropped(db):
    """The whole point. A recorder stopped by the failure it records is useless."""
    result = run(
        'find_runner() { return 1; }\n'
        'DB_OWNER=nobody-at-all\n'
        'record_sync_state "backup failed during: copying the database"',
        db,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT last_error, consecutive_failures FROM sync_state WHERE source='backup'"
    ).fetchone()
    conn.close()

    assert row is not None, "the failure was not recorded at all"
    assert "copying the database" in row[0]
    assert row[1] == 1


def test_repeated_failures_accumulate(db):
    snippet = (
        'find_runner() { return 1; }\n'
        'DB_OWNER=nobody-at-all\n'
        'record_sync_state "backup failed during: uploading"\n'
        'record_sync_state "backup failed during: uploading"\n'
        'record_sync_state "backup failed during: uploading"'
    )
    run(snippet, db)

    conn = sqlite3.connect(db)
    failures = conn.execute(
        "SELECT consecutive_failures FROM sync_state WHERE source='backup'"
    ).fetchone()[0]
    conn.close()
    assert failures == 3


def test_a_success_clears_the_error_and_the_count(db):
    run(
        'find_runner() { return 1; }\n'
        'DB_OWNER=nobody-at-all\n'
        'record_sync_state "backup failed during: uploading"\n'
        'record_sync_state ""',
        db,
    )

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT last_success_at, last_error, consecutive_failures FROM sync_state "
        "WHERE source='backup'"
    ).fetchone()
    conn.close()

    assert row[0] is not None
    assert row[1] is None
    assert row[2] == 0


def test_an_apostrophe_in_an_error_does_not_break_the_sql(db):
    run(
        'find_runner() { return 1; }\n'
        'DB_OWNER=nobody-at-all\n'
        "record_sync_state \"backup failed during: reading mason's key\"",
        db,
    )

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT last_error FROM sync_state WHERE source='backup'"
    ).fetchone()
    conn.close()
    assert row is not None and "mason's key" in row[0]


def test_the_preflight_names_the_missing_tool(db):
    """So the log says what is wrong, not "exit 127 during copying"."""
    body = SCRIPT.read_text()
    assert "no runuser, setpriv or su found" in body
    assert "util-linux" in body
