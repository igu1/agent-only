import json
import urllib.request
import urllib.error

from infra.tenants import get_tenant


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
        with urllib.request.urlopen(req, timeout=20) as resp:
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
        return parsed
    except urllib.error.URLError as e:
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
        with urllib.request.urlopen(req, timeout=20) as resp:
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
        return parsed
    except urllib.error.URLError as e:
        return {"ok": False, "status": 503, "error": str(e)}
