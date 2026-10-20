import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from pico.host_client import HostClient
from pico.providers.types import ModelResponse, ToolCall
from pico.state_store import TERMINAL, ApplicationError


def wait(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for Host")


@contextmanager
def running_host(root, script=None):
    runtime = tempfile.mkdtemp(prefix="pico-host-", dir="/tmp")
    env = {**os.environ, "PICO_RUNTIME_DIR": runtime}
    old = os.environ.get("PICO_RUNTIME_DIR")
    os.environ["PICO_RUNTIME_DIR"] = runtime
    code = (
        (
            "import asyncio; from pico.host import Host; from pico.providers.types import ModelResponse,ToolCall; "
            f"rows={json.dumps([asdict(item) for item in script])!r}; import json; "
            "script=[ModelResponse(**{**r,'tool_calls':[ToolCall(**c) for c in r['tool_calls']]}) for r in json.loads(rows)]; "
            f"asyncio.run(Host({str(root)!r},test_script=script).serve())"
        )
        if script is not None
        else None
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code]
        if code
        else [sys.executable, "-m", "pico.host", "--workspace", str(root)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = HostClient(root)
    try:

        def available():
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            try:
                return client.call("host.info")
            except OSError:
                return False

        wait(available)
        yield client, process, env
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.stderr.close()
        if old is None:
            os.environ.pop("PICO_RUNTIME_DIR", None)
        else:
            os.environ["PICO_RUNTIME_DIR"] = old
        shutil.rmtree(runtime, ignore_errors=True)


def submit(client, config=None):
    session = client.call("sessions.create", {"config": config or {"approval": "auto"}})
    run = client.call(
        "runs.start",
        {
            "session_id": session["id"],
            "prompt": "perform requested action",
            "client_request_id": "start",
        },
    )
    return session, run


def terminal(client, value):
    result = client.call("runs.get", {"run_id": value})
    return result if result["status"] in TERMINAL else False


def test_real_host_clients_reconnect_and_resolve_approval(tmp_path):
    script = [
        ModelResponse(
            tool_calls=[ToolCall("write_file", {"path": "result", "content": "hello"})]
        ),
        ModelResponse(text="Done"),
    ]
    with running_host(tmp_path, script) as (client, process, _env):
        _session, run = submit(client, {"approval": "ask"})
        approval = wait(lambda: client.call("approvals.list"))[0]
        replacement = HostClient(tmp_path)
        prefix = replacement.call("runs.events", {"run_id": run["run_id"], "limit": 2})
        replacement.call(
            "approvals.resolve", {"approval_id": approval["id"], "approved": True}
        )
        assert (
            wait(lambda: terminal(replacement, run["run_id"]))["status"] == "completed"
        )
        suffix = replacement.call(
            "runs.events",
            {"run_id": run["run_id"], "after_sequence": prefix["next_sequence"]},
        )
        assert all(e["sequence"] > prefix["next_sequence"] for e in suffix["events"])
        assert (tmp_path / "result").read_text() == "hello"
        assert process.poll() is None
        assert client.path.stat().st_mode & 0o777 == 0o600


def test_invalid_and_private_protocol_requests_never_execute(tmp_path):
    with running_host(tmp_path, []) as (client, _, _):
        for method, params in [
            ("permit", {}),
            ("runs.cancel", {"run_id": 7}),
            ("runs.events", {"run_id": "x", "limit": True}),
            ("sessions.create", {"config": {"arbitrary_module": "os"}}),
        ]:
            with pytest.raises(ApplicationError):
                client.call(method, params)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(client.path))
            connection.sendall(b"{not-json}\n")
            response = json.loads(connection.recv(4096))
            assert response["error"]["code"] == "invalid_request"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(client.path))
            connection.sendall(
                json.dumps(
                    {
                        "id": "x",
                        "method": "host.info",
                        "protocol": "wrong",
                        "workspace": str(tmp_path),
                    }
                ).encode()
                + b"\n"
            )
            assert (
                json.loads(connection.recv(4096))["error"]["code"]
                == "unsupported_protocol"
            )
        assert client.call("runs.list") == []


def test_host_is_singleton(tmp_path):
    with running_host(tmp_path, []) as (client, _, env):
        second = subprocess.run(
            [sys.executable, "-m", "pico.host", "--workspace", str(tmp_path)],
            env=env,
            capture_output=True,
            timeout=5,check=False,
        )
        assert second.returncode == 1
        assert client.call("host.info")["status"] == "ready"


def test_model_wait_is_cancelled_from_another_client(tmp_path):
    requested = Event()
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requested.set()
            release.wait(6)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b'{"content":[{"type":"text","text":"late"}],"stop_reason":"end_turn"}'
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with running_host(tmp_path) as (client, _, _):
            _, run = submit(
                client,
                {
                    "provider": "anthropic",
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "approval": "auto",
                },
            )
            assert requested.wait(5)
            started = time.monotonic()
            HostClient(tmp_path).call("runs.cancel", {"run_id": run["run_id"]})
            assert (
                wait(lambda: terminal(client, run["run_id"]))["status"] == "cancelled"
            )
            assert time.monotonic() - started < 5
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_sigkill_host_interrupts_old_run_and_invalidates_approval(tmp_path):
    script = [
        ModelResponse(
            tool_calls=[ToolCall("write_file", {"path": "no", "content": "no"})]
        ),
        ModelResponse(text="Done"),
    ]
    with running_host(tmp_path, script) as (client, process, env):
        _, run = submit(client, {"approval": "ask"})
        approval = wait(lambda: client.call("approvals.list"))[0]
        process.kill()
        process.wait(timeout=5)
        restarted = subprocess.Popen(
            [sys.executable, "-m", "pico.host", "--workspace", str(tmp_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:

            def ready():
                try:
                    return client.call("host.info")
                except (OSError, ApplicationError):
                    return False

            wait(ready)
            assert terminal(client, run["run_id"])["status"] == "interrupted"
            with pytest.raises(ApplicationError):
                client.call(
                    "approvals.resolve",
                    {"approval_id": approval["id"], "approved": True},
                )
            assert not (tmp_path / "no").exists()
        finally:
            restarted.terminate()
            restarted.wait(timeout=10)
            restarted.stderr.close()
