"""Tests for the migration runner and for 0001_core.sql itself."""

from __future__ import annotations

import sqlite3

import pytest

from app import db, migrate


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


# --- the real migrations ----------------------------------------------------


def test_real_migrations_apply_to_a_fresh_database(db_path):
    version = migrate.run(db_path)
    assert version >= 1

    conn = db.connect(db_path)
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    # Every table SPEC §5 assigns to M1-M3, plus the runner's bookkeeping.
    assert {
        "terms",
        "courses",
        "assignments",
        "reminder_rules",
        "reminder_instances",
        "sync_state",
        "audit_log",
        "schema_migrations",
    } <= names


def test_running_twice_changes_nothing(db_path):
    first = migrate.run(db_path)
    second = migrate.run(db_path)
    assert first == second

    conn = db.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    finally:
        conn.close()
    assert count == len(migrate.discover())


def test_migration_sets_wal_and_has_fts5(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        assert db.journal_mode(conn) == "wal"
        assert db.fts5_available(conn) is True
    finally:
        conn.close()


def test_every_migration_file_is_recorded_with_its_checksum(db_path):
    migrate.run(db_path)
    on_disk = {m.version: m.sha256 for m in migrate.discover()}

    conn = db.connect(db_path)
    try:
        recorded = {
            int(row["version"]): row["sha256"]
            for row in conn.execute("SELECT version, sha256 FROM schema_migrations")
        }
    finally:
        conn.close()
    assert recorded == on_disk


# --- schema behaviour that later milestones depend on -----------------------


def test_assignment_enums_are_enforced(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assignments (title, type, source) VALUES (?,?,?)",
                ("Bad type", "essay", "manual"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assignments (title, source, status) VALUES (?,?,?)",
                ("Bad status", "manual", "done"),
            )
    finally:
        conn.close()


def test_ics_uid_is_unique_but_allows_many_manual_rows(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO assignments (title, source, ics_uid) VALUES (?,?,?)",
            ("From Canvas", "ics", "event-abc"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assignments (title, source, ics_uid) VALUES (?,?,?)",
                ("Same UID", "ics", "event-abc"),
            )
        # Manual entries have no UID, and NULLs must not collide with each other.
        for title in ("Manual one", "Manual two"):
            conn.execute(
                "INSERT INTO assignments (title, source) VALUES (?,?)", (title, "manual")
            )
    finally:
        conn.close()


def test_assignment_may_have_no_course_for_the_review_queue(db_path):
    """SPEC §6.5: an unmatched feed event is queued, never dropped."""
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO assignments (title, source, ics_uid) VALUES (?,?,?)",
            ("Unmatched event [Some Course]", "ics", "event-xyz"),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM assignments WHERE course_id IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert row["n"] == 1


def test_updated_at_advances_on_update(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO assignments (title, source, updated_at) VALUES (?,?,?)",
            ("Paper", "manual", "2026-01-01T00:00:00Z"),
        )
        assignment_id = cur.lastrowid
        conn.execute(
            "UPDATE assignments SET status='in_progress' WHERE id=?", (assignment_id,)
        )
        row = conn.execute(
            "SELECT updated_at FROM assignments WHERE id=?", (assignment_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["updated_at"] > "2026-01-01T00:00:00Z"


def test_audit_log_is_append_only(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id) VALUES (?,?,?)",
            ("create", "assignments", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET action='tamper'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log")
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 1
    finally:
        conn.close()


def test_reminder_rule_scope_must_match_its_target(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        # A global rule may not name a type.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reminder_rules (scope, assignment_type, offsets_json) "
                "VALUES ('global','exam','[\"P1D\"]')"
            )
        # A type-scoped rule must name one.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reminder_rules (scope, offsets_json) "
                "VALUES ('assignment_type','[\"P1D\"]')"
            )
        conn.execute(
            "INSERT INTO reminder_rules (scope, offsets_json) "
            "VALUES ('global','[\"P1D\",\"PT3H\"]')"
        )
    finally:
        conn.close()


def test_deleting_an_assignment_removes_its_reminders(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO assignments (title, source) VALUES ('Quiz','manual')"
        )
        assignment_id = cur.lastrowid
        conn.execute(
            "INSERT INTO reminder_instances (assignment_id, kind, fire_at, channel) "
            "VALUES (?,'due_by','2026-09-01T14:00:00Z','caldav')",
            (assignment_id,),
        )
        conn.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM reminder_instances"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert left == 0


def test_a_course_cannot_be_deleted_out_from_under_its_assignments(db_path):
    migrate.run(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO terms (id, name, start_date, end_date) "
            "VALUES (1,'Fall 2026','2026-08-24','2026-12-18')"
        )
        conn.execute(
            "INSERT INTO courses (id, term_id, name) VALUES (1,1,'Music Theory III')"
        )
        conn.execute(
            "INSERT INTO assignments (course_id, title, source) VALUES (1,'Species counterpoint','manual')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM courses WHERE id=1")
    finally:
        conn.close()


# --- runner error handling --------------------------------------------------


def _write(directory, name, sql):
    (directory / name).write_text(sql, encoding="utf-8")


def test_editing_an_applied_migration_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0001_first.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")

    migrate.run(db_path, migrations)

    _write(
        migrations,
        "0001_first.sql",
        "CREATE TABLE a (id INTEGER PRIMARY KEY, extra TEXT);\n",
    )
    with pytest.raises(migrate.MigrationError, match="modified after it was applied"):
        migrate.run(db_path, migrations)


def test_a_missing_applied_migration_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0001_first.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
    migrate.run(db_path, migrations)

    (migrations / "0001_first.sql").unlink()
    with pytest.raises(migrate.MigrationError, match="file is missing"):
        migrate.run(db_path, migrations)


def test_a_failing_migration_rolls_back_completely(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "0001_broken.sql",
        "CREATE TABLE good (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE bad (id INTEGER PRIMARY KEY, oops NOT A TYPE);\n",
    )

    with pytest.raises(migrate.MigrationError, match="rolled back"):
        migrate.run(db_path, migrations)

    conn = db.connect(db_path)
    try:
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        applied = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
    finally:
        conn.close()
    # The half of the file that was valid must not have survived.
    assert "good" not in names
    assert applied["n"] == 0


def test_pending_migration_below_the_applied_high_water_mark_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0002_second.sql", "CREATE TABLE b (id INTEGER PRIMARY KEY);\n")
    migrate.run(db_path, migrations)

    _write(migrations, "0001_late_arrival.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
    with pytest.raises(migrate.MigrationError, match="renumber"):
        migrate.run(db_path, migrations)


def test_transaction_control_in_a_migration_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "0001_first.sql",
        "CREATE TABLE a (id INTEGER PRIMARY KEY);\nCOMMIT;\n",
    )
    with pytest.raises(migrate.MigrationError, match="top-level COMMIT"):
        migrate.run(db_path, migrations)


def test_prose_mentioning_commit_in_a_comment_is_allowed(tmp_path, db_path):
    """The forbidden-statement check must not fire on documentation."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "0001_first.sql",
        "-- COMMIT and ROLLBACK are handled by the runner, not by this file.\n"
        "CREATE TABLE a (id INTEGER PRIMARY KEY);\n",
    )
    assert migrate.run(db_path, migrations) == 1


def test_a_trigger_body_is_not_mistaken_for_transaction_control(tmp_path, db_path):
    """BEGIN ... END delimiting a trigger must survive the check."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "0001_first.sql",
        "CREATE TABLE a (id INTEGER PRIMARY KEY, n INTEGER);\n"
        "CREATE TRIGGER a_guard BEFORE DELETE ON a\n"
        "BEGIN\n"
        "  SELECT RAISE(ABORT, 'no');\n"
        "END;\n",
    )
    assert migrate.run(db_path, migrations) == 1


def test_badly_named_migration_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "initial.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);\n")
    with pytest.raises(migrate.MigrationError, match="not in the required form"):
        migrate.run(db_path, migrations)


def test_empty_migration_is_refused(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "0001_empty.sql", "\n\n")
    with pytest.raises(migrate.MigrationError, match="is empty"):
        migrate.run(db_path, migrations)


def test_migrations_apply_in_numeric_order(tmp_path, db_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # 0010 must apply after 0009, which plain lexical sorting of these names also
    # happens to get right; the test guards the case where it would not.
    _write(migrations, "0009_ninth.sql", "CREATE TABLE nine (id INTEGER PRIMARY KEY);\n")
    _write(
        migrations,
        "0010_tenth.sql",
        "ALTER TABLE nine ADD COLUMN extra TEXT;\n",
    )
    assert migrate.run(db_path, migrations) == 10
