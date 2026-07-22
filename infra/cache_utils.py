from typing import Any, Callable
import hashlib
import time
import threading
import os
import json

try:
    import redis  # type: ignore
except Exception:
    redis = None

def _int_env(name: str, default: int) -> int:
    try:
        raw = (os.getenv(name) or '').strip()
        return int(raw) if raw else default
    except Exception:
        return default

CACHE_TTL = _int_env('CRONO_AGENT_CACHE_TTL', 60)

_KEY_PREFIX = 'crono_agent_cache:'

class SimpleCache:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            if key in self._cache:
                value, expiry = self._cache[key]
                if time.time() < expiry:
                    return value
                del self._cache[key]
        return None

    def set(self, key: str, value: Any, ttl: int = CACHE_TTL):
        with self._lock:
            self._cache[key] = (value, time.time() + ttl)

    def delete(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()


_cache_instance = SimpleCache()

_redis_client = None


def _get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    if redis is None:
        _redis_client = False
        return None

    redis_url = (os.getenv('REDIS_URL') or '').strip()
    if not redis_url:
        _redis_client = False
        return None

    try:
        client = redis.Redis.from_url(redis_url, decode_responses=False)
        client.ping()
        _redis_client = client
        return client
    except Exception:
        _redis_client = False
        return None


def get_cache_key(prefix: str, *args) -> str:
    key_data = f"{prefix}:{':'.join(map(str, args))}"
    return _KEY_PREFIX + hashlib.md5(key_data.encode()).hexdigest()


def cached_query(cache_key: str, query_func: Callable, ttl: int = CACHE_TTL) -> Any:
    client = _get_redis_client()
    if client is not None:
        try:
            raw = client.get(cache_key)
            if raw is not None:
                return json.loads(raw.decode('utf-8'))
        except Exception:
            client = None

    cached_data = _cache_instance.get(cache_key)
    if cached_data is not None:
        return cached_data

    result = query_func()

    if client is not None:
        try:
            payload = json.dumps(result, ensure_ascii=False).encode('utf-8')
            client.setex(cache_key, int(ttl), payload)
            return result
        except Exception:
            pass

    _cache_instance.set(cache_key, result, ttl)
    return result


def invalidate_ai_cache():
    _cache_instance.clear()
    client = _get_redis_client()
    if client is not None:
        try:
            cursor = 0
            pattern = _KEY_PREFIX + '*'
            while True:
                cursor, keys = client.scan(cursor=cursor, match=pattern, count=500)
                if keys:
                    client.delete(*keys)
                if int(cursor) == 0:
                    break
        except Exception:
            return
