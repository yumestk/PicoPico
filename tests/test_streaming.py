import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_providers import client, payload, request

from pico.preview import Preview
from pico.providers.streaming import consume
from pico.providers.types import ModelProtocolError, ModelRequestError


def sse(*events):
    return ''.join('data: ' + json.dumps(e, ensure_ascii=False) + '\n\n' for e in events).encode()


def messages(text='中文 answer', tools=True):
    events = [{'type': 'message_start', 'message': {'role': 'assistant', 'usage': {'input_tokens': 9}}},
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
              {'type': 'content_block_stop', 'index': 0}]
    if tools:
        events += [{'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
                   {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE'}},
                   {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'signature_delta', 'signature': 'SIGNED'}},
                   {'type': 'content_block_stop', 'index': 1},
                   {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'call1', 'name': 'read_file', 'input': {}}},
                   {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":"'}},
                   {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': 'README.md"}'}},
                   {'type': 'content_block_stop', 'index': 2}]
    return events + [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tools else 'end_turn'}, 'usage': {'output_tokens': 7}}, {'type': 'message_stop'}]


class Response(io.BytesIO):
    def __init__(self, data, content_type='text/event-stream'):
        super().__init__(data)
        self.headers = {'Content-Type': content_type}


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
def test_live_protocol_deltas_and_complete_native_response(kind):
    if kind == 'messages':
        data = sse(*messages())
    elif kind == 'responses':
        data = sse({'type': 'response.output_text.delta', 'delta': '中文 answer'},
                   {'type': 'response.completed', 'response': payload(kind, text='中文 answer', calls=True)})
    else:
        data = b'\n'.join(json.dumps(e, ensure_ascii=False).encode() for e in [
            {'message': {'role': 'assistant', 'content': '中文 '}, 'done': False},
            {'message': {'role': 'assistant', 'content': 'answer'}, 'done': False},
            {**payload(kind, calls=True), 'message': {'tool_calls': payload(kind, calls=True)['message']['tool_calls']}}]) + b'\n'
    seen = []
    with patch('urllib.request.urlopen', return_value=Response(data, 'application/x-ndjson' if kind == 'ollama' else 'text/event-stream')) as post:
        result = client(kind).complete(request(), on_text=seen.append)
    assert ''.join(seen) == '中文 answer'
    assert result.tool_calls and result.finish_reason == 'tool_calls'
    assert json.loads(post.call_args.args[0].data)['stream'] is True
    if kind == 'messages':
        assert result.provider_blocks[1] == {'type': 'thinking', 'thinking': 'PRIVATE', 'signature': 'SIGNED'}
        assert result.tool_calls[0].arguments == {'path': 'README.md'}
        assert result.usage['input_tokens'] == 9 and result.usage['output_tokens'] == 7


@pytest.mark.parametrize('kind', ['messages', 'responses', 'ollama'])
def test_json_fallback_does_not_fake_preview(kind):
    seen = []
    with patch('urllib.request.urlopen', return_value=Response(json.dumps(payload(kind)).encode(), 'application/json')):
        assert client(kind).complete(request(), on_text=seen.append).text == 'done'
    assert not seen


@pytest.mark.parametrize('events', [messages()[:-1], messages()[:-3] + [{'type': 'message_stop'}],
    messages()[:3] + [{'type': 'message_stop'}], messages()[:2] + [messages()[1]]])
def test_incomplete_or_duplicate_stream_is_rejected(events):
    with pytest.raises(ModelProtocolError):
        consume(io.BytesIO(sse(*events)), 'messages', lambda _: None)


def test_sse_multiline_and_network_limits():
    result = consume(io.BytesIO(b'data: {"type":"response.completed",\ndata: "response":{"output":[],"status":"completed"}}\n\n'), 'responses', lambda _: None)
    assert result['status'] == 'completed'
    with pytest.raises(ModelProtocolError, match='size limit'):
        consume(io.BytesIO(b'data: ' + b'x' * (256 * 1024)), 'responses', lambda _: None)


@pytest.mark.parametrize('status,category', [(401, 'authentication'), (403, 'authentication'), (429, 'rate_limit'), (0, 'network'), (500, 'service')])
def test_request_error_categories(status, category):
    assert ModelRequestError(status).category == category


def test_stream_failure_never_retries_or_executes_partial_tools(tmp_path):
    from test_agent_loop import build_agent
    agent = build_agent(tmp_path, [])
    agent.model_client = client('messages')
    data = sse(*messages()[:-1])
    with patch('urllib.request.urlopen', return_value=Response(data)) as post, pytest.raises(ModelProtocolError):
        agent.ask('read')
    assert post.call_count == 1
    assert agent.current_run_state.tool_steps == 0
    assert agent.current_run_state.status == 'failed'


def test_preview_cross_chunk_redaction_size_and_cancel():
    secret = 'sk-private-credential-12345678'
    records = []
    agent = SimpleNamespace(cancel_event=threading.Event(), detected_secret_env_items=lambda: [('KEY', secret)],
                            redact_text=lambda text: text.replace(secret, '[REDACTED]'),
                            emit_trace=lambda state, kind, value: records.append(value))
    preview = Preview(agent, SimpleNamespace(attempts=2))
    for char in 'prefix ' + secret + ' suffix':
        preview.feed(char)
        preview.flush()
    preview.flush(final=True)
    assert ''.join(r['text'] for r in records) == 'prefix [REDACTED] suffix'
    assert all(r['attempt'] == 2 for r in records)
    preview = Preview(agent, SimpleNamespace(attempts=3))
    for _ in range(20):
        preview.feed('文' * 10000)
    preview.flush(final=True)
    large = [r for r in records if r['attempt'] == 3]
    assert sum(len(r['text']) for r in large) == 16384
    assert large[-1]['truncated']
    assert all(len(json.dumps(r, ensure_ascii=False).encode()) < 65536 for r in large)
    agent.cancel_event.set()
    with pytest.raises(KeyboardInterrupt):
        preview.feed('stop')


def test_explicit_unsupported_stream_falls_back_before_any_deltas():
    import urllib.error
    rejected = urllib.error.HTTPError('https://example.test/v1/messages', 400, 'bad', {},
        io.BytesIO(b'{"error":{"param":"stream","code":"unsupported_parameter"}}'))
    seen = []
    with patch('urllib.request.urlopen', side_effect=[rejected, Response(json.dumps(payload('messages')).encode(), 'application/json')]) as post:
        result = client('messages').complete(request(), on_text=seen.append)
    assert result.text == 'done' and not seen and post.call_count == 2
    assert json.loads(post.call_args.args[0].data)['stream'] is False
