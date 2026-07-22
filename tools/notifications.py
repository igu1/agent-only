# notifications - Django API alle, no more direct DB writes
from agno.tools import tool
from infra.django_api import post


@tool
def create_notification(
    user_id: int,
    title: str,
    message: str,
    notification_type: str = 'info',
    priority: str = 'medium',
    link: str = None,
    metadata: dict = None,
) -> dict:
    """Create a notification for a CRM user."""
    resp = post('/api/agent/v1/notifications/', {
        'user_id': user_id,
        'title': title,
        'message': message,
        'notification_type': notification_type,
        'priority': priority,
        'link': link,
        'metadata': metadata or {},
    })
    return resp if resp.get('ok') else {'success': False, 'error': resp.get('error', 'API call failed')}


@tool
def notify_conversation_escalated(
    conversation_id: int,
    contact_name: str,
    escalated_by_user_id: int,
    notify_owner: bool = True,
    notify_all_users: bool = False,
) -> dict:
    """Notify relevant users when a conversation is escalated."""
    resp = post('/api/agent/v1/notifications/escalation/', {
        'conversation_id': conversation_id,
        'contact_name': contact_name,
        'escalated_by_user_id': escalated_by_user_id,
        'notify_owner': notify_owner,
        'notify_all_users': notify_all_users,
    })
    return resp if resp.get('ok') else {'success': False, 'error': resp.get('error', 'API call failed')}


@tool
def notify_lead_created(
    lead_id: int,
    lead_name: str,
    created_by_user_id: int,
    notify_owner: bool = True,
) -> dict:
    """Notify relevant users when a new lead is created."""
    resp = post('/api/agent/v1/notifications/lead-created/', {
        'lead_id': lead_id,
        'lead_name': lead_name,
        'created_by_user_id': created_by_user_id,
        'notify_owner': notify_owner,
    })
    return resp if resp.get('ok') else {'success': False, 'error': resp.get('error', 'API call failed')}
