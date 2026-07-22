import os

from asgiref.sync import async_to_sync
from channels_redis.core import RedisChannelLayer


_layer = None


def _get_layer() -> RedisChannelLayer:
    global _layer
    if _layer is None:
        host = os.getenv('REDIS_HOST', 'localhost')
        port = int(os.getenv('REDIS_PORT', '6379'))
        _layer = RedisChannelLayer(hosts=[(host, port)])
    return _layer


def broadcast_to_conversation(*, conversation_id: int, payload: dict) -> None:
    layer = _get_layer()
    async_to_sync(layer.group_send)(f"conv_{int(conversation_id)}", {"type": "broadcast", "payload": payload})


def broadcast_to_org(*, organization_id: int, payload: dict) -> None:
    layer = _get_layer()
    async_to_sync(layer.group_send)(f"org_{int(organization_id)}", {"type": "broadcast", "payload": payload})


async def broadcast_to_conversation_async(*, conversation_id: int, payload: dict) -> None:
    layer = _get_layer()
    await layer.group_send(f"conv_{int(conversation_id)}", {"type": "broadcast", "payload": payload})


async def broadcast_to_org_async(*, organization_id: int, payload: dict) -> None:
    layer = _get_layer()
    await layer.group_send(f"org_{int(organization_id)}", {"type": "broadcast", "payload": payload})
