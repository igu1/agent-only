from api.routes.shared.channel_adapters import TelegramAdapter, WhatsAppAdapter, InstagramAdapter
from api.routes.shared.channel_registry import registry

registry.register('telegram', TelegramAdapter())
registry.register('whatsapp', WhatsAppAdapter())
registry.register('instagram', InstagramAdapter())

__all__ = []
