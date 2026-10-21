"""Private synchronous execution process. Only the Host may use this IPC entry."""

import fcntl
import json
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from .events import PersistenceError
from .state_store import ApplicationError

MAX_MESSAGE = 16 * 1024 * 1024


class WorkerBridge:
    managed = True

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / ".pico/state.sqlite3"
        self.cancel_event = threading.Event()
        self._condition = threading.Condition()
        self._responses = {}
        self._counter = 0
        self._closed = False
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
            if not line or len(line) > MAX_MESSAGE:
                with self._condition:
                    self._closed = True
                    self._condition.notify_all()
                if not self.cancel_event.is_set():
                    self.cancel_event.set()
                    os.kill(os.getpid(), signal.SIGINT)
                return
            message = json.loads(line)
            if message.get("cancel"):
                if not self.cancel_event.is_set():
                    self.cancel_event.set()
                    os.kill(os.getpid(), signal.SIGINT)
            else:
                with self._condition:
                    self._responses[message["id"]] = message
                    self._condition.notify_all()

    def call(self, method, *args):
        with self._condition:
            if self._closed:
                raise PersistenceError("Host disconnected; execution stopped")
            self._counter += 1
            request_id = self._counter
            try:
                raw = (
                    json.dumps(
                        {"id": request_id, "method": method, "args": args},
                        ensure_ascii=False,
                    ).encode()
                    + b"\n"
                )
                if len(raw) > MAX_MESSAGE:
                    raise PersistenceError("Internal state message exceeds size limit")
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError) as exc:
                raise PersistenceError("Host disconnected") from exc
            while request_id not in self._responses:
                if self._closed:
                    raise PersistenceError("Host disconnected")
                self._condition.wait(0.1)
            response = self._responses.pop(request_id)
        if "error" in response:
            error = response["error"]
            if error["code"] == "persistence_error":
                raise PersistenceError(error["message"])
            raise ApplicationError(error["code"], error["message"])
        return response.get("result")

    def save_session(self, session):
        return self.call("save_session", session)

    def load_session(self, id):
        return self.call("load_session", id)

    def begin_run(self, session, state, config=None):
        return self.call("begin_run", session, state, config)

    def update_run(self, state):
        return self.call("update_run", state)

    def emit(self, value, name, payload, state=None):
        return self.call("emit", value, name, payload, state)

    def record(self, session, item, value=None, state=None, event=None):
        return self.call("record", session, item, value, state, event)

    def finish(self, session, state, report):
        return self.call("finish", session, state, report)

    def events(self, value, after=0, limit=1000):
        return self.call("events", value, after, limit)

    def review_begin(self, value):
        return self.call("review_begin", value)

    def shell_begin(self, value, call_id, evidence):
        return self.call("shell_begin", value, call_id, evidence)

    def review_blob(self, value, content):
        return self.call("review_blob", value, content)

    def review_save(self, value, stage, snapshot):
        return self.call("review_save", value, stage, snapshot)

    def review_unavailable(self, value, reason):
        return self.call("review_unavailable", value, reason)

    def request_approval(self, value, call_id, name, args, baseline):
        return self.call("request_approval", value, call_id, name, args, baseline)

    def get_approval(self, id):
        return self.call("get_approval", id)

    def resolve_approval(self, id, approved):
        return self.call("resolve_approval", id, approved)

    def permit(self, value, call_id, baseline="", approval_id=None):
        return self.call("permit", value, call_id, baseline, approval_id)

    def process_started(self, value, pid, token):
        return self.call("process_started", value, pid, token)

    def process_stopped(self, value, token):
        return self.call("process_stopped", value, token)


def main():
    bootstrap = json.loads(sys.stdin.buffer.readline(MAX_MESSAGE + 1))
    root = Path(bootstrap["workspace"])
    execution_lock = (root / ".pico/execution.lock").open("a+")
    fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    bridge = WorkerBridge(root)
    from .application import _build_model_client, create_runtime
    from .config import load_project_env
    from .providers.clients import FakeModelClient
    from .providers.types import ModelResponse, ToolCall
    from .workspace import WorkspaceContext

    load_project_env(root)
    config = bootstrap["config"]
    if "script" in bootstrap:
        outputs = []
        for row in bootstrap["script"]:
            row = {
                **row,
                "tool_calls": [ToolCall(**c) for c in row.get("tool_calls", [])],
            }
            outputs.append(ModelResponse(**row))
        model = FakeModelClient(outputs)
    else:
        model = _build_model_client(SimpleNamespace(**config))
    agent = create_runtime(
        config,
        WorkspaceContext.build(root, repo_root_override=root),
        bridge,
        model_client=model,
        session=bootstrap["session"],
        cancel_event=bridge.cancel_event,
        execution_lock_fd=execution_lock.fileno(),
    )
    try:
        agent.ask(bootstrap["run"]["user_request"], run_id=bootstrap["run"]["run_id"])
    except (KeyboardInterrupt, RuntimeError, ValueError, OSError):
        return 1
    finally:
        execution_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
