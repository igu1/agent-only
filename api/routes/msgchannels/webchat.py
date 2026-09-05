# webchat routes - website chat widget: send message, poll replies, mint session
import uuid

from fastapi import APIRouter, HTTPException, Request

from api.routes.shared.channel_registry import registry
from api.routes.shared.generic_webhook import handle_incoming_webhook
from api.routes.shared.webchat_store import store
from infra.profile import (
    build_institution_choices,
    build_institution_prompt,
    build_introduction,
)
from infra.tenants import is_known_org, set_org

router = APIRouter(prefix="/webhooks")


def _adapter():
    return registry.get('webchat')


def _enter_org(org: str) -> None:
    """SaaS Phase 1: org-prefixed routes select the tenant for this request."""
    if not is_known_org(org):
        raise HTTPException(status_code=404, detail='Unknown organization')
    set_org(org)


def _channel_agent_id(channel_id: str) -> int | None:
    """Agent behind this widget - 404s for unknown/inactive channels."""
    service = _adapter().get_service(channel_id)
    channel = getattr(service, 'channel_config', None) or {}
    try:
        return int(channel['agent_id']) if channel.get('agent_id') is not None else None
    except (TypeError, ValueError):
        return None


def _requested_institution(value) -> int | None:
    try:
        return int(value) if value is not None and value != '' else None
    except (TypeError, ValueError):
        return None


@router.post("/webchat/{channel_id}/session")
async def webchat_session(channel_id: str, institution_id: int | None = None) -> dict:
    # 404s for unknown/inactive channels before handing out a session.
    # The greeting rides along so the widget has something to show the moment
    # it opens - a web visitor should never face an empty panel. It is still
    # SERVER-owned (built from this school's live documents and data, never a
    # string baked into the widget), and it is the same text WhatsApp gets on
    # its first message.
    #
    # `institution_id` is the college the INQUIRY PAGE already selected in its
    # dropdown. Given one, the greeting names that college and offers only what
    # it has, and the assistant is never asked to work the routing out in chat.
    # `institutions` is returned so a page that has no dropdown of its own can
    # build one from live data; it is empty for a single-institution agent,
    # which is the signal to show no picker at all.
    agent_id = _channel_agent_id(channel_id)
    chosen = _requested_institution(institution_id)
    choices = build_institution_choices(agent_id=agent_id)
    needs_choice = bool(choices) and chosen is None
    return {
        "ok": True,
        "session_id": uuid.uuid4().hex,
        "institutions": choices,
        "institution_id": chosen,
        "institution_required": needs_choice,
        "introduction": (
            build_institution_prompt(agent_id=agent_id)
            if needs_choice
            else build_introduction(agent_id=agent_id, institution_id=chosen)
        ),
    }


@router.get("/webchat/{channel_id}/introduction")
async def webchat_introduction(channel_id: str, institution_id: int | None = None) -> dict:
    """The greeting for a college picked AFTER the panel opened, so a widget
    with its own in-panel dropdown can swap the welcome without minting a
    second session."""
    agent_id = _channel_agent_id(channel_id)
    chosen = _requested_institution(institution_id)
    return {
        "ok": True,
        "institution_id": chosen,
        "introduction": build_introduction(agent_id=agent_id, institution_id=chosen),
    }


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
async def webchat_session_org(org: str, channel_id: str, institution_id: int | None = None) -> dict:
    _enter_org(org)
    return await webchat_session(channel_id, institution_id)


@router.get("/{org}/webchat/{channel_id}/introduction")
async def webchat_introduction_org(org: str, channel_id: str, institution_id: int | None = None) -> dict:
    _enter_org(org)
    return await webchat_introduction(channel_id, institution_id)


@router.post("/{org}/webchat/{channel_id}")
async def webchat_message_org(org: str, channel_id: str, request: Request) -> dict:
    _enter_org(org)
    return await webchat_message(channel_id, request)


@router.get("/{org}/webchat/{channel_id}/messages")
async def webchat_messages_org(org: str, channel_id: str, session_id: str, after_id: int = 0) -> dict:
    _enter_org(org)
    return await webchat_messages(channel_id, session_id, after_id)
