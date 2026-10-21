"""Bounded, read-only observations of a run's working tree, never a Git reset."""

import base64
import difflib
import hashlib
import json
import os
import selectors
import sqlite3
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath

from .workspace import IGNORED_PATH_NAMES, now

MAX_FILES = 10000
MAX_FILE_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RUN_BYTES = 32 * 1024 * 1024
# ponytail: evidence stops at 256 MiB per workspace; add explicit retention before raising it.
MAX_WORKSPACE_BYTES = 256 * 1024 * 1024
CAPTURE_SECONDS = 5
SKIP = IGNORED_PATH_NAMES | {"node_modules", "dist", "build", "htmlcov", ".next", ".tox"}


def file_id(path):
    return hashlib.sha256(path.encode()).hexdigest()[:24]


def sensitive(path):
    name = PurePosixPath(path).name.lower()
    return (bool(set(PurePosixPath(path).parts) & {".ssh", ".aws", ".gnupg"})
            or name == ".env" or name.startswith(".env.") or name in {
        "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc", "credentials.json",
        "credentials", "secrets.toml", "secrets.yaml", "secrets.yml",
    } or name.endswith((".pem", ".key", ".p12", ".pfx")))


def directory_fd(root_fd, parts):
    """Resolve every directory relative to an open root without following symlinks."""
    handle = os.dup(root_fd)
    try:
        for part in parts:
            next_handle = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            os.close(handle)
            handle = next_handle
        return handle
    except BaseException:
        os.close(handle)
        raise


def git_read(root, args, deadline, *, config=(), allow_missing=False):
    """Bound stdout at enumeration time, with no optional index writes or hooks."""
    process = subprocess.Popen(
        ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *config, *args],
        cwd=root, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    complete = True
    drained = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    complete = False
                    break
                for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(chunk) > MAX_MANIFEST_BYTES:
                        complete = False
                        break
                    output.extend(chunk)
                if not complete:
                    break
            drained = not selector.get_map()
    finally:
        if complete and drained:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                complete = False
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdout.close()
    return bytes(output), complete and process.returncode in ((0, 1) if allow_missing else (0,))


def git_status(root, deadline, *, nul=True):
    # Status may run clean/process filters while comparing dirty files. Disable them for observation.
    raw, complete = git_read(root, ["config", "--null", "--name-only", "--get-regexp",
                                    r"^filter\..*\.(clean|smudge|process|required)$"], deadline, allow_missing=True)
    if not complete:
        return b"", False
    config = []
    try:
        for key in sorted(set(raw.split(b"\x00")) - {b""}):
            name = key.decode("utf-8")
            config.extend(["-c", name + ("=false" if name.endswith(".required") else "=")])
    except UnicodeError:
        return b"", False
    args = ["status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "--ignore-submodules=all"]
    if nul:
        args.extend(["-z", "--", "."])
    return git_read(root, args, deadline, config=config)


def plain_paths(root_fd, deadline, notes, limit):
    pending = [()]
    visited = 0
    while pending:
        parts = pending.pop()
        try:
            handle = directory_fd(root_fd, parts)
        except OSError:
            notes.add("unreadable_directory")
            continue
        try:
            with os.scandir(handle) as entries:
                for entry in entries:
                    visited += 1
                    if visited > limit or time.monotonic() >= deadline:
                        notes.add("enumeration_limit")
                        return
                    if entry.name in SKIP:
                        continue
                    relative = (*parts, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        if len(relative) >= 64:
                            notes.add("directory_depth_limit")
                        else:
                            pending.append(relative)
                    else:
                        yield "/".join(relative)
        except OSError:
            notes.add("unreadable_directory")
        finally:
            os.close(handle)


def read_entry(root_fd, path, put_blob):
    entry = {"id": file_id(path), "path": path, "blob": None, "reason": ""}
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in ("..", ".") for p in parts):
        return {**entry, "reason": "invalid_path"}
    parent = directory_fd(root_fd, parts[:-1])
    try:
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        entry.update(size=info.st_size, mode=stat.S_IMODE(info.st_mode))
        entry["fingerprint"] = f"metadata:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}"
        if stat.S_ISLNK(info.st_mode):
            entry["reason"] = "symlink"
            entry["fingerprint"] = "link:" + hashlib.sha256(os.fsencode(os.readlink(parts[-1], dir_fd=parent))).hexdigest()
        elif not stat.S_ISREG(info.st_mode):
            entry["reason"] = "not_regular_file"
        elif sensitive(path):
            entry["reason"] = "sensitive_file"
        elif info.st_size > MAX_FILE_BYTES:
            entry["reason"] = "file_size_limit"
        else:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    return {**entry, "reason": "unstable_file"}
                data = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            try:
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return {**entry, "reason": "unstable_file"}
            def signature(s):
                return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if signature(info) != signature(opened) or signature(opened) != signature(after) or signature(after) != signature(current):
                entry["reason"] = "unstable_file"
            elif len(data) > MAX_FILE_BYTES:
                entry["reason"] = "file_size_limit"
            else:
                entry["fingerprint"] = hashlib.sha256(data).hexdigest() + f":{entry['mode']}"
                try:
                    data.decode("utf-8")
                    text = b"\x00" not in data
                except UnicodeError:
                    text = False
                if not text:
                    entry["reason"] = "binary_or_non_utf8"
                else:
                    entry["blob"] = put_blob(data)
                    if entry["blob"] is None:
                        entry["reason"] = "content_budget_limit"
                    elif len(data.splitlines()) > 5000:
                        entry["reason"] = "diff_line_limit"
        return entry
    finally:
        os.close(parent)


def capture(root, put_blob, *, max_files=MAX_FILES, seconds=CAPTURE_SECONDS, cancel=None):
    root = Path(root).resolve()
    deadline = time.monotonic() + seconds
    notes = set()
    snapshot = {"captured_at": now(), "files": {}, "git": False, "initial_status": [], "complete": False}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Explicit fixture/workspace roots inside another repo still define their own scope.
        marker = root / ".git"
        if marker.exists() and not marker.is_symlink():
            try:
                raw, complete = git_read(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."], deadline)
                if not complete:
                    notes.add("git_enumeration_incomplete")
                snapshot["git"] = True
                paths = raw.split(b"\x00")[:-1]
                status, complete = git_status(root, deadline)
                snapshot["git_status_mode"] = "external_filters_disabled"
                if not complete:
                    notes.add("git_status_incomplete")
                for item in status.split(b"\x00")[:max_files]:
                    if len(item) > 3:
                        try:
                            snapshot["initial_status"].append({"path": item[3:].decode("utf-8"), "index": chr(item[0]), "worktree": chr(item[1])})
                        except UnicodeError:
                            notes.add("unsupported_filename")
                if len(status.split(b"\x00")) > max_files + 1:
                    notes.add("git_status_limit")
            except OSError:
                notes.add("git_unavailable")
                paths = []
        else:
            paths = plain_paths(root_fd, deadline, notes, max_files)
        manifest_bytes = 0
        for index, candidate in enumerate(paths):
            if index >= max_files or time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                notes.add("capture_limit_or_cancelled")
                break
            try:
                path = candidate.decode("utf-8") if isinstance(candidate, bytes) else candidate
                path.encode("utf-8")
                if len(path.encode()) > 2048:
                    notes.add("path_length_limit")
                    continue
                if any(part in SKIP for part in PurePosixPath(path).parts):
                    continue
                try:
                    entry = read_entry(root_fd, path, put_blob)
                except FileNotFoundError:
                    continue  # A tracked deletion remains in ls-files, but is absent from the tree.
                except OSError:
                    entry = {"id": file_id(path), "path": path, "blob": None, "reason": "unreadable_or_unstable"}
                manifest_bytes += len(json.dumps(entry, ensure_ascii=False).encode())
                if manifest_bytes > MAX_MANIFEST_BYTES:
                    notes.add("manifest_size_limit")
                    break
                snapshot["files"][path] = entry
            except UnicodeError:
                notes.add("unsupported_filename")
        snapshot.update(complete=not notes, notes=sorted(notes))
        return snapshot
    finally:
        os.close(root_fd)


def changes(baseline, end):
    if end is None:
        return []  # An absent end observation is not evidence that every baseline file was deleted.
    before = baseline.get("files", {}) if baseline else {}
    after = end.get("files", {}) if end else {}
    rows = []
    for path in sorted(before.keys() | after.keys()):
        left, right = before.get(path), after.get(path)
        if left and right and left.get("fingerprint") and left.get("fingerprint") == right.get("fingerprint"):
            continue
        kind = "modified" if left and right else "created" if right else "deleted"
        if (left is None and not (baseline or {}).get("complete")) or (right is None and not (end or {}).get("complete")):
            kind = "unknown"
        reasons = sorted({e["reason"] for e in (left, right) if e and e.get("reason")})
        rows.append({"id": file_id(path), "path": path, "kind": kind,
                     "reason": ",".join(reasons), "diff_available": kind != "unknown" and not reasons and all(e is None or e.get("blob") for e in (left, right))})
    return rows


def scope_fingerprint(snapshot):
    """Content identity of the bounded review scope, independent of stored diff blobs."""
    if snapshot is None:
        return None
    files = snapshot.get("files", {})
    omissions = sorted({e.get("reason", "") for e in files.values()
                        if not e.get("fingerprint") or e.get("reason") in {
                            "sensitive_file", "file_size_limit", "not_regular_file",
                            "unstable_file", "unreadable_or_unstable", "invalid_path"}})
    payload = [(path, e.get("fingerprint"), e.get("mode")) for path, e in sorted(files.items())]
    return {"version": 1, "fingerprint": hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest(),
            "captured_at": snapshot.get("captured_at"), "file_count": len(files),
            "complete": bool(snapshot.get("complete")) and not omissions,
            "notes": sorted(set(snapshot.get("notes", [])) | set(omissions)),
            "scope": "bounded_review_files", "git": bool(snapshot.get("git"))}


def observe_scope(root):
    """No blob writes; observation failure never changes tool execution permission."""
    try:
        return scope_fingerprint(capture(root, lambda _: "fingerprint_only", seconds=1))
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {"complete": False, "notes": ["capture_failed"], "captured_at": now()}


def render_diff(value):
    """Pure bounded input, safe to compute outside the Host's connection loop."""
    if not value["available"]:
        return value
    if any(len(value[key].splitlines()) > 5000 for key in ("before", "after")):
        return {"available": False, "reason": "diff_line_limit", "text": ""}
    path = value["path"]
    if any(ord(c) < 32 for c in path):
        path = json.dumps(path, ensure_ascii=False)
    lines = difflib.unified_diff(value["before"].splitlines(keepends=True), value["after"].splitlines(keepends=True),
                                fromfile="before/" + path, tofile="after/" + path)
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    return {"available": True, "reason": "", "text": text, "redacted": value.get("redacted", False)}


class RunReview:
    """One root run's observation lifecycle; no execution or approval authority."""

    def __init__(self, store, root, value):
        self.store, self.root, self.value = store, root, value

    def collect(self, stage, cancel=None, seconds=CAPTURE_SECONDS):
        try:
            snapshot = capture(self.root, lambda data: self.store.review_blob(
                self.value, base64.b64encode(data).decode("ascii")), cancel=cancel, seconds=seconds)
            self.store.review_save(self.value, stage, snapshot)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self.store.review_unavailable(self.value, "capture_failed")

    def start(self, cancel=None):
        try:
            self.store.review_begin(self.value)
            self.collect("baseline", cancel)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass  # Review failure cannot authorize tools or invent execution facts.

    def finish(self, cancelled=False):
        try:
            self.collect("end", seconds=1 if cancelled else CAPTURE_SECONDS)
        except KeyboardInterrupt:
            try:
                self.store.review_unavailable(self.value, "capture_cancelled")
            except (OSError, ValueError, TypeError, sqlite3.Error):
                pass
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass
