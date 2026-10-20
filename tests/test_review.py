import asyncio
import json
import os
import sqlite3
import subprocess

import pytest
from test_run_manager import start, wait_for

from pico import FakeModelClient, Pico, StateStore, WorkspaceContext
from pico.application import ApplicationService
from pico.events import PersistenceError
from pico.providers.types import ModelResponse, ToolCall
from pico.review import MAX_FILE_BYTES, capture
from pico.state_store import TERMINAL, ApplicationError


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def agent_at(root, calls):
    return Pico(FakeModelClient([ModelResponse(tool_calls=calls), ModelResponse(text="Done")]),
                WorkspaceContext.build(root, repo_root_override=root), StateStore(root),
                approval_policy="auto", max_steps=6)


def test_diff_uses_dirty_worktree_not_head_and_freezes_content(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("head\n")
    (tmp_path / "deleted.txt").write_text("old\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "initial")
    (tmp_path / "README.md").write_text("staged\n")
    git(tmp_path, "add", "README.md")
    (tmp_path / "README.md").write_text("user\n")
    (tmp_path / "user-notes.txt").write_text("keep this untracked file\n")
    agent = agent_at(tmp_path, [
        ToolCall("patch_file", {"path": "README.md", "old_text": "user\n", "new_text": "user\nagent\n"}),
        ToolCall("write_file", {"path": "新 file.txt", "content": "新内容"}),
        ToolCall("run_shell", {"command": "rm deleted.txt"}),
    ])
    index = (tmp_path / ".git/index").read_bytes()
    agent.ask("make the requested changes")
    value = agent.current_run_state.run_id
    rows = agent.store.review_changes(value)
    assert {r["path"]: r["kind"] for r in rows} == {
        "README.md": "modified", "deleted.txt": "deleted", "新 file.txt": "created"}
    by_path = {r["path"]: agent.store.review_diff(value, r["id"])["text"] for r in rows}
    assert " user\n+agent\n" in by_path["README.md"]
    assert "head" not in by_path["README.md"] and "staged" not in by_path["README.md"]
    assert "-old\n" in by_path["deleted.txt"]
    assert "+新内容\n\\ No newline at end of file\n" in by_path["新 file.txt"]
    assert agent.store.review_get(value)["state"] == "ready"
    initial = {r["path"]: r for r in agent.store.review_changes(value, "initial")}
    assert initial["README.md"]["index"] == initial["README.md"]["worktree"] == "M"
    assert initial["user-notes.txt"]["index"] == "?"
    assert (tmp_path / ".git/index").read_bytes() == index
    assert (tmp_path / "user-notes.txt").read_text() == "keep this untracked file\n"
    (tmp_path / "README.md").write_text("later external edit\n")
    assert agent.store.review_diff(value, rows[0]["id"])["text"] == by_path["README.md"]
    assert len([e for e in agent.store.events(value) if e["event"] == "run_finished"]) == 1


def test_capture_bounds_and_excluded_content_are_explicit(tmp_path):
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("outside must not be captured")
    (tmp_path / "link").symlink_to(outside)
    (tmp_path / ".env").write_text("SECRET=never-copy-this\n")
    (tmp_path / "binary").write_bytes(b"\x00\x01")
    (tmp_path / "large").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    (tmp_path / "text").write_text("captured")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/vendor").write_text("do not traverse")
    blobs = []
    snapshot = capture(tmp_path, lambda data: blobs.append(data) or "blob")
    assert snapshot["complete"]
    assert {r["reason"] for r in snapshot["files"].values()} == {
        "", "symlink", "sensitive_file", "binary_or_non_utf8", "file_size_limit"}
    assert blobs == [b"captured"]
    bounded = capture(tmp_path, lambda _: "blob", max_files=2)
    assert not bounded["complete"] and len(bounded["files"]) <= 2
    timeout = capture(tmp_path, lambda _: "blob", seconds=0)
    assert not timeout["complete"]


def test_linked_parent_cannot_escape_capture_root(tmp_path):
    from pico.review import read_entry

    outside = tmp_path.parent / "external"
    outside.mkdir()
    (outside / "secret").write_text("outside")
    (tmp_path / "redirect").symlink_to(outside, target_is_directory=True)
    handle = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            read_entry(handle, "redirect/secret", lambda _: pytest.fail("Must not read through directory symlinks"))
    finally:
        os.close(handle)


def test_read_only_git_observation_never_executes_configured_filters(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "source").write_text("initial\n")
    (tmp_path / ".gitattributes").write_text("source filter=fixture\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "initial")
    git(tmp_path, "config", "filter.fixture.clean", "touch filter-ran; cat")
    git(tmp_path, "config", "filter.fixture.required", "true")
    (tmp_path / "source").write_text("dirty\n")
    index = (tmp_path / ".git/index").read_bytes()
    WorkspaceContext.build(tmp_path)
    observed = capture(tmp_path, lambda _: "blob")
    assert observed["complete"] and observed["git_status_mode"] == "external_filters_disabled"
    assert not (tmp_path / "filter-ran").exists()
    assert (tmp_path / ".git/index").read_bytes() == index


def test_ignored_write_is_reported_as_missing_evidence_not_clean(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    agent = agent_at(tmp_path, [ToolCall("write_file", {"path": "ignored.txt", "content": "changed"})])
    agent.ask("write ignored file")
    value = agent.current_run_state.run_id
    assert agent.store.review_get(value)["state"] == "partial"
    row, = agent.store.review_changes(value)
    assert row["path"] == "ignored.txt" and not row["diff_available"]
    assert row["reason"] == "outside_captured_scope"


def test_content_budget_and_export_redaction_do_not_change_run_outcome(tmp_path, monkeypatch):
    from pico import review

    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "phase3-fixture-secret")
    (tmp_path / "file").write_text("before\n")
    agent = agent_at(tmp_path, [ToolCall("write_file", {"path": "file", "content": "phase3-fixture-secret\n"})])
    agent.ask("change file")
    value = agent.current_run_state.run_id
    exported = agent.store.export_run(value)
    assert "phase3-fixture-secret" not in (exported / "diffs.json").read_text()
    assert "<redacted>" in (exported / "diffs.json").read_text()
    monkeypatch.setattr(review, "MAX_WORKSPACE_BYTES", 1)
    agent.model_client = FakeModelClient([ModelResponse(tool_calls=[ToolCall("write_file", {"path": "new", "content": "new content"})]), ModelResponse(text="Done again")])
    assert agent.ask("another task") == "Done again"
    assert agent.store.review_get(agent.current_run_state.run_id)["state"] == "partial"
    assert agent.current_run_state.status == "completed"


def test_capture_failure_is_separate_from_execution_outcome(tmp_path, monkeypatch):
    from pico import review

    def unavailable(*_args, **_kwargs):
        raise OSError("Injected observation failure")

    monkeypatch.setattr(review, "capture", unavailable)
    agent = agent_at(tmp_path, [ToolCall("write_file", {"path": "result", "content": "known tool result"})])
    assert agent.ask("write") == "Done"
    assert (tmp_path / "result").read_text() == "known tool result"
    evidence = agent.store.review_get(agent.current_run_state.run_id)
    assert evidence["state"] == "unavailable" and evidence["reason"] == "capture_failed"
    assert agent.current_run_state.status == "completed"


def test_review_save_rollback_does_not_commit_manifest_without_event(tmp_path):
    agent = agent_at(tmp_path, [])
    agent.ask("inspect")
    store = agent.store
    session = store.load_session(agent.session["id"])
    run = store.create_run(session["id"], "new")
    store.begin_run(session, run)
    run = run["run_id"]
    store.review_begin(run)
    store.connection.execute("CREATE TRIGGER review_fault BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(PersistenceError):
        store.review_save(run, "baseline", {"files": {}, "complete": True})
    assert store._review(run)["baseline"] is None
    assert store.get_run(run)["status"] == "running"


def legacy_database(root):
    directory = root / ".pico"
    directory.mkdir()
    connection = sqlite3.connect(directory / "state.sqlite3")
    # This is the actual phase-2 schema; the migration test does not derive it from v2.
    connection.executescript("""
      CREATE TABLE metadata (workspace TEXT NOT NULL);
      CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        parent_run_id TEXT, request_id TEXT, request_hash TEXT NOT NULL, config TEXT NOT NULL,
        body TEXT NOT NULL, status TEXT NOT NULL, report TEXT, instance_id TEXT, worker_pid INTEGER,
        created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(session_id,request_id));
      CREATE TABLE tool_calls (run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL,
        name TEXT NOT NULL, args TEXT NOT NULL, phase TEXT NOT NULL, result TEXT, PRIMARY KEY(run_id,call_id));
      CREATE TABLE approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL,
        body TEXT NOT NULL, status TEXT NOT NULL, decision INTEGER, UNIQUE(run_id,call_id),
        FOREIGN KEY(run_id,call_id) REFERENCES tool_calls(run_id,call_id));
      CREATE TABLE events (run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
      PRAGMA user_version=1;
    """)
    connection.execute("INSERT INTO metadata VALUES (?)", (str(root),))
    session = {"id": "legacy", "format": "native-v1", "history": [{"role": "assistant", "content": "kept", "provider_blocks": [{"type": "text", "text": "raw"}]}]}
    connection.execute("INSERT INTO sessions VALUES (?,?,?)", ("legacy", json.dumps(session), "2026-01-01"))
    run = {"run_id": "legacy-run", "session_id": "legacy", "status": "completed", "final_answer": "kept"}
    connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy-run", "legacy", None, "old-request", "hash", "{}", json.dumps(run), "completed", '{"status":"completed"}', "old-host", 123, "2026-01-01", "2026-01-01"))
    event = {"event": "run_finished", "sequence": 7, "event_id": "legacy-run:7", "status": "completed"}
    connection.execute("INSERT INTO events VALUES (?,?,?)", ("legacy-run", 7, json.dumps(event)))
    connection.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?)", ("legacy-run", "legacy-call", "write_file", '{"path":"legacy-file"}', "finished", '{"status":"rejected","error_code":"approval_denied"}'))
    connection.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)", ("legacy-approval", "legacy-run", "legacy-call", '{"id":"legacy-approval","run_id":"legacy-run","call_id":"legacy-call"}', "denied", 0))
    connection.commit()
    connection.close()
    return session, event


def test_v1_migration_preserves_history_events_and_has_private_backup(tmp_path):
    session, event = legacy_database(tmp_path)
    store = StateStore(tmp_path)
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.load_session("legacy") == session
    assert store.events("legacy-run") == [event]
    assert store.get_report("legacy-run")["status"] == "completed"
    assert store.review_get("legacy-run")["reason"] == "not_captured"
    backup, = (tmp_path / ".pico/backups").glob("*.sqlite3")
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert json.loads(old.execute("SELECT body FROM sessions").fetchone()[0]) == session
        for table in ("sessions", "runs", "tool_calls", "approvals", "events"):
            before = old.execute(f"SELECT * FROM {table}").fetchall()
            after = [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
            assert before == after
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_failure_rolls_back_schema_and_keeps_legacy_data(tmp_path, monkeypatch):
    session, _ = legacy_database(tmp_path)
    connect = sqlite3.connect

    class FailingMigration(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA user_version=2":
                raise sqlite3.OperationalError("Injected commit boundary failure")
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingMigration))
    with pytest.raises(PersistenceError):
        StateStore(tmp_path)
    with connect(tmp_path / ".pico/state.sqlite3") as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert old.execute("SELECT name FROM sqlite_master WHERE name='run_reviews'").fetchone() is None
        assert json.loads(old.execute("SELECT body FROM sessions").fetchone()[0]) == session


def test_queued_run_captures_after_previous_run_and_public_diff_is_scoped(tmp_path):
    (tmp_path / "file").write_text("zero\n")

    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[ModelResponse(tool_calls=[
            ToolCall("write_file", {"path": "file", "content": "first\n"})]), ModelResponse(text="Done")]).open()
        try:
            session, first = await start(app, {"approval": "ask"})
            approval = (await wait_for(lambda: app.store.list_approvals()))[0]
            second = await app.dispatch("runs.start", {"session_id": session["id"], "prompt": "second", "client_request_id": "two"})
            app.manager.scripts[second["run_id"]] = [
                {"text": "", "tool_calls": [{"id": "second-write", "name": "write_file", "arguments": {"path": "file", "content": "second\n"}}]},
                {"text": "Done"}]
            assert (await app.dispatch("runs.review", {"run_id": second["run_id"]}))["state"] == "pending"
            await app.dispatch("approvals.resolve", {"approval_id": approval["id"], "approved": True})
            pending = (await wait_for(lambda: app.store.list_approvals()))[0]
            await app.dispatch("approvals.resolve", {"approval_id": pending["id"], "approved": True})
            await wait_for(lambda: app.store.get_run(second["run_id"])["status"] in TERMINAL)
            rows = await app.dispatch("runs.changes", {"run_id": second["run_id"]})
            row, = rows["files"]
            diff = await app.dispatch("runs.diff", {"run_id": second["run_id"], "file_id": row["id"]})
            assert "-first\n+second\n" in diff["text"] and "zero" not in diff["text"]
            with pytest.raises(ApplicationError):
                await app.dispatch("runs.diff", {"run_id": first["run_id"], "file_id": "../../.env"})
            with pytest.raises(ApplicationError):
                await app.dispatch("runs.changes", {"run_id": first["run_id"], "scope": "sql"})
            assert (await app.dispatch("runs.review", {"run_id": second["run_id"]}))["state"] == "ready"
        finally:
            await app.close()

    asyncio.run(scenario())
