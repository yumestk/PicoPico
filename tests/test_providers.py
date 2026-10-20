import copy
import json
from unittest.mock import patch

import pytest

from pico.providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from pico.providers.types import ModelProtocolError, ModelRequest
from pico.tools import BASE_TOOL_SPECS


def client(kind):
    if kind == 'messages':
        return AnthropicCompatibleModelClient('model', 'https://example.test/v1', 'secret', None, 5, thinking={'type': 'disabled'})
    if kind == 'responses':
        return OpenAICompatibleModelClient('model', 'https://example.test/v1', 'secret', None, 5)
    return OllamaModelClient('model', 'http://localhost:11434', 0.2, 0.9, 5)


def payload(kind, text='done', calls=False, length=False):
    if kind == 'messages':
        blocks = ([{'type': 'thinking', 'thinking': 'opaque', 'signature': 'signed'}] + [
            {'type': 'tool_use', 'id': f'call_{i}', 'name': 'read_file', 'input': {'path': 'README.md'}} for i in range(2)]) if calls else [{'type': 'text', 'text': text}]
        return {'content': blocks, 'stop_reason': 'max_tokens' if length else 'tool_use' if calls else 'end_turn', 'usage': {'input_tokens': 11, 'output_tokens': 5}}
    if kind == 'responses':
        blocks = ([{'type': 'reasoning', 'id': 'rs1', 'encrypted_content': 'opaque'}] + [
            {'type': 'function_call', 'id': f'fc_{i}', 'call_id': f'call_{i}', 'name': 'read_file', 'arguments': '{"path":"README.md"}'} for i in range(2)]) if calls else [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]
        return {'output': blocks, 'status': 'incomplete' if length else 'completed', 'incomplete_details': {'reason': 'max_output_tokens'}, 'usage': {'input_tokens': 11, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 7}}}
    message = {'role': 'assistant', 'content': '' if calls else text}
    if calls:
        message['tool_calls'] = [{'function': {'name': 'read_file', 'arguments': {'path': 'README.md'}}} for _ in range(2)]
    return {'message': message, 'done': True, 'done_reason': 'length' if length else 'stop', 'prompt_eval_count': 11, 'eval_count': 5}


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self):
        return json.dumps(self.value).encode()


def request():
    spec = BASE_TOOL_SPECS['read_file']
    return ModelRequest('instructions', [{'role': 'user', 'content': 'read files'}],
                        [{'name': 'read_file', 'description': spec['description'], 'parameters': spec['schema']}], 512)


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
@pytest.mark.parametrize('mode', ['text', 'tools', 'length'])
def test_native_response_and_request(kind, mode):
    c = client(kind)
    value = payload(kind, calls=mode == 'tools', length=mode == 'length')
    with patch('urllib.request.urlopen', return_value=Response(value)) as post:
        result = c.complete(request())
    sent = json.loads(post.call_args.args[0].data)
    assert sent['tools']
    assert result.usage['input_tokens'] == 11
    assert result.finish_reason == ('length' if mode == 'length' else 'tool_calls' if mode == 'tools' else 'stop')
    assert len(result.tool_calls) == (2 if mode == 'tools' else 0)
    if mode == 'tools':
        assert result.provider_blocks
        assert result.tool_calls[0].id != result.tool_calls[1].id
    if kind == 'ollama':
        assert post.call_args.args[0].full_url.endswith('/api/chat')
    if kind == 'messages':
        assert sent['thinking'] == {'type': 'disabled'}
        assert sent['system'] == 'instructions'
    if kind == 'responses':
        assert sent['instructions'] == 'instructions'
        assert result.usage['cached_tokens'] == 7


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
def test_all_calls_and_provider_blocks_roundtrip(kind):
    c = client(kind)
    req = request()
    original = payload(kind, calls=True)
    with patch('urllib.request.urlopen', return_value=Response(original)):
        response = c.complete(req)
    req.messages.append(response.message())
    for call in response.tool_calls:
        req.messages.append({'role': 'tool', 'call_id': call.id, 'name': call.name,
                             'content': '{"status":"error","output":"permission denied"}', 'status': 'error'})
    snapshot = copy.deepcopy(req.messages)
    with patch('urllib.request.urlopen', return_value=Response(payload(kind))) as post:
        assert c.complete(req).text == 'done'
    assert req.messages == snapshot
    sent = json.loads(post.call_args.args[0].data)
    if kind == 'responses':
        assert sent['input'][1:1 + len(original['output'])] == original['output']
        assert [i['call_id'] for i in sent['input'] if i.get('type') == 'function_call_output'] == ['call_0', 'call_1']
    elif kind == 'messages':
        assert sent['messages'][1]['content'] == original['content']
        assert [b['tool_use_id'] for b in sent['messages'][2]['content']] == ['call_0', 'call_1']
        assert all(b['is_error'] for b in sent['messages'][2]['content'])
    else:
        assert sent['messages'][2] == original['message']
        assert len([m for m in sent['messages'] if m['role'] == 'tool']) == 2


@pytest.mark.parametrize('kind', ['messages', 'responses'])
def test_duplicate_call_ids_rejected_before_execution(kind):
    value = payload(kind, calls=True)
    blocks = value['content'] if kind == 'messages' else value['output']
    key = 'id' if kind == 'messages' else 'call_id'
    blocks[2][key] = blocks[1][key]
    with patch('urllib.request.urlopen', return_value=Response(value)), pytest.raises(ModelProtocolError, match='duplicate'):
        client(kind).complete(request())


def test_responses_complete_sse_preserves_tool_calls():
    value = payload('responses', calls=True)
    raw = ('data: ' + json.dumps({'type': 'response.completed', 'response': value}) + '\n\ndata: [DONE]\n').encode()
    with patch('urllib.request.urlopen', return_value=Response(None)) as post:
        post.return_value.read = lambda: raw
        assert len(client('responses').complete(request()).tool_calls) == 2


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
def test_backend_errors_not_interpreted_as_final_text(kind):
    with patch('urllib.request.urlopen', return_value=Response({'error': 'tools unsupported'})), pytest.raises(RuntimeError, match='unsupported'):
        client(kind).complete(request())


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
def test_malformed_blocks_raise_a_protocol_error(kind):
    value = payload(kind, calls=True)
    if kind == 'messages':
        value['content'] = [42]
    elif kind == 'responses':
        value['output'] = [42]
    else:
        value['message']['tool_calls'] = [42]
    with patch('urllib.request.urlopen', return_value=Response(value)), pytest.raises(ModelProtocolError):
        client(kind).complete(request())
