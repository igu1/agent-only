# lead statuses - via API, org scoped by agent_id
from infra.django_api import get
from infra.cache_utils import cached_query, get_cache_key


def get_status(*, agent_id: int | None = None) -> list[dict[str, int | str | bool]]:
    def fetch():
        path = '/api/agent/v1/statuses/'
        if agent_id:
            path += f'?agent_id={agent_id}'
        resp = get(path)
        return resp.get('data', []) if resp.get('ok') else []
    key = get_cache_key(f'lead_statuses_active_{agent_id or "all"}')
    return cached_query(key, fetch)

