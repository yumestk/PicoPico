"""Serializable run events; persistence is authoritative, observers are optional."""

import copy

from .workspace import now


class PersistenceError(OSError):
    """A durable boundary failed; do not perform more side effects."""


def emit(agent, task_state, name, payload):
    sequence = agent._event_sequences.get(task_state.run_id, 0) + 1
    event = {**agent.redact_artifact(payload or {}), 'event': name,
             'event_id': f'{task_state.run_id}:{sequence}', 'sequence': sequence,
             'run_id': task_state.run_id, 'session_id': agent.session['id'], 'created_at': now()}
    try:
        agent.run_store.append_trace(task_state, event)
    except (OSError, ValueError, TypeError) as exc:
        raise PersistenceError('Could not persist run event') from exc
    agent._event_sequences[task_state.run_id] = sequence
    if agent.event_handler is not None:
        try:
            agent.event_handler(copy.deepcopy(event))
        except Exception:
            # An observer cannot invalidate a recorded execution fact.
            pass
    return event
