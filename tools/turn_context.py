# Which conversation the CURRENT turn belongs to, for tools that must know.
#
# Tools are built once per agent and shared by every chat it serves, so they
# cannot carry a conversation in their arguments without trusting the model to
# supply it - and the model is exactly what must not decide whose data a lookup
# reaches. The turn processor sets this before the agent runs; the value rides
# the same context propagation that already carries the tenant (org) into
# tools (anyio.to_thread copies the context into the worker thread).
import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnContext:
    lead_id: int | None = None
    channel_type: str | None = None


_current: contextvars.ContextVar[TurnContext] = contextvars.ContextVar(
    'turn_context', default=TurnContext())


def set_turn(*, lead_id: int | None, channel_type: str | None):
    return _current.set(TurnContext(lead_id=lead_id, channel_type=(channel_type or '').lower() or None))


def current_turn() -> TurnContext:
    return _current.get()
