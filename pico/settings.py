"""Pico-owned model profiles, shared by desktop and workspace Hosts."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    'deepseek': ('deepseek-v4-pro', 'https://api.deepseek.com/anthropic'),
    'openai': ('gpt-5.4', 'https://www.right.codes/codex/v1'),
    'anthropic': ('claude-sonnet-4-6', 'https://www.right.codes/claude/v1'),
    'ollama': ('qwen3.5:4b', 'http://127.0.0.1:11434'),
}
MAX_BYTES = 65536


def directory():
    if os.environ.get('PICO_CONFIG_DIR'):
        return Path(os.environ['PICO_CONFIG_DIR']).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Pico'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'pico'


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid model settings')
    return value.strip()


def _endpoint(value):
    value = _text(value, 2048)
    if value:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid service URL')
        if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Service URL requires HTTPS')
    return value.rstrip('/')


def read():
    file = directory() / 'models.json'
    try:
        with file.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {'version': 1, 'provider': 'deepseek', 'profiles': {}}
    if len(raw) > MAX_BYTES:
        raise ValueError('Model settings exceed size limit')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'version', 'provider', 'profiles'} or value['version'] != 1 or not isinstance(value['provider'], str) or value['provider'] not in DEFAULTS:
        raise ValueError('Invalid model settings')
    if not isinstance(value['profiles'], dict) or set(value['profiles']) - DEFAULTS.keys():
        raise ValueError('Invalid model profiles')
    for profile in value['profiles'].values():
        if not isinstance(profile, dict) or set(profile) != {'model', 'endpoint', 'api_key'}:
            raise ValueError('Invalid model profile')
        _text(profile['model'], 256)
        _endpoint(profile['endpoint'])
        _text(profile['api_key'], 16384)
    return value


def status():
    value = read()
    return {'provider': value['provider'], 'configured': bool(value['profiles']), 'profiles': {
        name: {'model': p['model'], 'endpoint': p['endpoint'], 'api_key_configured': bool(p['api_key'])}
        for name, p in value['profiles'].items()}}


def _write(value):
    data = json.dumps(value, ensure_ascii=False).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('Model settings exceed size limit')
    root = directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.models-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / 'models.json')
    finally:
        Path(temporary).unlink(missing_ok=True)
    return status()


def save(params):
    if not isinstance(params, dict) or set(params) - {'provider', 'model', 'endpoint', 'api_key', 'clear_api_key'}:
        raise ValueError('Invalid model settings')
    name = params.get('provider')
    if not isinstance(name, str) or name not in DEFAULTS or type(params.get('clear_api_key', False)) is not bool:
        raise ValueError('Invalid provider')
    value = read()
    previous = value['profiles'].get(name, {})
    key = _text(params['api_key'], 16384) if 'api_key' in params else previous.get('api_key', '')
    if params.get('clear_api_key'):
        key = ''
    value['provider'] = name
    value['profiles'][name] = {'model': _text(params.get('model', ''), 256),
                               'endpoint': _endpoint(params.get('endpoint', '')), 'api_key': key}
    return _write(value)


def import_env(file):
    from .config import _parse_env_line

    with Path(file).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Configuration exceeds size limit')
    env = dict(row for line in raw.decode().splitlines() if (row := _parse_env_line(line)))
    name = env.get('PICO_PROVIDER', 'deepseek')
    if not isinstance(name, str) or name not in DEFAULTS:
        raise ValueError('Invalid provider')
    prefix = 'PICO_' + name.upper()
    key = env.get(prefix + '_API_KEY') or env.get(name.upper() + '_API_KEY') or ''
    if name in ('openai', 'anthropic'):
        key = key or env.get('PICO_RIGHT_CODES_API_KEY', '')
    model = env.get(prefix + '_MODEL') or env.get(name.upper() + '_MODEL', '')
    endpoint = env.get(prefix + '_API_BASE') or env.get(name.upper() + '_API_BASE', '')
    if name == 'ollama':
        endpoint = env.get('PICO_OLLAMA_HOST', env.get('OLLAMA_HOST', ''))
    if not (model or endpoint or key):
        raise ValueError('No model settings found')
    return save({'provider': name, 'model': model, 'endpoint': endpoint, **({'api_key': key} if key else {})})


def model_env():
    value = read()
    if not value['profiles']:
        return {}
    result = {'PICO_PROVIDER': value['provider']}
    for name, profile in value['profiles'].items():
        prefix = 'PICO_' + name.upper()
        result[prefix + '_MODEL'] = profile['model'] or DEFAULTS[name][0]
        result[prefix + ('_HOST' if name == 'ollama' else '_API_BASE')] = profile['endpoint'] or DEFAULTS[name][1]
        result[prefix + '_API_KEY'] = profile['api_key']
    return result


def bound_key(provider, endpoint, fallback):
    profile = read()['profiles'].get(provider)
    if profile is None:
        return fallback
    expected = profile['endpoint'] or DEFAULTS[provider][1]
    if provider in ('deepseek', 'anthropic'):
        endpoint = endpoint.rstrip('/').removesuffix('/v1')
        expected = expected.rstrip('/').removesuffix('/v1')
    return profile['api_key'] if endpoint.rstrip('/') == expected.rstrip('/') else ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'save', 'import'))
    parser.add_argument('--file')
    args = parser.parse_args()
    try:
        if args.action == 'save':
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Settings exceed size limit')
            result = save(json.loads(raw))
        elif args.action == 'import':
            result = import_env(args.file)
        else:
            result = status()
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'error': '无法读取或保存模型设置。请检查配置格式、服务地址和文件权限；原有设置未覆盖。'}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
