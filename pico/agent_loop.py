"""Sequential native tool loop with explicit budgets and durable boundaries."""

import json
import time

from .checkpoint import CHECKPOINT_NONE_STATUS
from .events import PersistenceError
from .providers.types import ModelProtocolError
from .tool_result import ToolExecutionResult
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent
        self._finished = False
        self._seen_call_ids = set()

    def _finish(self, state, request, started, answer, reason='final_answer_returned', response=None):
        if self._finished:
            return state.final_answer
        self._finished = True
        agent = self.agent
        if reason == 'final_answer_returned' and agent.cancel_event.is_set():
            reason='cancelled'
            answer='Cancelled before finalization.'
        if reason == 'final_answer_returned':
            state.finish_success(answer)
        else:
            state.stop(reason, status='failed' if reason in {'model_error','persistence_error'} else 'cancelled' if reason=='cancelled' else 'stopped', final_answer=answer)
        try:
            if reason == 'final_answer_returned':
                agent.promote_durable_memory(request, answer)
            if agent.review is not None:
                agent.review.finish(cancelled=agent.cancel_event.is_set())
            message = response.message() if response is not None else {'role':'assistant','content':answer}
            agent.session['history'].append({**message, 'run_id': state.run_id, 'created_at':now()})
            agent.create_checkpoint(state, request, trigger=reason, persist=False)
            report = agent.redact_artifact(agent.build_report(state))
            report['run_duration_ms'] = int((time.monotonic() - started) * 1000)
            persisted = agent.store.finish(agent.session, state.to_dict(), report)
            state.status, state.stop_reason, state.final_answer = persisted['status'], persisted['stop_reason'], persisted['final_answer']
            from .events import notify
            terminal = agent.store.events(state.run_id, limit=1000000)[-1]
            notify(agent, terminal)
        except (OSError, ValueError, TypeError) as exc:
            state.stop('persistence_error', status='failed', final_answer='Could not persist completion; inspect the workspace before retrying.')
            try:
                agent.store.finish(agent.session, state.to_dict(), agent.redact_artifact(agent.build_report(state)))
            except (OSError, ValueError, TypeError):
                pass
            raise PersistenceError(state.final_answer) from exc
        return state.final_answer

    def _record_result(self, state, call, result):
        agent = self.agent
        content = json.dumps({'status': result.status, 'output': result.content, 'error_code': result.error_code}, ensure_ascii=False)
        payload = {'call_id':call.id,'name':call.name,'args':call.arguments,'result':clip(result.content,500),
                   **result.metadata,'tool_status':result.status,'tool_error_code':result.error_code}
        agent.record({'role':'tool','call_id':call.id,'name':call.name,'args':call.arguments,
                      'content':content,'status':result.status,'result':result.to_dict(),'created_at':now()},
                     event={'event':'tool_executed','payload':agent.redact_artifact(payload)})

    def run(self, user_message, state):
        agent = self.agent
        started = time.monotonic()
        state.resume_status = agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)
        agent.current_run_state = state
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        try:
            agent.store.begin_run(agent.session, state.to_dict(), {"approval":agent.approval_policy,"read_only":agent.read_only})
            agent.last_completion_metadata = {}
            agent.memory.set_task_summary(user_message)
            agent.record({'role': 'user', 'content': user_message, 'created_at': now()})
            agent.emit_trace(state, 'run_started', {'parent_run_id': state.parent_run_id, 'user_request': clip(user_message, 300)})
            agent.review = None
            if not state.parent_run_id:
                from .review import RunReview
                agent.review = RunReview(agent.store, agent.root, state.run_id)
                agent.review.start(agent.cancel_event)
            while state.attempts < max_attempts:
                if agent.cancel_event.is_set():
                    raise KeyboardInterrupt
                finalization = state.tool_steps >= agent.max_steps
                state.record_attempt()
                agent.store.update_run(state.to_dict())
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
                if getattr(agent.model_client, 'supports_streaming', False):
                    from .preview import Preview
                    preview = Preview(agent, state)
                    response = agent.model_client.complete(request, on_text=preview.feed)
                    preview.flush(final=True)
                else:
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
                    answer = response.text or f'Model stopped: {response.finish_reason}'
                    if response.finish_reason == 'length':
                        answer += '\n\n回答因输出预算耗尽而截断，尚未完成。可以明确发送继续，或在新会话提高预算。'
                    return self._finish(state, user_message, started, answer, 'model_error')
                if response.tool_calls:
                    ids = [call.id for call in response.tool_calls]
                    if any(not call_id for call_id in ids) or len(set(ids)) != len(ids) or self._seen_call_ids.intersection(ids):
                        raise ModelProtocolError('Missing or duplicate call ID')
                    self._seen_call_ids.update(ids)
                    agent.record({**response.message(), 'created_at': now()})
                    for call in response.tool_calls:
                        agent._active_call_id = call.id
                        agent._approval_id = None
                        if agent.cancel_event.is_set():
                            raise KeyboardInterrupt
                        agent.emit_trace(state, 'tool_started', {'call_id': call.id, 'name': call.name, 'args': call.arguments})
                        if finalization or state.tool_steps >= agent.max_steps:
                            result = ToolExecutionResult(status='rejected', error_code='budget_exceeded', message='Tool budget exhausted')
                        else:
                            state.record_tool(call.name)
                            result = agent.execute_tool(call.name, call.arguments)
                        self._record_result(state, call, result)
                        agent.store.update_run(state.to_dict())
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
            return self._finish(state, user_message, started, 'Cancelled. Inspect any interrupted tool before retrying.', 'cancelled')
        except Exception as exc:
            if self._finished:
                raise
            error = agent.redact_text(str(exc))
            reason = 'persistence_error' if isinstance(exc, OSError) else 'model_error'
            try:
                agent.emit_trace(state, 'model_failed' if reason == 'model_error' else 'persistence_failed',
                                 {'error': error, 'error_category': getattr(exc, 'category', 'unknown'),
                                  'completion_metadata': dict(agent.last_completion_metadata)})
            except OSError:
                reason = 'persistence_error'
            self._finish(state, user_message, started, f'Run failed: {error}', reason)
            raise
