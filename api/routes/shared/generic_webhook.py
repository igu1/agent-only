import logging

import anyio
from fastapi import HTTPException

from infra.django_api import post as django_post
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async
from api.routes.shared.message_batcher import enqueue
from api.routes.shared.msgchannel_processor import handle_agent_batch
from api.routes.shared.channel_adapters import ChannelAdapter, IncomingMessage
from api.routes.shared.channel_handler import save_message
from api.routes.shared.channel_registry import registry

logger = logging.getLogger(__name__)

# Statuses that mean "try again shortly", not "this request was wrong".
# 503 covers connection refused / timeout too: django_api maps every transport
# failure onto it, since no HTTP status ever existed for those.
_TEMPORARY_BACKEND_FAILURES = frozenset({500, 502, 503, 504})

# What a parent hears after their conversation has been handed to a person.
# It promises a follow-up, NOT a reply in this chat: staff have no way to send
# into a conversation yet, and an acknowledgement that implied otherwise would
# be a promise the system cannot keep.
ESCALATED_ACK = (
    "Thanks - our admissions team has your message and will follow up with you "
    "directly. I've paused here so a colleague can take over."
)

# One acknowledgement per conversation per window. A parent typing a paragraph
# as five messages asked once, not five times.
_ACK_COOLDOWN_SECONDS = 15 * 60
_acked: dict[str, float] = {}


def _should_ack(key: str, now: float) -> bool:
    """True at most once per conversation per window.

    In-process, so it does not survive a restart and is per-worker: the worst
    case is a second courtesy line, which is why this is not worth a column on
    the conversation. Old keys are dropped here rather than by a timer.
    """
    for stale in [k for k, t in _acked.items() if now - t > _ACK_COOLDOWN_SECONDS]:
        _acked.pop(stale, None)
    if now - _acked.get(key, float('-inf')) < _ACK_COOLDOWN_SECONDS:
        return False
    _acked[key] = now
    return True


_UNREADABLE_MEDIA_REPLY = {
    'image': "I can't read images here yet.",
    'sticker': "I can't read images here yet.",
    'document': "I can't open documents here yet.",
    'video': "I can't watch videos here yet.",
}
_UNREADABLE_MEDIA_DEFAULT = "I can't process attachments here yet."
_UNREADABLE_MEDIA_ASK = " Please type the information you'd like me to use."

# One reply per conversation per minute. A parent sending an album of six
# photos asked one question, not six.
_MEDIA_ACK_COOLDOWN_SECONDS = 60
_media_acked: dict[str, float] = {}


def _should_ack_media(key: str, now: float) -> bool:
    for stale in [k for k, t in _media_acked.items()
                  if now - t > _MEDIA_ACK_COOLDOWN_SECONDS]:
        _media_acked.pop(stale, None)
    if now - _media_acked.get(key, float('-inf')) < _MEDIA_ACK_COOLDOWN_SECONDS:
        return False
    _media_acked[key] = now
    return True


async def _acknowledge_unreadable_media(*, adapter, service, message,
                                        lead_id, batch_key) -> None:
    """Tell the parent plainly that the attachment cannot be read.

    Never raises: a courtesy reply is not worth failing a webhook Meta would
    then retry.
    """
    import time as _time
    if not _should_ack_media(batch_key, _time.monotonic()):
        return
    text = (_UNREADABLE_MEDIA_REPLY.get(message.media_kind or '',
                                        _UNREADABLE_MEDIA_DEFAULT)
            + _UNREADABLE_MEDIA_ASK)
    try:
        if adapter.send_message(service=service, message=message, text=text) \
                and lead_id is not None:
            save_message(lead_id=lead_id, content=text, sender_type='ai')
    except Exception:
        logger.exception('unreadable-media acknowledgement failed for chat %s',
                         message.chat_id)


def _to_int(value):
    return int(value) if value is not None else None


def _to_iso(epoch_seconds: float | None) -> str | None:
    """Send time -> ISO 8601 carrying the tenant's UTC offset. The backend
    stamps a conversation's CHAT START TIME from the first inbound message it
    receives, so this must be when the parent sent it (WhatsApp reports the
    real send time), not when a retried webhook happened to reach us."""
    if not epoch_seconds:
        return None
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from infra.config import get_timezone
    try:
        tz = ZoneInfo(get_timezone())
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(float(epoch_seconds), tz).isoformat()


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

    # WHICH INSTITUTION this chat is about, settled before the conversation:
    # the host page's dropdown sends it with the message; failing that, a
    # channel bound to one branch already answers it. Only when both are empty
    # does the assistant have to ask.
    institution_id = message.institution_id
    if institution_id is None:
        try:
            institution_id = adapter.get_context(channel_id=channel_id, message=message).branch_id
        except Exception as e:
            _ = e
            institution_id = None

    # SaaS Phase 1: batch/lock keys are tenant-scoped - every org has a
    # "channel 1", so without the org two schools' chats could collide
    from infra.tenants import get_org
    batch_key = f"{get_org()}:{channel_type}:{channel_id}:{message.chat_id}"
    adapter.send_typing_indicator(service=service, message=message)        

    sent_at = _to_iso(message.sent_at)
    # the widget shows the chat start time from THIS value (the first message
    # of a chat is where it starts), so it never has to trust a browser clock
    ack = {'ok': True, 'sent_at': sent_at}

    inbound = django_post(
        '/api/agent/v1/inbound/',
        {
            'channel_type': str(channel_type),
            'channel_id': str(channel_id),
            'chat_id': str(message.chat_id),
            'message_id': message.message_id,
            'text': message.text,
            'has_media': bool(message.has_media),
            'sent_at': sent_at,
        },
        extra_headers={
            'X-Telegram-Bot-Api-Secret-Token': (headers or {}).get('x_telegram_bot_api_secret_token'),
        },
    )

    if not inbound.get('ok'):
        # A business refusal - unknown channel, blank chat id - arrives on HTTP
        # 200 with {"ok": false} and NO status key, because urlopen succeeded
        # and this is just the parsed body. Only a transport or HTTP failure
        # carries `status`. Defaulting a missing one to 500 would turn every
        # such refusal into a retry storm, so absent means permanent.
        raw_status = inbound.get('status')
        code = int(raw_status) if raw_status is not None else None
        if code == 401:
            raise HTTPException(status_code=401, detail='Unauthorized')
        if code == 404:
            raise HTTPException(status_code=404, detail='Channel not found or inactive')
        if code in _TEMPORARY_BACKEND_FAILURES:
            # /inbound/ is what PERSISTS the parent's message - nothing before
            # it writes anything - so answering 200 here would tell the channel
            # a message we never took had been handled, and it would never be
            # sent again. 503 hands it back for the channel's own retry, which
            # already exists and already backs off. Stop the turn here: no
            # agent, no lead processing, nothing that assumes a conversation.
            logger.warning(
                'backend unavailable (status=%s) - returning 503 so %s retries '
                'the inbound webhook for chat %s',
                code, channel_type, message.chat_id)
            raise HTTPException(status_code=503,
                                detail='Backend temporarily unavailable')
        return ack

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
                    'timestamp': sent_at,
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
                    'last_message_time': sent_at,
                    'indicator_type': 'lead',
                    'ai_enabled': bool(inbound.get('ai_enabled', True)),
                    'escalation_status': bool(inbound.get('escalated', False)),
                },
            )

    if inbound.get('ignore'):
        # Escalated: a person owns this conversation, so the AI stays out of it
        # - but the parent is still typing into what looks like a dead channel.
        # Answer once, from the server, without waking anything: this is
        # upstream of the batcher and of handle_agent_batch, so no agent is
        # built, and no conversation state is written.
        # The OTHER ignore reasons (unknown or inactive channel, blank chat id)
        # have no conversation and nobody expecting us, so they stay silent.
        if inbound.get('escalated') and lead_id_int is not None:
            import time as _time
            if _should_ack(batch_key, _time.monotonic()):
                try:
                    adapter.send_message(service=service, message=message,
                                         text=ESCALATED_ACK)
                    save_message(lead_id=lead_id_int, content=ESCALATED_ACK,
                                 sender_type='ai')
                except Exception:
                    logger.exception(
                        'escalated acknowledgement failed for conversation %s',
                        lead_id_int)
        return ack

    if lead_id_int is None:
        return ack

    agent_id = _to_int(inbound.get('agent_id'))
    if agent_id is None:
        return ack
    
    if message.has_media and not message.is_voice:
        # Something arrived that the assistant cannot read - a photographed
        # document, most often. Answering HERE, before an agent is built, is
        # deliberate: the model never sees it, so it cannot reply as though it
        # had read it. Saying nothing was the worst option, because a parent
        # who sends their child's birth certificate and hears nothing back
        # reasonably assumes it was received.
        await _acknowledge_unreadable_media(
            adapter=adapter, service=service, message=message,
            lead_id=lead_id_int, batch_key=batch_key)
        return ack

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
        async def send_message(text: str) -> bool:
            return adapter.send_message(
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
            channel_type=str(channel_type),
            institution_id=institution_id,
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
            'institution_id': institution_id,
        })
        return ack

    await enqueue(key=batch_key, text=message.text, debounce_seconds=1.5, handler=_handle_batch)
    return ack
