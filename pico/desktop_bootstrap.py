"""Fixed desktop bootstrap: resolve a selected directory and connect its Host."""

import argparse
import json
import sys
import time
from pathlib import Path

from .host_client import HostClient
from .security import redact_text
from .state_store import ApplicationError
from .workspace import WorkspaceContext


def bootstrap(directory, upgrade=False):
    selected = Path(directory).resolve(strict=True)
    if not selected.is_dir():
        raise ApplicationError("invalid_workspace", "Choose an existing directory")
    workspace = WorkspaceContext.build(selected)
    client = HostClient(workspace.repo_root)
    info = client.ensure()
    if upgrade:
        if "idle_upgrade" not in info.get("capabilities", []):
            raise ApplicationError("manual_upgrade_required", "旧 Host 不支持原子空闲升级。请先完成任务，再用 CLI 停止 Host，然后重新连接；桌面不会停止活动任务。")
        try:
            client.call("host.stop_if_idle")
        except ApplicationError as exc:
            # An older Host may close after accepting the stop, before sending its ACK.
            # Observe shutdown below; never replace the atomic guard with a forced stop.
            if exc.code != "invalid_response":
                raise
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                client.call("host.info")
            except OSError:
                break
            except ApplicationError as exc:
                if exc.code != "invalid_response":
                    raise
            time.sleep(0.05)
        else:
            raise ApplicationError("upgrade_timeout", "Host is still stopping; reconnect later")
        info = client.ensure()
    if info.get("workspace") != str(client.root):
        raise ApplicationError("workspace_mismatch", "Host targets another workspace")
    return {"workspace": str(client.root), "socket": str(client.path), "info": info}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    try:
        result = bootstrap(args.workspace, args.upgrade)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "bootstrap_failed"),
                                   "message": redact_text(str(exc))}}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
