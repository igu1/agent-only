import time
import threading
from core.manager import create_agent

_agents: dict[int | None, object] = {}
_agents_ts: dict[int | None, float] = {}
_lock = threading.Lock()

_AGENT_TTL_SECONDS = 300


def get_agent(agent_id: int | None = None):
    now = time.time()
    with _lock:
        if agent_id in _agents and (now - _agents_ts.get(agent_id, 0)) < _AGENT_TTL_SECONDS:
            return _agents[agent_id]
        agent = create_agent(agent_id=agent_id)
        _agents[agent_id] = agent
        _agents_ts[agent_id] = now
        return agent


def clear_agent_cache(agent_id: int | None = None):
    with _lock:
        if agent_id is None:
            _agents.clear()
            _agents_ts.clear()
        else:
            _agents.pop(agent_id, None)
            _agents_ts.pop(agent_id, None)
