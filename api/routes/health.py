from fastapi import APIRouter, Depends

from api.routes.shared.admin_auth import require_admin_token

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/infra/clear-cache", dependencies=[Depends(require_admin_token)])
def clear_cache() -> dict:
    from infra.cache_utils import invalidate_ai_cache
    invalidate_ai_cache()
    return {"status": "cleared"}


@router.post("/infra/reload-tenants", dependencies=[Depends(require_admin_token)])
def reload_tenants() -> dict:
    """SaaS Phase 1: re-read TENANTS_FILE - onboarding an organization
    becomes 'append to tenants.json + call this' with no restart."""
    from infra.tenants import reload_registry
    return {"status": "reloaded", "organizations": reload_registry()}
