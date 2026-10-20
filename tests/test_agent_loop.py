
import pytest

from pico import FakeModelClient, Pico, StateStore, WorkspaceContext
from pico.agent_loop import AgentLoop
from pico.providers.types import ModelResponse, ToolCall
from pico.run_state import RunState


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = StateStore(tmp_path)
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        store=store,
        approval_policy="auto",
        **kwargs,
    )


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            ModelResponse(tool_calls=[ToolCall('read_file', {'path': 'hello.txt', 'start': 1, 'end': 1})]),
            ModelResponse(text='Done.'),
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt", RunState.create(user_request="Inspect hello.txt"))

    assert answer == "Done."
    assert agent.current_run_state.status == "completed"
    assert bool(agent.store.get_report(agent.current_run_state.run_id))


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse(text='Facade works.')])

    assert agent.ask("Use facade") == "Facade works."


def test_agent_loop_reserves_a_final_answer_after_tool_budget_is_exhausted(tmp_path):
    (tmp_path / "facts.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    tool_outputs = [
        ModelResponse(tool_calls=[ToolCall('read_file', {'path': 'facts.txt', 'start': line, 'end': line})])
        for line in range(1, 7)
    ]
    agent = build_agent(
        tmp_path,
        [*tool_outputs, ModelResponse(text='All six facts were inspected.')],
        max_steps=6,
    )

    answer = agent.ask("Inspect all facts and summarize them")

    assert answer == "All six facts were inspected."
    assert agent.current_run_state.status == "completed"
    assert agent.current_run_state.tool_steps == 6
    assert agent.current_run_state.attempts == 7
    trace_events = agent.store.events(agent.current_run_state)
    assert any(
        event["event"] == "model_requested" and event.get("purpose") == "finalization"
        for event in trace_events
    )


def test_agent_loop_persists_model_failure_before_reraising(tmp_path):
    class FailingModelClient:
        supports_prompt_cache = False


        def complete(self, *args, **kwargs):
            raise RuntimeError(
                "Anthropic-compatible response ended before a text block "
                "(stop_reason=max_tokens, content_types=thinking)"
            )

    agent = build_agent(tmp_path, [])
    agent.model_client = FailingModelClient()

    with pytest.raises(RuntimeError, match="ended before a text block"):
        agent.ask("Inspect the tests")

    state = agent.current_run_state
    assert state.status == "failed"
    assert state.stop_reason == "model_error"
    assert state.attempts == 1
    assert bool(agent.store.get_run(state))
    assert bool(agent.store.get_report(state))
    report = agent.store.get_report(state.run_id)
    assert report["stop_reason"] == "model_error"
    assert "ended before a text block" in report["final_answer"]


def test_multiple_calls_are_sequential_and_text_does_not_finish(tmp_path):
    agent = build_agent(tmp_path, [
        ModelResponse(text='I will read twice', tool_calls=[
            ToolCall('read_file', {'path': 'README.md'}, 'first'),
            ToolCall('read_file', {'path': 'README.md', 'end': 1}, 'second')]),
        ModelResponse(text='Both done')])
    assert agent.ask('read') == 'Both done'
    results = [m for m in agent.session['history'] if m['role'] == 'tool']
    assert [m['call_id'] for m in results] == ['first', 'second']
    assert agent.current_run_state.tool_steps == 2
    assert agent.model_client.requests[1].messages[-3]['tool_calls'][0]['id'] == 'first'


def test_batch_budget_rejects_remaining_calls_and_disables_tools(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse(tool_calls=[
        ToolCall('write_file', {'path': 'one.txt', 'content': 'one'}),
        ToolCall('write_file', {'path': 'two.txt', 'content': 'two'})]), ModelResponse(text='Only one written')], max_steps=1)
    assert agent.ask('write files') == 'Only one written'
    assert (tmp_path / 'one.txt').exists()
    assert not (tmp_path / 'two.txt').exists()
    assert agent.current_run_state.tool_steps == 1
    assert agent.model_client.requests[-1].tools == []
    results = [m for m in agent.session['history'] if m['role'] == 'tool']
    assert results[-1]['result']['error_code'] == 'budget_exceeded'


def test_truncated_response_never_executes_its_calls(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse(text='partial', finish_reason='length', tool_calls=[
        ToolCall('write_file', {'path': 'bad.txt', 'content': 'bad'})])])
    assert agent.ask('write') == 'partial'
    assert agent.current_run_state.status == 'failed'
    assert not (tmp_path / 'bad.txt').exists()
    assert agent.last_completion_metadata['finish_reason'] == 'length'


def test_unknown_tool_returns_correlated_error(tmp_path):
    agent = build_agent(tmp_path, [ModelResponse(tool_calls=[ToolCall('unknown', {}, 'bad')]), ModelResponse(text='Handled')])
    assert agent.ask('inspect') == 'Handled'
    result = next(m for m in agent.session['history'] if m['role'] == 'tool')
    assert result['call_id'] == 'bad'
    assert result['result']['error_code'] == 'unknown_tool'


def test_old_session_and_changed_protocol_are_explicit_errors(tmp_path):
    agent = build_agent(tmp_path, [])
    session = dict(agent.session)
    session.pop('format')
    with pytest.raises(ValueError, match='Unsupported session format'):
        Pico(agent.model_client, agent.workspace, agent.store, session=session)
    session['format'] = 'native-v1'
    session['provider_protocol'] = 'another-protocol'
    with pytest.raises(ValueError, match='protocol changed'):
        Pico(agent.model_client, agent.workspace, agent.store, session=session)


def test_reused_call_id_in_later_round_is_a_protocol_error_not_a_second_effect(tmp_path):
    from pico.providers.types import ModelProtocolError
    agent=build_agent(tmp_path,[ModelResponse(tool_calls=[ToolCall('write_file',{'path':'first','content':'one'},'same')]),
                                 ModelResponse(tool_calls=[ToolCall('write_file',{'path':'second','content':'two'},'same')])])
    with pytest.raises(ModelProtocolError,match='duplicate'):
        agent.ask('write files')
    assert (tmp_path/'first').read_text()=='one'
    assert not (tmp_path/'second').exists()
    assert agent.current_run_state.stop_reason=='model_error'
    from pico.context_manager import message_groups
    assert message_groups(agent.store.load_session(agent.session['id'])['history'])
