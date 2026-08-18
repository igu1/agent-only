# Tenant registry - SaaS Phase 1 (see SAAS-ARCHITECTURE.md).
#
# The agent's identity (backend URL, API token, Gemini key, Qdrant prefix)
# used to come from process-wide env vars, which limits one deployment to one
# organization. This module makes identity PER REQUEST:
#
#   * a registry of tenants loaded from TENANTS_FILE (JSON, org -> config);
#   * a contextvars-based "current org" set at every entry point (webhooks,
#     admin endpoints) and read by everything downstream (_api_get, caches,
#     RAG collection names, model keys, batch keys);
#   * a built-in "default" tenant synthesized from the classic env vars, so a
#     per-org deployment with no registry file behaves EXACTLY as before.
#
# Isolation rule: any code that needs org-specific config must call
# get_tenant() - never os.getenv - so a request can never see another
# tenant's identity.
import contextvars
import json
import logging
import os
import threading

log = logging.getLogger(__name__)

DEFAULT_ORG = 'default'

_registry: dict[str, dict] = {}
_loaded = False
_lock = threading.Lock()

_current_org: contextvars.ContextVar[str] = contextvars.ContextVar(
    'tenant_org', default=DEFAULT_ORG)


def _env_default_tenant() -> dict:
    """The classic single-org configuration, expressed as a tenant."""
    return {
        'backend_url': (os.getenv('DJANGO_INTERNAL_URL') or 'http://localhost:8000').rstrip('/'),
        'api_token': (os.getenv('AGENT_API_TOKEN') or '').strip(),
        'google_api_key': (os.getenv('GOOGLE_API_KEY') or '').strip() or None,
        'qdrant_prefix': os.getenv('QDRANT_COLLECTION_PREFIX') or 'cronocrm-ai-profile',
        'phone_number_ids': [],
        'webchat_origins': [],
    }


def _normalize(org: str, cfg: dict) -> dict:
    return {
        'backend_url': str(cfg.get('backend_url') or '').rstrip('/'),
        'api_token': str(cfg.get('api_token') or '').strip(),
        'google_api_key': (str(cfg.get('google_api_key') or '').strip() or None),
        'qdrant_prefix': str(cfg.get('qdrant_prefix') or org),
        'phone_number_ids': [str(p) for p in (cfg.get('phone_number_ids') or [])],
        'webchat_origins': list(cfg.get('webchat_origins') or []),
    }


def _load() -> None:
    global _registry, _loaded
    reg: dict[str, dict] = {}
    path = (os.getenv('TENANTS_FILE') or '').strip()
    if path and os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f) or {}
            for org, cfg in data.items():
                key = str(org).strip().lower()
                if key and key != 'webchat' and isinstance(cfg, dict):
                    reg[key] = _normalize(key, cfg)
            log.info("tenant registry loaded: %s org(s) from %s", len(reg), path)
        except Exception:
            log.exception("tenant registry load failed (%s) - continuing with default only", path)
    # the default tenant always exists so a registry-less deployment (the
    # current per-org model) keeps its exact behavior
    reg.setdefault(DEFAULT_ORG, _env_default_tenant())
    _registry = reg
    _loaded = True


def _ensure_loaded() -> None:
    if not _loaded:
        with _lock:
            if not _loaded:
                _load()


def reload_registry() -> int:
    """Re-read TENANTS_FILE (onboarding without restart). Returns org count."""
    with _lock:
        _load()
    return len(_registry)


def get_org() -> str:
    return _current_org.get()


def set_org(org: str):
    """Set the request's tenant. Returns the contextvars token (optional reset)."""
    return _current_org.set((org or DEFAULT_ORG).strip().lower())


def is_known_org(org: str) -> bool:
    _ensure_loaded()
    return (org or '').strip().lower() in _registry


def get_tenant() -> dict:
    """The current request's tenant config (falls back to default)."""
    _ensure_loaded()
    return _registry.get(get_org()) or _registry[DEFAULT_ORG]


def resolve_org_by_token(token: str) -> str | None:
    """Each org's API token is unique - the token identifies the tenant."""
    _ensure_loaded()
    t = (token or '').strip()
    if not t:
        return None
    for org, cfg in _registry.items():
        if cfg.get('api_token') and cfg['api_token'] == t:
            return org
    return None


def resolve_org_by_phone_number_id(pnid: str) -> str | None:
    """WhatsApp webhooks can share ONE callback URL - the payload's
    phone_number_id identifies the tenant."""
    _ensure_loaded()
    p = str(pnid or '').strip()
    if not p:
        return None
    for org, cfg in _registry.items():
        if p in (cfg.get('phone_number_ids') or []):
            return org
    return None
