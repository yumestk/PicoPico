import asyncio
import shlex
import sys

import pytest

from pico.application import ApplicationService
from pico.providers.types import ModelResponse, ToolCall
from pico.run_manager import execution_idle
from pico.state_store import TERMINAL, ApplicationError


async def wait_for(predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for supervised execution")


async def start(app, config=None, request="one"):
    session = await app.dispatch(
        "sessions.create", {"config": config or {"approval": "auto"}}
    )
    run = await app.dispatch(
        "runs.start",
        {
            "session_id": session["id"],
            "prompt": "perform task",
            "client_request_id": request,
        },
    )
    return session, run


def test_worker_performs_native_write_and_persists_terminal(tmp_path):
    async def scenario():
        app = await ApplicationService(
            tmp_path,
            test_script=[
                ModelResponse(
                    tool_calls=[
                        ToolCall("write_file", {"path": "done", "content": "hello"})
                    ]
                ),
                ModelResponse(text="Done"),
            ],
        ).open()
        try:
            _, run = await start(app)
            await wait_for(
                lambda: app.store.get_run(run["run_id"])["status"] in TERMINAL
            )
            assert (tmp_path / "done").read_text() == "hello"
            assert app.store.get_run(run["run_id"])["status"] == "completed"
            assert (
                len(
                    [
                        e
                        for e in app.store.events(run["run_id"])
                        if e["event"] == "run_finished"
                    ]
                )
                == 1
            )
        finally:
            await app.close()

    asyncio.run(scenario())


def test_queue_and_approval_can_be_controlled_after_submitter_detaches(tmp_path):
    async def scenario():
        app = await ApplicationService(
            tmp_path,
            test_script=[
                ModelResponse(
                    tool_calls=[
                        ToolCall("write_file", {"path": "approved", "content": "yes"})
                    ]
                ),
                ModelResponse(text="Done"),
            ],
        ).open()
        try:
            session, first = await start(app, {"approval": "ask"})
            pending = await wait_for(lambda: app.store.list_approvals())
            second = await app.dispatch(
                "runs.start",
                {
                    "session_id": session["id"],
                    "prompt": "next",
                    "client_request_id": "two",
                },
            )
            repeated = await app.dispatch(
                "runs.start",
                {
                    "session_id": session["id"],
                    "prompt": "next",
                    "client_request_id": "two",
                },
            )
            assert second["run_id"] == repeated["run_id"]
            assert app.store.get_run(second["run_id"])["status"] == "queued"
            with pytest.raises(ApplicationError, match="unfinished"):
                await app.dispatch("sessions.reset", {"session_id": session["id"]})
            await app.dispatch(
                "approvals.resolve", {"approval_id": pending[0]["id"], "approved": True}
            )
            await wait_for(
                lambda: app.store.get_run(first["run_id"])["status"] in TERMINAL
            )
            assert (tmp_path / "approved").read_text() == "yes"
            await app.dispatch("runs.cancel", {"run_id": second["run_id"]})
            await wait_for(
                lambda: app.store.get_run(second["run_id"])["status"] in TERMINAL
            )
        finally:
            await app.close()

    asyncio.run(scenario())


def test_workspace_change_invalidates_approved_operation(tmp_path):
    async def scenario():
        app = await ApplicationService(
            tmp_path,
            test_script=[
                ModelResponse(
                    tool_calls=[ToolCall("write_file", {"path": "no", "content": "no"})]
                ),
                ModelResponse(text="Stopped"),
            ],
        ).open()
        try:
            _, run = await start(app, {"approval": "ask"})
            approval = (await wait_for(lambda: app.store.list_approvals()))[0]
            (tmp_path / "external-change").write_text("changed")
            await app.dispatch(
                "approvals.resolve", {"approval_id": approval["id"], "approved": True}
            )
            await wait_for(
                lambda: app.store.get_run(run["run_id"])["status"] in TERMINAL
            )
            assert not (tmp_path / "no").exists()
            assert app.store.get_approval(approval["id"])["status"] == "invalidated"
        finally:
            await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kill_worker", [False, True])
def test_cancel_or_kill_worker_cleans_command_descendants(tmp_path, kill_worker):
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import pathlib,time;pathlib.Path('ready').write_text('ready');time.sleep(1);pathlib.Path('late').write_text('late')",
        ]
    )

    async def scenario():
        app = await ApplicationService(
            tmp_path,
            test_script=[
                ModelResponse(tool_calls=[ToolCall("run_shell", {"command": command})]),
                ModelResponse(text="Done"),
            ],
        ).open()
        try:
            _, run = await start(app)
            await wait_for(lambda: (tmp_path / "ready").exists())
            assert not execution_idle(tmp_path)
            if kill_worker:
                app.manager.process.kill()
            else:
                await app.dispatch("runs.cancel", {"run_id": run["run_id"]})
            await wait_for(
                lambda: app.store.get_run(run["run_id"])["status"] in TERMINAL
            )
            await wait_for(lambda: execution_idle(tmp_path))
            assert app.store.get_run(run["run_id"])["status"] == (
                "interrupted" if kill_worker else "cancelled"
            )
            await asyncio.sleep(1.1)
            assert not (tmp_path / "late").exists()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_read_only_delegate_runs_in_parent_slot_and_has_distinct_identity(tmp_path):
    (tmp_path / "README.md").write_text("hello")

    async def scenario():
        app = await ApplicationService(
            tmp_path,
            test_script=[
                ModelResponse(
                    tool_calls=[
                        ToolCall("delegate", {"task": "inspect", "max_steps": 1})
                    ]
                ),
                ModelResponse(
                    tool_calls=[ToolCall("read_file", {"path": "README.md"})]
                ),
                ModelResponse(text="child done"),
                ModelResponse(text="parent done"),
            ],
        ).open()
        try:
            _, run = await start(app)
            await wait_for(
                lambda: app.store.get_run(run["run_id"])["status"] in TERMINAL
            )
            children = [
                r
                for r in app.store.list_runs()
                if r.get("parent_run_id") == run["run_id"]
            ]
            assert len(children) == 1
            assert children[0]["status"] == "completed"
            assert children[0]["run_id"] != run["run_id"]
            assert app.store.get_run(run["run_id"])["status"] == "completed"
        finally:
            await app.close()

    asyncio.run(scenario())
