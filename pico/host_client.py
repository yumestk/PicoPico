"""Small synchronous client shared by CLI clients; each call owns its connection."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .host_protocol import MAX_PUBLIC_MESSAGE, PROTOCOL, socket_path
from .state_store import ApplicationError, identity


class HostClient:
    def __init__(self, root):
        if os.name != "posix":
            raise ApplicationError(
                "unsupported_platform", "The workspace Host currently requires POSIX"
            )
        self.root = Path(root).resolve()
        self.path = socket_path(self.root)

    def call(self, method, params=None):
        request = {
            "id": identity("request"),
            "protocol": PROTOCOL,
            "workspace": str(self.root),
            "method": method,
            "params": params or {},
        }
        raw = json.dumps(request, ensure_ascii=False).encode() + b"\n"
        if len(raw) > MAX_PUBLIC_MESSAGE:
            raise ApplicationError("invalid_request", "Request exceeds size limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(max(5, (params or {}).get("wait_ms", 0) / 1000 + 5))
            connection.connect(str(self.path))
            connection.sendall(raw)
            with connection.makefile("rb") as stream:
                response = stream.readline(MAX_PUBLIC_MESSAGE + 1)
        if not response or len(response) > MAX_PUBLIC_MESSAGE:
            raise ApplicationError(
                "invalid_response", "Missing or oversized Host response"
            )
        value = json.loads(response)
        if value.get("id") != request["id"]:
            raise ApplicationError("invalid_response", "Response ID mismatch")
        if "error" in value:
            raise ApplicationError(value["error"]["code"], value["error"]["message"])
        return value["result"]

    def ensure(self):
        try:
            return self.call("host.info")
        except OSError:
            pass
        directory = self.root / ".pico"
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, *(["-I"] if sys.flags.isolated else []),
             "-m", "pico.host", "--workspace", str(self.root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                return self.call("host.info")
            except OSError:
                time.sleep(0.05)
        raise ApplicationError(
            "host_unavailable", "Host did not start; inspect .pico/host.log"
        )
