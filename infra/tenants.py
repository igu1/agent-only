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
#     deployment with no registry file behaves EXACTLY as before.
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

# Never usable as an org key: each is a literal segment in an org-prefixed URL.
_RESERVED_ORG_KEYS = frozenset({'webchat', 'whatsapp', 'telegram', 'instagram'})

_registry: dict[str, dict] = {}
_loaded = False
_lock = threading.Lock()

_current_org: contextvars.ContextVar[str] = contextvars.ContextVar(
    'tenant_org', default=DEFAULT_ORG)

# Whether THIS request actually identified an organisation, as opposed to
# never having been told. The org alone cannot answer that: it reads
# 'default' in both cases.
_org_resolved: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'tenant_org_resolved', default=False)


def _env_default_tenant() -> dict:
    """The classic single-org configuration, expressed as a tenant."""
    return {
        'backend_url': (os.getenv('DJANGO_INTERNAL_URL') or 'http://localhost:8000').rstrip('/'),
        'api_token': (os.getenv('AGENT_API_TOKEN') or '').strip(),
        'google_api_key': (os.getenv('GOOGLE_API_KEY') or '').strip() or None,
        'qdrant_prefix': os.getenv('QDRANT_COLLECTION_PREFIX') or 'cronocrm-ai-profile',
        'phone_number_ids': [],
        # The single-org deployment's browser origins, expressed as this
        # tenant's own - so CORS has one source (the registry) instead of two.
        'webchat_origins': [o.strip() for o in
                            (os.getenv('WEBCHAT_ALLOWED_ORIGINS') or '').split(',')
                            if o.strip()],
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
                # A leading underscore marks documentation, not an organization.
                # The only guard used to be isinstance(cfg, dict), so a NESTED
                # comment block - field notes, a routing table - was loaded as a
                # tenant: the count was wrong, /infra/reload-tenants reported it,
                # and /webhooks/_field_notes/webchat/... answered instead of 404.
                # Channel names are path segments in the org-prefixed routes
                # (/webhooks/<org>/webchat/..., /<org>/whatsapp/...), so an org
                # called "whatsapp" would make those URLs ambiguous.
                if not key or key.startswith('_') or key in _RESERVED_ORG_KEYS:
                    continue
                if isinstance(cfg, dict):
                    reg[key] = _normalize(key, cfg)
            log.info("tenant registry loaded: %s org(s) from %s", len(reg), path)
        except Exception:
            log.exception("tenant registry load failed (%s) - continuing with default only", path)
    # the default tenant always exists so a registry-less deployment (the
    # current per-org model) keeps its exact behavior
    reg.setdefault(DEFAULT_ORG, _env_default_tenant())
    _warn_about_wildcard_origins(reg)
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
    _org_resolved.set(True)
    return _current_org.set((org or DEFAULT_ORG).strip().lower())


def org_resolved() -> bool:
    """True once this request has been told which organisation it belongs to."""
    return _org_resolved.get()


def strict_tenancy() -> bool:
    """Refuse a request that names no organisation, instead of serving it from
    the default tenant. Off by default so a single-org deployment is unchanged;
    turn it on wherever tenants.json is the source of truth."""
    return (os.getenv('STRICT_TENANCY') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def is_known_org(org: str) -> bool:
    _ensure_loaded()
    return (org or '').strip().lower() in _registry


def get_tenant() -> dict:
    """The current request's tenant config.

    Still falls back to the default tenant rather than raising - this is called
    from inside a turn, where an exception would surface as a 500 long after
    the point where the request could have been refused cleanly. But it no
    longer does so silently: an unidentified request being served from whatever
    .env points at is exactly the failure worth seeing in a log.
    """
    _ensure_loaded()
    org = get_org()
    cfg = _registry.get(org)
    if cfg is None:
        log.warning(
            'no tenant registered for %r - serving from the DEFAULT tenant; '
            'if this is a real organisation it is missing from the registry',
            org)
    elif not _org_resolved.get() and len(_registry) > 1:
        # The case that actually loses data, and the one a lookup can never
        # report: nothing told this request which organisation it belongs to,
        # so get_org() reads 'default', the registry HAS a default, and the
        # request is served - from whatever .env points at. Only worth saying
        # where there is another organisation for it to have belonged to.
        log.warning(
            'this request named no organization - serving it from the DEFAULT '
            'tenant while %d are registered. Any inquiry it creates is written '
            'to the default backend, not theirs. Set STRICT_TENANCY=true to '
            'refuse instead of guessing.', len(_registry) - 1)
    return cfg or _registry[DEFAULT_ORG]


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


def _warn_about_wildcard_origins(reg: dict) -> None:
    """A "*" is one organisation's setting with everyone's consequences.

    CORS is deployment-wide, so a single org listing "*" opens web chat to
    every website for every org on the stack. Fine on a laptop, which is
    where it comes from; worth saying out loud on a shared one.
    """
    named = [o for o in reg if o != DEFAULT_ORG]
    if len(named) < 2:
        return
    for org in named:
        if '*' in (reg[org].get('webchat_origins') or []):
            log.warning(
                'organization %r allows web chat from ANY origin ("*"). CORS is '
                'deployment-wide, so this opens every organization on this stack, '
                'not just %r. List real origins instead.', org, org)


def all_webchat_origins() -> set[str]:
    """Every browser origin any organisation allows for web chat.

    CORS is negotiated before routing - the browser sends an Origin, not a
    URL path - so the middleware cannot yet know which org the request is
    for, and the union is the only answer it can give. That is not a hole:
    CORS decides which PAGE may make the call, never whose data it reaches.
    The org segment in the path decides that, and STRICT_TENANCY refuses a
    request that has none.

    Read live, per request, so onboarding stays "append one org + reload"
    with no restart - the promise that would otherwise quietly exclude every
    new customer's website until someone bounced the container.
    """
    _ensure_loaded()
    out: set[str] = set()
    for cfg in _registry.values():
        for origin in (cfg.get('webchat_origins') or []):
            text = str(origin).strip()
            if text:
                out.add(text)
    return out


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
