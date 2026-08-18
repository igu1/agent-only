# webchat reply store - bridges AI replies to the polling widget. Durable
# copies still flow to the backend via save_message.
#
# Two modes:
#   * in-memory (default) - single process, exactly the original behavior
#   * Redis (QUEUE_MODE=redis) - SaaS Phase 3: replies are written by the
#     WORKER process and polled from the GATEWAY process, so the outbox must
#     live in shared state
import json
import os
import threading
import time

_TTL_SECONDS = 1800        # drop sessions idle for 30 min
_TYPING_SECONDS = 15       # typing flag auto-expires
_MAX_MESSAGES = 200        # per-session cap, oldest trimmed


class WebchatStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def _session(self, session_id: str) -> dict:
        s = self._sessions.get(session_id)
        if s is None:
            s = {'messages': [], 'next_id': 1, 'last_seen': time.time(), 'typing_until': 0.0}
            self._sessions[session_id] = s
        return s

    def _cleanup(self) -> None:
        cutoff = time.time() - _TTL_SECONDS
        stale = [k for k, s in self._sessions.items() if s['last_seen'] < cutoff]
        for k in stale:
            del self._sessions[k]

    def append_message(self, session_id: str, text: str) -> None:
        with self._lock:
            self._cleanup()
            s = self._session(session_id)
            s['messages'].append({'id': s['next_id'], 'text': text, 'ts': time.time()})
            s['next_id'] += 1
            s['typing_until'] = 0.0
            if len(s['messages']) > _MAX_MESSAGES:
                del s['messages'][:len(s['messages']) - _MAX_MESSAGES]
            s['last_seen'] = time.time()

    def set_typing(self, session_id: str) -> None:
        with self._lock:
            s = self._session(session_id)
            s['typing_until'] = time.time() + _TYPING_SECONDS
            s['last_seen'] = time.time()

    def get_since(self, session_id: str, after_id: int = 0) -> dict:
        with self._lock:
            self._cleanup()
            s = self._session(session_id)
            s['last_seen'] = time.time()
            return {
                'messages': [m for m in s['messages'] if m['id'] > after_id],
                'typing': time.time() < s['typing_until'],
            }


class RedisWebchatStore:
    """Same contract as WebchatStore, backed by Redis - the reply outbox is
    shared between the gateway (polling) and the workers (writing)."""

    @staticmethod
    def _client():
        from infra.cache_utils import _get_redis_client
        return _get_redis_client()

    def append_message(self, session_id: str, text: str) -> None:
        r = self._client()
        mid = int(r.incr(f'wc:id:{session_id}'))
        r.rpush(f'wc:out:{session_id}',
                json.dumps({'id': mid, 'text': text, 'ts': time.time()}))
        r.ltrim(f'wc:out:{session_id}', -_MAX_MESSAGES, -1)
        r.expire(f'wc:out:{session_id}', _TTL_SECONDS)
        r.expire(f'wc:id:{session_id}', _TTL_SECONDS)
        r.delete(f'wc:typing:{session_id}')

    def set_typing(self, session_id: str) -> None:
        self._client().set(f'wc:typing:{session_id}', '1', ex=_TYPING_SECONDS)

    def get_since(self, session_id: str, after_id: int = 0) -> dict:
        r = self._client()
        raw = r.lrange(f'wc:out:{session_id}', 0, -1) or []
        r.expire(f'wc:out:{session_id}', _TTL_SECONDS)
        messages = []
        for item in raw:
            try:
                m = json.loads(item.decode('utf-8') if isinstance(item, bytes) else item)
                if int(m.get('id') or 0) > after_id:
                    messages.append(m)
            except Exception:
                continue
        return {'messages': messages, 'typing': bool(r.get(f'wc:typing:{session_id}'))}


def _make_store():
    if (os.getenv('QUEUE_MODE') or '').strip().lower() == 'redis':
        from infra.cache_utils import _get_redis_client
        if _get_redis_client() is not None:
            return RedisWebchatStore()
    return WebchatStore()


store = _make_store()
