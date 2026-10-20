"""One workspace queue with supervised execution processes and no automatic replay."""

import asyncio
import fcntl
import json
import logging
import sqlite3
import sys
from pathlib import Path

from .security import redact_text
from .state_store import TERMINAL, ApplicationError, identity
from .worker import MAX_MESSAGE

STORE_OPERATIONS = frozenset(
    {
        "save_session",
        "load_session",
        "begin_run",
        "update_run",
        "emit",
        "record",
        "finish",
        "events",
        "request_approval",
        "get_approval",
        "resolve_approval",
        "permit",
    }
)


def execution_idle(root):
    with (Path(root) / ".pico/execution.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


class RunManager:
    def __init__(self, store, instance_id=None):
        self.store = store
        self.instance_id = instance_id or identity("host")
        self.active = None
        self.process = None
        self.active_id = None
        self.accepting = True
        self.blocked = False
        self.scripts = {}
        self.cancel_tasks = set()
        self._cancelled_process = None

    async def recover(self):
        deadline = asyncio.get_running_loop().time() + 5
        while (
            not execution_idle(self.store.root)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)
        self.blocked = not execution_idle(self.store.root)
        for run in self.store.list_runs(limit=1000000):
            if run["status"] not in TERMINAL:
                self.store.interrupt(run["run_id"], "host_restarted")

    def pump(self):
        if not self.accepting or self.active is not None:
            return
        self.blocked = not execution_idle(self.store.root)
        if self.blocked:
            return
        # ponytail: one root execution slot; add concurrency only with workspace isolation.
        queued = [
            r
            for r in reversed(self.store.list_runs(limit=1000000))
            if r["status"] == "queued" and not r.get("parent_run_id")
        ]
        if queued:
            self.active_id = queued[0]["run_id"]
            self.active = asyncio.create_task(self._execute(queued[0]))
            self.active.add_done_callback(self._done)

    def _done(self, task):
        try:
            task.result()
        except Exception as exc:  # noqa: BLE001 - a dead worker must never strand the execution slot
            if self.active_id:
                try:
                    self.store.interrupt(self.active_id, "supervisor_error")
                except (OSError, ValueError, sqlite3.Error):
                    self.blocked = True
                    self.accepting = False
            logging.getLogger(__name__).error(
                "Run supervisor failed (%s)", type(exc).__name__
            )
        self.active = None
        self.process = None
        self.active_id = None
        self.pump()

    async def _execute(self, run):
        if self.store.get_run(run["run_id"])["status"] in TERMINAL:
            return
        config = run["config"]
        bootstrap = {
            "workspace": str(self.store.root),
            "config": config,
            "session": self.store.load_session(run["session_id"]),
            "run": run,
        }
        if run["run_id"] in self.scripts:
            bootstrap["script"] = self.scripts.pop(run["run_id"])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pico.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_MESSAGE,
        )
        self.process = process
        errors = asyncio.create_task(self._read_stderr(process, config))
        try:
            if self.store.get_run(run["run_id"])["status"] in TERMINAL:
                process.terminate()
                return
            self.store.set_worker(run["run_id"], self.instance_id, process.pid)
            process.stdin.write(
                json.dumps(bootstrap, ensure_ascii=False).encode() + b"\n"
            )
            await process.stdin.drain()
            if self.store.get_run(run["run_id"])["status"] == "cancelling":
                self._signal_cancel(process)
            while line := await process.stdout.readline():
                request = json.loads(line)
                response = {"id": request["id"]}
                try:
                    method = request["method"]
                    args = request["args"]
                    if method in STORE_OPERATIONS:
                        result = getattr(self.store, method)(*args)
                    elif method == "process_started":
                        value, pid, token = args
                        state = self.store.get_run(value)
                        if state["status"] != "running":
                            raise ApplicationError(
                                "run_cancelled", "Execution no longer active"
                            )
                        processes = [
                            *state.get("processes", []),
                            {"pid": pid, "token": token},
                        ]
                        self.store.update_run({**state, "processes": processes})
                        result = True
                    elif method == "process_stopped":
                        value, token = args
                        state = self.store.get_run(value)
                        self.store.update_run(
                            {
                                **state,
                                "processes": [
                                    p
                                    for p in state.get("processes", [])
                                    if p["token"] != token
                                ],
                            }
                        )
                        result = True
                    else:
                        raise ApplicationError(
                            "invalid_request", "Unknown internal operation"
                        )
                    response["result"] = result
                except ApplicationError as exc:
                    response["error"] = {"code": exc.code, "message": str(exc)}
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    response["error"] = {
                        "code": "persistence_error",
                        "message": "Could not commit execution state",
                    }
                process.stdin.write(
                    json.dumps(response, ensure_ascii=False).encode() + b"\n"
                )
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            await errors
        deadline = asyncio.get_running_loop().time() + 2
        while (
            not execution_idle(self.store.root)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)
        self.blocked = not execution_idle(self.store.root)
        current = self.store.get_run(run["run_id"])
        if current["status"] not in TERMINAL:
            self.store.interrupt(run["run_id"], "worker_exited")
        for child in self.store.list_runs(limit=1000000):
            if (
                child.get("parent_run_id") == run["run_id"]
                and child["status"] not in TERMINAL
            ):
                self.store.interrupt(child["run_id"], "parent_exited")

    async def _read_stderr(self, process, config):
        retained = bytearray()
        while chunk := await process.stderr.read(4096):
            retained.extend(chunk[: max(0, 8192 - len(retained))])
        if retained:
            message = redact_text(
                retained.decode(errors="replace"),
                secret_env_names=config["secret_env_names"],
            )
            try:
                self.store.emit(
                    self.active_id, "worker_diagnostic", {"message": message}
                )
            except (OSError, ValueError, sqlite3.Error):
                pass

    def cancel(self, value):
        run = self.store.get_run(value)
        if run.get("parent_run_id"):
            value = run["parent_run_id"]
        if self.store.get_run(value)["status"] in TERMINAL | {"cancelling"}:
            return self.store.get_run(value)
        result = self.store.cancel(value)
        for child in self.store.list_runs(limit=1000000):
            if child.get("parent_run_id") == value and child["status"] not in TERMINAL:
                self.store.cancel(child["run_id"])
        if (
            value == self.active_id
            and self.process is not None
            and self.process.returncode is None
        ):
            self._signal_cancel(self.process)
        return result

    def _signal_cancel(self, process):
        if self._cancelled_process is process:
            return
        self._cancelled_process = process
        process.stdin.write(b'{"cancel":true}\n')
        task = asyncio.create_task(self._cancel_deadline(process))
        self.cancel_tasks.add(task)
        task.add_done_callback(self.cancel_tasks.discard)

    async def _cancel_deadline(self, process):
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), 5)
        except asyncio.TimeoutError:
            # Killing the owned worker closes its command lifelines; guardians clean groups independently.
            if process.returncode is None:
                process.kill()

    async def close(self):
        self.accepting = False
        for run in self.store.list_runs(limit=1000000):
            if not run.get("parent_run_id") and run["status"] not in TERMINAL:
                self.cancel(run["run_id"])
        if self.active is not None:
            await self.active
        if self.cancel_tasks:
            await asyncio.gather(*self.cancel_tasks)
