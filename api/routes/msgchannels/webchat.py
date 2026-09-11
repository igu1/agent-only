# webchat routes - website chat widget: send message, poll replies, mint session
import uuid

from fastapi import APIRouter, HTTPException, Request

from api.routes.shared.channel_registry import registry, require_tenant
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
    """The campus a caller asked for, or None.

    Non-positive is None, not a campus: the gateway tests every institution as
    `is > 0`, so a 0 arriving here is the absence of a choice wearing the shape
    of one - and it slipped past a null check further down.
    """
    try:
        parsed = int(value) if value is not None and value != '' else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed > 0 else None


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
    # Which CRM this greeting is built from is decided here. The org-prefixed
    # variant below has already settled it; a bare call under strict tenancy
    # has not, and would mint a session against whatever .env points at.
    require_tenant('webchat')
    agent_id = _channel_agent_id(channel_id)
    chosen = _requested_institution(institution_id)
    choices = build_institution_choices(agent_id=agent_id)
    # ONE school is not a choice, it is the answer. Returning null here made a
    # single-school company indistinguishable from a visitor who had not picked
    # yet: same null, two meanings. The widget then opened its composer (nothing
    # to choose) and sent every message unrouted, so the conversation row was
    # written with InstitutionID NULL and fell out of the staff list's
    # institution filter.
    if chosen is None and len(choices) == 1:
        chosen = choices[0]['id']
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
    require_tenant('webchat')
    agent_id = _channel_agent_id(channel_id)
    chosen = _requested_institution(institution_id)
    return {
        "ok": True,
        "institution_id": chosen,
        "introduction": build_introduction(agent_id=agent_id, institution_id=chosen),
    }


@router.post("/webchat/{channel_id}")
async def webchat_message(channel_id: str, request: Request) -> dict:
    # Before the campus question comes the company one: this turn writes an
    # inquiry, and an unidentified request would write it into another
    # organization's database.
    require_tenant('webchat')
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # The campus is settled BEFORE the conversation exists, not argued out in
    # it. Both clients already honour that - the Angular panel renders only once
    # a campus is chosen, and the embeddable widget keeps its composer locked
    # behind the picker - so this refuses the one route left open: a caller
    # posting straight to the endpoint. Without it a company-level row is
    # written with InstitutionID NULL, the company agent answers out of the
    # POOLED data of every school, and the chat never reaches the staff list's
    # institution filter.
    #
    # Gated on there being something to choose. An agent bound to one school has
    # no choices - its institution comes from the channel, not the payload - and
    # a single-school company now resolves to that school in the session above,
    # so neither is caught here.
    if _requested_institution(payload.get('institution_id')) is None:
        agent_id = _channel_agent_id(channel_id)
        # More than ONE, deliberately. Where a single school is all there is
        # there is no wrong school to route to, so demanding the id buys no
        # safety and only risks locking a client out of its own chat - the
        # session hands it over anyway.
        if len(build_institution_choices(agent_id=agent_id)) > 1:
            raise HTTPException(
                status_code=400,
                detail='institution_id is required: choose a campus before sending')

    return await handle_incoming_webhook(
        channel_type='webchat',
        channel_id=channel_id,
        payload=payload,
        headers={},
    )


@router.get("/webchat/{channel_id}/messages")
async def webchat_messages(channel_id: str, session_id: str, after_id: int = 0) -> dict:
    require_tenant('webchat')
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
