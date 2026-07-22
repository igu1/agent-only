import logging
from fastapi import APIRouter, Header, HTTPException, Request

from api.routes.shared.generic_webhook import handle_incoming_webhook
from api.routes.shared.channel_registry import registry


def _get_service(channel_id: str):
    return registry.get('telegram').get_service(channel_id)

router = APIRouter(prefix="/webhooks")


@router.post("/telegram/{channel_id}/register")
async def register_telegram_webhook(channel_id: str, request: Request):
    """API endpoint to register/update Telegram webhook"""
    try:
        service = _get_service(channel_id)
        payload = await request.json()
        webhook_url = payload.get("webhook_url")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="webhook_url is required")
        
        result = service.register_webhook(webhook_url)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logging.error(f"Error registering telegram webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/telegram/{channel_id}/info")
async def get_telegram_webhook_info(channel_id: str):
    """API endpoint to get Telegram webhook info"""
    try:
        service = _get_service(channel_id)
        result = service.get_webhook_info()
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logging.error(f"Error getting telegram webhook info: {e}")
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
        logging.error(f"Error parsing webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    return await handle_incoming_webhook(
        channel_type='telegram',
        channel_id=channel_id,
        payload=payload,
        headers={
            'x_telegram_bot_api_secret_token': x_telegram_bot_api_secret_token,
        },
    )

