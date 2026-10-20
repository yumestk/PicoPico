"""Exercise configured providers through the real detached Host in isolated workspaces."""

import argparse
import json
import shlex
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from pico.application import DEFAULT_OPTIONS, _build_model_client
from pico.config import load_project_env
from pico.host_client import HostClient
from pico.security import redact_artifact
from pico.state_store import TERMINAL, identity


def run_case(provider, name, model=None):
    root = Path(tempfile.mkdtemp(prefix="pico-phase2-" + name + "-", dir="/tmp"))
    original = 'header = \'<tag attr="x">\'\nvalue = "beta"\n'
    (root / "sample.txt").write_text(original)
    client = HostClient(root)
    row = {"scenario": name, "workspace": str(root), "status": "failed"}
    try:
        client.ensure()
        options = {
            "provider": provider,
            "approval": "ask" if name != "denial" else "never",
            "max_steps": 8,
            "max_new_tokens": 1024,
            "openai_timeout": 45,
            "ollama_timeout": 5,
            "allowed_tools": ["read_file", "patch_file", "run_shell"]
            if name == "workflow"
            else ["write_file"],
        }
        if model:
            options["model"] = model
        session = client.call("sessions.create", {"config": options})
        if name == "workflow":
            command = shlex.join(
                [
                    sys.executable,
                    "-c",
                    "import sys;print('expected-failure',file=sys.stderr);sys.exit(3)",
                ]
            )
            prompt = (
                'Read sample.txt using read_file, patch only value = "beta" to value = "beta-native", '
                "preserving the header line. Read it again to verify. "
                f"Then use run_shell once for this exact command: {command}. "
                "Exit code 3 is intentional; report it honestly and do not fix or rerun the command."
            )
        else:
            prompt = "Use write_file once to create forbidden.txt containing hello. If denied or cancelled, stop and explain."
        run = client.call(
            "runs.start",
            {
                "session_id": session["id"],
                "prompt": prompt,
                "client_request_id": identity("smoke"),
            },
        )
        value = run["run_id"]
        row["run_id"] = value
        # Drop the submitting client; the replacement replays persisted events and controls approvals.
        client = HostClient(root)
        cursor = 0
        approvals = 0
        cancelled = False
        seen = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            page = client.call(
                "runs.events",
                {"run_id": value, "after_sequence": cursor, "wait_ms": 1000},
            )
            seen.extend(page["events"])
            cursor = page["next_sequence"]
            for approval in client.call("approvals.list"):
                if approval["run_id"] != value:
                    continue
                replacement = HostClient(root)
                if name == "cancellation":
                    replacement.call("runs.cancel", {"run_id": value})
                    cancelled = True
                else:
                    replacement.call(
                        "approvals.resolve",
                        {"approval_id": approval["id"], "approved": True},
                    )
                    approvals += 1
            result = client.call("runs.get", {"run_id": value})
            if result["status"] in TERMINAL and cursor >= result["last_sequence"]:
                break
        else:
            client.call("runs.cancel", {"run_id": value})
            raise RuntimeError("Host smoke exceeded its deadline")
        row.update(
            run_status=result["status"],
            tool_steps=result["tool_steps"],
            final_answer=result["final_answer"],
            approval_count=approvals,
            exported=client.call("runs.export", {"run_id": value})["path"],
        )
        if name == "workflow":
            tool_events = [e for e in seen if e["event"] == "tool_executed"]
            names = [e["name"] for e in tool_events]
            row["checks"] = {
                "exact_file": (root / "sample.txt").read_text()
                == original.replace('"beta"', '"beta-native"'),
                "read_patch_read": len([n for n in names if n == "read_file"]) >= 2
                and "patch_file" in names
                and names.index("read_file")
                < names.index("patch_file")
                < len(names) - 1 - names[::-1].index("read_file"),
                "reported_command_failure": any(
                    e["name"] == "run_shell"
                    and e["tool_status"] == "error"
                    and "exit_code: 3" in e["result"]
                    for e in tool_events
                ),
                "cross_client_approval": approvals >= 2,
                "normal_completion": result["status"] == "completed",
            }
        elif name == "denial":
            row["checks"] = {
                "no_file": not (root / "forbidden.txt").exists(),
                "denied": any(
                    e["event"] == "tool_executed"
                    and e.get("tool_error_code") == "approval_denied"
                    for e in seen
                ),
                "normal_completion": result["status"] == "completed",
            }
        else:
            row["checks"] = {
                "no_file": not (root / "forbidden.txt").exists(),
                "cancel_sent": cancelled,
                "cancelled": result["status"] == "cancelled",
            }
        row["status"] = "passed" if all(row["checks"].values()) else "failed"
        if "Could not reach model endpoint" in result["final_answer"]:
            row.update(status="blocked", reason="Model endpoint unreachable")
    except (RuntimeError, ValueError, OSError) as exc:
        row["error"] = str(exc)
    finally:
        try:
            client.call("host.stop")
        except (ValueError, OSError):
            pass
    return redact_artifact(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=["deepseek", "openai", "anthropic", "ollama"],
        action="append",
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--output", type=Path, default=Path(".pico/evaluations/phase2-live.json")
    )
    args = parser.parse_args(argv)
    load_project_env(Path.cwd())
    report = {"storage_schema": 1, "host_protocol": "pico-host-v1", "providers": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for provider in args.provider or ["deepseek", "openai", "anthropic", "ollama"]:
        config = {**DEFAULT_OPTIONS, "provider": provider, "model": args.model}
        client = _build_model_client(SimpleNamespace(**config))
        row = {
            "provider": provider,
            "model": client.model,
            "host": urlsplit(
                getattr(client, "base_url", getattr(client, "host", ""))
            ).hostname,
        }
        if provider != "ollama" and not client.api_key:
            row.update(status="blocked", reason="API key not configured")
        else:
            row["cases"] = []
            for name in ["workflow", "denial", "cancellation"]:
                case = run_case(provider, name, args.model)
                row["cases"].append(case)
                print(
                    json.dumps(
                        {
                            "provider": provider,
                            "scenario": name,
                            "status": case["status"],
                        }
                    ),
                    flush=True,
                )
                if case["status"] != "passed":
                    break
            row["status"] = (
                "passed"
                if len(row["cases"]) == 3
                and all(c["status"] == "passed" for c in row["cases"])
                else "blocked"
                if any(c["status"] == "blocked" for c in row["cases"])
                else "failed"
            )
        report["providers"].append(row)
        args.output.write_text(
            json.dumps(redact_artifact(report), ensure_ascii=False, indent=2) + "\n"
        )
    print(json.dumps({r["provider"]: r["status"] for r in report["providers"]}))
    return 1 if any(r["status"] == "failed" for r in report["providers"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
