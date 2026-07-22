# msg processor - no more DB sessions, pure API + websocket
import anyio
from dataclasses import dataclass
import json
import re
from typing import Awaitable, Callable

from infra.django_api import post as django_post
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async
from api.routes.shared.agent_singleton import get_agent
from api.routes.shared.channel_handler import (
    save_incoming_message_and_charge,
    save_message,
    update_lead_ai_summary,
    update_lead_from_payload,
)


def _to_int(value):
    return int(value) if value is not None else None


@dataclass(frozen=True)
class LeadContext:
    lead_id: int
    org_id: int | None
    channel_agent_id: int


def _summary_to_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return getattr(value, 'summary', '') or ''


def _extract_last_json_object(text: str) -> dict | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`')

    cleaned = cleaned.replace('```json', '').replace('```JSON', '').replace('```', '')

    decoder = json.JSONDecoder()
    best_obj: dict | None = None
    best_end = -1

    for i, ch in enumerate(cleaned):
        if ch != '{':
            continue
        try:
            obj, end = decoder.raw_decode(cleaned[i:])
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        absolute_end = i + int(end)
        if absolute_end >= best_end:
            best_end = absolute_end
            best_obj = obj

    if best_obj is not None:
        return best_obj

    blocks = re.findall(r'\{[\s\S]*\}', cleaned)
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj

    return None


async def _broadcast_escalation(*, conversation_id: int, escalation_message: str, org_id: int | None) -> None:
    await broadcast_to_conversation_async(
        conversation_id=conversation_id,
        payload={
            'type': 'conversation.updated',
            'conversation_id': conversation_id,
            'escalation_status': True,
            'indicator_type': 'escalation',
        },
    )

    if org_id is None:
        return

    await broadcast_to_org_async(
        organization_id=int(org_id),
        payload={
            'type': 'conversation.list_item.updated',
            'conversation_id': conversation_id,
            'lead_id': conversation_id,
            'last_message': escalation_message,
            'last_message_time': None,
            'indicator_type': 'escalation',
            'ai_enabled': True,
            'escalation_status': True,
        },
    )


async def _send_escalation(
    *,
    lead_id: int,
    escalation_message: str,
    send_message: Callable[[str], Awaitable[None]],
) -> tuple[int | None, int | None]:
    org_id = None
    try:
        response = django_post('/api/agent/v1/escalation/', {
            'lead_id': int(lead_id),
            'message': escalation_message,
        })
        if response.get('ok'):
            org_id = _to_int(response.get('organization_id'))
    except Exception as e:
        _ = e

    await send_message(escalation_message)
    save_message(lead_id=int(lead_id), content=escalation_message, sender_type='ai')
    return lead_id, org_id


def prepare_lead_context(
    *,
    channel_id: str,
    chat_id: str,
    channel_type: str,
    content: str,
    message_id: str | None,
    organization_id: int | None,
    branch_id: int | None,
    channel_agent_id: int | None,
) -> tuple[LeadContext | None, bool]:
    batch_key = f"{channel_type}:{channel_id}:{chat_id}"
    charge_key = f"ai_msg:{batch_key}:{message_id}"

    lead_id, ai_enabled, charged, escalated = save_incoming_message_and_charge(
        channel_id=str(channel_id),
        chat_id=str(chat_id),
        channel_type=channel_type,
        content=content,
        organization_id=organization_id,
        branch_id=branch_id,
        charge_key=charge_key,
    )

    if lead_id is None:
        return None, True

    if escalated or (not ai_enabled) or (not charged):
        return None, True

    if channel_agent_id is None:
        return None, True

    lead_id_int = _to_int(lead_id)
    if lead_id_int is None:
        return None, True

    return LeadContext(lead_id=lead_id_int, org_id=_to_int(organization_id), channel_agent_id=int(channel_agent_id)), False


async def handle_agent_batch(
    *,
    batched_text: str,
    user_id: str,
    session_id: str,
    lead_id: int,
    channel_agent_id: int,
    send_message: Callable[[str], Awaitable[None]],
    escalation_message_getter: Callable[[], str],
    voice_audio_bytes: bytes | None = None,
) -> None:
    from core.manager import supports_audio

    is_voice = voice_audio_bytes is not None and len(voice_audio_bytes) > 0

    # Voice message handling
    if is_voice and not supports_audio():
        unsupported_msg = "Voice messages are not supported yet."
        await send_message(unsupported_msg)
        save_message(lead_id=lead_id, content=unsupported_msg, sender_type='ai')
        return

    try:
        agent = get_agent(channel_agent_id)
    except ValueError:
        return

    run_kwargs: dict = dict(user_id=user_id, session_id=session_id, debug_mode=False)
    
    from datetime import datetime
    from zoneinfo import ZoneInfo
    kolkata_tz = ZoneInfo('Asia/Kolkata')
    now = datetime.now(kolkata_tz)
    current_date = now.strftime('%Y-%m-%d')
    current_time = now.strftime('%H:%M')
    current_datetime = now.isoformat()
    date_context = f"Context: Today is {current_date} and current time is {current_time} (Asia/Kolkata, {current_datetime})."
    
    batched_text = f"{date_context}\n\n{batched_text}"
    
    if is_voice:
        from agno.media import Audio
        run_kwargs['audio'] = [Audio(content=voice_audio_bytes, format='ogg')]

    run_output = await anyio.to_thread.run_sync(
        lambda: agent.run(batched_text, **run_kwargs)
    )

    # Get the agent's session summary (same instance that's handling the conversation)
    session_summary = None
    try:
        summary_obj = agent.get_session_summary(session_id=session_id)
        if summary_obj:
            session_summary = getattr(summary_obj, 'summary', None)
    except Exception as e:
        _ = e
    
    # Fall back to run_output.summary if session summary is not available
    summary_to_save = session_summary or getattr(run_output, 'summary', None)
    update_lead_ai_summary(lead_id=lead_id, ai_summary=_summary_to_text(summary_to_save))
    data = _extract_last_json_object(run_output.content)
    if not isinstance(data, dict):
        return
    lead_payload = data.get('lead') or {}
    if data.get('status') is not None:
        lead_payload['status'] = data['status']
    if data.get('source') is not None:
        lead_payload['source'] = data['source']
    if data.get('is_chat_completed') is not None:
        lead_payload['is_chat_completed'] = bool(data['is_chat_completed'])
    if lead_payload:
        update_lead_from_payload(lead_id=lead_id, payload=lead_payload)

    if bool(data.get('escalation')):
        escalation_message = data.get('message', '') or escalation_message_getter()
        _, org_id = await _send_escalation(
            lead_id=lead_id,
            escalation_message=escalation_message,
            send_message=send_message,
        )
        await _broadcast_escalation(conversation_id=lead_id, escalation_message=escalation_message, org_id=org_id)
        return

    if 'message' not in data:
        return

    message_text = data['message']
    await send_message(message_text)
    save_message(lead_id=lead_id, content=message_text, sender_type='ai')

