import logging

import anyio
from fastapi import HTTPException

from infra.django_api import post as django_post
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async
from api.routes.shared.message_batcher import enqueue
from api.routes.shared.msgchannel_processor import handle_agent_batch
from api.routes.shared.channel_adapters import ChannelAdapter, IncomingMessage
from api.routes.shared.channel_registry import registry

logger = logging.getLogger(__name__)


def _to_int(value):
    return int(value) if value is not None else None


def _download_whatsapp_voice(service, media_id: str) -> bytes | None:
    """Download voice audio bytes from WhatsApp using the service's access token."""
    try:
        import requests
        access_token = getattr(service, 'access_token', '') or ''
        api_version = getattr(service, 'api_version', 'v21.0') or 'v21.0'
        if not access_token:
            return None

        media_url = f'https://graph.facebook.com/{api_version}/{media_id}'
        headers = {'Authorization': f'Bearer {access_token}'}
        resp = requests.get(media_url, headers=headers, timeout=10)
        resp.raise_for_status()
        download_url = resp.json().get('url', '')
        if not download_url:
            return None

        file_resp = requests.get(download_url, headers=headers, timeout=30)
        file_resp.raise_for_status()
        return file_resp.content
    except Exception as e:
        logger.error('Failed to download WhatsApp voice: %s', e)
        return None


def _download_telegram_voice(service, file_id: str) -> bytes | None:
    """Download voice audio bytes from Telegram using the bot token."""
    try:
        import requests
        bot_token = getattr(service, 'bot_token', '') or ''
        if not bot_token:
            return None

        file_info_url = f'https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}'
        resp = requests.get(file_info_url, timeout=10)
        resp.raise_for_status()
        file_path = resp.json().get('result', {}).get('file_path', '')
        if not file_path:
            return None

        download_url = f'https://api.telegram.org/file/bot{bot_token}/{file_path}'
        file_resp = requests.get(download_url, timeout=30)
        file_resp.raise_for_status()
        return file_resp.content
    except Exception as e:
        logger.error('Failed to download Telegram voice: %s', e)
        return None


async def handle_incoming_webhook(
    *,
    channel_type: str,
    channel_id: str | None,
    payload: dict,
    headers: dict[str, str | None] | None = None,
) -> dict:
    if not registry.has(channel_type):
        raise HTTPException(status_code=404, detail='Channel not supported')

    adapter: ChannelAdapter = registry.get(channel_type)
    service = adapter.get_service(channel_id)
    adapter.validate(channel_id=channel_id, service=service, headers=headers or {})

    message = adapter.parse(service=service, payload=payload)
    if not message:
        return {'ok': True}

    if adapter.should_ignore(channel_id=channel_id, service=service, message=message):
        return {'ok': True}

    # SaaS Phase 1: batch/lock keys are tenant-scoped - every org has a
    # "channel 1", so without the org two schools' chats could collide
    from infra.tenants import get_org
    batch_key = f"{get_org()}:{channel_type}:{channel_id}:{message.chat_id}"
    adapter.send_typing_indicator(service=service, message=message)        

    inbound = django_post(
        '/api/agent/v1/inbound/',
        {
            'channel_type': str(channel_type),
            'channel_id': str(channel_id),
            'chat_id': str(message.chat_id),
            'message_id': message.message_id,
            'text': message.text,
            'has_media': bool(message.has_media),
        },
        extra_headers={
            'X-Telegram-Bot-Api-Secret-Token': (headers or {}).get('x_telegram_bot_api_secret_token'),
        },
    )

    if not inbound.get('ok'):
        code = int(inbound.get('status') or 500)
        if code == 401:
            raise HTTPException(status_code=401, detail='Unauthorized')
        if code == 404:
            raise HTTPException(status_code=404, detail='Channel not found or inactive')
        return {'ok': True}

    lead_id_int = _to_int(inbound.get('lead_id'))
    org_id = _to_int(inbound.get('organization_id'))

    if lead_id_int is not None:
        await broadcast_to_conversation_async(
            conversation_id=lead_id_int,
            payload={
                'type': 'message.created',
                'conversation_id': lead_id_int,
                'message': {
                    'id': _to_int(inbound.get('message_db_id')),
                    'content': message.text or '',
                    'sender_type': 'lead',
                    'timestamp': None,
                    'attachment_url': None,
                    'file_type': 'voice' if message.is_voice else None,
                },
            },
        )
        if org_id is not None:
            await broadcast_to_org_async(
                organization_id=org_id,
                payload={
                    'type': 'conversation.list_item.updated',
                    'conversation_id': lead_id_int,
                    'lead_id': lead_id_int,
                    'last_message': 'Voice message' if message.is_voice else (message.text or ''),
                    'last_message_time': None,
                    'indicator_type': 'lead',
                    'ai_enabled': bool(inbound.get('ai_enabled', True)),
                    'escalation_status': bool(inbound.get('escalated', False)),
                },
            )

    if inbound.get('ignore'):
        return {'ok': True}

    if lead_id_int is None:
        return {'ok': True}

    agent_id = _to_int(inbound.get('agent_id'))
    if agent_id is None:
        return {'ok': True}
    
    if message.has_media and not message.is_voice:
        return {'ok': True}

    # For voice messages, download audio bytes for direct model processing
    voice_audio_bytes: bytes | None = None
    if message.is_voice and message.voice_media_id:
        if channel_type == 'whatsapp':
            voice_audio_bytes = await anyio.to_thread.run_sync(
                lambda: _download_whatsapp_voice(service, message.voice_media_id)
            )
        elif channel_type == 'telegram':
            voice_audio_bytes = await anyio.to_thread.run_sync(
                lambda: _download_telegram_voice(service, message.voice_media_id)
            )

    async def _handle_batch(batched_text: str) -> None:
        async def send_message(text: str) -> None:
            adapter.send_message(
                service=service,
                message=message,
                text=text
            )
        
        def escalation_message_getter() -> str:
            # flow-configured message first, neutral fallback otherwise
            try:
                from tools.getAi import get_active_flows
                flow_message = (get_active_flows(agent_id=agent_id) or {}).get('escalation_message')
                if flow_message and str(flow_message).strip():
                    return str(flow_message).strip()
            except Exception as e:
                _ = e
            return "I'm connecting you with our team now. They'll be with you shortly to help you further. 👋"
        
        # SaaS Phase 1: chat memory ids are tenant-scoped for non-default
        # tenants (lead ids collide across org databases). The default
        # tenant keeps the bare id so existing conversations continue.
        from infra.tenants import get_org as _get_org, DEFAULT_ORG as _DEF
        _org = _get_org()
        _sid = str(lead_id_int) if _org == _DEF else f"{_org}:{lead_id_int}"
        await handle_agent_batch(
            batched_text=batched_text,
            user_id=message.user_id if (_org == _DEF or not message.user_id) else f"{_org}:{message.user_id}",
            session_id=_sid,
            lead_id=lead_id_int,
            channel_agent_id=int(agent_id),
            send_message=send_message,
            escalation_message_getter=escalation_message_getter,
            voice_audio_bytes=voice_audio_bytes,
            known_phone=inbound.get('known_phone'),
        )

    # SaaS Phase 3: in QUEUE_MODE=redis this process is a thin GATEWAY - the
    # AI turn runs in a separate worker. Voice turns carry raw audio bytes
    # and bypass the queue (processed in-process as before).
    from api.routes.shared.redis_queue import queue_enabled, enqueue_job
    if queue_enabled() and voice_audio_bytes is None:
        from infra.tenants import get_org as _qorg
        enqueue_job(key=batch_key, text=message.text, job={
            'org': _qorg(),
            'channel_type': str(channel_type),
            'channel_id': str(channel_id),
            'chat_id': str(message.chat_id),
            'user_id': message.user_id,
            'message_id': message.message_id,
            'lead_id': lead_id_int,
            'agent_id': int(agent_id),
            'known_phone': inbound.get('known_phone'),
        })
        return {'ok': True}

    await enqueue(key=batch_key, text=message.text, debounce_seconds=1.5, handler=_handle_batch)
    return {'ok': True}
