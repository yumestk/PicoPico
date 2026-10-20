import copy
import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_manager import ContextBudgetError, ContextManager, message_groups
from pico.providers.types import ModelProtocolError, ModelResponse, ToolCall


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n")
    return Pico(model_client=FakeModelClient(outputs), workspace=WorkspaceContext.build(tmp_path),
                session_store=SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto", **kwargs)


def tool_round(size=100):
    call = ToolCall("read_file", {"path": "README.md"}, "call_test")
    return [ModelResponse(tool_calls=[call], provider_blocks=[{"opaque": "SIGNED"}]).message(),
            {"role": "tool", "call_id": call.id, "name": call.name, "content": "X" * size}]


def test_context_builds_native_messages_and_keeps_user_request(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "old request"})
    agent.record({"role": "assistant", "content": "old answer"})
    req, meta = ContextManager(agent).build("current request")
    assert req.system.index("You are pico") < req.system.index("Memory:") < req.system.index("Relevant memory:")
    assert req.messages[-1] == {"role": "user", "content": "current request"}
    assert req.tools[0]["parameters"]["type"] == "object"
    assert meta["budget_unit"] == "characters"
    assert meta["tool_schema_chars"] > 0
    assert not any(m["content"] == "current request" for m in agent.session["history"])


def test_context_drops_complete_old_groups_without_modifying_history(tmp_path):
    agent = build_agent(tmp_path, [])
    for i in range(5):
        for message in tool_round(300):
            message = copy.deepcopy(message)
            if message.get("tool_calls"):
                message["tool_calls"][0]["id"] = f"old_{i}"
            if message["role"] == "tool":
                message["call_id"] = f"old_{i}"
            agent.record(message)
    agent.record({"role": "user", "content": "current"})
    for message in tool_round(100):
        agent.record(message)
    before = copy.deepcopy(agent.session["history"])
    req, meta = ContextManager(agent, section_budgets={"history": 400}).build("current")
    assert req.messages[-2:] == tool_round(100)
    assert not any(m.get("call_id", "").startswith("old_") for m in req.messages)
    assert meta["history"]["dropped_groups"] > 0
    assert agent.session["history"] == before
    assert message_groups(req.messages)


def test_required_round_too_large_fails_instead_of_truncating(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "current"})
    for message in tool_round(10000):
        agent.record(message)
    before = copy.deepcopy(agent.session["history"])
    with pytest.raises(ContextBudgetError):
        ContextManager(agent, total_budget=4000).build("current")
    assert agent.session["history"] == before


def test_schema_and_current_request_are_in_the_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    with pytest.raises(ContextBudgetError):
        ContextManager(agent, total_budget=50).build("keep this exact request")


@pytest.mark.parametrize("history", [[tool_round()[1]], [tool_round()[0]],
                                    [tool_round()[0], {**tool_round()[1], "call_id": "wrong"}]])
def test_orphan_and_unresolved_calls_rejected(history):
    with pytest.raises(ModelProtocolError):
        message_groups(history)


def test_relevant_memory_selection_and_bounded_rendering(tmp_path):
    agent = build_agent(tmp_path, [])
    for word in ("alpha", "beta", "gamma", "delta"):
        agent.memory.append_note(word + " recall " + "N" * 160, tags=("recall",))
    req, meta = ContextManager(agent, section_budgets={"relevant_memory": 300}).build("recall")
    relevant = meta["relevant_memory"]
    assert relevant["selected_count"] == 3
    assert relevant["rendered_count"] == 3
    assert relevant["rendered_chars"] <= 300
    assert all(note in req.system for note in relevant["rendered_notes"])


def test_overflow_reduces_optional_context_before_required_messages(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.render_memory_text = lambda: "Memory: " + "M" * 3000
    agent.record({"role": "user", "content": "old " + "H" * 3000})
    req, meta = ContextManager(agent, total_budget=4000).build("preserve current")
    assert req.char_count() <= 4000
    assert req.messages[-1]["content"] == "preserve current"
    assert meta["budget_reductions"]


def test_finalization_removes_tool_schemas(tmp_path):
    agent = build_agent(tmp_path, [])
    req, _ = ContextManager(agent).build("summarize", allow_tools=False)
    assert req.tools == []


def test_context_manager_relevant_memory_can_mix_durable_notes(tmp_path):
    memory_root = tmp_path / ".pico" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Durable Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n",
        encoding="utf-8",
    )

    agent = build_agent(tmp_path, [])

    prompt, metadata = ContextManager(agent).build("What conventions should I follow?")
    relevant_section = prompt.system.split("Relevant memory:\n", 1)[1]

    assert "Use constrained tools instead of guessing." in relevant_section
    assert any("Use constrained tools instead of guessing." in item for item in metadata["relevant_memory"]["selected_notes"])
    assert metadata["relevant_memory"]["selected_durable_count"] == 1
    assert metadata["relevant_memory"]["selected_sources"] == ["project-conventions"]
    assert metadata["relevant_memory"]["selected_kinds"] == ["durable"]
