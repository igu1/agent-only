import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi import Query

from api.routes.shared.channel_registry import registry
from api.routes.messaging.webhook_handler import handle_incoming_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")


@router.post("/telegram/{channel_id}/register")
async def register_telegram_webhook(channel_id: str, request: Request):
    try:
        service = registry.get('telegram').get_service(channel_id)
        payload = await request.json()
        webhook_url = payload.get("webhook_url")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="webhook_url is required")
        return service.register_webhook(webhook_url)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error registering telegram webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/telegram/{channel_id}/info")
async def get_telegram_webhook_info(channel_id: str):
    try:
        service = registry.get('telegram').get_service(channel_id)
        return service.get_webhook_info()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting telegram webhook info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/telegram/{channel_id}")
@router.post("/telegram/{channel_id}/")
async def telegram_webhook(
    request: Request,
    channel_id: str,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Error parsing webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await handle_incoming_webhook(
        channel_type='telegram',
        channel_id=channel_id,
        payload=payload,
        headers={
            'x_telegram_bot_api_secret_token': x_telegram_bot_api_secret_token,
        },
    )

@router.get("/whatsapp/{channel_id}")
@router.get("/whatsapp/{channel_id}/")
async def whatsapp_webhook_verify_by_channel(
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
    try:
        service = registry.get('whatsapp').get_service(channel_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="WhatsApp service not available")

    challenge = service.validate_webhook(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status_code=403, detail="Invalid webhook verification")

    return Response(content=challenge, status_code=200, media_type="text/plain")


@router.post("/whatsapp/{channel_id}/")
async def whatsapp_webhook_by_channel(channel_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Error parsing webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        _ = registry.get('whatsapp')
        return await handle_incoming_webhook(channel_type='whatsapp', channel_id=channel_id, payload=payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in webhook processing: {e} (channel_id={channel_id})")
        return {"ok": True}


@router.get("/instagram/{channel_id}")
@router.get("/instagram/{channel_id}/")
async def instagram_webhook_verify_by_channel(
    channel_id: str,
    hub_mode: str | None = Query(alias="hub.mode"),
    hub_verify_token: str | None = Query(alias="hub.verify_token"),
    hub_challenge: str | None = Query(alias="hub.challenge"),
):
    try:
        service = registry.get('instagram').get_service(channel_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Instagram service not available")

    challenge = service.validate_webhook(hub_mode, hub_verify_token, hub_challenge)
    if hub_mode == "subscribe" and hub_verify_token == service.verify_token:
        return Response(content=hub_challenge, status_code=200, media_type="text/plain")
    return Response(content="Verification failed", status_code=403, media_type="text/plain")

@router.post("/instagram/{channel_id}")
@router.post("/instagram/{channel_id}/")
async def instagram_webhook_by_channel(channel_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"Error parsing webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        _ = registry.get('instagram')
        return await handle_incoming_webhook(channel_type='instagram', channel_id=channel_id, payload=payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in webhook processing: {e} (channel_id={channel_id})")
        return {"ok": True}
