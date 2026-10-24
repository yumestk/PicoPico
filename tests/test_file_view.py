from types import SimpleNamespace

import pytest

from pico.file_view import read_page
from pico.tools import tool_list_files, tool_search


def context(root):
    return SimpleNamespace(root=root, path=lambda p: root / p)


def test_continuation_returns_every_character_without_silent_clip(tmp_path):
    lines = ['中文 ' + str(i) * 100 for i in range(450)] + ['X' * 10000]
    (tmp_path / '空 格.txt').write_text('\n'.join(lines))
    start, column, seen = 1, 0, []
    for _ in range(100):
        page = read_page(tmp_path, '空 格.txt', start, start + 199, column)
        assert len(page['text']) < 4000
        assert page['end'] <= page['start'] + 199
        for entry in page['text'].splitlines()[1:-1]:
            number, value = entry.split(': ', 1)
            seen.append((int(number), value))
        if not page['truncated']:
            break
        start, column = page['next_line'], page['next_column']
    else:
        pytest.fail('Paging did not terminate')
    reconstructed = ['' for _ in lines]
    for number, value in seen:
        reconstructed[number - 1] += value
    assert reconstructed == lines
    assert read_page(tmp_path, '空 格.txt', 451, 451)['truncated']


@pytest.mark.parametrize('name', ['../outside.txt', '/etc/passwd', '.env', '.git/config', '.pico/state.db', 'binary.txt', 'large.txt', 'link.txt'])
def test_file_reader_rejects_private_binary_large_and_escape(tmp_path, name):
    (tmp_path / '.env').write_text('SECRET=abc')
    (tmp_path / 'binary.txt').write_bytes(b'\x00binary')
    (tmp_path / 'large.txt').write_bytes(b'a' * (512 * 1024 + 1))
    (tmp_path / 'link.txt').symlink_to('/etc/passwd')
    with pytest.raises((OSError, ValueError)):
        read_page(tmp_path, name)


def test_directory_pages_detect_changes(tmp_path):
    for i in range(320):
        (tmp_path / f'file-{i:04}.txt').touch()
    first = tool_list_files(context(tmp_path), {})
    assert first['truncated'] and len(first['text']) < 4000
    second = tool_list_files(context(tmp_path), {'offset': first['next_offset'], 'revision': first['revision']})
    assert 'file-0000.txt' not in second['text']
    (tmp_path / 'new.txt').touch()
    with pytest.raises(ValueError, match='changed'):
        tool_list_files(context(tmp_path), {'offset': first['next_offset'], 'revision': first['revision']})


def test_search_without_rg_is_bounded_private_safe_and_explicit(tmp_path):
    (tmp_path / 'a.txt').write_text('match\n' * 800)
    (tmp_path / '.env').write_text('match SECRET')
    (tmp_path / 'link.txt').symlink_to('/etc/passwd')
    result = tool_search(context(tmp_path), {'pattern': 'match'})
    assert result['truncated'] and 'TRUNCATED' in result['text']
    assert len(result['text']) < 4000 and 'SECRET' not in result['text']
    result = tool_search(context(tmp_path), {'pattern': 'SECRET'})
    assert '(no matches)' in result['text'] and not result['truncated']


def test_secret_crossing_column_boundary_keeps_positions_without_leaking(tmp_path, monkeypatch):
    secret = 'very-private-credential'
    monkeypatch.setenv('DEMO_API_KEY', secret)
    (tmp_path / 'sample.txt').write_text('a' * 2780 + secret + 'end')
    first = read_page(tmp_path, 'sample.txt')
    second = read_page(tmp_path, 'sample.txt', first['next_line'], first['next_line'], first['next_column'])
    assert 'very-' not in first['text'] + second['text']
    assert 'private' not in first['text'] + second['text']
    assert first['next_column'] == 2788
    assert '•' in first['text'] and '•' in second['text']


def test_host_current_file_provenance_does_not_confuse_changed_version(tmp_path):
    from test_host import running_host, wait

    from pico.providers.types import ModelResponse, ToolCall
    (tmp_path / 'sample.txt').write_text('original\n')
    with running_host(tmp_path, [ModelResponse(tool_calls=[ToolCall('read_file', {'path': 'sample.txt'})]), ModelResponse(text='done')]) as (client, _, _):
        session = client.call('sessions.create', {'config': {'provider': 'ollama'}})
        assert session['config']['max_steps'] == 12 and session['config']['max_new_tokens'] == 2048
        legacy = client.call('sessions.create', {'config': {'provider': 'ollama', 'max_steps': 6, 'max_new_tokens': 512}})
        run = client.call('runs.start', {'session_id': session['id'], 'prompt': 'read', 'client_request_id': 'read'})
        wait(lambda: client.call('runs.get', {'run_id': run['run_id']})['status'] == 'completed')
        params = {'path': 'sample.txt', 'run_id': run['run_id']}
        assert client.call('workspace.file', params)['reads'] == [{'start': 1, 'end': 1, 'matches_current': True}]
        (tmp_path / 'sample.txt').write_text('changed\n')
        assert client.call('workspace.file', params)['reads'][0]['matches_current'] is False
        assert client.call('sessions.get', {'session_id': legacy['id']})['config']['max_steps'] == 6


def test_one_bounded_map_locates_nested_entry_and_tests_without_following_links(tmp_path):
    entry = tmp_path / 'src/main/java/com/example/app/Application.java'
    entry.parent.mkdir(parents=True)
    entry.write_text('class Application {}')
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git/private').write_text('hidden')
    (tmp_path / 'outside').symlink_to('/tmp', target_is_directory=True)
    result = tool_list_files(context(tmp_path), {})
    assert 'src/main/java/com/example/app/Application.java' in result['text']
    assert '.git/private' not in result['text'] and '[L] outside' in result['text']
    assert not result['truncated']
    shallow = tool_list_files(context(tmp_path), {'depth': 1})
    assert 'Application.java' not in shallow['text']
