import logging
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import Query

from api.routes.shared.generic_webhook import handle_incoming_webhook
from api.routes.shared.channel_registry import registry
from infra.tenants import resolve_org_by_phone_number_id, set_org


router = APIRouter(prefix="/webhooks")
_SERVICE_UNAVAILABLE_MSG = "WhatsApp service not available"


def _resolve_tenant_from_payload(payload: dict) -> None:
    """SaaS Phase 1: all orgs may share ONE Meta callback URL - the payload's
    phone_number_id identifies the tenant. Unknown/absent ids keep the
    default tenant (the classic per-org deployment)."""
    try:
        value = ((payload.get('entry') or [{}])[0].get('changes') or [{}])[0].get('value') or {}
        pnid = (value.get('metadata') or {}).get('phone_number_id')
        org = resolve_org_by_phone_number_id(pnid)
        if org:
            set_org(org)
    except Exception:
        pass


def _get_service(channel_id: str):
    return registry.get('whatsapp').get_service(channel_id)


@router.get("/whatsapp/{channel_id}/")
async def whatsapp_webhook_verify_by_channel(
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
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
        _ = registry.get('whatsapp')
        return await handle_incoming_webhook(channel_type='whatsapp', channel_id=channel_id, payload=payload)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error in webhook processing: {e} (channel_id={channel_id})")
        return {"ok": True}
