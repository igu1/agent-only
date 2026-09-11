import json
import logging
import urllib.request
import urllib.error

from infra.tenants import get_tenant

logger = logging.getLogger(__name__)

# Longer than the backend's SQL Command Timeout (30s in the connection string)
# ON PURPOSE. At 20s the client abandoned requests the server was still allowed
# to finish, so an inquiry could be created after Python had given up - and a
# caller that cannot tell what happened cannot safely retry.
REQUEST_TIMEOUT_SECONDS = 35


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


def post(path: str, payload: dict, extra_headers: dict | None = None) -> dict:
    url = f"{_get_base_url()}/{path.lstrip('/')}"
    token = _get_token()

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agent-Token"] = token
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v is not None})

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body or "{}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        try:
            parsed = json.loads(body or "{}")
        except Exception:
            parsed = {"ok": False, "error": body or str(e)}
        parsed.setdefault("ok", False)
        parsed.setdefault("status", int(getattr(e, "code", 500) or 500))
        _log_failure("POST", path, parsed.get("status"), getattr(e, "reason", None) or e.code)
        return parsed
    except urllib.error.URLError as e:
        # connection refused, DNS failure, TLS failure, timeout - no HTTP
        # status ever existed, so 503 is this layer's name for "unreachable"
        _log_failure("POST", path, 503, getattr(e, "reason", None) or e)
        return {"ok": False, "status": 503, "error": str(e)}


def get(path: str, extra_headers: dict | None = None) -> dict:
    url = f"{_get_base_url()}/{path.lstrip('/')}"
    token = _get_token()

    headers = {}
    if token:
        headers["X-Agent-Token"] = token
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v is not None})

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body or "{}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        try:
            parsed = json.loads(body or "{}")
        except Exception:
            parsed = {"ok": False, "error": body or str(e)}
        parsed.setdefault("ok", False)
        parsed.setdefault("status", int(getattr(e, "code", 500) or 500))
        _log_failure("GET", path, parsed.get("status"), getattr(e, "reason", None) or e.code)
        return parsed
    except urllib.error.URLError as e:
        # connection refused, DNS failure, TLS failure, timeout - no HTTP
        # status ever existed, so 503 is this layer's name for "unreachable"
        _log_failure("GET", path, 503, getattr(e, "reason", None) or e)
        return {"ok": False, "status": 503, "error": str(e)}
