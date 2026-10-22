import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from pico.application import DEFAULT_OPTIONS, ApplicationService, _build_model_client
from pico.config import load_project_env
from pico.settings import (
    bound_key,
    directory,
    import_env,
    model_env,
    read,
    save,
    status,
)


def test_app_profile_applies_to_all_projects_without_loading_their_env(tmp_path, monkeypatch):
    save({'provider': 'deepseek', 'model': 'deepseek-v4-flash', 'endpoint': 'https://example.invalid/anthropic', 'api_key': 'app-profile-secret'})
    assert (directory() / 'models.json').stat().st_mode & 0o777 == 0o600
    projects = [tmp_path / 'project-a', tmp_path / 'project-b']
    for project in projects:
        project.mkdir()
    (projects[0] / '.env').write_text('PICO_PROVIDER=openai\nPICO_DEEPSEEK_API_BASE=https://untrusted.invalid\nPICO_DEEPSEEK_MODEL=wrong\nBUSINESS_TOKEN=not-a-model-secret\n')
    (projects[1] / '.env').write_text('unrelated project dotenv syntax is not Pico configuration')
    for project in projects:
        load_project_env(project)
        client = _build_model_client(SimpleNamespace(**DEFAULT_OPTIONS))
        assert client.model == 'deepseek-v4-flash'
        assert client.api_key == 'app-profile-secret'
        assert client.base_url == 'https://example.invalid/anthropic/v1'
    assert 'app-profile-secret' not in json.dumps(status())
    assert bound_key('deepseek', 'https://untrusted.invalid', 'app-profile-secret') == ''
    assert bound_key('deepseek', 'https://example.invalid/anthropic/v1', '') == 'app-profile-secret'
    assert not (projects[1] / 'models.json').exists()


def test_import_only_model_fields_and_keep_replace_clear_key(tmp_path):
    source = tmp_path / '.env'
    original = 'PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_MODEL=deepseek-v4-flash\nPICO_DEEPSEEK_API_KEY=import-secret\nDATABASE_PASSWORD=business-secret\n'
    source.write_text(original)
    imported = import_env(source)
    assert imported['profiles']['deepseek']['api_key_configured']
    assert source.read_text() == original
    assert 'business-secret' not in (directory() / 'models.json').read_text()
    save({'provider': 'deepseek', 'model': 'another', 'endpoint': ''})
    assert read()['profiles']['deepseek']['api_key'] == 'import-secret'
    save({'provider': 'deepseek', 'api_key': 'replacement'})
    assert read()['profiles']['deepseek']['api_key'] == 'replacement'
    save({'provider': 'deepseek', 'clear_api_key': True})
    assert not status()['profiles']['deepseek']['api_key_configured']


def test_invalid_settings_never_overwrite_valid_file(tmp_path):
    save({'provider': 'deepseek', 'api_key': 'preserved'})
    file = directory() / 'models.json'
    before = file.read_bytes()
    for invalid in ({'provider': 'invalid'}, {'provider': 'deepseek', 'api_key': 'x\nSECRET'},
                    {'provider': 'deepseek', 'endpoint': 'https://user:password@example.invalid'},
                    {'provider': 'deepseek', 'endpoint': 'http://remote.invalid'}, {'provider': 'deepseek', 'private_path': '/escape'}):
        with pytest.raises(ValueError):
            save(invalid)
        assert file.read_bytes() == before
    bad = tmp_path / 'bad.env'
    bad.write_text('INVALID secret-value')
    with pytest.raises(ValueError):
        import_env(bad)
    assert file.read_bytes() == before


def test_host_sees_profile_saved_after_start_and_old_session_stays_frozen(tmp_path):
    async def scenario():
        app = await ApplicationService(tmp_path, test_script=[]).open()
        try:
            old = await app.dispatch('sessions.create', {'config': {'provider': 'deepseek', 'model': 'old-model'}})
            save({'provider': 'deepseek', 'model': 'deepseek-v4-flash', 'api_key': 'late-saved-secret'})
            current = await app.dispatch('configuration.get')
            assert current['model'] == 'deepseek-v4-flash' and current['api_key_configured']
            frozen = await app.dispatch('configuration.get', {'session_id': old['id']})
            assert frozen['model'] == 'old-model'
            new = await app.dispatch('sessions.create')
            assert new['config']['model'] == 'deepseek-v4-flash'
            save({'provider': 'deepseek', 'endpoint': 'https://new-provider.invalid', 'api_key': 'new-secret'})
            frozen = await app.dispatch('configuration.get', {'session_id': old['id']})
            assert not frozen['api_key_configured']  # New credentials never go to an old endpoint.
            assert 'late-saved-secret' not in json.dumps([current, frozen, new])
        finally:
            await app.close()
    asyncio.run(scenario())


def test_legacy_project_env_still_works_without_app_profiles(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('PICO_DEEPSEEK_MODEL=project-model\nPICO_CONFIG_DIR=/untrusted\n')
    original = os.environ['PICO_CONFIG_DIR']
    assert not model_env()
    load_project_env(tmp_path)
    assert os.environ['PICO_DEEPSEEK_MODEL'] == 'project-model'
    assert os.environ['PICO_CONFIG_DIR'] == original
