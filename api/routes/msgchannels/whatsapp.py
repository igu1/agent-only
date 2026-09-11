import logging
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import Query

from api.routes.shared.generic_webhook import handle_incoming_webhook
from api.routes.shared.channel_registry import registry, require_tenant
from infra.tenants import is_known_org, resolve_org_by_phone_number_id, set_org


router = APIRouter(prefix="/webhooks")
_SERVICE_UNAVAILABLE_MSG = "WhatsApp service not available"


def _resolve_tenant_from_payload(payload: dict) -> None:
    """SaaS Phase 1: all orgs may share ONE Meta callback URL - the payload's
    phone_number_id identifies the tenant. Unknown/absent ids keep the
    default tenant, which is only correct where this stack serves ONE
    organization - STRICT_TENANCY refuses instead."""
    try:
        value = ((payload.get('entry') or [{}])[0].get('changes') or [{}])[0].get('value') or {}
        pnid = (value.get('metadata') or {}).get('phone_number_id')
        org = resolve_org_by_phone_number_id(pnid)
        if org:
            set_org(org)
    except Exception:
        pass


def _enter_org(org: str) -> None:
    """SaaS: org-prefixed routes select the tenant for this request.

    This is what makes VERIFICATION possible under SaaS. Meta's GET carries no
    payload - no phone_number_id, nothing that names an organisation - so
    without the org in the URL the handler looked for the channel in the
    default tenant's database and every other org failed to verify.
    """
    if not is_known_org(org):
        raise HTTPException(status_code=404, detail='Unknown organization')
    set_org(org)


def _get_service(channel_id: str):
    return registry.get('whatsapp').get_service(channel_id)


@router.get("/whatsapp/{channel_id}/")
async def whatsapp_webhook_verify_by_channel(
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
    # Meta's verification GET carries no payload at all - no phone_number_id,
    # nothing that names an organisation - so on the bare route there is
    # nothing to resolve. _get_service reads the tenant's database, so the
    # refusal belongs above it.
    require_tenant('whatsapp')
    try:
        service = _get_service(channel_id)
    except HTTPException:
        raise
    except Exception as e:
        _ = e
        raise HTTPException(status_code=503, detail=_SERVICE_UNAVAILABLE_MSG)

    challenge = service.validate_webhook(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status_code=403, detail="Invalid webhook verification")

    return Response(content=challenge, status_code=200, media_type="text/plain")


@router.get("/whatsapp/{channel_id}/")
async def whatsapp_webhook_verify_by_channel_slash(
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
    return await whatsapp_webhook_verify_by_channel(
        channel_id=channel_id,
        hub_mode=hub_mode,
        hub_verify_token=hub_verify_token,
        hub_challenge=hub_challenge,
    )



@router.post("/whatsapp/{channel_id}/")
async def whatsapp_webhook_by_channel(channel_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        logging.error(f"Error parsing webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        _resolve_tenant_from_payload(payload)
        # The URL may already have settled the org; this only bites when
        # nothing did - an unregistered phone_number_id on the bare route.
        require_tenant('whatsapp')
        _ = registry.get('whatsapp')
        return await handle_incoming_webhook(channel_type='whatsapp', channel_id=channel_id, payload=payload)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in webhook processing: {e} (channel_id={channel_id})")
        return {"ok": True}


# ── org-prefixed routes (SaaS) ───────────────────────────────────────────
# One shared callback host serving every organisation. Give Meta
#   https://<agent-host>/webhooks/<org>/whatsapp/<channel_id>/
# and both the verification GET and the message POST know their tenant from
# the URL. The bare /webhooks/whatsapp/<channel_id>/ routes above still work
# unchanged for a single-org deployment.

@router.get("/{org}/whatsapp/{channel_id}/")
async def whatsapp_webhook_verify_by_org(
    org: str,
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
    _enter_org(org)
    return await whatsapp_webhook_verify_by_channel(
        channel_id=channel_id,
        hub_mode=hub_mode,
        hub_verify_token=hub_verify_token,
        hub_challenge=hub_challenge,
    )


@router.post("/{org}/whatsapp/{channel_id}/")
async def whatsapp_webhook_by_org(org: str, channel_id: str, request: Request):
    # The URL settles the tenant; the handler below still reads
    # phone_number_id, which now acts as a second opinion - it catches a number
    # configured against another org's callback URL instead of silently
    # serving it from the wrong database.
    _enter_org(org)
    return await whatsapp_webhook_by_channel(channel_id=channel_id, request=request)
