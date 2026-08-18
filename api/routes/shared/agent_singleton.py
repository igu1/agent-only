import time
import threading
from core.manager import create_agent
from infra.tenants import get_org

# SaaS Phase 1: agents are cached per (org, agent_id) - every org has an
# "agent 1", and each org's agent carries its own prompt/keys/knowledge
_agents: dict[tuple[str, int | None], object] = {}
_agents_ts: dict[tuple[str, int | None], float] = {}
_lock = threading.Lock()

_AGENT_TTL_SECONDS = 300


def get_agent(agent_id: int | None = None):
    key = (get_org(), agent_id)
    now = time.time()
    with _lock:
        if key in _agents and (now - _agents_ts.get(key, 0)) < _AGENT_TTL_SECONDS:
            return _agents[key]
        agent = create_agent(agent_id=agent_id)
        _agents[key] = agent
        _agents_ts[key] = now
        return agent


def clear_agent_cache(agent_id: int | None = None):
    with _lock:
        if agent_id is None:
            # full clear (any tenant) - safe: agents rebuild on demand
            _agents.clear()
            _agents_ts.clear()
        else:
            key = (get_org(), agent_id)
            _agents.pop(key, None)
            _agents_ts.pop(key, None)
