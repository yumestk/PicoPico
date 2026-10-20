"""Sequential native tool loop with explicit budgets and durable boundaries."""

import json
import time

from .checkpoint import CHECKPOINT_NONE_STATUS
from .events import PersistenceError
from .providers.types import ModelProtocolError
from .task_state import TaskState
from .tool_result import ToolExecutionResult
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent
        self._finished = False

    def _finish(self, state, request, started, answer, reason='final_answer_returned', response=None):
        if self._finished:
            return state.final_answer
        self._finished = True
        agent = self.agent
        if reason == 'final_answer_returned':
            state.finish_success(answer)
        elif reason in {'model_error', 'persistence_error'}:
            state.stop(reason, status='failed', final_answer=answer)
        else:
            state.stop(reason, status='cancelled' if reason == 'cancelled' else 'stopped', final_answer=answer)
        try:
            if reason == 'final_answer_returned':
                agent.promote_durable_memory(request, answer)
            message = response.message() if response is not None else {'role': 'assistant', 'content': answer}
            agent.record({**message, 'created_at': now()})
            checkpoint = agent.create_checkpoint(state, request, trigger=reason)
            agent.run_store.write_task_state(state)
            agent.run_store.write_report(state, agent.redact_artifact(agent.build_report(state)))
            agent.emit_trace(state, 'checkpoint_created', {'checkpoint_id': checkpoint['checkpoint_id'], 'trigger': reason})
            agent.emit_trace(state, 'run_finished', {'status': state.status, 'stop_reason': reason, 'final_answer': answer,
                                                   'run_duration_ms': int((time.monotonic() - started) * 1000)})
        except (OSError, ValueError, TypeError) as exc:
            state.stop('persistence_error', status='failed', final_answer='Could not persist run completion; inspect the workspace before retrying.')
            for save in (lambda: agent.session_store.save(agent.session),
                         lambda: agent.run_store.write_task_state(state),
                         lambda: agent.run_store.write_report(state, agent.redact_artifact(agent.build_report(state)))):
                try:
                    save()
                except (OSError, ValueError, TypeError):
                    pass
            raise PersistenceError(state.final_answer) from exc
        return answer

    def _cancel_pending(self, state):
        history = self.agent.session['history']
        for index in reversed(range(len(history))):
            message = history[index]
            if message['role'] == 'assistant' and message.get('tool_calls'):
                done = {m.get('call_id') for m in history[index + 1:] if m['role'] == 'tool'}
                from .providers.types import ToolCall
                for item in message['tool_calls']:
                    if item['id'] not in done:
                        result = ToolExecutionResult(status='cancelled', error_code='tool_cancelled',
                            message='Run cancelled; interrupted execution outcome must be inspected before retrying.')
                        self._record_result(state, ToolCall(**item), result)
                return
            if message['role'] == 'user':
                return

    def _record_result(self, state, call, result):
        agent = self.agent
        content = json.dumps({'status': result.status, 'output': result.content, 'error_code': result.error_code}, ensure_ascii=False)
        agent.record({'role': 'tool', 'call_id': call.id, 'name': call.name, 'args': call.arguments,
                      'content': content, 'status': result.status, 'result': result.to_dict(), 'created_at': now()})
        agent.emit_trace(state, 'tool_executed', {'call_id': call.id, 'name': call.name, 'args': call.arguments,
                                                'result': clip(result.content, 500), **result.metadata,
                                                'tool_status': result.status, 'tool_error_code': result.error_code})

    def run(self, user_message):
        agent = self.agent
        started = time.monotonic()
        state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        state.resume_status = agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)
        agent.current_task_state = state
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        try:
            agent.current_run_dir = agent.run_store.start_run(state)
            agent.last_completion_metadata = {}
            agent.memory.set_task_summary(user_message)
            agent.record({'role': 'user', 'content': user_message, 'created_at': now()})
            agent.emit_trace(state, 'run_started', {'task_id': state.task_id, 'user_request': clip(user_message, 300)})
            while state.attempts < max_attempts:
                if agent.cancel_event.is_set():
                    raise KeyboardInterrupt
                finalization = state.tool_steps >= agent.max_steps
                state.record_attempt()
                agent.run_store.write_task_state(state)
                request, metadata = agent._build_prompt_and_metadata(user_message, allow_tools=not finalization)
                if finalization:
                    metadata['finalization'] = True
                agent.last_prompt_metadata = metadata
                agent.emit_trace(state, 'prompt_built', {'prompt_metadata': metadata})
                status = metadata.get('resume_status')
                trigger = 'freshness_mismatch' if status == 'partial-stale' else 'workspace_mismatch' if status == 'workspace-mismatch' else 'context_reduction' if metadata.get('budget_reductions') else None
                if status == 'workspace-mismatch':
                    agent.emit_trace(state, 'runtime_identity_mismatch', {'fields': metadata.get('runtime_identity_mismatch_fields', [])})
                if trigger:
                    checkpoint = agent.create_checkpoint(state, user_message, trigger)
                    agent.emit_trace(state, 'checkpoint_created', {'checkpoint_id': checkpoint['checkpoint_id'], 'trigger': trigger})
                purpose = 'finalization' if finalization else 'action'
                agent.emit_trace(state, 'model_requested', {'attempts': state.attempts, 'tool_steps': state.tool_steps, 'purpose': purpose})
                response = agent.model_client.complete(request)
                if agent.cancel_event.is_set():
                    raise KeyboardInterrupt
                agent.last_completion_metadata = response.metadata
                agent.last_prompt_metadata.update(response.metadata)
                agent.emit_trace(state, 'model_completed', {'completion_metadata': response.metadata, 'purpose': purpose})
                if response.finish_reason not in {'stop', 'tool_calls'}:
                    # Do not execute even syntactically valid calls from a truncated response.
                    if response.tool_calls:
                        agent.record({**response.message(), 'created_at': now()})
                        for call in response.tool_calls:
                            self._record_result(state, call, ToolExecutionResult(status='rejected', error_code='incomplete_model_response', message=response.finish_reason))
                    return self._finish(state, user_message, started, response.text or f'Model stopped: {response.finish_reason}', 'model_error')
                if response.tool_calls:
                    ids = [call.id for call in response.tool_calls]
                    if any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
                        raise ModelProtocolError('Missing or duplicate call ID')
                    agent.record({**response.message(), 'created_at': now()})
                    for call in response.tool_calls:
                        agent._active_call_id = call.id
                        if agent.cancel_event.is_set():
                            raise KeyboardInterrupt
                        agent.emit_trace(state, 'tool_started', {'call_id': call.id, 'name': call.name, 'args': call.arguments})
                        if finalization or state.tool_steps >= agent.max_steps:
                            result = ToolExecutionResult(status='rejected', error_code='budget_exceeded', message='Tool budget exhausted')
                        else:
                            state.record_tool(call.name)
                            result = agent.execute_tool(call.name, call.arguments)
                        self._record_result(state, call, result)
                        agent.run_store.write_task_state(state)
                    if agent.cancel_event.is_set():
                        raise KeyboardInterrupt
                    checkpoint = agent.create_checkpoint(state, user_message, trigger='tool_executed')
                    agent.emit_trace(state, 'checkpoint_created', {'checkpoint_id': checkpoint['checkpoint_id'], 'trigger': 'tool_executed'})
                    if finalization:
                        return self._finish(state, user_message, started, 'Stopped: model called tools during finalization.', 'step_limit_reached')
                    continue
                if response.text.strip():
                    return self._finish(state, user_message, started, response.text.strip(), response=response)
                agent.record({'role': 'assistant', 'content': 'Runtime notice: model returned an empty response. Return a non-empty answer or use a tool.', 'created_at': now()})
                if finalization:
                    break
            return self._finish(state, user_message, started, 'Stopped after the response budget was exhausted.', 'retry_limit_reached')
        except KeyboardInterrupt:
            agent.cancel_event.set()
            if self._finished:
                return state.final_answer
            try:
                self._cancel_pending(state)
            except OSError:
                return self._finish(state, user_message, started, 'Could not persist cancellation.', 'persistence_error')
            return self._finish(state, user_message, started, 'Cancelled. Inspect any interrupted tool before retrying.', 'cancelled')
        except Exception as exc:
            if self._finished:
                raise
            error = agent.redact_text(str(exc))
            reason = 'persistence_error' if isinstance(exc, OSError) else 'model_error'
            try:
                agent.emit_trace(state, 'model_failed' if reason == 'model_error' else 'persistence_failed',
                                 {'error': error, 'completion_metadata': dict(agent.last_completion_metadata)})
            except OSError:
                reason = 'persistence_error'
            self._finish(state, user_message, started, f'Run failed: {error}', reason)
            raise
