# admin auth - backend-triggered endpoints (reindex, clear-cache) present the
# org's X-Agent-Token secret.
#
# SaaS Phase 1: each org's token is unique, so the token itself identifies
# the tenant - a valid token both authenticates AND selects the org context
# for the request (an org's CRM can only ever reindex its own collections).
import os

from fastapi import Header, HTTPException

from infra.tenants import resolve_org_by_token, set_org


def require_admin_token(x_agent_token: str | None = Header(default=None)) -> None:
    provided = (x_agent_token or '').strip()

    org = resolve_org_by_token(provided)
    if org:
        set_org(org)
        return

    expected = (os.getenv('AGENT_API_TOKEN') or '').strip()
    if not expected:
        # no token configured (local dev) - leave endpoints open
        return
    if provided != expected:
        raise HTTPException(status_code=401, detail='Unauthorized')


def require_org_token() -> None:
    """Org-scoped admin endpoints must know WHOSE data they are touching.

    An org's own token names its organisation; the shared operator token does
    not. It authenticates fine, so without this the call proceeds as the
    default tenant - reindexing the default organisation's Qdrant collections
    while the caller meant their own. Under strict tenancy, say so instead.

    Depends on require_admin_token having run first (list it first in the
    endpoint's dependencies): that is what resolves the org.
    """
    from infra.tenants import org_resolved, strict_tenancy

    if strict_tenancy() and not org_resolved():
        raise HTTPException(
            status_code=400,
            detail=("organization could not be determined - call this with the "
                    "organization's own X-Agent-Token from tenants.json, not "
                    "the shared operator token"))
