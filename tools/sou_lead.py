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
        "student_name": (lead.get("student_name") or "").strip() or None,
        "relation_id": lead.get("relation_id"),
        "class_id": payload.get("class_id"),
        "institution_id": payload.get("institution_id"),
        "inquiry_type_id": payload.get("inquiry_type_id"),
        "tour_date": (payload.get("tour_date") or "").strip() or None,
        "tour_time": (payload.get("tour_time") or "").strip() or None,
        "status": payload.get("status"),
        "source": payload.get("source"),
        "channel_id": payload.get("channel_id"),
        "channel_type": payload.get("channel_type"),
    }

    resp = post('/api/agent/v1/leads/save/', body)
    return resp.get('lead_id') if resp.get('ok') else None