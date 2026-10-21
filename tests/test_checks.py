import asyncio
import json
import subprocess
import sys

import pytest
from test_host import running_host, submit, terminal, wait
from test_review import agent_at
from test_run_manager import start, wait_for

from pico.application import ApplicationService
from pico.cli import build_arg_parser
from pico.providers.types import ModelResponse, ToolCall
from pico.state_store import ApplicationError
from pico.tools import BASE_TOOL_SPECS, validate_arguments


def verification(command, **kwargs):
    return ToolCall('run_shell', {'command': command, 'purpose': 'verification', **kwargs})


@pytest.mark.parametrize('command,options,outcome', [
    ('printf ok', {}, 'passed'), ('printf failed >&2; exit 1', {}, 'failed'),
    ('sleep 5', {'timeout': 1}, 'timed_out'),
    ('printf changed > source', {}, 'stale'),
])
def test_real_command_outcomes_ignore_model_summary(tmp_path, command, options, outcome):
    (tmp_path / 'source').write_text('original')
    agent = agent_at(tmp_path, [verification(command, **options)])
    agent.ask('run check')
    value = agent.current_run_state.run_id
    page = agent.store.tool_page(value, checks=True)
    check, = page['items']
    assert check['status'] == outcome
    assert check['command'] == command and check['cwd'] == str(tmp_path)
    assert check['started_at'] <= check['finished_at']
    assert check['before_scope']['fingerprint']
    assert check['after_scope']['fingerprint']
    assert check['timed_out'] == (outcome == 'timed_out')
    assert check['exit_code'] == (1 if outcome == 'failed' else 130 if outcome == 'timed_out' else 0)
    assert agent.store.review_get(value)['checks']['status'] == outcome
    assert agent.store.get_run(value)['status'] == 'completed'
    exported = agent.store.export_run(value)
    assert json.loads((exported / 'checks.json').read_text())[0]['status'] == outcome


def test_later_write_invalidates_check_but_old_shell_is_unclassified(tmp_path):
    (tmp_path / 'source').write_text('old')
    agent = agent_at(tmp_path, [verification('echo ok'), ToolCall('write_file', {'path': 'source', 'content': 'new'})])
    agent.ask('verify then edit')
    check, = agent.store.checks(agent.current_run_state.run_id)
    assert check['command_outcome'] == 'passed' and check['status'] == 'stale'
    agent = agent_at(tmp_path, [ToolCall('run_shell', {'command': 'echo tests passed'})])
    agent.ask('inspect')
    assert agent.store.review_get(agent.current_run_state.run_id)['checks']['status'] == 'not_run'
    assert agent.store.checks(agent.current_run_state.run_id) == []
    call, = agent.store.tool_page(agent.current_run_state.run_id)['items']
    assert call['result']['data']['purpose'] == 'general'


def test_partial_scope_and_missing_end_never_validate_current_version(tmp_path):
    (tmp_path / '.env').write_text('TOKEN=opaque')
    agent = agent_at(tmp_path, [verification('echo ok')])
    agent.ask('check')
    value = agent.current_run_state.run_id
    check, = agent.store.checks(value)
    assert check['command_outcome'] == 'passed' and check['status'] == 'inconclusive'
    assert 'sensitive_file' in check['before_scope']['notes']


def test_schema_enum_and_existing_permission_policy(tmp_path):
    schema = BASE_TOOL_SPECS['run_shell']['schema']
    validate_arguments(schema, {'command': 'true'})
    validate_arguments(schema, {'command': 'true', 'purpose': 'verification'})
    for purpose in ('trusted', True, [], None):
        with pytest.raises(ValueError):
            validate_arguments(schema, {'command': 'touch forbidden', 'purpose': purpose})
    agent = agent_at(tmp_path, [verification('touch forbidden')])
    agent.approval_policy = 'never'
    agent.ask('check')
    assert not (tmp_path / 'forbidden').exists()
    check, = agent.store.checks(agent.current_run_state.run_id)
    assert check['status'] == 'not_executed' and check['exit_code'] is None


def test_host_cli_queries_redaction_bounds_refresh_and_paging(tmp_path, monkeypatch):
    monkeypatch.setenv('PICO_DEEPSEEK_API_KEY', 'verification-private-token')
    (tmp_path / 'source').write_text('original')
    command = "printf verification-private-token; i=0; while [ $i -lt 10000 ]; do printf 'xxxxxxxxxx'; i=$((i+1)); done"
    script = [ModelResponse(tool_calls=[verification(command), verification('exit 1')]), ModelResponse(text='All tests passed!')]
    with running_host(tmp_path, script) as (client, _, env):
        session, run = submit(client)
        value = run['run_id']
        wait(lambda: terminal(client, value))
        params = {'run_id': value, 'limit': 1}
        first = client.call('runs.checks', params)
        assert first['items'][0]['status'] == 'passed'
        assert first['items'][0]['stdout_truncated']
        assert len(json.dumps(first).encode()) < 256 * 1024
        assert 'verification-private-token' not in json.dumps(first)
        second = client.call('runs.checks', {**params, 'cursor': first['next_cursor']})
        assert second['items'][0]['status'] == 'failed' and second['next_cursor'] is None
        assert client.call('runs.review', {'run_id': value})['checks']['status'] == 'failed'
        tool = client.call('runs.tool_calls', params)
        assert tool['items'][0]['call_id'] == first['items'][0]['call_id']
        for method, invalid in [('runs.checks', 'not-a-cursor'), ('runs.tool_calls', first['next_cursor'])]:
            with pytest.raises(ApplicationError):
                client.call(method, {**params, 'cursor': invalid})
        info = client.call('configuration.get')
        assert info['api_key_configured'] is True and set(info) == {'provider', 'model', 'endpoint', 'api_key_configured'}
        assert 'verification-private-token' not in json.dumps(info)
        result = subprocess.run([sys.executable, '-m', 'pico', '--cwd', str(tmp_path), 'runs', 'checks', value, '--limit', '1'],
                                env=env, capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)['items'][0]['status'] == 'passed'
        frozen = client.call('runs.review', {'run_id': value})
        assert client.call('runs.review_refresh', {'run_id': value})['version_status'] == 'matched'
        (tmp_path / 'source').write_text('external change')
        refreshed = client.call('runs.review_refresh', {'run_id': value})
        assert refreshed['version_status'] == 'stale'
        assert client.call('runs.review', {'run_id': value}) == frozen
        assert client.call('runs.checks', params) == first
        listed = client.call('runs.list', {'session_id': session['id'], 'cursor': ''})
        assert listed['items'][0]['run_id'] == value
        assert 'config' not in listed['items'][0] and 'final_answer' not in listed['items'][0]
        assert client.call('runs.list')  # Legacy clients still receive a list.


@pytest.mark.parametrize('interrupt', [False, True])
def test_cancel_and_dead_worker_keep_started_evidence_without_inventing_success(tmp_path, interrupt):
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[
            ModelResponse(tool_calls=[verification('printf ready > .pico/check-started; sleep 20')]), ModelResponse(text='Passed')]).open()
        try:
            _, run = await start(app)
            value = run['run_id']
            await wait_for(lambda: app.store.checks(value) and app.store.checks(value)[0]['started_at'])
            await wait_for(lambda: (tmp_path / '.pico/check-started').exists())
            assert (await app.dispatch('runs.review_refresh', {'run_id': value}))['state'] == 'busy'
            if interrupt:
                app.manager.process.kill()
            else:
                await app.dispatch('runs.cancel', {'run_id': value})
            await wait_for(lambda: app.store.get_run(value)['status'] in {'cancelled', 'interrupted'})
            check, = (await app.dispatch('runs.checks', {'run_id': value}))['items']
            assert check['status'] == ('unknown' if interrupt else 'cancelled')
            assert check['cwd'] == str(tmp_path) and check['before_scope']
            assert check['status'] != 'passed'
            return value, check
        finally:
            await app.close()
    value, check = asyncio.run(scenario())
    async def reopen():
        app = await ApplicationService(tmp_path).open()
        try:
            assert (await app.dispatch('runs.checks', {'run_id': value}))['items'][0] == check
        finally:
            await app.close()
    asyncio.run(reopen())


def test_stable_list_cursors_exclude_new_rows_and_survive_updates_and_reopen(tmp_path):
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            sessions = [await app.dispatch('sessions.create') for _ in range(4)]
            for i, session in enumerate(sessions):
                app.store.create_run(session['id'], 'queued', request_id=str(i))
            session_page = await app.dispatch('sessions.list', {'cursor': '', 'limit': 2})
            run_page = await app.dispatch('runs.list', {'cursor': '', 'limit': 2})
            assert len(session_page['items']) == 2
            new = await app.dispatch('sessions.create')
            app.store.save_session(sessions[0])  # Updating recency must not duplicate a row.
            app.store.create_run(new['id'], 'new')
            cursor = session_page['next_cursor']
            rest = await app.dispatch('sessions.list', {'cursor': cursor, 'limit': 2})
            assert {r['id'] for r in session_page['items'] + rest['items']} == {s['id'] for s in sessions}
            assert rest['next_cursor'] is None
            runs_rest = await app.dispatch('runs.list', {'cursor': run_page['next_cursor'], 'limit': 2})
            assert len({r['run_id'] for r in run_page['items'] + runs_rest['items']}) == 4
            assert all(r['session_id'] != new['id'] for r in runs_rest['items'])
            with pytest.raises(ApplicationError):
                await app.dispatch('runs.list', {'cursor': cursor})
            with pytest.raises(ApplicationError):
                await app.dispatch('runs.checks', {'run_id': 'missing', 'limit': 501})
            return cursor, rest
        finally:
            # No pump: these deliberately queued records are recovered on reopen.
            await app.close()
    cursor, rest = asyncio.run(scenario())
    async def reopen():
        app = await ApplicationService(tmp_path).open()
        try:
            assert await app.dispatch('sessions.list', {'cursor': cursor, 'limit': 2}) == rest
        finally:
            await app.close()
    asyncio.run(reopen())


def test_cli_review_and_diff_parameters():
    args = build_arg_parser().parse_args(['runs', 'diff', 'run', '--file-id', 'file', '--after', '12'])
    assert args.file_id == 'file' and args.after == 12
    assert build_arg_parser().parse_args(['configuration']).command == 'configuration'


def test_redaction_precedes_output_slicing_and_config_url_display(tmp_path, monkeypatch):
    token = 'boundary-secret-prefix-should-never-appear'
    monkeypatch.setenv('PICO_DEEPSEEK_API_KEY', token)
    agent = agent_at(tmp_path, [verification("printf '%8180s' ''; printf " + token)])
    agent.ask('check')
    check, = agent.store.tool_page(agent.current_run_state.run_id, checks=True)['items']
    assert 'boundary-secret' not in check['stdout']
    assert '<redacted>' in check['stdout']
    monkeypatch.setenv('PICO_DEEPSEEK_API_BASE', 'https://user:password@example.invalid/anthropic?token=url-secret')
    async def scenario():
        app = await ApplicationService(tmp_path).open()
        try:
            config = await app.dispatch('configuration.get')
            encoded = json.dumps(config)
            assert config['api_key_configured']
            assert all(secret not in encoded for secret in ('password', 'url-secret', token, 'user:'))
            assert config['endpoint'].startswith('https://example.invalid/')
        finally:
            await app.close()
    asyncio.run(scenario())


def test_large_tool_card_keeps_identity_and_bounds_without_leaking_secret_id(tmp_path, monkeypatch):
    from pico.context_manager import ContextBudgetError

    secret_id = 'secret-used-as-model-call-id'
    monkeypatch.setenv('PICO_DEEPSEEK_API_KEY', secret_id)
    agent = agent_at(tmp_path, [ToolCall('write_file', {'path': 'large.txt', 'content': 'x' * 300000}, secret_id),
                                ToolCall('read_file', {'path': 'large.txt', 'end': 1})])
    # Oversized native replay is still rejected; completed tools remain queryable after that failure.
    with pytest.raises(ContextBudgetError):
        agent.ask('write and inspect')
    page = agent.store.tool_page(agent.current_run_state.run_id)
    assert len(page['items']) == 2 and page['next_cursor'] is None
    assert page['items'][0]['details_truncated']
    assert page['items'][0]['call_id'] == '<redacted>'
    assert secret_id not in json.dumps(page)
    assert len(json.dumps(page).encode()) < 256 * 1024
    assert (tmp_path / 'large.txt').stat().st_size == 300000
