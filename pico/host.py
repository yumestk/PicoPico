"""Workspace-local Host. Public control never exposes the private worker protocol."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .application import ApplicationService
from .host_protocol import MAX_PUBLIC_MESSAGE, PROTOCOL, socket_path
from .state_store import ApplicationError


class Host:
    def __init__(self, root, *, test_script=None):
        self.root = Path(root).resolve()
        self.test_script = test_script
        self.app = None
        self.connections = 0

    async def serve(self):
        directory = self.root / ".pico"
        directory.mkdir(exist_ok=True, mode=0o700)
        lock = (directory / "host.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise ApplicationError(
                "host_running", "A Host already owns this workspace"
            ) from exc
        path = socket_path(self.root)
        server = None
        try:
            self.app = await ApplicationService(
                self.root, test_script=self.test_script
            ).open()
            path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(
                self._connection, path=str(path), limit=MAX_PUBLIC_MESSAGE
            )
            os.chmod(path, 0o600)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.app.stopping.set)
            await self.app.stopping.wait()
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            if self.app is not None:
                await self.app.close()
            path.unlink(missing_ok=True)
            lock.close()

    async def _connection(self, reader, writer):
        self.connections += 1
        try:
            if self.connections > 64:
                return
            while not reader.at_eof():
                request_id = None
                try:
                    raw = await asyncio.wait_for(reader.readline(), 30)
                    if not raw:
                        return
                    if len(raw) > MAX_PUBLIC_MESSAGE:
                        raise ApplicationError(
                            "invalid_request", "Request exceeds size limit"
                        )
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) - {
                        "id",
                        "protocol",
                        "workspace",
                        "method",
                        "params",
                    }:
                        raise ApplicationError(
                            "invalid_request", "Invalid request envelope"
                        )
                    request_id = value.get("id")
                    if type(request_id) not in (str, int) or len(str(request_id)) > 128:
                        raise ApplicationError("invalid_request", "Invalid request ID")
                    if value.get("protocol") != PROTOCOL:
                        raise ApplicationError(
                            "unsupported_protocol", "Unsupported Host protocol"
                        )
                    if value.get("workspace") != str(self.root):
                        raise ApplicationError(
                            "workspace_mismatch",
                            "Connection targets a different workspace",
                        )
                    result = await self.app.dispatch(
                        value.get("method"), value.get("params")
                    )
                    response = {"id": request_id, "result": result}
                except ApplicationError as exc:
                    response = {
                        "id": request_id,
                        "error": {"code": exc.code, "message": str(exc)},
                    }
                except (ValueError, TypeError, asyncio.LimitOverrunError):
                    response = {
                        "id": request_id,
                        "error": {
                            "code": "invalid_request",
                            "message": "Malformed or oversized JSON request",
                        },
                    }
                except (OSError, sqlite3.Error):
                    response = {
                        "id": request_id,
                        "error": {
                            "code": "persistence_error",
                            "message": "Could not access workspace state",
                        },
                    }
                    self.app.manager.accepting = False
                encoded = json.dumps(response, ensure_ascii=False).encode() + b"\n"
                if len(encoded) > MAX_PUBLIC_MESSAGE:
                    encoded = (
                        json.dumps(
                            {
                                "id": request_id,
                                "error": {
                                    "code": "response_too_large",
                                    "message": "Use a smaller page or export the run",
                                },
                            }
                        ).encode()
                        + b"\n"
                    )
                writer.write(encoded)
                await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            self.connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    directory = Path(args.workspace) / ".pico"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "host.log", maxBytes=1024 * 1024, backupCount=1
    )
    os.chmod(directory / "host.log", 0o600)
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        asyncio.run(Host(args.workspace).serve())
    except (ApplicationError, OSError, sqlite3.Error) as exc:
        logging.getLogger(__name__).error("Host stopped: %s", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
