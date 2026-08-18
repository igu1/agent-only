# realtime fan-out - two modes:
#   REALTIME_MODE=channels (default): push into Django Channels' Redis layer
#   REALTIME_MODE=http: POST /api/agent/v1/events/ so a .NET/SignalR backend fans out
import logging
import os

logger = logging.getLogger('agent.ws_bridge')

_layer = None


def _mode() -> str:
    return (os.getenv('REALTIME_MODE') or 'channels').strip().lower()


def _get_layer():
    global _layer
    if _layer is None:
        from channels_redis.core import RedisChannelLayer
        host = os.getenv('REDIS_HOST', 'localhost')
        port = int(os.getenv('REDIS_PORT', '6379'))
        _layer = RedisChannelLayer(hosts=[(host, port)])
    return _layer


def _post_event(*, conversation_id: int | None = None, organization_id: int | None = None, payload: dict) -> None:
    from infra.django_api import post
    post('/api/agent/v1/events/', {
        'event': payload.get('type') or 'unknown',
        'conversation_id': conversation_id,
        'organization_id': organization_id,
        'payload': payload,
    })


# broadcasts are best-effort side channels - a realtime failure must never
# break the message pipeline, so every public function swallows and logs

def broadcast_to_conversation(*, conversation_id: int, payload: dict) -> None:
    try:
        if _mode() == 'http':
            _post_event(conversation_id=int(conversation_id), payload=payload)
            return
        from asgiref.sync import async_to_sync
        layer = _get_layer()
        async_to_sync(layer.group_send)(f"conv_{int(conversation_id)}", {"type": "broadcast", "payload": payload})
    except Exception as e:
        logger.warning('broadcast_to_conversation failed: %s', e)


def broadcast_to_org(*, organization_id: int, payload: dict) -> None:
    try:
        if _mode() == 'http':
            _post_event(organization_id=int(organization_id), payload=payload)
            return
        from asgiref.sync import async_to_sync
        layer = _get_layer()
        async_to_sync(layer.group_send)(f"org_{int(organization_id)}", {"type": "broadcast", "payload": payload})
    except Exception as e:
        logger.warning('broadcast_to_org failed: %s', e)


async def broadcast_to_conversation_async(*, conversation_id: int, payload: dict) -> None:
    try:
        if _mode() == 'http':
            import anyio
            await anyio.to_thread.run_sync(
                lambda: _post_event(conversation_id=int(conversation_id), payload=payload)
            )
            return
        layer = _get_layer()
        await layer.group_send(f"conv_{int(conversation_id)}", {"type": "broadcast", "payload": payload})
    except Exception as e:
        logger.warning('broadcast_to_conversation_async failed: %s', e)


async def broadcast_to_org_async(*, organization_id: int, payload: dict) -> None:
    try:
        if _mode() == 'http':
            import anyio
            await anyio.to_thread.run_sync(
                lambda: _post_event(organization_id=int(organization_id), payload=payload)
            )
            return
        layer = _get_layer()
        await layer.group_send(f"org_{int(organization_id)}", {"type": "broadcast", "payload": payload})
    except Exception as e:
        logger.warning('broadcast_to_org_async failed: %s', e)
