"""Workspace state and auditable transitions in one SQLite transaction boundary."""

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import PersistenceError
from .security import redact_artifact
from .workspace import now

TERMINAL = frozenset({"completed", "stopped", "failed", "cancelled", "interrupted"})
SCHEMA_VERSION = 2


class ApplicationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identity(prefix):
    return prefix + "_" + uuid.uuid4().hex


def run_id(value):
    return value.run_id if hasattr(value, "run_id") else str(value)


class StateStore:
    managed = False

    def __init__(self, workspace):
        self.root = Path(workspace).resolve()
        self.secret_env_names = set()
        directory = self.root / ".pico"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / "state.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            self.connection.close()
            raise ApplicationError(
                "unsupported_schema", f"Unsupported state schema: {version}"
            )
        if version == 0:
            if self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchone():
                self.connection.close()
                raise ApplicationError(
                    "unsupported_schema", "Unversioned database was left intact"
                )
            self.connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE metadata (workspace TEXT NOT NULL);
                CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    parent_run_id TEXT, request_id TEXT, request_hash TEXT NOT NULL,
                    config TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL,
                    report TEXT, instance_id TEXT, worker_pid INTEGER,
                    created_at TEXT NOT NULL, finished_at TEXT,
                    UNIQUE(session_id, request_id));
                CREATE TABLE tool_calls (
                    run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL,
                    name TEXT NOT NULL, args TEXT NOT NULL, phase TEXT NOT NULL,
                    result TEXT, PRIMARY KEY(run_id, call_id));
                CREATE TABLE approvals (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL, decision INTEGER,
                    UNIQUE(run_id,call_id), FOREIGN KEY(run_id,call_id) REFERENCES tool_calls(run_id,call_id));
                CREATE TABLE events (
                    run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
                CREATE TABLE run_reviews (run_id TEXT PRIMARY KEY REFERENCES runs(id), body TEXT NOT NULL);
                CREATE TABLE review_blobs (run_id TEXT NOT NULL REFERENCES runs(id), hash TEXT NOT NULL,
                    body BLOB NOT NULL, PRIMARY KEY(run_id,hash));
                PRAGMA user_version=2;
            """)
            self.connection.execute(
                "INSERT INTO metadata VALUES (?)", (str(self.root),)
            )
            self.connection.execute("COMMIT")
        workspace_row = self.connection.execute(
            "SELECT workspace FROM metadata"
        ).fetchone()
        if workspace_row is None or workspace_row[0] != str(self.root):
            self.connection.close()
            raise ApplicationError(
                "workspace_mismatch", "State database belongs to another workspace"
            )
        if version == 1:
            try:
                self._migrate_review_schema()
            except BaseException:
                self.connection.close()
                raise

    def _migrate_review_schema(self):
        directory = self.path.parent / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        backup = directory / f"schema1-{identity('backup')}.sqlite3"
        destination = sqlite3.connect(backup)
        try:
            os.chmod(backup, 0o600)
            self.connection.backup(destination)
        finally:
            destination.close()
        with self.transaction():
            self.connection.execute("CREATE TABLE run_reviews (run_id TEXT PRIMARY KEY REFERENCES runs(id), body TEXT NOT NULL)")
            self.connection.execute("CREATE TABLE review_blobs (run_id TEXT NOT NULL REFERENCES runs(id), hash TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(run_id,hash))")
            self.connection.execute("PRAGMA user_version=2")

    def configured_secret_names(self):
        names = set()
        for row in self.connection.execute("SELECT config FROM runs"):
            names.update(json.loads(row[0]).get("secret_env_names", []))
        return names

    def redact(self, value):
        return redact_artifact(value, secret_env_names=self.secret_env_names)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise PersistenceError(
                    "State transaction could not be committed"
                ) from exc
            raise

    def _session(self, session):
        self.connection.execute(
            "INSERT INTO sessions VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (session["id"], encode(session), now()),
        )

    def save_session(self, session):
        with self.transaction():
            self._session(session)
        return session["id"]

    def load_session(self, session_id):
        row = self.connection.execute(
            "SELECT body FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ApplicationError("not_found", "Session not found")
        return json.loads(row[0])

    def list_sessions(self, limit=100):
        return [
            json.loads(r[0])
            for r in self.connection.execute(
                "SELECT body FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        ]

    def latest_session(self):
        row = self.connection.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def reset_session(self, session_id):
        from .features.memory import default_memory_state

        with self.transaction():
            if self.connection.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND status NOT IN ('completed','stopped','failed','cancelled','interrupted')",
                (session_id,),
            ).fetchone():
                raise ApplicationError("session_busy", "Session has unfinished runs")
            session = self.load_session(session_id)
            session.update(
                history=[],
                memory=default_memory_state(),
                checkpoints={},
                runtime_identity={},
                resume_state={},
            )
            self._session(session)
        return session

    def create_run(
        self,
        session_id,
        prompt,
        config=None,
        request_id=None,
        id=None,
        parent_run_id="",
        status="queued",
    ):
        config = config or {}
        request_hash = digest({"prompt": prompt, "config": config})
        with self.transaction():
            self.load_session(session_id)
            if request_id:
                row = self.connection.execute(
                    "SELECT id,request_hash FROM runs WHERE session_id=? AND request_id=?",
                    (session_id, request_id),
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise ApplicationError(
                            "request_conflict",
                            "Submission ID was already used with different content",
                        )
                    return self.get_run(row["id"])
            value = {
                "run_id": id or identity("run"),
                "parent_run_id": parent_run_id,
                "session_id": session_id,
                "user_request": prompt,
                "status": status,
                "tool_steps": 0,
                "attempts": 0,
                "last_tool": "",
                "stop_reason": "",
                "final_answer": "",
                "checkpoint_id": "",
                "resume_status": "",
            }
            self.connection.execute(
                "INSERT INTO runs(id,session_id,parent_run_id,request_id,request_hash,config,body,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    value["run_id"],
                    session_id,
                    parent_run_id,
                    request_id,
                    request_hash,
                    encode(config),
                    encode(value),
                    status,
                    now(),
                ),
            )
            self._event(
                value["run_id"],
                "run_queued" if status == "queued" else "run_created",
                {},
            )
        return value

    def get_run(self, value):
        row = self.connection.execute(
            "SELECT * FROM runs WHERE id=?", (run_id(value),)
        ).fetchone()
        if row is None:
            raise ApplicationError("not_found", "Run not found")
        return {
            **json.loads(row["body"]),
            "last_sequence": self.connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?",
                (run_id(value),),
            ).fetchone()[0],
            "status": row["status"],
            "session_id": row["session_id"],
            "config": json.loads(row["config"]),
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "worker_pid": row["worker_pid"],
            "instance_id": row["instance_id"],
        }

    def list_runs(self, limit=100):
        return [
            self.get_run(r[0])
            for r in self.connection.execute(
                "SELECT id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        ]

    def list_page(self, kind, cursor="", limit=100, session_id=""):
        """Insertion order with a fixed high-water mark; updates cannot move rows between pages."""
        table = "runs" if kind == "runs" else "sessions"
        maximum = self.connection.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0]
        high, after = self._cursor(cursor, [kind, session_id], maximum, maximum + 1)
        where = "rowid<=? AND rowid<?"
        parameters = [high, after]
        if kind == "runs" and session_id:
            where += " AND session_id=?"
            parameters.append(session_id)
        if kind == "sessions":
            where += " AND json_type(body,'$.config')='object'"
        columns = "rowid,id AS run_id,session_id,status,parent_run_id,created_at,finished_at" if kind == "runs" else (
            "rowid,id,json_extract(body,'$.created_at') AS created_at,"
            "json_extract(body,'$.provider_protocol') AS provider_protocol")
        rows = self.connection.execute(f"SELECT {columns} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ?",
                                       (*parameters, min(limit, 100) + 1)).fetchall()
        page = [dict(r) for r in rows[:min(limit, 100)]]
        next_cursor = self._encode_cursor([kind, session_id], high, page[-1]["rowid"]) if len(rows) > len(page) else None
        return {"items": self.redact([{k: v for k, v in row.items() if k != "rowid"} for row in page]),
                "next_cursor": next_cursor, "order": "created_desc"}

    @staticmethod
    def _encode_cursor(context, high, after):
        import base64
        return base64.urlsafe_b64encode(encode([context, high, after]).encode()).decode()

    def _cursor(self, cursor, context, high, after):
        import base64
        if not cursor:
            return high, after
        try:
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (len(cursor) > 1024 or not isinstance(value, list) or len(value) != 3
                    or value[0] != context or any(type(v) is not int or not 0 <= v < 2**63 for v in value[1:])):
                raise ValueError()
            return value[1], value[2]
        except (ValueError, TypeError, UnicodeError):
            raise ApplicationError("invalid_request", "Invalid or mismatched cursor") from None

    def shell_begin(self, value, call_id, evidence):
        # Persist before side effects; a dead worker leaves evidence, never a fabricated exit code.
        with self.transaction():
            self.connection.execute("UPDATE tool_calls SET result=? WHERE run_id=? AND call_id=? AND name='run_shell' AND phase='executing'",
                                    (encode({"data": evidence}), run_id(value), call_id))

    def tool_page(self, value, *, cursor="", limit=100, checks=False):
        self.get_run(value)
        maximum = self.connection.execute("SELECT COALESCE(MAX(rowid),0) FROM tool_calls WHERE run_id=?", (run_id(value),)).fetchone()[0]
        context = ["checks" if checks else "tool_calls", run_id(value)]
        high, after = self._cursor(cursor, context, maximum, 0)
        where = "run_id=? AND rowid<=? AND rowid>?"
        if checks:
            where += " AND name='run_shell' AND json_extract(args,'$.purpose')='verification'"
        rows = self.connection.execute(f"SELECT rowid,* FROM tool_calls WHERE {where} ORDER BY rowid LIMIT ?",
                                       (run_id(value), high, after, min(limit, 100) + 1)).fetchall()
        target = self._end_scope(value) if checks else None
        page, size, last = [], 0, after
        for row in rows[:min(limit, 100)]:
            item = self._call_view(row)
            if checks:
                from .checks import check_view
                item = check_view(item, target)
            item = self.redact(item)
            # Each card is bounded independently; aggregate pages stay below the 1 MiB public transport.
            encoded = encode(item)
            if len(encoded.encode()) > 192 * 1024:
                item = self.redact({"run_id": run_id(value), "call_id": row["call_id"], "name": row["name"],
                        "phase": row["phase"], "details_truncated": True, "preview": encoded[:8192]})
                encoded = encode(item)
            if page and size + len(encoded.encode()) > 256 * 1024:
                break
            size += len(encoded.encode())
            page.append(item)
            last = row["rowid"]
        return {"items": page, "next_cursor": self._encode_cursor(context, high, last) if len(rows) > len(page) else None}

    def _call_view(self, row):
        # Redact before any display slicing so a secret crossing a page boundary cannot leak a prefix.
        return self.redact({"run_id": row["run_id"], "call_id": row["call_id"], "name": row["name"],
                "args": json.loads(row["args"]), "phase": row["phase"],
                "result": json.loads(row["result"]) if row["result"] else None})

    def _end_scope(self, value):
        from .review import scope_fingerprint
        return scope_fingerprint(self._review(value).get("end"))

    def checks(self, value, target=None):
        from .checks import check_view
        self.get_run(value)
        target = self._end_scope(value) if target is None else target
        return [check_view(self._call_view(row), target) for row in self.connection.execute(
            "SELECT * FROM tool_calls WHERE run_id=? AND name='run_shell' AND json_extract(args,'$.purpose')='verification' ORDER BY rowid",
            (run_id(value),))]

    def set_worker(self, value, instance_id, pid):
        with self.transaction():
            self.connection.execute(
                "UPDATE runs SET worker_pid=?,instance_id=? WHERE id=?",
                (pid, instance_id, run_id(value)),
            )

    def _update(self, state):
        value = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        current = self.get_run(value["run_id"])
        if current["status"] in TERMINAL:
            return current
        if current["status"] == "cancelling":
            value["status"] = "cancelling"
        body = {**current, **value}
        self.connection.execute(
            "UPDATE runs SET body=?,status=? WHERE id=?",
            (encode(body), body["status"], body["run_id"]),
        )
        return body

    def update_run(self, state):
        with self.transaction():
            return self._update(state)

    def begin_run(self, session, state, config=None):
        value = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        try:
            self.get_run(value["run_id"])
        except ApplicationError:
            self.create_run(
                session["id"],
                value["user_request"],
                config,
                id=value["run_id"],
                parent_run_id=value.get("parent_run_id", ""),
                status="running",
            )
        with self.transaction():
            self._session(session)
            return self._update({**value, "status": "running"})

    def _event(self, value, name, payload):
        current = self.get_run(value)
        sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?",
            (run_id(value),),
        ).fetchone()[0]
        event = {
            **self.redact(payload),
            "event": name,
            "event_id": f"{run_id(value)}:{sequence}",
            "sequence": sequence,
            "run_id": run_id(value),
            "session_id": current["session_id"],
            "created_at": now(),
        }
        self.connection.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (run_id(value), sequence, encode(event)),
        )
        return event

    def emit(self, value, name, payload, state=None):
        with self.transaction():
            if state is not None:
                self._update(state)
            return self._event(value, name, payload)

    def events(self, value, after=0, limit=1000):
        return [
            json.loads(r[0])
            for r in self.connection.execute(
                "SELECT body FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id(value), after, limit),
            )
        ]

    def record(self, session, item, value=None, state=None, event=None):
        with self.transaction():
            self._session(session)
            if value:
                if state is not None:
                    self._update(state)
                if item["role"] == "assistant":
                    for call in item.get("tool_calls", []):
                        self.connection.execute(
                            "INSERT INTO tool_calls VALUES (?,?,?,?,?,NULL)",
                            (
                                run_id(value),
                                call["id"],
                                call["name"],
                                encode(call["arguments"]),
                                "proposed",
                            ),
                        )
                elif item["role"] == "tool":
                    self.connection.execute(
                        "UPDATE tool_calls SET phase='finished', result=? WHERE run_id=? AND call_id=?",
                        (
                            encode(item.get("result", {})),
                            run_id(value),
                            item["call_id"],
                        ),
                    )
                if event:
                    return self._event(value, event["event"], event["payload"])
        return None

    def get_report(self, value):
        row = self.connection.execute(
            "SELECT report FROM runs WHERE id=?", (run_id(value),)
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _pair_unfinished(self, session, value):
        pending = {
            row["call_id"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=? AND phase!='finished'",
                (run_id(value),),
            )
        }
        if not pending:
            return []
        history = session["history"]
        start = max(
            (i for i, m in enumerate(history) if m["role"] == "user"), default=0
        )
        rebuilt = history[:start]
        index = start
        unknown = []
        while index < len(history):
            message = history[index]
            rebuilt.append(message)
            index += 1
            if message["role"] != "assistant" or not message.get("tool_calls"):
                continue
            existing = {}
            while index < len(history) and history[index]["role"] == "tool":
                existing[history[index]["call_id"]] = history[index]
                index += 1
            for call in message["tool_calls"]:
                if call["id"] in existing:
                    rebuilt.append(existing[call["id"]])
                    continue
                row = pending.get(call["id"])
                if row is None:
                    continue
                phase = (
                    "outcome_unknown"
                    if row["phase"] in {"executing", "outcome_unknown"}
                    else "not_executed"
                )
                result = {
                    "status": "error",
                    "error_code": phase,
                    "data": json.loads(row["result"]).get("data", {}) if row["result"] else {},
                    "message": "Execution interrupted. Inspect the workspace before retrying.",
                    "metadata": {"outcome_unknown": phase == "outcome_unknown"},
                }
                rebuilt.append(
                    {
                        "role": "tool",
                        "call_id": call["id"],
                        "name": row["name"],
                        "args": json.loads(row["args"]),
                        "status": "error",
                        "content": encode(
                            {
                                "status": phase,
                                "output": result["message"],
                                "error_code": phase,
                            }
                        ),
                        "result": result,
                        "created_at": now(),
                    }
                )
                self.connection.execute(
                    "UPDATE tool_calls SET phase=?,result=? WHERE run_id=? AND call_id=?",
                    (phase, encode(result), run_id(value), call["id"]),
                )
                if phase == "outcome_unknown":
                    unknown.append(call["id"])
        session["history"][:] = rebuilt
        return unknown

    def finish(self, session, state, report):
        value = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        working_session = {**session, "history": list(session["history"])}
        with self.transaction():
            current = self.get_run(value["run_id"])
            if current["status"] in TERMINAL:
                return current
            committed_history = self.load_session(session["id"])["history"]
            final_message = session["history"][-1] if session["history"] else None
            working_session["history"] = list(committed_history)
            if (
                isinstance(final_message, dict)
                and final_message.get("role") == "assistant"
                and not final_message.get("tool_calls")
                and (not committed_history or committed_history[-1] != final_message)
            ):
                working_session["history"].append(final_message)
            if current["status"] == "cancelling" and value["status"] != "interrupted":
                value.update(
                    status="cancelled",
                    stop_reason="cancelled",
                    final_answer="Cancelled. Inspect interrupted operations before retrying.",
                )
            final = {**current, **value}
            final.pop("last_sequence", None)
            unknown = self._pair_unfinished(working_session, final["run_id"])
            report = {
                "unknown_calls": unknown,
                **report,
                "status": final["status"],
                "stop_reason": final["stop_reason"],
                "final_answer": final["final_answer"],
                "run_state": final,
            }
            self._session(working_session)
            self.connection.execute(
                "UPDATE runs SET body=?,status=?,report=?,finished_at=? WHERE id=?",
                (
                    encode(final),
                    final["status"],
                    encode(report),
                    now(),
                    final["run_id"],
                ),
            )
            self.connection.execute(
                "UPDATE approvals SET status='invalidated' WHERE run_id=? AND status IN ('pending','approved')",
                (final["run_id"],),
            )
            self.connection.execute(
                "UPDATE tool_calls SET phase='outcome_unknown' WHERE run_id=? AND phase='executing'",
                (final["run_id"],),
            )
            if final.get("checkpoint_id"):
                self._event(
                    final["run_id"],
                    "checkpoint_created",
                    {
                        "checkpoint_id": final["checkpoint_id"],
                        "trigger": final["stop_reason"],
                    },
                )
            self._event(
                final["run_id"],
                "run_finished",
                {
                    "status": final["status"],
                    "stop_reason": final["stop_reason"],
                    "final_answer": self.redact(final["final_answer"]),
                    "run_duration_ms": report.get("run_duration_ms"),
                },
            )
        session["history"][:] = working_session["history"]
        return self.get_run(final["run_id"])

    def interrupt(self, value, reason="worker_exited"):
        current = self.get_run(value)
        if current["status"] in TERMINAL:
            return current
        session = self.load_session(current["session_id"])
        current.update(
            status="interrupted",
            stop_reason=reason,
            final_answer="Execution interrupted; inspect unknown effects before submitting a new run.",
        )
        return self.finish(session, current, {"run_id": run_id(value)})

    def cancel(self, value):
        with self.transaction():
            current = self.get_run(value)
            if current["status"] in TERMINAL or current["status"] == "cancelling":
                return current
            queued = current["status"] == "queued"
            current.update(status="cancelling", stop_reason="cancel_requested")
            self._update(current)
            self.connection.execute(
                "UPDATE approvals SET status='invalidated' WHERE run_id=? AND status IN ('pending','approved')",
                (run_id(value),),
            )
            self._event(value, "cancel_requested", {})
        if queued:
            current.update(
                status="cancelled",
                stop_reason="cancelled",
                final_answer="Cancelled before execution.",
            )
            return self.finish(self.load_session(current["session_id"]), current, {})
        return self.get_run(value)

    def request_approval(self, value, call_id, name, args, baseline):
        with self.transaction():
            current = self.get_run(value)
            if current["status"] in TERMINAL or current["status"] == "cancelling":
                raise ApplicationError(
                    "run_cancelled", "Run no longer accepts approvals"
                )
            approval = {
                "id": identity("approval"),
                "run_id": run_id(value),
                "call_id": call_id,
                "name": name,
                "args": self.redact(args),
                "args_hash": digest(args),
                "baseline": baseline,
                "created_at": now(),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            }
            self.connection.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,NULL)",
                (approval["id"], run_id(value), call_id, encode(approval), "pending"),
            )
            self._update({**current, "status": "waiting_approval"})
            self._event(
                value, "approval_requested", {"approval_id": approval["id"], **approval}
            )
        return approval

    def get_approval(self, id):
        row = self.connection.execute(
            "SELECT * FROM approvals WHERE id=?", (id,)
        ).fetchone()
        if row is None:
            raise ApplicationError("not_found", "Approval not found")
        body = {
            **json.loads(row["body"]),
            "status": row["status"],
            "decision": row["decision"],
        }
        if body["status"] in {"pending", "approved"} and body["expires_at"] <= now():
            with self.transaction():
                self.connection.execute(
                    "UPDATE approvals SET status='expired' WHERE id=?", (id,)
                )
                current = self.get_run(body["run_id"])
                if current["status"] == "waiting_approval":
                    self._update({**current, "status": "running"})
                self._event(
                    body["run_id"],
                    "approval_expired",
                    {"approval_id": id, "call_id": body["call_id"]},
                )
            body["status"] = "expired"
        return body

    def list_approvals(self):
        values = [
            self.get_approval(r[0])
            for r in self.connection.execute(
                "SELECT id FROM approvals WHERE status='pending'"
            ).fetchall()
        ]
        return [v for v in values if v["status"] == "pending"]

    def resolve_approval(self, id, approved):
        approval = self.get_approval(id)
        with self.transaction():
            current = self.get_run(approval["run_id"])
            if approval["decision"] is not None:
                if bool(approval["decision"]) != approved:
                    raise ApplicationError(
                        "approval_conflict", "Approval already has a different decision"
                    )
                return approval
            if (
                approval["status"] != "pending"
                or current["status"] != "waiting_approval"
            ):
                raise ApplicationError(
                    "approval_expired", "Approval is no longer valid"
                )
            self.connection.execute(
                "UPDATE approvals SET status=?,decision=? WHERE id=?",
                ("approved" if approved else "denied", int(approved), id),
            )
            self._update({**current, "status": "running"})
            self._event(
                approval["run_id"],
                "approval_resolved",
                {
                    "approval_id": id,
                    "call_id": approval["call_id"],
                    "name": approval["name"],
                    "approved": approved,
                },
            )
        return self.get_approval(id)

    def permit(self, value, call_id, baseline="", approval_id=None):
        approval = self.get_approval(approval_id) if approval_id else None
        with self.transaction():
            current = self.get_run(value)
            call = self.connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=? AND call_id=?",
                (run_id(value), call_id),
            ).fetchone()
            if (
                current["status"] != "running"
                or call is None
                or call["phase"] != "proposed"
            ):
                raise ApplicationError(
                    "execution_rejected", "Call is not eligible for execution"
                )
            from .tools import BASE_TOOL_SPECS

            risky = BASE_TOOL_SPECS.get(call["name"], {}).get("risky", False)
            policy = current["config"].get("approval", "ask")
            if risky and (
                current["config"].get("read_only")
                or policy == "never"
                or (policy == "ask" and approval is None)
            ):
                raise ApplicationError(
                    "execution_rejected", "Policy does not authorize this write"
                )
            if approval:
                if (
                    approval["status"] != "approved"
                    or approval["run_id"] != run_id(value)
                    or approval["call_id"] != call_id
                    or approval["args_hash"] != digest(json.loads(call["args"]))
                ):
                    raise ApplicationError(
                        "approval_expired", "Approval does not authorize this call"
                    )
                if approval["baseline"] != baseline:
                    self.connection.execute(
                        "UPDATE approvals SET status='invalidated' WHERE id=?",
                        (approval_id,),
                    )
                    return False
                self.connection.execute(
                    "UPDATE approvals SET status='consumed' WHERE id=?", (approval_id,)
                )
            self.connection.execute(
                "UPDATE tool_calls SET phase='executing' WHERE run_id=? AND call_id=?",
                (run_id(value), call_id),
            )
            self._event(
                value, "tool_authorized", {"call_id": call_id, "name": call["name"]}
            )
        return True

    def export_run(self, value):
        self.get_run(value)
        root = self.root / ".pico" / "exports" / run_id(value)
        root.mkdir(parents=True, exist_ok=True)
        (root / "run_state.json").write_text(
            encode(self.redact(self.get_run(value))) + "\n"
        )
        (root / "trace.jsonl").write_text(
            "".join(encode(e) + "\n" for e in self.events(value, limit=1000000))
        )
        report = self.get_report(value)
        if report is not None:
            (root / "report.json").write_text(encode(self.redact(report)) + "\n")
        calls = [
            {
                "call_id": r["call_id"],
                "name": r["name"],
                "args": json.loads(r["args"]),
                "phase": r["phase"],
                "result": json.loads(r["result"]) if r["result"] else None,
            }
            for r in self.connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=? ORDER BY rowid",
                (run_id(value),),
            )
        ]
        (root / "tool_results.json").write_text(encode(self.redact(calls)) + "\n")
        (root / "checks.json").write_text(encode(self.redact(self.checks(value))) + "\n")
        review = self.review_get(value)
        (root / "review.json").write_text(encode(review) + "\n")
        rows = self.review_changes(value)
        (root / "changes.json").write_text(encode(self.redact(rows)) + "\n")
        diffs = {}
        for row in rows:
            if row["diff_available"]:
                diffs[row["path"]] = self.review_diff(value, row["id"])
        (root / "diffs.json").write_text(encode(self.redact(diffs)) + "\n")
        return root

    def _review(self, value):
        current = self.get_run(value)
        row = self.connection.execute("SELECT body FROM run_reviews WHERE run_id=?", (run_id(value),)).fetchone()
        if row:
            return json.loads(row[0])
        return {"version": 1, "state": "pending" if current["status"] not in TERMINAL else "unavailable",
                "reason": "not_captured", "baseline": None, "end": None,
                "parent_run_id": current.get("parent_run_id", "")}

    def review_get(self, value):
        review = self._review(value)
        result = {k: review.get(k) for k in ("version", "state", "reason", "parent_run_id")}
        for stage in ("baseline", "end"):
            snapshot = review.get(stage)
            result[stage] = None if snapshot is None else {
                k: snapshot.get(k) for k in ("captured_at", "complete", "notes", "git", "git_status_mode")}
            if snapshot:
                result[stage]["file_count"] = len(snapshot["files"])
                omissions = {}
                for entry in snapshot["files"].values():
                    if entry.get("reason"):
                        omissions[entry["reason"]] = omissions.get(entry["reason"], 0) + 1
                result[stage]["content_omissions"] = omissions
        result["change_count"] = len(self.review_changes(value))
        result["initial_change_count"] = len((review.get("baseline") or {}).get("initial_status", []))
        from .checks import summary
        result["checks"] = summary(self.checks(value))
        return self.redact(result)

    def review_begin(self, value):
        run = self.get_run(value)
        if run["status"] not in {"running", "cancelling"} or run.get("parent_run_id"):
            raise ApplicationError("invalid_request", "Only an executing root run can capture a review")
        with self.transaction():
            self.connection.execute("INSERT OR IGNORE INTO run_reviews VALUES (?,?)", (run_id(value), encode({
                "version": 1, "state": "collecting", "reason": "", "baseline": None, "end": None})))

    def review_blob(self, value, content):
        from .review import MAX_FILE_BYTES, MAX_RUN_BYTES, MAX_WORKSPACE_BYTES

        if self._review(value)["state"] != "collecting":
            return None
        if not isinstance(content, str) or len(content) > (MAX_FILE_BYTES + 2) // 3 * 4:
            raise ApplicationError("invalid_request", "Review content exceeds size limit")
        data = base64.b64decode(content, validate=True)
        if len(data) > MAX_FILE_BYTES:
            raise ApplicationError("invalid_request", "Review content exceeds size limit")
        key = hashlib.sha256(data).hexdigest()
        with self.transaction():
            if self.connection.execute("SELECT 1 FROM review_blobs WHERE run_id=? AND hash=?", (run_id(value), key)).fetchone():
                return key
            run_bytes = self.connection.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM review_blobs WHERE run_id=?", (run_id(value),)).fetchone()[0]
            workspace_bytes = self.connection.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM review_blobs").fetchone()[0]
            if run_bytes + len(data) > MAX_RUN_BYTES or workspace_bytes + len(data) > MAX_WORKSPACE_BYTES:
                return None
            self.connection.execute("INSERT INTO review_blobs VALUES (?,?,?)", (run_id(value), key, data))
        return key

    def review_save(self, value, stage, snapshot):
        from .review import MAX_FILES, MAX_MANIFEST_BYTES

        if stage not in {"baseline", "end"} or not isinstance(snapshot, dict):
            raise ApplicationError("invalid_request", "Invalid review snapshot")
        if len(encode(snapshot).encode()) > MAX_MANIFEST_BYTES * 2 or len(snapshot.get("files", {})) > MAX_FILES:
            raise ApplicationError("invalid_request", "Review manifest exceeds size limit")
        review = self._review(value)
        if review["state"] != "collecting" or review.get(stage) is not None:
            return self.review_get(value)
        review[stage] = snapshot
        if stage == "end":
            rows = self._review_changes(value, review)
            complete = (review.get("baseline") or {}).get("complete") and snapshot["complete"]
            review["state"] = "ready" if complete and all(r["diff_available"] for r in rows) else "partial"
            if not review.get("baseline"):
                review["reason"] = "baseline_missing"
        with self.transaction():
            self.connection.execute("UPDATE run_reviews SET body=? WHERE run_id=?", (encode(review), run_id(value)))
            self._event(value, "review_updated", {"state": review["state"], "stage": stage})
        return {"state": review["state"]}

    def review_unavailable(self, value, reason="execution_interrupted"):
        review = self._review(value)
        if review["state"] != "collecting":
            return
        review.update(state="partial" if review.get("baseline") else "unavailable", reason=reason)
        with self.transaction():
            self.connection.execute("UPDATE run_reviews SET body=? WHERE run_id=?", (encode(review), run_id(value)))
            self._event(value, "review_updated", {"state": review["state"], "reason": reason})

    def review_changes(self, value, scope="run"):
        review = self._review(value)
        if scope == "initial":
            return self.redact((review.get("baseline") or {}).get("initial_status", []))
        return self.redact(self._review_changes(value, review))

    def _review_changes(self, value, review):
        from .review import changes, file_id

        rows = changes(review.get("baseline"), review.get("end"))
        known = {r["path"] for r in rows}
        captured = set((review.get("baseline") or {}).get("files", {})) | set((review.get("end") or {}).get("files", {}))
        # Recorded tool evidence can reveal excluded paths, but cannot reconstruct their old content.
        for row in self.connection.execute("SELECT result FROM tool_calls WHERE run_id=? AND result IS NOT NULL", (run_id(value),)):
            result = json.loads(row[0])
            for path in result.get("metadata", {}).get("affected_paths", []):
                if path not in known and (path not in captured or review.get("end") is None):
                    rows.append({"id": file_id(path), "path": path, "kind": "unknown",
                                 "diff_available": False, "reason": "outside_captured_scope" if review.get("end") else "end_snapshot_missing"})
                    known.add(path)
        return sorted(rows, key=lambda r: r["path"])

    def review_diff_input(self, value, entry_id):
        review = self._review(value)
        row = next((r for r in self._review_changes(value, review) if r["id"] == entry_id), None)
        if row is None:
            raise ApplicationError("not_found", "No captured file entry in this run")
        if not row["diff_available"]:
            return {"available": False, "reason": row["reason"] or "incomplete_snapshot", "text": ""}
        texts = []
        for stage in ("baseline", "end"):
            entry = (review.get(stage) or {}).get("files", {}).get(row["path"])
            if entry is None:
                texts.append("")
                continue
            blob = self.connection.execute("SELECT body FROM review_blobs WHERE run_id=? AND hash=?", (run_id(value), entry["blob"])).fetchone()
            if blob is None:
                return {"available": False, "reason": "content_missing", "text": ""}
            texts.append(bytes(blob[0]).decode("utf-8"))
        redacted = self.redact(texts)
        return {"available": True, "path": self.redact(row["path"]), "before": redacted[0],
                "after": redacted[1], "redacted": redacted != texts}

    def review_diff(self, value, entry_id):
        from .review import render_diff

        return self.redact(render_diff(self.review_diff_input(value, entry_id)))
