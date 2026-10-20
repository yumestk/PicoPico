"""Tool facts and their bounded text representation."""

import json
from dataclasses import asdict, dataclass, field

from .workspace import clip


@dataclass
class ToolExecutionResult:
    data: dict = field(default_factory=dict)
    status: str = "ok"
    error_code: str = ""
    message: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def content(self):
        if self.message:
            return clip(f"error: {self.message}")
        if "exit_code" in self.data:
            return clip(
                f"exit_code: {self.data['exit_code']}\nstdout:\n{self.data['stdout'].strip() or '(empty)'}"
                f"\nstderr:\n{self.data['stderr'].strip() or '(empty)'}"
            )
        return clip(self.data.get("text", json.dumps(self.data, ensure_ascii=False)))

    def to_dict(self):
        return asdict(self)
