# webchat routes - website chat widget: send message, poll replies, mint session
import uuid

from fastapi import APIRouter, HTTPException, Request

from api.routes.shared.channel_registry import registry
from api.routes.shared.generic_webhook import handle_incoming_webhook
from api.routes.shared.webchat_store import store
from infra.tenants import is_known_org, set_org

router = APIRouter(prefix="/webhooks")


def _adapter():
    return registry.get('webchat')


def _enter_org(org: str) -> None:
    """SaaS Phase 1: org-prefixed routes select the tenant for this request."""
    if not is_known_org(org):
        raise HTTPException(status_code=404, detail='Unknown organization')
    set_org(org)


@router.post("/webchat/{channel_id}/session")
async def webchat_session(channel_id: str) -> dict:
    # 404s for unknown/inactive channels before handing out a session
    _adapter().get_service(channel_id)
    return {"ok": True, "session_id": uuid.uuid4().hex}


@router.post("/webchat/{channel_id}")
async def webchat_message(channel_id: str, request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await handle_incoming_webhook(
        channel_type='webchat',
        channel_id=channel_id,
        payload=payload,
        headers={},
    )


@router.get("/webchat/{channel_id}/messages")
async def webchat_messages(channel_id: str, session_id: str, after_id: int = 0) -> dict:
    adapter = _adapter()
    if not adapter.is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    # 404s for unknown/inactive channels
    adapter.get_service(channel_id)
    result = store.get_since(session_id, after_id=after_id)
    return {"ok": True, **result}


# ── SaaS Phase 1: org-prefixed variants (multi-tenant deployments) ──────────
# /webhooks/{org}/webchat/... — the org selects the tenant; the classic
# unprefixed routes above keep serving the default tenant unchanged.

@router.post("/{org}/webchat/{channel_id}/session")
async def webchat_session_org(org: str, channel_id: str) -> dict:
    _enter_org(org)
    return await webchat_session(channel_id)


@router.post("/{org}/webchat/{channel_id}")
async def webchat_message_org(org: str, channel_id: str, request: Request) -> dict:
    _enter_org(org)
    return await webchat_message(channel_id, request)


@router.get("/{org}/webchat/{channel_id}/messages")
async def webchat_messages_org(org: str, channel_id: str, session_id: str, after_id: int = 0) -> dict:
    _enter_org(org)
    return await webchat_messages(channel_id, session_id, after_id)
