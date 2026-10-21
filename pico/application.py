"""Application assembly: one place resolves workspace, model and execution policy."""

import asyncio
import json
import os
from dataclasses import asdict
from types import SimpleNamespace

from .config import load_project_env, provider_env
from .providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from .runtime import Pico
from .state_store import StateStore
from .workspace import WorkspaceContext

DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"


DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


DEFAULT_OPENAI_MODEL = "gpt-5.4"


DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"


DEFAULT_PROVIDER = "deepseek"


PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")


SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def display_endpoint(endpoint):
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(endpoint)
    authority = parsed.hostname or ""
    if ":" in authority:
        authority = "[" + authority + "]"
    if parsed.port:
        authority += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _effective_provider(args):
    # Provider 选择优先级：
    # 1. 用户显式传入 --provider
    # 2. 项目 .env / shell 里的 PICO_PROVIDER
    # 3. 代码里的默认 provider
    provider = getattr(args, "provider", None) or provider_env(
        "PICO_PROVIDER", default=DEFAULT_PROVIDER
    )
    if provider not in PROVIDER_CHOICES:
        choices = ", ".join(PROVIDER_CHOICES)
        raise ValueError(f"unknown provider: {provider}. expected one of: {choices}")
    return provider


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper() for item in extra_names.split(",") if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = _effective_provider(args)
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 原生消息格式、工具声明和 HTTP 协议差异由 provider adapter 负责。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env(
            "PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL
        )
        api_key = provider_env(
            "PICO_OPENAI_API_KEY",
            (
                "OPENAI_API_KEY",
                "PICO_RIGHT_CODES_API_KEY",
                "RIGHT_CODES_API_KEY",
                "PICO_ANTHROPIC_API_KEY",
                "ANTHROPIC_API_KEY",
            ),
        )
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(
                args, "openai_timeout", getattr(args, "ollama_timeout", 300)
            ),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env(
            "PICO_ANTHROPIC_API_BASE",
            ("ANTHROPIC_API_BASE",),
            DEFAULT_ANTHROPIC_BASE_URL,
        )
        api_key = provider_env(
            "PICO_ANTHROPIC_API_KEY",
            (
                "ANTHROPIC_API_KEY",
                "PICO_RIGHT_CODES_API_KEY",
                "RIGHT_CODES_API_KEY",
                "PICO_OPENAI_API_KEY",
                "OPENAI_API_KEY",
            ),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(
                args, "openai_timeout", getattr(args, "ollama_timeout", 300)
            ),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env(
            "PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL
        )
        api_key = provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(
                args, "openai_timeout", getattr(args, "ollama_timeout", 300)
            ),
            # Native blocks are replayed; retain the bounded non-thinking default
            # while reasoning-mode budgets are outside this stage.
            thinking={"type": "disabled"},
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def build_runtime(args, approval_handler=None, event_handler=None):
    """Build an inline runtime for focused configuration tests and harnesses.

    Production clients submit runs through ApplicationService/Host instead.
    """
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = StateStore(workspace.repo_root)
    model = _build_model_client(args)
    session_id = getattr(args, "session", None)
    if session_id == "latest":
        session_id = store.latest_session()
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            approval_handler=approval_handler,
            event_handler=event_handler,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        approval_handler=approval_handler,
        event_handler=event_handler,
    )


def create_runtime(
    config, workspace, store, *, model_client=None, session=None, **execution
):
    """Assemble the same execution kernel for workers and deterministic harness runs."""
    model = (
        model_client
        if model_client is not None
        else _build_model_client(SimpleNamespace(**config))
    )
    return Pico(
        model,
        workspace,
        store,
        session=session,
        approval_policy=config["approval"],
        max_steps=config["max_steps"],
        max_new_tokens=config["max_new_tokens"],
        secret_env_names=config.get("secret_env_names", []),
        allowed_tools=config.get("allowed_tools"),
        **execution,
    )


DEFAULT_OPTIONS = {
    "provider": None,
    "model": None,
    "base_url": None,
    "host": DEFAULT_OLLAMA_HOST,
    "temperature": 0.2,
    "top_p": 0.9,
    "ollama_timeout": 300,
    "openai_timeout": 300,
    "approval": "ask",
    "max_steps": 6,
    "max_new_tokens": 512,
    "secret_env_names": [],
    "allowed_tools": None,
}


def freeze_options(root, supplied=None):
    from .state_store import ApplicationError
    from .tools import legal_tool_names

    load_project_env(root)
    supplied = supplied or {}
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_OPTIONS):
        raise ApplicationError("invalid_request", "Unknown runtime option")
    options = {**DEFAULT_OPTIONS, **supplied}
    for key in ["provider", "model", "base_url", "host"]:
        if options[key] is not None and (
            not isinstance(options[key], str) or not options[key].strip()
        ):
            raise ApplicationError(
                "invalid_request", f"{key} must be a nonempty string"
            )
    for key, maximum in [
        ("max_steps", 100),
        ("max_new_tokens", 65536),
        ("ollama_timeout", 3600),
        ("openai_timeout", 3600),
    ]:
        if type(options[key]) is not int or not 1 <= options[key] <= maximum:
            raise ApplicationError("invalid_request", f"Invalid {key}")
    for key, minimum, maximum in [("temperature", 0, 2), ("top_p", 0, 1)]:
        if (
            type(options[key]) not in (int, float)
            or not minimum <= options[key] <= maximum
        ):
            raise ApplicationError("invalid_request", f"Invalid {key}")
    if options["approval"] not in {"ask", "auto", "never"}:
        raise ApplicationError("invalid_request", "Invalid approval policy")
    if not isinstance(options["secret_env_names"], list) or any(
        not isinstance(v, str) for v in options["secret_env_names"]
    ):
        raise ApplicationError("invalid_request", "Invalid secret environment names")
    if options["allowed_tools"] is not None and (
        not isinstance(options["allowed_tools"], list)
        or not options["allowed_tools"]
        or any(v not in legal_tool_names() for v in options["allowed_tools"])
    ):
        raise ApplicationError("invalid_request", "Invalid allowed tools")
    args = SimpleNamespace(**options)
    options["provider"] = _effective_provider(args)
    options["model"] = _effective_model(args, options["provider"])
    options["secret_env_names"] = _configured_secret_names(args)
    model = _build_model_client(SimpleNamespace(**options))
    if hasattr(model, "base_url"):
        options["base_url"] = model.base_url
    return options, model.protocol


class ApplicationService:
    def __init__(self, root, *, test_script=None):
        from .run_manager import RunManager

        load_project_env(root)
        self.secret_env_names = set(
            _configured_secret_names(SimpleNamespace(secret_env_names=[]))
        )
        self.store = StateStore(root)
        self.store.secret_env_names = self.secret_env_names
        self.manager = RunManager(self.store)
        self.test_script = test_script
        self.stopping = asyncio.Event()
        self.review_refreshing = False

    @staticmethod
    def run(agent, prompt):
        from .run_state import RunState

        return agent.ask(prompt, run_id=RunState.create(user_request=prompt).run_id)

    async def open(self):
        self.secret_env_names.update(self.store.configured_secret_names())
        await self.manager.recover()
        return self

    async def close(self):
        await self.manager.close()
        self.store.close()

    async def dispatch(self, method, params=None):
        from .features.memory import default_memory_state
        from .security import redact_artifact
        from .state_store import TERMINAL, ApplicationError, identity
        from .workspace import now

        specifications = {
            "host.info": ({}, {}),
            "host.stop": ({}, {}),
            "host.stop_if_idle": ({}, {}),
            "configuration.get": ({}, {"session_id": str}),
            "sessions.create": ({}, {"config": dict}),
            "sessions.list": ({}, {"limit": int, "cursor": str}),
            "sessions.get": ({"session_id": str}, {"after_message": int, "limit": int}),
            "sessions.reset": ({"session_id": str}, {}),
            "runs.start": (
                {"session_id": str, "prompt": str, "client_request_id": str},
                {"config": dict},
            ),
            "runs.list": ({}, {"limit": int, "cursor": str, "session_id": str}),
            "runs.get": ({"run_id": str}, {}),
            "runs.cancel": ({"run_id": str}, {}),
            "runs.export": ({"run_id": str}, {}),
            "runs.review": ({"run_id": str}, {}),
            "runs.review_refresh": ({"run_id": str}, {}),
            "runs.tool_calls": ({"run_id": str}, {"cursor": str, "limit": int}),
            "runs.checks": ({"run_id": str}, {"cursor": str, "limit": int}),
            "runs.changes": ({"run_id": str}, {"scope": str, "after_file": int, "limit": int}),
            "runs.diff": ({"run_id": str, "file_id": str}, {"after_character": int, "max_characters": int}),
            "runs.events": (
                {"run_id": str},
                {"after_sequence": int, "limit": int, "wait_ms": int},
            ),
            "approvals.list": ({}, {}),
            "approvals.resolve": ({"approval_id": str, "approved": bool}, {}),
        }
        if method not in specifications:
            raise ApplicationError("invalid_request", "Unknown method")
        params = {} if params is None else params
        required, optional = specifications[method]
        if (
            not isinstance(params, dict)
            or set(params) - set(required) - set(optional)
            or set(required) - set(params)
        ):
            raise ApplicationError("invalid_request", "Invalid method parameters")
        for key, value in params.items():
            expected = {**required, **optional}[key]
            if type(value) is not expected or (expected is str and key != "cursor" and not value.strip()):
                raise ApplicationError("invalid_request", f"Invalid {key}")
        if len(params.get("cursor", "")) > 1024:
            raise ApplicationError("invalid_request", "Invalid cursor")
        for name, low, high in [
            ("limit", 0 if method == "sessions.get" else 1, 500),
            ("after_message", 0, 2**63 - 1),
            ("after_file", 0, 2**63 - 1),
            ("after_character", 0, 2**63 - 1),
            ("max_characters", 1, 16384),
            ("after_sequence", 0, 2**63 - 1),
            ("wait_ms", 0, 20000),
        ]:
            if name in params and not low <= params[name] <= high:
                raise ApplicationError("invalid_request", f"Invalid {name}")

        def conversation(session_id):
            session = self.store.load_session(session_id)
            if "config" not in session:
                raise ApplicationError(
                    "unsupported_session",
                    "Internal delegate sessions are read-only; inspect their run instead",
                )
            return session

        def public_value(value):
            if "config" in value:
                config = dict(value["config"])
                for key in ("base_url", "host"):
                    if config.get(key):
                        config[key] = display_endpoint(config[key])
                value = {**value, "config": config}
            report = value.get("report")
            if isinstance(report, dict) and isinstance(report.get("run_state"), dict):
                value = {**value, "report": {**report, "run_state": public_value(report["run_state"])}}
            return redact_artifact(value, secret_env_names=self.secret_env_names)

        def resolved_options(overrides=None):
            try:
                config, protocol = freeze_options(self.store.root, overrides)
                for key in ("base_url", "host"):
                    if config.get(key):
                        display_endpoint(config[key])
                return config, protocol
            except ApplicationError:
                raise
            except (ValueError, OSError):
                raise ApplicationError("configuration_error", "模型配置无效。请检查项目 .env 的提供方、模型、地址及文件格式，再刷新连接。") from None

        def public_session(session, after=0, limit=0):
            # Raw replay blocks and old checkpoint snapshots stay private; history is paged for display.
            value = {
                key: session[key]
                for key in [
                    "id",
                    "format",
                    "created_at",
                    "provider_protocol",
                    "workspace_root",
                    "config",
                    "memory",
                    "history_revision",
                ]
                if key in session
            }
            history, size = [], 0
            for index in range(after, min(after + limit, len(session["history"]))):
                item = redact_artifact(session["history"][index], secret_env_names=self.secret_env_names)
                content = str(item.get("content", ""))
                view = {k: item[k] for k in ("role", "name", "call_id", "created_at") if k in item}
                view.update(message_id=f"{session['id']}:{session.get('history_revision', '')}:{index}", message_index=index,
                            content=content[:16384], content_truncated=len(content) > 16384)
                if item.get("tool_calls"):
                    view["tool_calls"] = [{"id": c["id"], "name": c["name"]} for c in item["tool_calls"][:100]]
                encoded_size = len(json.dumps(view, ensure_ascii=False).encode())
                if encoded_size > 192 * 1024:
                    view = {k: view[k] for k in ("role", "content", "content_truncated", "message_id", "message_index")}
                    view.update(details_truncated=True, tool_call_count=len(item.get("tool_calls", [])))
                    encoded_size = len(json.dumps(view, ensure_ascii=False).encode())
                if history and size + encoded_size > 256 * 1024:
                    break
                history.append(view)
                size += encoded_size
            value.update(
                history=history,
                next_message=min(after + len(history), len(session["history"])),
                history_count=len(session["history"]),
            )
            return public_value(value)

        if method == "host.info":
            self.manager.pump()
            return {
                "protocol": "pico-host-v1",
                "backend_version": "0.1.0",
                "api_version": 4,
                "capabilities": ["sessions", "runs", "approvals", "events", "run_reviews",
                                 "verification_evidence", "query_cursors", "configuration_summary", "desktop_control", "idle_upgrade"],
                "instance_id": self.manager.instance_id,
                "workspace": str(self.store.root),
                "status": "stopping"
                if not self.manager.accepting
                else "blocked"
                if self.manager.blocked
                else "ready",
            }
        if method in {"host.stop", "host.stop_if_idle"}:
            if method == "host.stop_if_idle":
                from .run_manager import execution_idle
                if self.review_refreshing or not execution_idle(self.store.root) or self.store.connection.execute(
                    "SELECT 1 FROM runs WHERE status NOT IN ('completed','stopped','failed','cancelled','interrupted') LIMIT 1").fetchone():
                    raise ApplicationError("workspace_busy", "Active or queued tasks must finish before upgrading")
            # No await between the idle check and rejecting all subsequent starts.
            self.manager.accepting = False
            self.stopping.set()
            return {"status": "stopping"}
        if method == "configuration.get":
            overrides = conversation(params["session_id"])["config"] if "session_id" in params else None
            config, _ = resolved_options(overrides)
            model = _build_model_client(SimpleNamespace(**config))
            endpoint = getattr(model, "base_url", getattr(model, "host", ""))
            # Credentials and query tokens in custom URLs are never configuration display data.
            return self.store.redact({"provider": config["provider"], "model": config["model"],
                                      "endpoint": display_endpoint(endpoint),
                                      "api_key_configured": bool(getattr(model, "api_key", ""))})
        if method == "sessions.create":
            config, protocol = resolved_options(params.get("config"))
            if self.test_script is not None:
                protocol = "fake"
            self.secret_env_names.update(config["secret_env_names"])
            session = {
                "id": identity("session"),
                "format": "native-v1",
                "provider_protocol": protocol,
                "workspace_root": str(self.store.root),
                "created_at": now(),
                "history": [],
                "memory": default_memory_state(),
                "config": config,
            }
            self.store.save_session(session)
            return public_session(session)
        if method == "sessions.list":
            if "cursor" in params:
                return self.store.list_page("sessions", params["cursor"], params.get("limit", 100))
            return [
                {
                    "id": s["id"],
                    "created_at": s["created_at"],
                    "provider_protocol": s["provider_protocol"],
                }
                for s in self.store.list_sessions(params.get("limit", 100))
                if "config" in s
            ]
        if method == "sessions.get":
            return public_session(
                conversation(params["session_id"]),
                params.get("after_message", 0),
                params.get("limit", 0),
            )
        if method == "sessions.reset":
            conversation(params["session_id"])
            return public_session(self.store.reset_session(params["session_id"]))
        if method == "runs.start":
            if self.review_refreshing:
                raise ApplicationError("workspace_busy", "A bounded review observation is in progress; retry shortly")
            self.manager.pump()
            if not self.manager.accepting or self.manager.blocked:
                raise ApplicationError(
                    "workspace_blocked", "Workspace is not accepting new execution"
                )
            session = conversation(params["session_id"])
            from .worker import MAX_MESSAGE

            # ponytail: whole-session IPC is capped at 16 MiB; page kernel history if that ceiling becomes practical.
            if (
                len(json.dumps(session, ensure_ascii=False).encode())
                > MAX_MESSAGE - 65536
            ):
                raise ApplicationError(
                    "session_too_large",
                    "Start a new session; original history remains intact",
                )
            config, protocol = resolved_options({**session.get("config", {}), **params.get("config", {})})
            if self.test_script is not None:
                protocol = "fake"
            if protocol != session["provider_protocol"]:
                raise ApplicationError(
                    "unsupported_protocol",
                    "Create a new session for a different provider protocol",
                )
            self.secret_env_names.update(config["secret_env_names"])
            run = self.store.create_run(
                session["id"], params["prompt"], config, params["client_request_id"]
            )
            if self.test_script is not None and run["status"] == "queued":
                self.manager.scripts[run["run_id"]] = [
                    asdict(item) for item in self.test_script
                ]
            self.manager.pump()
            return public_value(run)
        if method == "runs.list":
            if "cursor" in params or "session_id" in params:
                return self.store.list_page("runs", params.get("cursor", ""), params.get("limit", 100), params.get("session_id", ""))
            return [public_value(run) for run in self.store.list_runs(params.get("limit", 100))]
        if method == "runs.get":
            return public_value(
                {
                    **self.store.get_run(params["run_id"]),
                    "report": self.store.get_report(params["run_id"]),
                },
            )
        if method == "runs.cancel":
            return public_value(self.manager.cancel(params["run_id"]))
        if method == "runs.export":
            return {"path": str(self.store.export_run(params["run_id"]))}
        if method == "runs.review":
            return self.store.review_get(params["run_id"])
        if method in {"runs.tool_calls", "runs.checks"}:
            return self.store.tool_page(params["run_id"], cursor=params.get("cursor", ""),
                                        limit=params.get("limit", 100), checks=method == "runs.checks")
        if method == "runs.review_refresh":
            from .checks import summary, version_status
            from .review import observe_scope
            from .run_manager import execution_idle
            value = params["run_id"]
            frozen = self.store._end_scope(value)
            def busy():
                return self.review_refreshing or not execution_idle(self.store.root) or bool(self.store.connection.execute(
                    "SELECT 1 FROM runs WHERE status NOT IN ('completed','stopped','failed','cancelled','interrupted') LIMIT 1").fetchone())
            if busy():
                return {"state": "busy", "reason": "workspace_execution_or_observation_active"}
            self.review_refreshing = True
            try:
                current = await asyncio.to_thread(observe_scope, self.store.root)
                return self.store.redact({"state": "observed", "current_scope": current,
                    "version_status": version_status(frozen, current), "history_unchanged": True,
                    "checks": summary(self.store.checks(value, current))})
            finally:
                self.review_refreshing = False
        if method == "runs.changes":
            scope = params.get("scope", "run")
            if scope not in {"initial", "run"}:
                raise ApplicationError("invalid_request", "Invalid change scope")
            rows = self.store.review_changes(params["run_id"], scope)
            after = params.get("after_file", 0)
            page, size = [], 0
            for row in rows[after : after + min(params.get("limit", 100), 100)]:
                size += len(json.dumps(row, ensure_ascii=False).encode())
                if size > 128 * 1024:
                    break
                page.append(row)
            return {"files": page, "next_file": min(after + len(page), len(rows)), "total": len(rows)}
        if method == "runs.diff":
            from .review import render_diff
            data = self.store.review_diff_input(params["run_id"], params["file_id"])
            diff = self.store.redact(await asyncio.to_thread(render_diff, data))
            after = params.get("after_character", 0)
            # A page holds at most 16 Ki characters, including escaped JSON/control characters.
            end = min(after + params.get("max_characters", 16384), len(diff["text"]))
            return {**diff, "text": diff["text"][after:end], "next_character": end,
                    "has_more": end < len(diff["text"])}
        if method == "approvals.list":
            values = self.store.list_approvals()
            for approval in values:
                args = json.dumps(approval["args"], ensure_ascii=False)
                approval["scope"] = {"cwd": str(self.store.root), "path": str(approval["args"].get("path", "."))[:2048]}
                approval["args_truncated"] = len(args) > 16384
                if approval["args_truncated"]:
                    approval["args"] = {"preview": args[:16384]}
            return values
        if method == "approvals.resolve":
            return self.store.resolve_approval(
                params["approval_id"], params["approved"]
            )
        if method == "runs.events":
            after = params.get("after_sequence", 0)
            deadline = (
                asyncio.get_running_loop().time() + params.get("wait_ms", 0) / 1000
            )
            while True:
                rows = self.store.events(
                    params["run_id"], after, params.get("limit", 100)
                )
                run = self.store.get_run(params["run_id"])
                if (
                    rows
                    or run["status"] in TERMINAL
                    or asyncio.get_running_loop().time() >= deadline
                ):
                    return {
                        "events": rows,
                        "next_sequence": rows[-1]["sequence"] if rows else after,
                    }
                # No transaction/lock is held across a long poll. The next DB read cannot lose a notification.
                await asyncio.sleep(0.05)
