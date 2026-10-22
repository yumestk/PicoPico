import asyncio
import json
from pathlib import Path

import pytest
from test_host import running_host

from pico.application import ApplicationService
from pico.desktop_bootstrap import bootstrap
from pico.state_store import ApplicationError


def test_display_history_is_bounded_stable_redacted_and_reset_has_new_identity(tmp_path, monkeypatch):
    secret = 'desktop-history-boundary-secret'
    monkeypatch.setenv('PICO_DEEPSEEK_API_KEY', secret)
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            session = await app.dispatch('sessions.create')
            private = app.store.load_session(session['id'])
            private['history'] = [
                {'role': 'user', 'content': 'x' * 16370 + secret + 'y' * 100000},
                {'role': 'assistant', 'content': 'call', 'provider_blocks': [{'private': 'never-public'}],
                 'tool_calls': [{'id': 'call' * 300000, 'name': 'write_file', 'arguments': {'content': 'private-args' * 100000}}]},
                {'role': 'tool', 'name': 'write_file', 'call_id': 'call' * 300000, 'content': 'result', 'result': {'raw': 'private-result'}},
            ] + [{'role': 'assistant', 'content': '\x01' * 50000} for _ in range(70)]
            app.store.save_session(private)
            first = await app.dispatch('sessions.get', {'session_id': session['id'], 'limit': 500})
            assert len(json.dumps(first).encode()) < 1024 * 1024
            assert first['next_message'] < first['history_count']
            assert first['history'][0]['content_truncated']
            assert '<redacted>' in first['history'][0]['content']
            assert first['history'][1]['details_truncated'] and first['history'][2]['details_truncated']
            public = json.dumps(first)
            assert all(s not in public for s in ('private-args', 'private-result', 'never-public', 'provider_blocks', 'desktop-history'))
            next_page = await app.dispatch('sessions.get', {'session_id': session['id'], 'after_message': first['next_message'], 'limit': 2})
            assert first['history'][-1]['message_index'] < next_page['history'][0]['message_index']
            again = await app.dispatch('sessions.get', {'session_id': session['id'], 'limit': 500})
            assert first['history'] == again['history']
            run = app.store.create_run(session['id'], 'inspect oversized identity')
            app.store.connection.execute('INSERT INTO tool_calls VALUES (?,?,?,?,?,NULL)',
                (run['run_id'], 'call' * 300000, 'unknown' * 100000, '{}', 'proposed'))
            card = await app.dispatch('runs.tool_calls', {'run_id': run['run_id']})
            assert len(json.dumps(card).encode()) < 256 * 1024
            assert card['items'][0]['identity_truncated'] and card['items'][0]['call_ref']
            app.store.cancel(run['run_id'])
            await app.dispatch('sessions.reset', {'session_id': session['id']})
            reset = app.store.load_session(session['id'])
            reset['history'] = [{'role': 'user', 'content': 'new first message'}]
            app.store.save_session(reset)
            updated = await app.dispatch('sessions.get', {'session_id': session['id'], 'limit': 1})
            assert updated['history'][0]['message_id'] != first['history'][0]['message_id']
        finally:
            await app.close()
    asyncio.run(scenario())


def test_session_configuration_summary_uses_frozen_provider_without_exposing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('PICO_PROVIDER', 'deepseek')
    monkeypatch.setenv('PICO_ANTHROPIC_API_KEY', 'desktop-config-secret')
    monkeypatch.setenv('PICO_ANTHROPIC_API_BASE', 'https://user:desktop-password@example.invalid/mock?token=desktop-url-token')
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            session = await app.dispatch('sessions.create', {'config': {'provider': 'anthropic', 'model': 'chosen-model'}})
            summary = await app.dispatch('configuration.get', {'session_id': session['id']})
            assert summary['provider'] == 'anthropic' and summary['model'] == 'chosen-model'
            assert summary['api_key_configured'] is True
            assert 'desktop-config-secret' not in json.dumps(summary)
            private = app.store.load_session(session['id'])
            run = app.store.create_run(session['id'], 'inspect', private['config'])
            app.store.begin_run(private, run)
            app.store.finish(private, {**run, 'status': 'completed'}, {})
            public_run = await app.dispatch('runs.get', {'run_id': run['run_id']})
            encoded = json.dumps([session, summary, public_run])
            assert 'desktop-password' not in encoded and 'desktop-url-token' not in encoded
            assert 'desktop-password' in private['config']['base_url']
        finally:
            await app.close()
    asyncio.run(scenario())


def test_idle_stop_is_atomic_against_new_starts_and_never_stops_queued_work(tmp_path):
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            session = await app.dispatch('sessions.create')
            queued = app.store.create_run(session['id'], 'pending task')
            with pytest.raises(ApplicationError, match='must finish'):
                await app.dispatch('host.stop_if_idle')
            assert app.manager.accepting and not app.stopping.is_set()
            assert app.store.get_run(queued['run_id'])['status'] == 'queued'
            app.store.cancel(queued['run_id'])
            assert (await app.dispatch('host.stop_if_idle'))['status'] == 'stopping'
            with pytest.raises(ApplicationError, match='not accepting'):
                await app.dispatch('runs.start', {'session_id': session['id'], 'prompt': 'late', 'client_request_id': 'late'})
            assert len(app.store.list_runs()) == 1
        finally:
            await app.close()
    asyncio.run(scenario())


def test_safe_idle_upgrade_restarts_host_and_preserves_sessions(tmp_path):
    with running_host(tmp_path, []) as (client, _, _):
        session = client.call('sessions.create')
        before = client.call('host.info')
        result = bootstrap(tmp_path, upgrade=True)
        try:
            assert result['info']['instance_id'] != before['instance_id']
            assert result['info']['api_version'] >= 4
            assert client.call('sessions.get', {'session_id': session['id']})['id'] == session['id']
        finally:
            client.call('host.stop')


def test_old_host_upgrade_requires_manual_stop_and_never_calls_unconditional_stop(tmp_path, monkeypatch):
    calls = []
    class OldHost:
        def __init__(self, root):
            self.root = Path(root)
            self.path = self.root / 'unused'
        def ensure(self):
            return {'workspace': str(self.root), 'api_version': 3, 'capabilities': ['runs']}
        def call(self, method):
            calls.append(method)
            raise AssertionError('Must not stop old Hosts without an atomic idle guard')
    monkeypatch.setattr('pico.desktop_bootstrap.HostClient', OldHost)
    with pytest.raises(ApplicationError) as caught:
        bootstrap(tmp_path, upgrade=True)
    assert caught.value.code == 'manual_upgrade_required' and calls == []


def test_detached_long_polls_release_host_capacity_for_control_requests(tmp_path):
    import socket
    import time

    from test_host import submit, wait

    from pico.providers.types import ModelResponse, ToolCall

    script = [ModelResponse(tool_calls=[ToolCall('write_file', {'path': 'guarded', 'content': 'not yet'})])]
    with running_host(tmp_path, script) as (client, _, _):
        _, run = submit(client, {'approval': 'ask'})
        wait(lambda: client.call('approvals.list'))
        # More than the Host's 64-connection ceiling, without waiting out a 10s poll.
        for index in range(80):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(client.path))
                connection.sendall((json.dumps({'id': f'poll-{index}', 'protocol': 'pico-host-v1',
                    'workspace': str(client.root), 'method': 'runs.events',
                    'params': {'run_id': run['run_id'], 'after_sequence': 1000000, 'wait_ms': 10000}}) + '\n').encode())
            time.sleep(0.004)
        assert client.call('host.info')['status'] == 'ready'
        assert client.call('runs.cancel', {'run_id': run['run_id']})['status'] == 'cancelling'
        assert not (tmp_path / 'guarded').exists()


def test_invalid_project_config_is_a_configuration_error_without_env_contents(tmp_path, monkeypatch):
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            (tmp_path / '.env').write_text('MALFORMED private-value-never-send\n')
            with pytest.raises(ApplicationError) as error:
                await app.dispatch('configuration.get')
            assert error.value.code == 'configuration_error'
            assert 'private-value-never-send' not in str(error.value)
        finally:
            (tmp_path / '.env').unlink()
            await app.close()
    asyncio.run(scenario())


def test_idle_upgrade_tolerates_lost_stop_ack_without_forcing_stop(tmp_path, monkeypatch):
    calls = []
    class LostAckHost:
        def __init__(self, root):
            self.root = Path(root)
            self.path = self.root / 'unused'
            self.stopped = False
        def ensure(self):
            return {'workspace': str(self.root), 'api_version': 4, 'capabilities': ['idle_upgrade'], 'restarted': self.stopped}
        def call(self, method):
            calls.append(method)
            if method == 'host.stop_if_idle':
                self.stopped = True
                raise ApplicationError('invalid_response', 'ACK lost after accepting stop')
            if method == 'host.info':
                raise OSError('Closed')
            raise AssertionError('Unexpected method')
    monkeypatch.setattr('pico.desktop_bootstrap.HostClient', LostAckHost)
    assert bootstrap(tmp_path, upgrade=True)['info']['restarted']
    assert calls == ['host.stop_if_idle', 'host.info']
