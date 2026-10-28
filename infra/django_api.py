import json
import logging

import httpx

from infra.tenants import get_tenant

logger = logging.getLogger(__name__)

# Longer than the backend's SQL Command Timeout (30s in the connection string)
# ON PURPOSE. At 20s the client abandoned requests the server was still allowed
# to finish, so an inquiry could be created after Python had given up - and a
# caller that cannot tell what happened cannot safely retry.
REQUEST_TIMEOUT_SECONDS = 35

# ONE pooled client for the process, so a turn's backend calls reuse open
# connections. urllib opened a fresh TCP + TLS connection for every call: one
# chat turn makes ~5-8 calls in a row, and each new HTTPS handshake to a
# remote CRM cost 0.7-1.1s (measured through a Cloudflare tunnel) against
# ~0.1s on a warm connection - several seconds of every reply spent shaking
# hands. The pool is keyed by host, so every tenant's backend gets its own
# warm connections. httpx.Client is thread-safe.
# HTTP/2 when the h2 package is present (pinned in requirements.txt): the
# parallel settings warm-up (tools/prefetch.py) then multiplexes over ONE
# connection. Over HTTP/1.1 each parallel request opened its own, and the
# first cold load paid ~11 handshakes. Missing h2 just means HTTP/1.1.
try:
    import h2  # noqa: F401
    _HTTP2 = True
except ImportError:
    _HTTP2 = False

_client = httpx.Client(
    http2=_HTTP2,
    timeout=REQUEST_TIMEOUT_SECONDS,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20,
                        keepalive_expiry=60),
)


def _send(method: str, url: str, headers: dict, content: bytes | None = None) -> httpx.Response:
    """The only line that touches the network - tests patch this."""
    return _client.request(method, url, headers=headers, content=content)


def _log_failure(method: str, path: str, status, reason) -> None:
    """Record a failed backend call.

    Deliberately narrow: method, path, status and the transport reason. NOT the
    request payload - it carries OTP codes and parent contact details - NOT the
    headers, which carry the agent token, and NOT the response body, which for
    an unhandled .NET exception is a developer page that can contain the
    connection string.
    """
    logger.warning('.NET API request failed: %s %s -> status=%s error=%s',
                   method, path, status, reason)


def _get_base_url() -> str:
    # SaaS Phase 1: backend identity comes from the CURRENT TENANT context
    # (the default tenant mirrors the classic env vars exactly)
    return get_tenant()['backend_url']


def _get_token() -> str:
    return get_tenant()['api_token']


def _call(method: str, path: str, headers: dict, content: bytes | None = None) -> dict:
    """Never raises - every outcome is classified into the response dict."""
    url = f"{_get_base_url()}/{path.lstrip('/')}"
    try:
        resp = _send(method, url, headers, content)
    except httpx.HTTPError as e:
        # connection refused, DNS failure, TLS failure, timeout - no HTTP
        # status ever existed, so 503 is this layer's name for "unreachable"
        _log_failure(method, path, 503, type(e).__name__)
        return {"ok": False, "status": 503, "error": str(e)}

    body = resp.text
    if resp.status_code < 400:
        return json.loads(body or "{}")
    try:
        parsed = json.loads(body or "{}")
        if not isinstance(parsed, dict):
            raise ValueError
    except Exception:
        parsed = {"ok": False, "error": body or resp.reason_phrase}
    parsed.setdefault("ok", False)
    parsed.setdefault("status", resp.status_code)
    _log_failure(method, path, parsed.get("status"), resp.reason_phrase or resp.status_code)
    return parsed


def _headers(extra_headers: dict | None, *, json_body: bool) -> dict:
    headers = {"Content-Type": "application/json"} if json_body else {}
    token = _get_token()
    if token:
        headers["X-Agent-Token"] = token
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v is not None})
    return headers


def post(path: str, payload: dict, extra_headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    return _call("POST", path, _headers(extra_headers, json_body=True), data)


def get(path: str, extra_headers: dict | None = None) -> dict:
    return _call("GET", path, _headers(extra_headers, json_body=False))
