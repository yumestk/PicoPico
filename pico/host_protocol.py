"""Shared public protocol limits and workspace endpoint discovery."""

import hashlib
import os
from pathlib import Path

from .state_store import ApplicationError

PROTOCOL = "pico-host-v1"
MAX_PUBLIC_MESSAGE = 1024 * 1024


def socket_path(root):
    directory = Path(os.environ.get("PICO_RUNTIME_DIR", Path.home() / ".pico/run"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    path = directory / (key + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise ApplicationError(
            "invalid_request",
            "Unix socket path is too long; choose a shorter PICO_RUNTIME_DIR",
        )
    return path
