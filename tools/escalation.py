# escalation - let Django do the heavy lifting
from agno.tools import tool
from infra.django_api import post


def set_escalation(*, lead_id: int | None = None, chat_id: str | None = None, channel_type: str | None = None) -> bool:
    resp = post('/api/agent/v1/escalation/', {
        'conversation_id': lead_id,
        'chat_id': chat_id,
        'channel_type': channel_type,
        'escalation_status': True,
    })
    return resp.get('ok', False)


def clear_escalation(*, lead_id: int | None = None, chat_id: str | None = None, channel_type: str | None = None) -> bool:
    resp = post('/api/agent/v1/escalation/', {
        'conversation_id': lead_id,
        'chat_id': chat_id,
        'channel_type': channel_type,
        'escalation_status': False,
    })
    return resp.get('ok', False)


@tool
def escalate_to_human(
    reason: str = '',
    lead_id: int | None = None,
    chat_id: str | None = None,
    channel_type: str | None = None,
) -> dict:
    """Escalate this conversation to a human by marking the lead as escalated."""
    ok = set_escalation(lead_id=lead_id, chat_id=chat_id, channel_type=channel_type)
    return {"ok": bool(ok), "reason": reason}
