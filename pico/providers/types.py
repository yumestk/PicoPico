"""Messages shared by adapters, the agent loop and offline evaluations."""

import json
import uuid
from dataclasses import asdict, dataclass, field


class ModelProtocolError(ValueError):
    """The provider did not produce a complete, replayable response."""


@dataclass
class ToolCall:
    name: str
    arguments: object
    id: str = field(default_factory=lambda: 'call_' + uuid.uuid4().hex)


@dataclass
class ModelResponse:
    text: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = 'stop'
    usage: dict = field(default_factory=dict)
    provider_blocks: list = field(default_factory=list)
    protocol: str = 'fake'

    def message(self):
        return {'role': 'assistant', 'content': self.text,
                'tool_calls': [asdict(call) for call in self.tool_calls],
                'provider_blocks': self.provider_blocks, 'protocol': self.protocol}

    @property
    def metadata(self):
        return {**self.usage, 'finish_reason': self.finish_reason, 'protocol': self.protocol}


@dataclass
class ModelRequest:
    system: str
    messages: list[dict]
    tools: list[dict]
    max_new_tokens: int
    prompt_cache_key: str = ''

    def text(self):
        """Readable request view for metrics; never used as a model transcript."""
        last_user = max((i for i, m in enumerate(self.messages) if m['role'] == 'user'), default=-1)
        current = self.messages[last_user]['content'] if last_user >= 0 else ''
        history = [m for i, m in enumerate(self.messages) if i != last_user]
        transcript = '\n'.join(f"[{m['role']}] {m.get('content', '')}" for m in history)
        return self.system + '\n\nTranscript:\n' + transcript + '\n\nCurrent user request:\n' + current


    def char_count(self):
        return len(self.system) + len(json.dumps(self.messages, ensure_ascii=False)) + len(json.dumps(self.tools, ensure_ascii=False))
