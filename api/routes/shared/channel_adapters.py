import time
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException

_SERVICE_CACHE_TTL = 300


def _to_int(value: Any) -> int | None:
    return int(value) if value is not None else None


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: str
    user_id: str
    text: str
    message_id: str | None
    has_media: bool = False
    is_voice: bool = False
    voice_media_id: str | None = None


@dataclass(frozen=True)
class ChannelContext:
    organization_id: int | None
    branch_id: int | None
    agent_id: int | None


class ChannelAdapter(Protocol):
    channel_type: str

    def get_service(self, channel_id: str | None) -> Any: ...

    def validate(self, *, channel_id: str | None, service: Any, headers: dict[str, str | None]) -> None: ...

    def parse(self, *, service: Any, payload: dict) -> IncomingMessage | None: ...

    def get_context(self, *, channel_id: str | None, message: IncomingMessage) -> ChannelContext: ...

    def should_ignore(self, *, channel_id: str | None, service: Any, message: IncomingMessage) -> bool: ...

    def send_message(self, *, service: Any, message: IncomingMessage, text: str) -> None: ...

    def send_typing_indicator(self, *, service: Any, message: IncomingMessage) -> None: ...


class TelegramAdapter:
    channel_type = 'telegram'

    def __init__(self) -> None:
        self._services: dict[str, tuple[object, float]] = {}

    def get_service(self, channel_id: str | None) -> object:
        if not channel_id:
            raise HTTPException(status_code=404, detail='Telegram channel not found or inactive')
        cached = self._services.get(channel_id)
        if cached is not None and time.time() < cached[1]:
            return cached[0]

        from tools.getMsgChannels import get_telegram_channel
        channel = get_telegram_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail='Telegram channel not found or inactive')

        channel_config = {
            'bot_token': channel.get('bot_token') or channel.get('access_token', ''),
            'webhook_secret': channel.get('webhook_secret') or channel.get('verify_token', 'cronocrm'),
        }

        from services.telegram import TelegramService
        service = TelegramService(channel_config=channel_config)
        self._services[channel_id] = (service, time.time() + _SERVICE_CACHE_TTL)
        return service

    def validate(self, *, channel_id: str | None, service: object, headers: dict[str, str | None]) -> None:
        return None

    def parse(self, *, service: object, payload: dict) -> IncomingMessage | None:
        message = payload.get('message') or payload.get('edited_message')
        if not message:
            return None

        chat = message.get('chat') or {}
        chat_id = chat.get('id')
        if chat_id is None:
            return None

        text = message.get('text')
        photo = message.get('photo')
        video = message.get('video')
        document = message.get('document')
        voice = message.get('voice')
        audio = message.get('audio')
        if not text and not photo and not video and not document and not voice and not audio:
            return None

        has_media = bool(photo or video or document or voice or audio)
        is_voice = bool(voice or audio)
        if not text and is_voice:
            text = "[Voice message]"
        elif not text and has_media:
            text = "[Media received]"

        voice_media_id = None
        if is_voice:
            voice_obj = voice or audio
            voice_media_id = voice_obj.get('file_id') if isinstance(voice_obj, dict) else None

        from_user = message.get('from') or {}
        user_id = from_user.get('id')
        message_id = message.get('message_id') or payload.get('update_id')

        return IncomingMessage(
            chat_id=str(chat_id),
            user_id=str(user_id) if user_id is not None else str(chat_id),
            text=str(text or ''),
            message_id=str(message_id) if message_id is not None else None,
            has_media=has_media,
            is_voice=is_voice,
            voice_media_id=str(voice_media_id) if voice_media_id else None,
        )

    def get_context(self, *, channel_id: str | None, message: IncomingMessage) -> ChannelContext:
        return ChannelContext(None, None, None)

    def should_ignore(self, *, channel_id: str | None, service: object, message: IncomingMessage) -> bool:
        return message.text.startswith('/')

    def send_message(self, *, service: object, message: IncomingMessage, text: str) -> None:
        try:
            service.send_message(int(message.chat_id), text)
        except Exception as e:
            _ = e

    def send_typing_indicator(self, *, service: object, message: IncomingMessage) -> None:
        try:
            service.send_typing_indicator(int(message.chat_id))
        except Exception as e:
            _ = e


class WhatsAppAdapter:
    channel_type = 'whatsapp'

    def __init__(self) -> None:
        self._services: dict[str, tuple[object, float]] = {}

    def get_service(self, channel_id: str | None) -> object:
        cache_key = channel_id or 'default'
        cached = self._services.get(cache_key)
        if cached is not None and time.time() < cached[1]:
            return cached[0]

        from tools.getMsgChannels import get_whatsapp_channel
        channel = get_whatsapp_channel(channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail='WhatsApp channel not found or inactive')

        channel_config = {
            'phone_number_id': channel.get('phone_number_id', ''),
            'access_token': channel.get('access_token', ''),
            'verify_token': channel.get('verify_token', 'whatsapp_verify'),
            'api_version': channel.get('api_version', 'v21.0'),
        }

        from services.whatsapp import WhatsAppService
        service = WhatsAppService(channel_config=channel_config)
        self._services[cache_key] = (service, time.time() + _SERVICE_CACHE_TTL)
        return service

    def validate(self, *, channel_id: str | None, service: object, headers: dict[str, str | None]) -> None:
        return None

    def parse(self, *, service: object, payload: dict) -> IncomingMessage | None:
        try:
            entry = (payload.get('entry') or [])[0]
            change = (entry.get('changes') or [])[0]
            value = (change.get('value') or {})
            message = (value.get('messages') or [])[0]
        except Exception:
            return None

        phone_number = str(message.get('from') or '')
        if not phone_number:
            return None

        message_type = message.get('type')
        text = ''
        has_media = False
        is_voice = False
        voice_media_id = None
        if message_type == 'text':
            text = str(((message.get('text') or {}).get('body') or '')).strip()
        elif message_type in ('audio', 'voice'):
            text = '[Voice message]'
            has_media = True
            is_voice = True
            voice_media_id = (message.get('audio') or message.get('voice') or {}).get('id')
        if not text:
            return None

        msg_id = message.get('id')
        return IncomingMessage(
            chat_id=phone_number,
            user_id=f'wa_{phone_number}',
            text=text,
            message_id=str(msg_id) if msg_id is not None else None,
            has_media=has_media,
            is_voice=is_voice,
            voice_media_id=str(voice_media_id) if voice_media_id else None,
        )

    def get_context(self, *, channel_id: str | None, message: IncomingMessage) -> ChannelContext:
        return ChannelContext(None, None, None)

    def should_ignore(self, *, channel_id: str | None, service: object, message: IncomingMessage) -> bool:
        if message.text.startswith('/'):
            return True
        return False

    def send_message(self, *, service: object, message: IncomingMessage, text: str) -> None:
        try:
            phone_number = message.chat_id.lstrip('+')
            service.send_message(phone_number, text)
        except Exception as e:
            _ = e

    def send_typing_indicator(self, *, service: object, message: IncomingMessage) -> None:
        try:
            phone_number = message.chat_id.lstrip('+')
            service.send_typing_indicator(phone_number, message.message_id)
        except Exception as e:
            _ = e

    def send_media_message(self, *, service: object, message: IncomingMessage, media_type: str, link: str, caption: str | None = None, filename: str | None = None) -> None:
        try:
            phone_number = message.chat_id.lstrip('+')
            service.send_media_message(phone_number, media_type=media_type, link=link, caption=caption, filename=filename)
        except Exception as e:
            _ = e


# instagram

class InstagramAdapter:
    channel_type = 'instagram'

    def get_service(self, channel_id: str | None) -> object:
        if not channel_id:
            raise HTTPException(status_code=404, detail='Instagram channel not found or inactive')
        try:
            from tools.getMsgChannels import get_instagram_channel
            channel = get_instagram_channel(channel_id)
            if not channel:
                raise HTTPException(status_code=404, detail='Instagram channel not found or inactive')

            channel_config = {
                'access_token': channel.get('access_token', ''),
                'business_account_id': channel.get('business_account_id', ''),
                'verify_token': channel.get('verify_token', 'instagram_verify'),
                'api_version': channel.get('api_version', 'v18.0'),
            }

            from services.instagram import InstagramService
            service = InstagramService(channel_config=channel_config)
            return service
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f'Instagram service error: {str(e)}')

    def validate(self, *, channel_id: str | None, service: object, headers: dict[str, str | None]) -> None:
        return None

    def parse(self, *, service: object, payload: dict) -> IncomingMessage | None:
        try:
            if payload.get('object') == 'instagram':
                for entry in payload.get('entry', []):
                    for messaging in entry.get('messaging', []):
                        if 'message' in messaging:
                            sender_id = messaging['sender']['id']
                            message_data = messaging['message']
                            
                            text = message_data.get('text', '')
                            has_media = 'attachments' in message_data
                            is_voice = False
                            voice_media_id = None
                            
                            if not text and has_media:
                                text = '[Media received]'
                            elif not text:
                                return None

                            return IncomingMessage(
                                chat_id=sender_id,
                                user_id=f'ig_{sender_id}',
                                text=str(text),
                                message_id=message_data.get('mid'),
                                has_media=has_media,
                                is_voice=is_voice,
                                voice_media_id=voice_media_id,
                            )
        except Exception as e:
            logger.error(f"Error parsing Instagram payload: {e}")
        return None

    def should_ignore(self, *, channel_id: str | None, service: object, message: IncomingMessage) -> bool:
        return message.text.startswith('/')

    def send_message(self, *, service: object, message: IncomingMessage, text: str) -> None:
        try:
            service.send_message(message.chat_id, text)
        except Exception as e:
            _ = e

    def send_typing_indicator(self, *, service: object, message: IncomingMessage) -> None:
        try:
            service.send_typing_indicator(message.chat_id)
        except Exception as e:
            _ = e

    def send_media_message(self, *, service: object, message: IncomingMessage, media_type: str, link: str, caption: str | None = None, filename: str | None = None) -> None:
        try:
            service.send_media_message(message.chat_id, media_type=media_type, link=link, caption=caption, filename=filename)
        except Exception as e:
            _ = e