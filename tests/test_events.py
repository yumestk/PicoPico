import json
import os
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from pico import FakeModelClient, Pico, StateStore, WorkspaceContext, cli
from pico.context_manager import message_groups
from pico.events import PersistenceError
from pico.providers.types import ModelResponse, ToolCall


def agent_at(path, outputs, **kwargs):
    (path / 'README.md').write_text('hello')
    return Pico(FakeModelClient(outputs), WorkspaceContext.build(path), StateStore(path),
                approval_policy=kwargs.pop('approval_policy', 'auto'), **kwargs)


def events(agent):
    return [json.loads(line) for line in [json.dumps(event) for event in agent.store.events(agent.current_run_state)]]


def assert_terminal(agent, status):
    state = agent.current_run_state
    assert state.status == status
    assert agent.store.get_run(state.run_id)['status'] == status
    assert agent.store.get_report(state.run_id)['status'] == status
    assert agent.store.load_session(agent.session['id'])['history'] == agent.session['history']
    terminal = [e for e in events(agent) if e['event'] == 'run_finished']
    assert len(terminal) == 1 and terminal[0]['status'] == status


def test_observers_cannot_mutate_execution_or_fail_the_producer(tmp_path):
    seen = []
    def observe(event):
        seen.append(event['event'])
        event['name'] = 'mutated'
        event['args'] = {'path': 'mutated'}
        raise ValueError('broken view')
    agent = agent_at(tmp_path, [ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'ok.txt', 'content': 'ok'}, 'write')]),
                               ModelResponse(text='Done')], event_handler=observe)
    assert agent.ask('write') == 'Done'
    trace = events(agent)
    assert [e['sequence'] for e in trace] == list(range(1, len(trace) + 1))
    assert len({e['event_id'] for e in trace}) == len(trace)
    assert (tmp_path / 'ok.txt').read_text() == 'ok'
    assert next(e for e in trace if e['event'] == 'tool_started')['name'] == 'write_file'
    assert 'tool_started' in seen
    assert_terminal(agent, 'completed')


@pytest.mark.parametrize('point', ['model', 'approval', 'tool'])
def test_keyboard_interrupt_closes_pending_calls_and_stops_later_tools(tmp_path, point):
    calls = [ToolCall('write_file', {'path': 'first', 'content': 'first'}),
             ToolCall('write_file', {'path': 'later', 'content': 'later'})]
    agent = agent_at(tmp_path, [ModelResponse(tool_calls=calls)], approval_policy='ask')
    def interrupt(*_):
        raise KeyboardInterrupt
    if point == 'model':
        agent.model_client.complete = interrupt
    elif point == 'approval':
        agent.approval_handler = interrupt
    else:
        agent.approval_policy = 'auto'
        agent.tools['write_file']['run'] = interrupt
    agent.ask('write')
    assert_terminal(agent, 'cancelled')
    assert not (tmp_path / 'later').exists()
    assert message_groups(agent.session['history'])
    assert len(agent.model_client.requests) <= 1
    # A new user input can proceed in the same session after cancellation.
    agent.model_client = FakeModelClient([ModelResponse(text='Continued')])
    assert agent.ask('continue') == 'Continued'


def test_refused_approval_is_a_tool_result_not_run_cancellation(tmp_path):
    agent = agent_at(tmp_path, [ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'no', 'content': 'no'}, 'deny')]),
                               ModelResponse(text='No file written')], approval_policy='ask', approval_handler=lambda *_: False)
    assert agent.ask('write') == 'No file written'
    assert not (tmp_path / 'no').exists()
    assert_terminal(agent, 'completed')
    decision = next(e for e in events(agent) if e['event'] == 'approval_resolved')
    assert decision['call_id'] == 'deny' and not decision['approved']
    replayed = agent.model_client.requests[-1].messages
    assert next(m for m in replayed if m['role'] == 'tool')['status'] == 'rejected'


def test_cancel_signal_after_model_response_prevents_calls(tmp_path):
    agent = agent_at(tmp_path, [])
    def complete(_):
        agent.cancel_event.set()
        return ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'no', 'content': 'no'})])
    agent.model_client.complete = complete
    agent.ask('write')
    assert_terminal(agent, 'cancelled')
    assert not (tmp_path / 'no').exists()


def test_cancel_signal_during_approval_prevents_an_approved_write(tmp_path):
    agent = agent_at(tmp_path, [ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'no', 'content': 'no'})])],
                     approval_policy='ask')
    def approve(*_):
        agent.cancel_event.set()
        return True
    agent.approval_handler = approve
    agent.ask('write')
    assert_terminal(agent, 'cancelled')
    assert not (tmp_path / 'no').exists()


def test_trace_failure_prevents_tool_effects(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, [ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'no', 'content': 'no'})])])
    append = agent.store.emit
    def failing_append(value, name, payload, state=None):
        if name == 'tool_started':
            raise OSError('disk failed')
        return append(value, name, payload, state)
    monkeypatch.setattr(agent.store, 'emit', failing_append)
    with pytest.raises(PersistenceError):
        agent.ask('write')
    assert not (tmp_path / 'no').exists()
    assert_terminal(agent, 'failed')
    assert agent.current_run_state.stop_reason == 'persistence_error'


@pytest.mark.parametrize('point', ['report', 'memory'])
def test_completion_persistence_failure_cannot_leave_a_completed_terminal_event(tmp_path, monkeypatch, point):
    agent = agent_at(tmp_path, [ModelResponse(text='Done')])
    write = agent.store.finish
    count = 0
    def fail_once(session, state, report):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError('report unavailable')
        return write(session, state, report)
    if point == 'report':
        monkeypatch.setattr(agent.store, 'finish', fail_once)
    else:
        def fail_memory(*_):
            raise OSError('memory unavailable')
        monkeypatch.setattr(agent, 'promote_durable_memory', fail_memory)
    with pytest.raises(PersistenceError):
        agent.ask('answer')
    state = agent.current_run_state
    assert state.status == 'failed'
    assert agent.store.get_report(state.run_id)['status'] == 'failed'
    assert not any(e['event'] == 'run_finished' and e['status'] == 'completed' for e in events(agent))


def test_cli_progress_and_approval_do_not_pollute_stdout(tmp_path, capsys):
    class Client:
        def call(self,method,params=None):
            if method=='runs.events':return {'events':[{'event':'tool_started','name':'write_file','sequence':1}]}
            if method=='approvals.list':return [{'run_id':'r','id':'a','name':'write_file','args':{'path':'ok'}}]
            if method=='approvals.resolve':return {'status':'approved'}
            if method=='runs.get':return {'status':'completed','final_answer':'Done','last_sequence':1}
    with patch('builtins.input',return_value='yes'):
        assert cli.attach(Client(),'r')==0
    output=capsys.readouterr()
    assert output.out=='Done\n'
    assert 'Using write_file' in output.err and 'approve write_file' in output.err


def test_cli_interrupt_sends_remote_cancel_and_returns_130(capsys):
    class Client:
        cancelled=False
        def call(self,method,params=None):
            if method=='runs.events':
                if not self.cancelled:raise KeyboardInterrupt
                return {'events':[]}
            if method=='runs.cancel':self.cancelled=True;return {'status':'cancelling'}
            if method=='runs.get':return {'status':'cancelled','final_answer':'Cancelled','last_sequence':0}
    client=Client()
    assert cli.attach(client,'r')==130
    assert client.cancelled
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.skipif(os.name != 'posix', reason='POSIX signals')
def test_real_sigint_cleans_shell_child_and_saves_cancellation(tmp_path):
    shell_script = "import time; open('ready','w').write('ready'); time.sleep(1); open('late','w').write('late')"
    command = shlex.join([sys.executable, '-c', shell_script])
    script = f"""
from pathlib import Path
from pico import Pico, FakeModelClient, StateStore, WorkspaceContext
from pico.providers.types import ModelResponse, ToolCall
root=Path({str(tmp_path)!r})
agent=Pico(FakeModelClient([ModelResponse(tool_calls=[ToolCall('run_shell', {{'command': {command!r}}})])]), WorkspaceContext.build(root), StateStore(root), approval_policy='auto')
agent.ask('run')
assert agent.current_run_state.status == 'cancelled'
"""
    process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / 'ready').exists()
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
        time.sleep(1)
        assert not (tmp_path / 'late').exists()
        store=StateStore(tmp_path)
        report=store.get_report(store.list_runs()[0]['run_id'])
        assert report['status'] == 'cancelled'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
