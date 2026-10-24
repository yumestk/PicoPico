"""Bounded SSE/NDJSON readers; only a complete terminal response can run tools."""

import json
import urllib.error
import urllib.request

from .types import ModelProtocolError, ModelRequestError

MAX_BYTES = 16 * 1024 * 1024
MAX_LINE = 256 * 1024


def stream_post(url, payload, headers, timeout, on_text):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'pico/0.1', **headers}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get('Content-Type', '')
            if 'json' in content_type and 'ndjson' not in content_type:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ModelProtocolError('Model response exceeds size limit')
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get('error'):
                    raise ModelProtocolError('Provider returned an invalid response')
                return data
            return consume(response, 'ollama' if url.endswith('/api/chat') else 'responses' if url.endswith('/responses') else 'messages', on_text)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 422):
            try:
                error = json.loads(exc.read(65536)).get('error', {})
            except (ValueError, AttributeError):
                error = {}
            if isinstance(error, dict) and (error.get('param') == 'stream' or error.get('code') == 'unsupported_stream'):
                from .clients import _post
                return _post(url, {**payload, 'stream': False}, headers, timeout)
        raise ModelRequestError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelRequestError(0) from exc


def consume(response, protocol, on_text):
    data, blocks, arguments, pending = {}, {}, {}, []
    open_blocks = set()
    total, terminal = 0, False
    ollama_message = {'role': 'assistant', 'content': ''}

    def event(value):
        nonlocal data, terminal
        if not isinstance(value, dict) or value.get('error') or value.get('type') in {'error', 'response.failed'}:
            raise ModelProtocolError('Provider returned a failed stream event')
        kind = value.get('type', '')
        if protocol == 'responses':
            if kind == 'response.output_text.delta':
                on_text(value.get('delta', ''))
            elif kind == 'response.completed' or kind == 'response.incomplete':
                data = value.get('response', {})
                terminal = True
        elif protocol == 'ollama':
            message = value.get('message', {})
            text = message.get('content', '')
            on_text(text)
            ollama_message['content'] += text
            if message.get('tool_calls'):
                ollama_message.setdefault('tool_calls', []).extend(message['tool_calls'])
            if value.get('done') is True:
                data = {**value, 'message': ollama_message}
                terminal = True
        elif kind == 'message_start':
            data = value['message']
        elif kind == 'content_block_start':
            index = value['index']
            if index in blocks:
                raise ModelProtocolError('Duplicate stream block')
            blocks[index] = dict(value['content_block'])
            open_blocks.add(index)
        elif kind == 'content_block_delta':
            index, delta = value['index'], value['delta']
            if index not in open_blocks:
                raise ModelProtocolError('Missing stream block')
            block = blocks[index]
            if delta.get('type') == 'text_delta' and block.get('type') == 'text':
                text = delta.get('text', '')
                on_text(text)
                block['text'] = block.get('text', '') + text
            elif delta.get('type') == 'input_json_delta' and block.get('type') == 'tool_use':
                arguments[index] = arguments.get(index, '') + delta.get('partial_json', '')
            elif delta.get('type') == 'thinking_delta':
                block['thinking'] = block.get('thinking', '') + delta.get('thinking', '')
            elif delta.get('type') == 'signature_delta':
                block['signature'] = block.get('signature', '') + delta.get('signature', '')
        elif kind == 'content_block_stop':
            index = value['index']
            if index not in open_blocks:
                raise ModelProtocolError('Missing or closed stream block')
            open_blocks.remove(index)
            if index in arguments:
                blocks[index]['input'] = json.loads(arguments.pop(index))
        elif kind == 'message_delta':
            data.update(value.get('delta', {}))
            data['usage'] = {**data.get('usage', {}), **value.get('usage', {})}
        elif kind == 'message_stop':
            if arguments or open_blocks:
                raise ModelProtocolError('Incomplete tool arguments')
            data['content'] = [blocks[index] for index in sorted(blocks)]
            terminal = True

    while not terminal:
        raw = response.readline(MAX_LINE + 1)
        if not raw:
            break
        total += len(raw)
        if len(raw) > MAX_LINE or total > MAX_BYTES:
            raise ModelProtocolError('Model stream exceeds size limit')
        line = raw.decode('utf-8').rstrip('\r\n')
        if protocol == 'ollama':
            if line:
                event(json.loads(line))
        elif not line:
            if pending:
                value = '\n'.join(pending)
                pending.clear()
                if value != '[DONE]':
                    event(json.loads(value))
        elif line.startswith('data:'):
            pending.append(line[5:].lstrip(' '))
    if not terminal:
        raise ModelProtocolError('Model stream ended without a terminal response')
    return data
