import anyio
from fastapi import HTTPException

from api.routes.messaging.agent_runner import run_agent_reply
from api.routes.messaging.parsing import to_int
from api.routes.messaging.voice_download import download_telegram_voice, download_whatsapp_voice
from api.routes.shared.channel_adapters import ChannelAdapter
from api.routes.shared.channel_registry import registry
from api.routes.shared.message_batcher import enqueue
from infra.django_api import post as django_post
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async


async def handle_incoming_webhook(*, channel_type: str, channel_id: str | None, payload: dict, headers: dict[str, str | None] | None = None) -> dict:
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

    key = f"{channel_type}:{channel_id}:{message.chat_id}"
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
        extra_headers={'X-Telegram-Bot-Api-Secret-Token': (headers or {}).get('x_telegram_bot_api_secret_token')},
    )

    if not inbound.get('ok'):
        code = int(inbound.get('status') or 500)
        if code == 401:
            raise HTTPException(status_code=401, detail='Unauthorized')
        if code == 404:
            raise HTTPException(status_code=404, detail='Channel not found or inactive')
        return {'ok': True}

    lead_id = to_int(inbound.get('lead_id'))
    org_id = to_int(inbound.get('organization_id'))

    if lead_id is not None:
        await broadcast_to_conversation_async(
            conversation_id=lead_id,
            payload={
                'type': 'message.created',
                'conversation_id': lead_id,
                'message': {
                    'id': to_int(inbound.get('message_db_id')),
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
                    'conversation_id': lead_id,
                    'lead_id': lead_id,
                    'last_message': 'Voice message' if message.is_voice else (message.text or ''),
                    'last_message_time': None,
                    'indicator_type': 'lead',
                    'ai_enabled': bool(inbound.get('ai_enabled', True)),
                    'escalation_status': bool(inbound.get('escalated', False)),
                },
            )

    if inbound.get('ignore'):
        return {'ok': True}

    if lead_id is None:
        return {'ok': True}

    agent_id = to_int(inbound.get('agent_id'))
    if agent_id is None:
        return {'ok': True}

    if message.has_media and not message.is_voice:
        return {'ok': True}

    voice_audio_bytes: bytes | None = None
    if message.is_voice and message.voice_media_id:
        if channel_type == 'whatsapp':
            voice_audio_bytes = await anyio.to_thread.run_sync(lambda: download_whatsapp_voice(service, message.voice_media_id))
        elif channel_type == 'telegram':
            voice_audio_bytes = await anyio.to_thread.run_sync(lambda: download_telegram_voice(service, message.voice_media_id))

    async def _handle_batch(batched_text: str) -> None:
        async def send_message(text: str) -> None:
            adapter.send_message(service=service, message=message, text=text)

        async def send_media_message(chat_id: int, media_type: str, link: str, caption: str | None = None, filename: str | None = None) -> None:
            adapter.send_media_message(service=service, message=message, media_type=media_type, link=link, caption=caption, filename=filename)

        await run_agent_reply(
            text=batched_text,
            user_id=message.user_id,
            session_id=str(lead_id),
            lead_id=lead_id,
            channel_agent_id=int(agent_id),
            send_message=send_message,
            send_media_message=send_media_message,
            voice_audio_bytes=voice_audio_bytes,
        )

    await enqueue(key=key, text=message.text, debounce_seconds=1.5, handler=_handle_batch)
    return {'ok': True}
