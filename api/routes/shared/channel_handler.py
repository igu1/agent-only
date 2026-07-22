# channel handler - all DB ops delegated to Django API now
import logging
from infra.django_api import post


def _safe_int(value) -> int | None:
    return int(value) if value is not None else None


def save_incoming_message_and_charge(
    *,
    channel_id: str,
    chat_id: str,
    channel_type: str,
    content: str,
    organization_id: int | None,
    branch_id: int | None,
    charge_key: str,
) -> tuple[int | None, bool, bool, bool]:
    try:
        resp = post('/api/agent/v1/inbound/save-and-charge/', {
            'channel_id': str(channel_id),
            'chat_id': str(chat_id),
            'channel_type': channel_type,
            'content': content,
            'organization_id': organization_id,
            'branch_id': branch_id,
            'charge_key': charge_key,
        })
        if not resp.get('ok'):
            return None, False, False, True
        return (
            _safe_int(resp.get('lead_id')),
            bool(resp.get('ai_enabled', False)),
            bool(resp.get('charged', False)),
            bool(resp.get('escalated', True)),
        )
    except Exception:
        logging.exception("save_incoming_message_and_charge API call failed")
        return None, False, False, True


def save_message(*, lead_id: int, content: str, sender_type: str) -> None:
    try:
        post('/api/agent/v1/messages/save/', {
            'lead_id': int(lead_id),
            'content': content,
            'sender_type': sender_type,
        })
    except Exception:
        logging.exception("save_message API call failed (lead_id=%s)", lead_id)


def update_lead_from_payload(*, lead_id: int, payload: dict) -> None:
    try:
        post('/api/agent/v1/leads/update/', {
            'lead_id': int(lead_id),
            **{k: v for k, v in payload.items() if v is not None},
        })
    except Exception:
        logging.exception("update_lead_from_payload API call failed (lead_id=%s)", lead_id)


def update_lead_ai_summary(*, lead_id: int, ai_summary: str | None) -> bool:
    summary = (ai_summary or '').strip()
    if not summary:
        return False
    try:
        resp = post('/api/agent/v1/leads/update-summary/', {
            'lead_id': int(lead_id),
            'ai_summary': summary,
        })
        return resp.get('changed', False)
    except Exception:
        logging.exception("update_lead_ai_summary API call failed (lead_id=%s)", lead_id)
        return False
