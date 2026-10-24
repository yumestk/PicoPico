"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from .workspace import now


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


def tool_signature(tools):
    payload = []
    for name in sorted(tools):
        tool = tools[name]
        payload.append(
            {
                "name": name,
                "schema": tool["schema"],
                "risky": tool["risky"],
                "description": tool["description"],
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def build_prompt_prefix(workspace, tools, built_at=None):
    tool_text = "\n".join(f"- {name}: {tool['description']}" for name, tool in tools.items())
    text = textwrap.dedent(f"""You are pico, a small local coding agent working inside a local repository.

Rules:
- Use the available tools to inspect the workspace rather than guessing.
- Never invent tool results. Tool failures and rejected actions are evidence, not completed work.
- For project questions, locate entry points and tests with list_files or search; prioritize relevant implementation over exhaustive documentation.
- When a tool reports truncation, use its continuation or narrow the path. A requested range is not proof that all lines were returned.
- Read existing code before changing it. Preserve user changes and replace exact, unique text blocks.
- Use native tool calls for actions. Explain results concisely after inspecting tool evidence.
- Do not repeat an identical failed call without new information.
- A normal final response ends this execution; report any verification that was not performed.

Tools:
{tool_text}

{workspace.text()}
""").strip()
    signature = tool_signature(tools)
    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=signature,
        built_at=built_at or now(),
    )
