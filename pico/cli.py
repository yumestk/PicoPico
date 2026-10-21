"""Terminal client for the workspace Host; execution belongs to the service."""

import argparse
import json
import shutil
import sys
from types import SimpleNamespace

from .application import DEFAULT_OPTIONS, PROVIDER_CHOICES
from .host_client import HostClient
from .security import redact_text
from .state_store import TERMINAL, ApplicationError, identity
from .workspace import WorkspaceContext, middle

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)


WELCOME_NAME = "pico"


WELCOME_SUBTITLE = "local coding agent"


WELCOME_STATUS = "calm shell, ready for work"


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def display_event(event):
    name = event["event"]
    if name == "model_requested":
        message = "Requesting model"
    elif name == "tool_started":
        message = f"Using {event['name']}"
    elif name == "tool_executed":
        message = f"{event['name']}: {event['tool_status']} ({event.get('duration_ms', 0)} ms)"
    elif name == "approval_requested":
        message = f"Waiting for approval: {event['name']}"
    elif name == "run_finished":
        message = f"Run {event['status']} ({event['stop_reason']})"
    else:
        return
    print(f"[pico] {message}", file=sys.stderr, flush=True)


def console_approve(name, args):
    print(
        f"approve {name} {json.dumps(args, ensure_ascii=False)}? [y/N] ",
        file=sys.stderr,
        end="",
        flush=True,
    )
    return input().strip().lower() in {"y", "yes"}


class RuntimeOption(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if self.dest == "secret_env_names":
            value = [*getattr(namespace, self.dest, []), value]
        setattr(namespace, self.dest, value)
        namespace.explicit_config = {
            **getattr(namespace, "explicit_config", {}),
            self.dest: value,
        }


def add_options(parser):
    def option(name, **kwargs):
        key = name.replace("-", "_")
        default = DEFAULT_OPTIONS.get(key)
        parser.add_argument(
            "--" + name,
            action=RuntimeOption,
            default=default,
            **kwargs,
        )

    option("provider", choices=PROVIDER_CHOICES)
    for name in ["model", "host", "base-url"]:
        option(name)
    for name in ["ollama-timeout", "openai-timeout", "max-steps", "max-new-tokens"]:
        option(name, type=int)
    for name in ["temperature", "top-p"]:
        option(name, type=float)
    option("approval", choices=["ask", "auto", "never"])
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action=RuntimeOption,
        default=[],
    )
    parser.add_argument("--cwd", default=".")
    parser.add_argument(
        "--session",
        default=None,
        help="Database session ID, or latest.",
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Pico: local coding agent with a reconnectable workspace Host."
    )
    add_options(parser)
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="Submit a task and attach to its progress.")
    run.add_argument("prompt", nargs="+")
    run.add_argument(
        "--detach",
        action="store_true",
        help="Print run ID and leave execution with the Host.",
    )
    commands.add_parser("chat", help="Interactive session (default command).")
    commands.add_parser("configuration", help="Show non-secret provider configuration.")
    host = commands.add_parser("host")
    host.add_argument("action", choices=["start", "status", "stop"])
    host.add_argument("--cwd", default=argparse.SUPPRESS)
    runs = commands.add_parser("runs")
    runs.add_argument("action", choices=["list", "get", "attach", "cancel", "export", "review", "changes", "diff", "checks", "tool_calls", "review_refresh"])
    runs.add_argument("run_id", nargs="?")
    runs.add_argument("--after", type=int, default=0)
    runs.add_argument("--cursor", default="")
    runs.add_argument("--limit", type=int, default=100)
    runs.add_argument("--session-id")
    runs.add_argument("--file-id")
    runs.add_argument("--scope", choices=["initial", "run"], default="run")
    runs.add_argument("--cwd", default=argparse.SUPPRESS)
    approvals = commands.add_parser("approvals")
    approvals.add_argument("action", choices=["list", "approve", "deny"])
    approvals.add_argument("approval_id", nargs="?")
    approvals.add_argument("--cwd", default=argparse.SUPPRESS)
    sessions = commands.add_parser("sessions")
    sessions.add_argument("action", choices=["list", "get", "create", "reset"])
    sessions.add_argument("session_id", nargs="?")
    sessions.add_argument("--cursor", default="")
    sessions.add_argument("--limit", type=int, default=100)
    return parser


def attach(client, run_id, after=0):
    cancelling = False
    while True:
        try:
            page = client.call(
                "runs.events",
                {
                    "run_id": run_id,
                    "after_sequence": after,
                    "wait_ms": 1000,
                    "limit": 100,
                },
            )
            for event in page["events"]:
                display_event(event)
                after = event["sequence"]
            if not cancelling:
                for approval in client.call("approvals.list"):
                    if approval["run_id"] == run_id:
                        approved = console_approve(approval["name"], approval["args"])
                        try:
                            client.call(
                                "approvals.resolve",
                                {"approval_id": approval["id"], "approved": approved},
                            )
                        except ApplicationError as exc:
                            if exc.code not in {
                                "approval_expired",
                                "approval_conflict",
                            }:
                                raise
                            print(str(exc), file=sys.stderr)
            run = client.call("runs.get", {"run_id": run_id})
            if run["status"] in TERMINAL and after >= run["last_sequence"]:
                if run["final_answer"]:
                    print(run["final_answer"])
                return (
                    0
                    if run["status"] == "completed"
                    else 130
                    if run["status"] == "cancelled"
                    else 1
                )
        except KeyboardInterrupt:
            if cancelling:
                return 130
            client.call("runs.cancel", {"run_id": run_id})
            cancelling = True
        except EOFError:
            print(
                f"Detached. Reconnect with: pico runs attach {run_id}", file=sys.stderr
            )
            return 0


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        workspace = WorkspaceContext.build(args.cwd)
        client = HostClient(workspace.repo_root)
        if args.command == "host":
            result = (
                client.ensure()
                if args.action == "start"
                else client.call(
                    "host.info" if args.action == "status" else "host.stop"
                )
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        client.ensure()
        if args.command == "configuration":
            print(json.dumps(client.call("configuration.get"), ensure_ascii=False))
            return 0
        if args.command == "runs":
            if args.action != "list" and not args.run_id:
                raise ApplicationError("invalid_request", "A run ID is required")
            if args.action == "attach":
                return attach(client, args.run_id, args.after)
            params = {} if args.action == "list" else {"run_id": args.run_id}
            if args.action in {"list", "tool_calls", "checks"}:
                params.update(cursor=args.cursor, limit=args.limit)
            if args.action == "list" and args.session_id:
                params["session_id"] = args.session_id
            if args.action == "changes":
                params.update(after_file=args.after, limit=args.limit, scope=args.scope)
            if args.action == "diff":
                if not args.file_id:
                    raise ApplicationError("invalid_request", "--file-id from runs changes is required")
                params.update(file_id=args.file_id, after_character=args.after)
            print(
                json.dumps(
                    client.call("runs." + args.action, params), ensure_ascii=False
                )
            )
            return 0
        if args.command == "approvals":
            if args.action == "list":
                result = client.call("approvals.list")
            else:
                if not args.approval_id:
                    raise ApplicationError(
                        "invalid_request", "An approval ID is required"
                    )
                result = client.call(
                    "approvals.resolve",
                    {
                        "approval_id": args.approval_id,
                        "approved": args.action == "approve",
                    },
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        config = getattr(args, "explicit_config", {})
        if args.command == "sessions":
            if args.action == "list":
                result = client.call("sessions.list", {"cursor": args.cursor, "limit": args.limit})
            elif args.action == "create":
                result = client.call("sessions.create", {"config": config})
            else:
                if not args.session_id:
                    raise ApplicationError(
                        "invalid_request", "A session ID is required"
                    )
                result = client.call(
                    "sessions." + args.action, {"session_id": args.session_id}
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        session_id = args.session
        if session_id == "latest":
            existing = client.call("sessions.list")
            session_id = existing[0]["id"] if existing else None
        session = (
            client.call("sessions.get", {"session_id": session_id})
            if session_id
            else client.call("sessions.create", {"config": config})
        )
        view = SimpleNamespace(
            workspace=workspace,
            session=session,
            approval_policy=session["config"]["approval"],
        )
        print(
            build_welcome(
                view,
                session["config"]["model"],
                session["config"].get("base_url") or session["config"]["host"],
            ),
            file=sys.stderr,
        )

        def submit(prompt):
            return client.call(
                "runs.start",
                {
                    "session_id": session["id"],
                    "prompt": prompt,
                    "client_request_id": identity("submit"),
                    "config": config,
                },
            )

        if args.command == "run":
            run = submit(" ".join(args.prompt))
            if args.detach:
                print(run["run_id"])
                return 0
            return attach(client, run["run_id"])
        while True:
            try:
                print("pico> ", end="", file=sys.stderr, flush=True)
                prompt = input().strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                return 0
            if prompt == "/help":
                print("/session  /memory  /reset  /exit", file=sys.stderr)
                continue
            if prompt == "/session":
                print(session["id"], file=sys.stderr)
                continue
            if prompt == "/memory":
                print(
                    json.dumps(
                        client.call("sessions.get", {"session_id": session["id"]})[
                            "memory"
                        ],
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                continue
            if prompt == "/reset":
                session = client.call("sessions.reset", {"session_id": session["id"]})
                continue
            attach(client, submit(prompt)["run_id"])
    except KeyboardInterrupt:
        return 130
    except (ApplicationError, RuntimeError, OSError) as exc:
        print(redact_text(str(exc)), file=sys.stderr)
        return 1
