"""Fixed desktop bootstrap: resolve a selected directory and connect its Host."""

import argparse
import json
import sys
from pathlib import Path

from .host_client import HostClient
from .security import redact_text
from .state_store import ApplicationError
from .workspace import WorkspaceContext


def bootstrap(directory):
    selected = Path(directory).resolve(strict=True)
    if not selected.is_dir():
        raise ApplicationError("invalid_workspace", "Choose an existing directory")
    workspace = WorkspaceContext.build(selected)
    client = HostClient(workspace.repo_root)
    info = client.ensure()
    if info.get("workspace") != str(client.root):
        raise ApplicationError("workspace_mismatch", "Host targets another workspace")
    return {"workspace": str(client.root), "socket": str(client.path), "info": info}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    try:
        result = bootstrap(args.workspace)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "bootstrap_failed"),
                                   "message": redact_text(str(exc))}}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
