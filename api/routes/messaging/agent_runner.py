import anyio
from dataclasses import dataclass
from typing import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo
from api.routes.messaging.parsing import extract_last_json_object, summary_to_text, to_int
from api.routes.shared.agent_singleton import get_agent
from api.routes.shared.channel_handler import save_message, update_lead_ai_summary, update_lead_from_payload
from infra.ws_bridge import broadcast_to_conversation_async, broadcast_to_org_async
from infra.django_api import post as django_post


@dataclass(frozen=True)
class LeadContext:
    lead_id: int
    org_id: int | None
    channel_agent_id: int


async def _broadcast_escalation(*, lead_id: int, escalation_message: str, org_id: int | None) -> None:
    await broadcast_to_conversation_async(
        conversation_id=lead_id,
        payload={
            'type': 'conversation.updated',
            'conversation_id': lead_id,
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
            'conversation_id': lead_id,
            'lead_id': lead_id,
            'last_message': escalation_message,
            'last_message_time': None,
            'indicator_type': 'escalation',
            'ai_enabled': True,
            'escalation_status': True,
        },
    )


async def _send_escalation(*, lead_id: int, escalation_message: str, send_message: Callable[[str], Awaitable[None]]):
    org_id = None
    try:
        resp = django_post('/api/agent/v1/escalation/', {'lead_id': int(lead_id), 'message': escalation_message})
        if resp.get('ok'):
            org_id = to_int(resp.get('organization_id'))
    except Exception:
        pass

    await send_message(escalation_message)
    save_message(lead_id=int(lead_id), content=escalation_message, sender_type='ai')
    await _broadcast_escalation(lead_id=lead_id, escalation_message=escalation_message, org_id=org_id)


async def run_agent_reply(
    *,
    text: str,
    user_id: str,
    session_id: str,
    lead_id: int,
    channel_agent_id: int,
    send_message: Callable[[str], Awaitable[None]],
    send_media_message: Callable[[int, str, str], Awaitable[None]],
    voice_audio_bytes: bytes | None = None,
) -> None:
    from core.manager import supports_audio

    is_voice = bool(voice_audio_bytes)
    if is_voice and not supports_audio():
        msg = 'Voice messages are not supported yet.'
        await send_message(msg)
        save_message(lead_id=lead_id, content=msg, sender_type='ai')
        return

    try:
        agent = get_agent(channel_agent_id)
    except ValueError:
        return

    run_kwargs: dict = {'user_id': user_id, 'session_id': session_id, 'debug_mode': False}

    tz = ZoneInfo('Asia/Kolkata')
    now = datetime.now(tz)
    context = f"Context: Today is {now.strftime('%Y-%m-%d')} and current time is {now.strftime('%H:%M')} (Asia/Kolkata, {now.isoformat()})."
    prompt = f"{context}\n\n{text}"

    if is_voice:
        from agno.media import Audio
        run_kwargs['audio'] = [Audio(content=voice_audio_bytes or b'', format='ogg')]

    run_output = await anyio.to_thread.run_sync(lambda: agent.run(prompt, **run_kwargs))

    session_summary = None
    try:
        summary_obj = agent.get_session_summary(session_id=session_id)
        if summary_obj:
            session_summary = getattr(summary_obj, 'summary', None)
    except Exception:
        pass

    update_lead_ai_summary(lead_id=lead_id, ai_summary=summary_to_text(session_summary or getattr(run_output, 'summary', None)))

    data = extract_last_json_object(run_output.content)
    print(data)
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
        await _send_escalation(
            lead_id=lead_id,
            escalation_message=(data.get('message')),
            send_message=send_message,
        )
        return

    msg = (data.get('message') or '').strip()
    if not msg:
        return
    await send_message(msg)
    save_message(lead_id=lead_id, content=msg, sender_type='ai')
