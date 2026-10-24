"""Redacted, bounded public preview. Complete model replies remain authoritative."""

import time


class Preview:
    def __init__(self, agent, state):
        self.agent, self.state = agent, state
        self.pending = ''
        self.sent = 0
        self.truncated = False
        self.updated = time.monotonic()
        self.hold = max((len(value) for _, value in agent.detected_secret_env_items()), default=1) - 1

    def feed(self, text):
        if self.agent.cancel_event.is_set():
            raise KeyboardInterrupt
        if not isinstance(text, str):
            raise TypeError('Model text delta must be a string')
        if self.truncated:
            return
        self.pending += text
        if self.sent == 0 or len(self.pending) > self.hold + 4096 or time.monotonic() - self.updated >= 0.1:
            self.flush()

    def flush(self, final=False):
        if self.truncated:
            self.pending = ''
            return
        size = len(self.pending) if final else max(0, len(self.pending) - self.hold)
        if not size:
            return
        # Redact before cutting: even credentials split across network events stay private.
        redacted = self.agent.redact_text(self.pending)
        # Replacement may shorten the prefix; keep a conservative suffix after redaction.
        size = len(redacted) if final else max(0, len(redacted) - self.hold)
        chunk, self.pending = redacted[:size], redacted[size:]
        if not chunk:
            return
        room = 16384 - self.sent
        chunk = chunk[:room]
        self.sent += len(chunk)
        self.truncated = self.sent >= 16384
        self.agent.emit_trace(self.state, 'assistant_preview', {'attempt': self.state.attempts,
            'text': chunk, 'truncated': self.truncated})
        self.updated = time.monotonic()
