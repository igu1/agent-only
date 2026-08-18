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
