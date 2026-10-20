"""Native tool protocols. Adapters preserve the blocks required for replay."""

import json
import time
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

from .types import ModelProtocolError, ModelRequest, ModelResponse, ToolCall


def _blocks(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ModelProtocolError("Provider content blocks must be objects in an array")
    return value


def _text(value):
    if not isinstance(value, str):
        raise ModelProtocolError("Provider text must be a string")
    return value


def _arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value  # The executor rejects it and returns an error for this call ID.
    return value


def _usage(data):
    usage = data.get('usage') or {}
    if not isinstance(usage, dict):
        raise ModelProtocolError('Provider usage must be an object')
    details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
    if not isinstance(details, dict):
        raise ModelProtocolError('Provider token details must be an object')
    cached = int(details.get('cached_tokens') or usage.get('cache_read_input_tokens') or 0)
    return {'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens')),
            'output_tokens': usage.get('output_tokens', usage.get('completion_tokens')),
            'cached_tokens': cached, 'cache_hit': cached > 0,
            'total_tokens': usage.get('total_tokens')}


def _post(url, payload, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={'Content-Type': 'application/json', 'User-Agent': 'pico/0.1', **headers}, method='POST')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode('utf-8')
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            if exc.code >= 500 and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f'Model request failed with HTTP {exc.code}: {body}') from exc
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(f'Could not reach model endpoint: {url}') from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        # Some Responses gateways send SSE even for a nonstreaming request.
        data = None
        for line in body.splitlines():
            if line.startswith('data:') and line[5:].strip() != '[DONE]':
                try:
                    event = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    raise ModelProtocolError('SSE event must be an object')
                if event.get('type') == 'response.completed':
                    data = event.get('response')
                if event.get('type') in {'response.failed', 'error'}:
                    raise ModelProtocolError('Provider returned a failed event') from exc
        if data is None:
            raise ModelProtocolError('Provider returned no complete JSON response') from exc
    if not isinstance(data, dict):
        raise ModelProtocolError('Provider response must be an object')
    if data.get('error'):
        raise RuntimeError(f'Model error: {data["error"]}')
    return data


def _validate_response(response):
    _text(response.text)
    ids = [call.id for call in response.tool_calls]
    if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(ids) != len(set(ids)):
        raise ModelProtocolError('Missing or duplicate tool call ID')
    if not all(isinstance(call.name, str) and call.name for call in response.tool_calls):
        raise ModelProtocolError('Tool call is missing its name')
    return response


class FakeModelClient:
    protocol = 'fake'
    model = 'scripted-deterministic'
    supports_prompt_cache = False

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    def complete(self, request):
        if not isinstance(request, ModelRequest):
            raise TypeError('Expected ModelRequest')
        self.requests.append(request)
        if not self.outputs:
            raise RuntimeError('fake model ran out of outputs')
        result = self.outputs.pop(0)
        if not isinstance(result, ModelResponse):
            raise TypeError('FakeModelClient outputs must be ModelResponse objects')
        return _validate_response(result)


class OllamaModelClient:
    protocol = 'ollama-chat'
    supports_prompt_cache = False

    def __init__(self, model, host, temperature, top_p, timeout):
        self.model, self.host = model, host.rstrip('/')
        self.temperature, self.top_p, self.timeout = temperature, top_p, timeout

    def complete(self, request):
        messages = [{'role': 'system', 'content': request.system}]
        for message in request.messages:
            if message['role'] == 'assistant':
                messages.append(message.get('provider_blocks', [])[0] if message.get('provider_blocks') else
                                {'role': 'assistant', 'content': message['content'], 'tool_calls': [
                                    {'function': {'name': c['name'], 'arguments': c['arguments']}}
                                    for c in _blocks(message.get('tool_calls', []))]})
            elif message['role'] == 'tool':
                messages.append({'role': 'tool', 'tool_name': message['name'], 'content': message['content']})
            else:
                messages.append({'role': message['role'], 'content': message['content']})
        payload = {'model': self.model, 'messages': messages, 'stream': False, 'think': False,
                   'tools': [{'type': 'function', 'function': t} for t in request.tools],
                   'options': {'num_predict': request.max_new_tokens, 'temperature': self.temperature, 'top_p': self.top_p}}
        data = _post(self.host + '/api/chat', payload, {}, self.timeout)
        message = data.get('message')
        if not isinstance(message, dict) or not data.get('done', False):
            raise ModelProtocolError('Ollama returned an incomplete chat response')
        calls = []
        for call in _blocks(message.get('tool_calls', [])):
            function = call.get('function')
            if not isinstance(function, dict):
                raise ModelProtocolError('Ollama tool call must contain a function object')
            calls.append(ToolCall(function.get('name'), _arguments(function.get('arguments'))))
        reason = 'length' if data.get('done_reason') == 'length' else 'tool_calls' if calls else 'stop'
        return _validate_response(ModelResponse(text=message.get('content', ''), tool_calls=calls, finish_reason=reason,
                             usage={'input_tokens': data.get('prompt_eval_count'), 'output_tokens': data.get('eval_count')},
                             provider_blocks=[message], protocol=self.protocol))


class OpenAICompatibleModelClient:
    protocol = 'openai-responses'

    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model, self.base_url = model, base_url.rstrip('/')
        self.api_key, self.temperature, self.timeout = api_key or "", temperature, timeout
        self.supports_prompt_cache = any(host in self.base_url for host in ('openai.com', 'right.codes'))

    def complete(self, request):
        items = []
        for message in request.messages:
            if message['role'] == 'assistant':
                if message.get('provider_blocks'):
                    items.extend(message['provider_blocks'])
                else:
                    if message['content']:
                        items.append({'role': 'assistant', 'content': message['content']})
                    items.extend({'type': 'function_call', 'call_id': c['id'], 'name': c['name'],
                                  'arguments': json.dumps(c['arguments'])} for c in message.get('tool_calls', []))
            elif message['role'] == 'tool':
                items.append({'type': 'function_call_output', 'call_id': message['call_id'], 'output': message['content']})
            else:
                items.append({'role': message['role'], 'content': message['content']})
        payload = {'model': self.model, 'instructions': request.system, 'input': items,
                   'tools': [{'type': 'function', **t, 'strict': False} for t in request.tools],
                   'tool_choice': 'auto' if request.tools else 'none', 'store': False,
                   'include': ['reasoning.encrypted_content'], 'max_output_tokens': request.max_new_tokens, 'stream': False}
        if self.temperature is not None:
            payload['temperature'] = self.temperature
        if self.supports_prompt_cache and request.prompt_cache_key:
            payload['prompt_cache_key'] = request.prompt_cache_key
            payload['prompt_cache_retention'] = 'in_memory'
        data = _post(self.base_url + '/responses', payload, {'Authorization': 'Bearer ' + self.api_key} if self.api_key else {}, self.timeout)
        blocks = _blocks(data.get('output', []))
        calls, texts, refused = [], [], False
        for block in blocks:
            if block.get('type') == 'function_call':
                calls.append(ToolCall(block.get('name'), _arguments(block.get('arguments')), block.get('call_id')))
            elif block.get('type') == 'message':
                for part in _blocks(block.get('content', [])):
                    if part.get('type') == 'output_text':
                        texts.append(_text(part.get('text', '')))
                    elif part.get('type') == 'refusal':
                        texts.append(_text(part.get('refusal', '')))
                        refused = True
        status = data.get('status', 'completed')
        reason = 'refusal' if refused else 'tool_calls' if calls else 'stop'
        if status != 'completed':
            reason = 'length' if data.get('incomplete_details', {}).get('reason') == 'max_output_tokens' else status
        return _validate_response(ModelResponse(text='\n'.join(texts), tool_calls=calls, finish_reason=reason,
                                               usage={**_usage(data), 'prompt_cache_supported': self.supports_prompt_cache},
                                               provider_blocks=blocks, protocol=self.protocol))


class AnthropicCompatibleModelClient:
    protocol = 'anthropic-messages'
    supports_prompt_cache = False

    def __init__(self, model, base_url, api_key, temperature, timeout, thinking=None):
        self.model = model
        self.base_url = base_url.rstrip('/')
        if not self.base_url.endswith('/v1'):
            self.base_url += '/v1'
        self.api_key, self.temperature, self.timeout = api_key or "", temperature, timeout
        self.thinking = dict(thinking) if thinking else None

    def complete(self, request):
        messages = []
        for message in request.messages:
            if message['role'] == 'assistant':
                blocks = message.get('provider_blocks') or (
                    ([{'type': 'text', 'text': message['content']}] if message['content'] else []) +
                    [{'type': 'tool_use', 'id': c['id'], 'name': c['name'], 'input': c['arguments']}
                     for c in _blocks(message.get('tool_calls', []))])
                messages.append({'role': 'assistant', 'content': blocks})
            elif message['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': message['call_id'], 'content': message['content'],
                         'is_error': message.get('status', 'ok') != 'ok'}
                if messages and messages[-1]['role'] == 'user' and isinstance(messages[-1]['content'], list):
                    messages[-1]['content'].append(block)
                else:
                    messages.append({'role': 'user', 'content': [block]})
            else:
                messages.append({'role': message['role'], 'content': message['content']})
        payload = {'model': self.model, 'system': request.system, 'messages': messages,
                   'tools': [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']} for t in request.tools],
                   'tool_choice': {'type': 'auto' if request.tools else 'none'},
                   'max_tokens': request.max_new_tokens, 'stream': False}
        if self.temperature is not None:
            payload['temperature'] = self.temperature
        if self.thinking is not None:
            payload['thinking'] = self.thinking
        data = _post(self.base_url + '/messages', payload, {'x-api-key': self.api_key, 'anthropic-version': '2023-06-01'}, self.timeout)
        blocks = _blocks(data.get('content', []))
        texts = [_text(b.get('text', '')) for b in blocks if b.get('type') == 'text']
        calls = [ToolCall(b.get('name'), b.get('input'), b.get('id')) for b in blocks if b.get('type') == 'tool_use']
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length',
                  'refusal': 'refusal', 'stop_sequence': 'stop'}.get(data.get('stop_reason'), 'protocol_error')
        return _validate_response(ModelResponse(text='\n'.join(texts), tool_calls=calls, finish_reason=reason,
                                               usage={**_usage(data), 'stop_reason': data.get('stop_reason'),
                                                      'content_block_types': [b.get('type') for b in blocks]},
                                               provider_blocks=blocks, protocol=self.protocol))
