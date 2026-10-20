"""Persisted events are authoritative; observers receive isolated redacted copies."""

import copy
import logging


class PersistenceError(OSError):
    """Do not continue side effects after a durable boundary fails."""


def notify(agent, event):
    if agent.event_handler is not None:
        try:
            agent.event_handler(copy.deepcopy(event))
        except Exception as exc:  # noqa: BLE001 - observers cannot invalidate a committed fact
            logging.getLogger(__name__).warning("Event observer failed (%s)", type(exc).__name__)
    return event


def emit(agent, state, name, payload):
    try:
        event = agent.store.emit(state.run_id, name, agent.redact_artifact(payload or {}), state.to_dict())
    except (OSError, ValueError, TypeError) as exc:
        raise PersistenceError('Could not persist run event') from exc
    return notify(agent, event)
