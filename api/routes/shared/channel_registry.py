import logging
from typing import Any

_log = logging.getLogger(__name__)


class ChannelRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, channel_type: str, adapter: Any) -> None:
        self._adapters[str(channel_type)] = adapter

    def get(self, channel_type: str) -> Any:
        return self._adapters[str(channel_type)]

    def has(self, channel_type: str) -> bool:
        return str(channel_type) in self._adapters


registry = ChannelRegistry()


# How a request on each channel can name its organisation. A channel missing
# from this map has no way at all - which is the most useful thing to say back.
_ORG_ROUTES = {
    'webchat': 'the org-prefixed URL /webhooks/<org>/webchat/<channel_id>',
    'whatsapp': ('the org-prefixed URL /webhooks/<org>/whatsapp/<channel_id>/, '
                 'or list this phone_number_id under that org in tenants.json'),
}


def require_tenant(channel: str) -> None:
    """Refuse a channel request that never identified its organisation.

    Only bites under STRICT_TENANCY, and only when nothing resolved a tenant:
    an org-prefixed URL, or a WhatsApp payload whose phone_number_id is in the
    registry, has already called set_org and passes straight through.

    Raised here, at the edge, so the answer is a 400 the caller can act on -
    rather than an inquiry quietly filed in another company's database, which
    is what serving the default tenant actually means once the registry holds
    more than one organisation.
    """
    from fastapi import HTTPException
    from infra.tenants import org_resolved, strict_tenancy

    if not strict_tenancy() or org_resolved():
        return

    how = _ORG_ROUTES.get(channel)
    _log.warning('refused a %s request that named no organization '
                 '(STRICT_TENANCY is on)', channel)
    raise HTTPException(
        status_code=400,
        detail='organization could not be determined for this %s request - %s'
               % (channel,
                  ('use %s' % how) if how else
                  ('this channel has no org-prefixed route, so it cannot be '
                   'served while STRICT_TENANCY is on')))
