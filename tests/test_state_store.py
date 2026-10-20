import json

import pytest

from pico.events import PersistenceError
from pico.state_store import ApplicationError, StateStore


def setup(store):
    session = {
        "id": "s",
        "history": [],
        "provider_protocol": "fake",
        "format": "native-v1",
    }
    store.save_session(session)
    state = store.create_run("s", "inspect", {"approval": "auto"}, request_id="request")
    store.begin_run(session, state)
    return session, state


def test_submit_is_idempotent_and_rejects_changed_content(tmp_path):
    store = StateStore(tmp_path)
    _, state = setup(store)
    assert (
        store.create_run("s", "inspect", {"approval": "auto"}, request_id="request")[
            "run_id"
        ]
        == state["run_id"]
    )
    with pytest.raises(ApplicationError, match="different content"):
        store.create_run("s", "different", request_id="request")
    assert len(store.list_runs()) == 1


def test_completion_and_event_are_atomic_and_unique(tmp_path):
    store = StateStore(tmp_path)
    session, state = setup(store)
    state.update(
        status="completed", stop_reason="final_answer_returned", final_answer="Done"
    )
    store.finish(session, state, {"answer": "Done"})
    store.finish(session, {**state, "status": "failed"}, {})
    assert store.get_run(state["run_id"])["status"] == "completed"
    assert store.get_report(state["run_id"])["run_state"]["status"] == "completed"
    assert (
        len([e for e in store.events(state["run_id"]) if e["event"] == "run_finished"])
        == 1
    )
    assert [e["sequence"] for e in store.events(state["run_id"])] == list(
        range(1, len(store.events(state["run_id"])) + 1)
    )
    root = store.export_run(state["run_id"])
    assert json.loads((root / "report.json").read_text())["status"] == "completed"
    assert len((root / "trace.jsonl").read_text().splitlines()) == len(
        store.events(state["run_id"])
    )


def test_event_commit_failure_rolls_back_entire_completion(tmp_path):
    store = StateStore(tmp_path)
    session, state = setup(store)
    store.connection.execute(
        "CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disk fault'); END"
    )
    with pytest.raises(PersistenceError):
        store.finish(
            {**session, "history": ["must roll back"]},
            {**state, "status": "completed"},
            {},
        )
    assert store.get_run(state["run_id"])["status"] == "running"
    assert store.get_report(state["run_id"]) is None
    assert store.load_session("s")["history"] == []


def tool(store, session, state):
    item = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call",
                "name": "write_file",
                "arguments": {"path": "x", "content": "hello"},
            }
        ],
    }
    session["history"].append(item)
    store.record(session, item, state["run_id"])


def test_approval_consumed_once_and_stale_baseline_rejected(tmp_path):
    store = StateStore(tmp_path)
    session, state = setup(store)
    tool(store, session, state)
    approval = store.request_approval(
        state["run_id"],
        "call",
        "write_file",
        {"path": "x", "content": "hello"},
        "before",
    )
    assert store.get_run(state["run_id"])["status"] == "waiting_approval"
    store.resolve_approval(approval["id"], True)
    store.resolve_approval(approval["id"], True)
    with pytest.raises(ApplicationError, match="different decision"):
        store.resolve_approval(approval["id"], False)
    assert not store.permit(state["run_id"], "call", "changed", approval["id"])
    with pytest.raises(ApplicationError):
        store.permit(state["run_id"], "call", "before", approval["id"])


def test_cancel_wins_over_late_completion_and_prevents_permission(tmp_path):
    store = StateStore(tmp_path)
    session, state = setup(store)
    tool(store, session, state)
    store.cancel(state["run_id"])
    with pytest.raises(ApplicationError):
        store.permit(state["run_id"], "call")
    store.finish(session, {**state, "status": "completed"}, {})
    assert store.get_run(state["run_id"])["status"] == "cancelled"


def test_interrupted_call_is_paired_as_unknown_and_never_replayed(tmp_path):
    store = StateStore(tmp_path)
    session, state = setup(store)
    tool(store, session, state)
    assert store.permit(state["run_id"], "call")
    with pytest.raises(ApplicationError):
        store.permit(state["run_id"], "call")
    store.interrupt(state["run_id"])
    assert store.get_run(state["run_id"])["status"] == "interrupted"
    assert "outcome_unknown" in store.load_session("s")["history"][-1]["content"]
    with pytest.raises(ApplicationError):
        store.permit(state["run_id"], "call")


def test_unknown_schema_and_legacy_json_left_intact(tmp_path):
    legacy = tmp_path / ".pico/sessions/old.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"id":"old"}')
    store = StateStore(tmp_path)
    assert store.latest_session() is None
    store.connection.execute("PRAGMA user_version=100")
    store.close()
    before = (tmp_path / ".pico/state.sqlite3").read_bytes()
    with pytest.raises(ApplicationError, match="Unsupported"):
        StateStore(tmp_path)
    assert before == (tmp_path / ".pico/state.sqlite3").read_bytes()
    assert legacy.read_text() == '{"id":"old"}'
