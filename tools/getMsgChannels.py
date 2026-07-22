# channel fetchers - API alle, DB illa
from infra.django_api import get
from infra.cache_utils import cached_query, get_cache_key


def get_active_channel(channel_id: str) -> dict | None:
    def fetch():
        resp = get(f'/api/agent/v1/channels/{channel_id}/')
        return resp.get('data') if resp.get('ok') else None
    return cached_query(get_cache_key('active_channel', channel_id), fetch)


def get_active_channel_by_type(channel_type: str, channel_id: str) -> dict | None:
    channel = get_active_channel(channel_id)
    if channel and channel.get('channel_type') == channel_type:
        return channel
    return None


def get_whatsapp_channel(channel_id: str | None = None) -> dict | None:
    if channel_id:
        return get_active_channel_by_type('whatsapp', channel_id)
    def fetch():
        resp = get('/api/agent/v1/channels/default/whatsapp/')
        return resp.get('data') if resp.get('ok') else None
    return cached_query(get_cache_key('active_channel', 'whatsapp_default'), fetch)


def get_telegram_channel(channel_id: str) -> dict | None:
    return get_active_channel_by_type('telegram', channel_id)


def get_instagram_channel(channel_id: str) -> dict | None:
    return get_active_channel_by_type('instagram', channel_id)
