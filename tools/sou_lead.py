# save/update lead - Django handles the DB, we just POST machane
import json
from infra.django_api import post


def _as_dict(data):
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    raise TypeError("data must be dict or JSON string")


def save_update_lead(data: dict):
    payload = _as_dict(data)
    if not payload:
        return None

    lead = payload.get("lead") or {}
    phone = (lead.get("phone_number") or "").replace("-", "").replace(" ", "").strip()
    if not phone:
        return None

    body = {
        "name": (lead.get("name") or "").strip() or None,
        "phone_number": phone,
        "email": (lead.get("email") or "").strip() or None,
        "location": (lead.get("location") or "").strip() or None,
        "health_conditions": (lead.get("health_condition") or "").strip() or None,
        "assessment_call_date": (lead.get("assessment_call_date") or "").strip() or None,
        "assessment_call_time": (lead.get("assessment_call_time") or "").strip() or None,
        "status": payload.get("status"),
        "source": payload.get("source"),
        "channel_id": payload.get("channel_id"),
        "channel_type": payload.get("channel_type"),
    }

    resp = post('/api/agent/v1/leads/save/', body)
    return resp.get('lead_id') if resp.get('ok') else None