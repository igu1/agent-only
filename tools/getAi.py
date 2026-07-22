# ai data fetchers - DB gone, API vibes only machane
from urllib.parse import urlencode
from infra.django_api import get
from infra.cache_utils import cached_query, get_cache_key

_DEFAULT_PROFILE = {
    'id': None,
    'business_name': 'Cronomind AI',
    'business_description': 'AI-powered CRM and automation platform',
    'responsibilities': '',
    'tone_preference': 'professional',
}


def _api_get(path: str, params: dict | None = None) -> dict:
    filtered = {k: v for k, v in (params or {}).items() if v is not None}
    full_path = f'{path}?{urlencode(filtered)}' if filtered else path
    return get(full_path)


def get_active_profile(*, agent_id: int | None = None) -> dict:
    def fetch():
        resp = _api_get('/api/agent/v1/ai-profile/', {'agent_id': agent_id})
        return resp.get('data', _DEFAULT_PROFILE) if resp.get('ok') else _DEFAULT_PROFILE
    return cached_query(get_cache_key('ai_profile_active', agent_id), fetch)


def get_active_locations(*, agent_id: int | None = None) -> list[dict]:
    def fetch():
        resp = _api_get('/api/agent/v1/locations/', {'agent_id': agent_id})
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('ai_locations_active', agent_id), fetch)


def get_active_faqs(*, agent_id: int | None = None) -> list[dict]:
    def fetch():
        resp = _api_get('/api/agent/v1/faqs/', {'agent_id': agent_id})
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('ai_faqs_active', agent_id), fetch)


def get_active_knowledge_base(*, agent_id: int | None = None) -> list[dict]:
    def fetch():
        resp = _api_get('/api/agent/v1/knowledge-base/', {'agent_id': agent_id})
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('ai_knowledge_base_active', agent_id), fetch)


def get_active_flows(*, agent_id: int | None = None) -> dict:
    def fetch():
        resp = _api_get('/api/agent/v1/flows/', {'agent_id': agent_id})
        return resp.get('data', {}) if resp.get('ok') else {}
    return cached_query(get_cache_key('ai_flow_active', agent_id), fetch)


def get_flow_nodes(flow_id: int) -> list[dict]:
    def fetch():
        resp = _api_get(f'/api/agent/v1/flows/{flow_id}/nodes/')
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('ai_flow_nodes', flow_id), fetch)


def get_flow_qa_examples(flow_id: int) -> list[dict]:
    def fetch():
        resp = _api_get(f'/api/agent/v1/flows/{flow_id}/qa-examples/')
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('ai_flow_qa_examples', flow_id), fetch)


def get_rule_bot_rules(*, agent_id: int | None = None) -> list[dict]:
    def fetch():
        resp = _api_get('/api/agent/v1/rule-bot-rules/', {'agent_id': agent_id})
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('rule_bot_rules_active', agent_id), fetch)


def get_active_products(*, agent_id: int | None = None) -> list[dict]:
    def fetch():
        resp = _api_get('/api/agent/v1/products/', {'agent_id': agent_id})
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('products_active', agent_id), fetch)


def get_product_variants(product_id: int) -> list[dict]:
    def fetch():
        resp = _api_get(f'/api/agent/v1/products/{product_id}/variants/')
        return resp.get('data', []) if resp.get('ok') else []
    return cached_query(get_cache_key('product_variants', product_id), fetch)
