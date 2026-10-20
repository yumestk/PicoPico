from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.tool_executor import ToolExecutor
from pico.tool_result import ToolExecutionResult

import pytest


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_tool_executor_returns_content_and_metadata_without_side_channel(tmp_path):
    agent = build_agent(tmp_path)

    result = ToolExecutor(agent).execute("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert isinstance(result, ToolExecutionResult)
    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["read_only"] is True
    assert result.metadata["workspace_changed"] is False


def test_pico_returns_structured_result(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"


@pytest.mark.parametrize("args", [{"timeout": True, "command": "echo bad"},
                                 {"command": "echo bad", "extra": 1}])
def test_invalid_schema_never_executes_or_asks(tmp_path, args):
    agent = build_agent(tmp_path)
    agent.approve = lambda *_: pytest.fail("invalid arguments must not request approval")
    assert agent.execute_tool("run_shell", args).error_code == "invalid_arguments"


def test_shell_exit_code_is_not_parsed_from_output(tmp_path):
    agent = build_agent(tmp_path)
    result = agent.execute_tool("run_shell", {"command": "printf 'exit_code: 0'; exit 7"})
    assert result.data["exit_code"] == 7
    assert result.status == "error"
