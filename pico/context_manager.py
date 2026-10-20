"""Bounded native context: original history is never rewritten or partially replayed."""

import copy
import json
from dataclasses import dataclass

from .providers.types import ModelProtocolError, ModelRequest

DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {"prefix": 3600, "memory": 1600, "relevant_memory": 1200, "history": 5200}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
RELEVANT_MEMORY_LIMIT = 3


class ContextBudgetError(ValueError):
    pass


def _tail_clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)] + ("..." if limit >= 3 else "")


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict


def message_groups(history):
    groups = []
    index = 0
    while index < len(history):
        message = history[index]
        if message.get("role") not in {"user", "assistant", "tool"}:
            raise ModelProtocolError("Unsupported history message role")
        if message["role"] == "tool":
            raise ModelProtocolError("Orphan tool result in history")
        group = [message]
        calls = message.get("tool_calls", [])
        if calls:
            ids = [call["id"] for call in calls]
            if len(ids) != len(set(ids)):
                raise ModelProtocolError("Duplicate call ID in history")
            results = history[index + 1:index + 1 + len(calls)]
            if len(results) != len(calls) or any(m["role"] != "tool" for m in results):
                raise ModelProtocolError("Unresolved tool calls; inspect workspace before continuing")
            if [m.get("call_id") for m in results] != ids:
                raise ModelProtocolError("Tool result IDs do not match assistant calls")
            group.extend(results)
        groups.append(group)
        index += len(group)
    return groups


class ContextManager:
    def __init__(self, agent, total_budget=DEFAULT_TOTAL_BUDGET, section_budgets=None,
                 section_floors=None, reduction_order=None):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = {**DEFAULT_SECTION_BUDGETS, **(section_budgets or {})}
        self.section_floors = {key: 20 for key in self.section_budgets}
        self.section_floors.update(section_floors or {})
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message, allow_tools=True):
        agent = self.agent
        reduction = agent.feature_enabled("context_reduction")
        memory_on = agent.feature_enabled("memory")
        notes = agent.memory.retrieval_candidates(user_message, limit=3) if memory_on and agent.feature_enabled("relevant_memory") else []
        checkpoint = agent.render_checkpoint_text()
        prefix = agent.prefix + ("\n\n" + checkpoint if checkpoint else "")
        if not allow_tools:
            prefix += "\nTool budget exhausted. Summarize evidence without further tools."
        memory = agent.memory_text() if memory_on else "Memory:\n- disabled"
        texts = {"prefix": prefix, "memory": memory}
        budgets = dict(self.section_budgets)
        history = copy.deepcopy(agent.session["history"])
        # During a tool round the current user message already exists further back.
        if not any(m.get("role") == "user" and m.get("content") == user_message for m in history):
            history.append({"role": "user", "content": user_message})
        groups = message_groups(history)
        groups = [[{k: v for k, v in m.items() if k in {"role", "content", "tool_calls", "provider_blocks", "protocol", "call_id", "name", "status"}} for m in group] for group in groups]
        current = max(i for i, group in enumerate(groups) if group[0]["role"] == "user" and group[0]["content"] == user_message)
        required = {current, len(groups) - 1}
        tools = [{"name": name, "description": tool["description"], "parameters": tool["schema"]}
                 for name, tool in agent.tools.items()] if allow_tools else []
        reductions = []
        while True:
            relevant = self._render_relevant_memory(notes, budgets["relevant_memory"] if reduction else 10**9)
            rendered = {key: _tail_clip(value, budgets[key]) if reduction else value for key, value in texts.items()}
            rendered["relevant_memory"] = relevant.rendered
            system = "\n\n".join(rendered.values())
            selected = set(required)
            selected_chars = sum(len(json.dumps(groups[i], ensure_ascii=False)) for i in selected)
            for i in reversed(range(len(groups))):
                size = len(json.dumps(groups[i], ensure_ascii=False))
                if i not in selected and (not reduction or selected_chars + size <= budgets["history"]):
                    selected.add(i)
                    selected_chars += size
            messages = [m for i, group in enumerate(groups) if i in selected for m in group]
            request = ModelRequest(system, messages, tools, agent.max_new_tokens, agent.prefix_state.hash)
            if not reduction or request.char_count() <= self.total_budget:
                break
            for section in self.reduction_order:
                floor = self.section_floors.get(section, 20)
                if budgets[section] > floor:
                    previous = budgets[section]
                    budgets[section] = max(floor, budgets[section] - max(1, request.char_count() - self.total_budget))
                    reductions.append({"section": section, "before_chars": previous, "after_chars": budgets[section]})
                    break
            else:
                raise ContextBudgetError("Required request, tool schemas and latest complete tool round exceed context character budget")
        sections = {key: {"raw_chars": len(texts.get(key, relevant.raw)), "rendered_chars": len(value),
                          "budget_chars": budgets[key]} for key, value in rendered.items()}
        history_chars = len(json.dumps(messages, ensure_ascii=False))
        sections["history"] = {"raw_chars": len(json.dumps(history)), "rendered_chars": history_chars, "budget_chars": budgets["history"]}
        sections["current_request"] = {"raw_chars": len(user_message), "rendered_chars": len(user_message), "budget_chars": None}
        metadata = {"budget_unit": "characters", "prompt_chars": request.char_count(), "prompt_budget_chars": self.total_budget,
                    "prompt_over_budget": request.char_count() > self.total_budget, "tool_schema_chars": len(json.dumps(tools, ensure_ascii=False)),
                    "section_order": list(SECTION_ORDER), "section_budgets": {**budgets, "current_request": None},
                    "sections": sections, "budget_reductions": reductions, "reduction_order": list(self.reduction_order),
                    "current_request": {"text": user_message, "raw_chars": len(user_message), "rendered_chars": len(user_message)},
                    "relevant_memory": {"limit": 3, "selected_count": len(notes), "selected_notes": [n["text"] for n in notes],
                        "selected_sources": [n.get("source", "") for n in notes], "selected_kinds": [n.get("kind", "episodic") for n in notes],
                        "selected_durable_count": sum(n.get("kind") == "durable" for n in notes),
                        "raw_chars": len(relevant.raw), "rendered_chars": len(relevant.rendered), **relevant.details},
                    "history": {"raw_chars": len(json.dumps(history)), "rendered_chars": history_chars,
                        "dropped_groups": len(groups) - len(selected)}}
        return request, metadata

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else f"{header}\n- none"
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

