"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import hashlib
import os
import time
from functools import partial
from pathlib import Path

from .command_runner import run_command
from .workspace import IGNORED_PATH_NAMES


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def string_schema(default=None, nonempty=False):
    schema = {"type": "string"}
    if default is not None:
        schema["default"] = default
    if nonempty:
        schema["minLength"] = 1
    return schema


def integer_schema(default, minimum=1, maximum=None):
    schema = {"type": "integer", "default": default, "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


BASE_TOOL_SPECS = {
    "list_files": {"schema": object_schema({"path": string_schema("."), "offset": integer_schema(0, minimum=0, maximum=10000), "depth": integer_schema(8, maximum=16), "revision": string_schema()}), "risky": False,
                   "description": "Map project paths recursively (default depth=8); use once to locate source and tests. Results are paged; depth=1 lists immediate children."},
    "read_file": {"schema": object_schema({"path": string_schema(nonempty=True), "start": integer_schema(1),
                  "end": integer_schema(200), "column": integer_schema(0, minimum=0)}, ["path"]), "risky": False,
                  "description": "Read a UTF-8 file by line range."},
    "search": {"schema": object_schema({"pattern": string_schema(nonempty=True), "path": string_schema(".")},
               ["pattern"]), "risky": False, "description": "Search literal text in bounded UTF-8 files (smart case); narrow path or pattern if truncated."},
    "run_shell": {"schema": object_schema({"command": string_schema(nonempty=True),
                  "purpose": {"type": "string", "enum": ["general", "verification"], "default": "general"},
                  "timeout": integer_schema(20, maximum=120)}, ["command"]), "risky": True,
                  "description": "Run a shell command in the repo root. Declare purpose=verification for checks; success only proves this command exited zero."},
    "write_file": {"schema": object_schema({"path": string_schema(nonempty=True), "content": string_schema()},
                   ["path", "content"]), "risky": True, "description": "Write a text file."},
    "patch_file": {"schema": object_schema({"path": string_schema(nonempty=True),
                   "old_text": string_schema(nonempty=True), "new_text": string_schema()},
                   ["path", "old_text", "new_text"]), "risky": True,
                   "description": "Replace one exact text block in a file."},
}
DELEGATE_TOOL_SPEC = {"schema": object_schema({"task": string_schema(nonempty=True), "max_steps": integer_schema(3)},
                      ["task"]), "risky": False, "description": "Ask a bounded read-only child agent to investigate."}


def validate_arguments(schema, args):
    if not isinstance(args, dict):
        raise TypeError("arguments must be an object")
    for name in schema["required"]:
        if name not in args:
            raise ValueError(f"missing required argument: {name}")
    for name, value in args.items():
        spec = schema["properties"].get(name)
        if spec is None:
            raise ValueError(f"unknown argument: {name}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{name} must be one of {spec['enum']}")
        if spec["type"] == "string":
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if spec.get("minLength") and not value.strip():
                raise ValueError(f"{name} must not be empty")
        elif spec["type"] == "integer":
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer")
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise ValueError(f"{name} is outside the allowed range")


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate"}



def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, context)}
    return tools


def validate_tool(context, name, args):
    spec = BASE_TOOL_SPECS.get(name, DELEGATE_TOOL_SPEC if name == "delegate" else None)
    if spec is None:
        raise ValueError(f"unknown tool: {name}")
    validate_arguments(spec["schema"], args)

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    from .review import SKIP
    entries, directories = [], [(path, 1)]
    depth, deadline, scanned = args.get('depth', 8), time.monotonic() + 5, 0
    while directories:
        directory, level = directories.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > 10000 or time.monotonic() > deadline:
                    raise ValueError('Directory map exceeds scan limit; choose a narrower subdirectory or depth')
                if entry.name in SKIP:
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
                kind = '[L]' if entry.is_symlink() else '[D]' if is_directory else '[F]'
                entries.append(f'{kind} {(directory / entry.name).relative_to(context.root)}')
                if is_directory and level < depth:
                    directories.append((directory / entry.name, level + 1))
    entries.sort(key=str.casefold)
    revision = hashlib.sha256((str(depth) + '\0' + '\0'.join(entries)).encode()).hexdigest()
    if args.get('revision') and args['revision'] != revision:
        raise ValueError('Directory changed; restart listing at offset=0')
    offset = args.get('offset', 0)
    lines, used = [], 0
    budget = min(2800, 3800 - len(f'\nReturned 200 of 10000 entries. Continue list_files(path={args.get("path", ".")!r}, depth={depth}, offset=10000, revision={revision!r}).'))
    for line in entries[offset:offset + 200]:
        if used + len(line) > budget:
            if not lines:
                raise ValueError('Entry path exceeds display limit; choose a shorter subdirectory path')
            break
        lines.append(line)
        used += len(line) + 1
    next_offset = offset + len(lines)
    truncated = next_offset < len(entries)
    footer = f'\nReturned {len(lines)} of {len(entries)} entries (depth <= {depth}).'
    if truncated:
        footer += f' Continue list_files(path={args.get("path", ".")!r}, depth={depth}, offset={next_offset}, revision={revision!r}).'
    return {"text": ("\n".join(lines) or "(empty)") + footer, "path": str(path.relative_to(context.root)),
            "truncated": truncated, "next_offset": next_offset if truncated else None, "revision": revision}


def tool_read_file(context, args):
    from .file_view import read_page

    context.path(args["path"])  # Validate workspace confinement without resolving away a symlink for the reader.
    raw = Path(args["path"])
    relative = raw.relative_to(context.root) if raw.is_absolute() else raw
    return read_page(context.root, str(relative), args.get("start", 1),
                     args.get("end", 200), args.get("column", 0), getattr(context, 'secret_env_names', ()))


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))
    if any(part in IGNORED_PATH_NAMES for part in path.relative_to(context.root).parts):
        raise ValueError('Search path is excluded')

    # One bounded implementation also works without rg. Never follow links or read private files.
    from .file_view import protected_text
    from .review import SKIP, read_entry
    case_sensitive = any(c.isupper() for c in pattern)
    needle = pattern if case_sensitive else pattern.casefold()
    matches, used, scanned, total_bytes = [], 0, 0, 0
    truncated, reason = False, ''
    deadline = time.monotonic() + 5
    def candidates():
        nonlocal truncated, reason
        if path.is_file():
            yield path
            return
        directories, entries = [path], 0
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > 10000 or time.monotonic() > deadline:
                        truncated, reason = True, 'directory scan limit'
                        return
                    if entry.name in SKIP or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(directory / entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        yield directory / entry.name
    descriptor = os.open(context.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for file_path in candidates():
            scanned += 1
            if scanned > 500 or total_bytes >= 8 * 1024 * 1024 or time.monotonic() > deadline:
                truncated, reason = True, 'scan limit'
                break
            blobs = []
            read_entry(descriptor, str(file_path.relative_to(context.root)), lambda data, blobs=blobs: blobs.append(data) or 'search')
            if not blobs:
                continue
            total_bytes += len(blobs[0])
            for number, line in enumerate(protected_text(blobs[0].decode('utf-8'), getattr(context, 'secret_env_names', ())).splitlines(), 1):
                if needle in (line if case_sensitive else line.casefold()):
                    item = f'{file_path.relative_to(context.root)}:{number}:{line}'
                    if len(matches) >= 200 or used + len(item) > 2800:
                        truncated, reason = True, 'result limit'
                        break
                    matches.append(item)
                    used += len(item) + 1
            if truncated:
                break
    finally:
        os.close(descriptor)
    footer = f'\nSearch scanned {min(scanned, 500)} files (bounded UTF-8 text only, <=512 KiB per file).'
    if truncated:
        footer += f' TRUNCATED ({reason}); narrow path or pattern, then search again. This is not an exhaustive result.'
    return {"text": ("\n".join(matches) or "(no matches)") + footer, "truncated": truncated, "scanned_files": min(scanned, 500)}


def tool_run_shell(context, args):
    command = str(args.get("command", ""))
    if not command.strip():
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    return run_command(command, context.root, context.shell_env(), timeout=timeout,
                       cancel_event=context.cancel_event, process_started=context.process_started,
                       process_stopped=context.process_stopped, execution_lock_fd=context.execution_lock_fd)


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"text": f"wrote {path.relative_to(context.root)} ({len(content)} chars)", "path": str(path.relative_to(context.root)), "chars": len(content)}


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return {"text": f"patched {path.relative_to(context.root)}", "path": str(path.relative_to(context.root))}


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return {"text": context.spawn_delegate(args)}


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
}
