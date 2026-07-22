import json
import os
import urllib.request
import urllib.error


class TelegramService:
    def __init__(
        self,
        bot_token: str | None = None,
        secret_token: str | None = None,
        api_base: str | None = None,
        channel_config: dict | None = None,
    ):
        if channel_config:
            self.bot_token = (channel_config.get('bot_token') or '').strip()
            self.secret_token = (channel_config.get('webhook_secret') or '').strip()
        else:
            self.bot_token = (bot_token or '').strip()
            self.secret_token = (secret_token or '').strip()

        self.api_base = (api_base or os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")).rstrip("/")

        if not self.bot_token:
            raise ValueError("bot_token is required - pass channel_config from DB")

    def validate_secret(self, header_value: str | None) -> bool:
        if not self.secret_token:
            return True
        return bool(header_value) and header_value == self.secret_token

    def parse_update(self, update: dict) -> dict | None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        message_id = message.get('message_id')

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None

        text = message.get("text")
        photo = message.get("photo")
        video = message.get("video")
        document = message.get("document")
        
        if not text and not photo and not video and not document:
            return None
            
        if not text and (photo or video or document):
            text = "[Media received - no AI response needed]"

        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        username = from_user.get("username")

        return {
            "chat_id": chat_id,
            "text": text,
            "user_id": str(user_id) if user_id is not None else str(chat_id),
            "username": username,
            "message_id": str(message_id) if message_id is not None else str(update.get('update_id') or chat_id),
            "has_media": bool(photo or video or document),
        }

    def _post(self, method: str, payload: dict) -> dict:
        url = f"{self.api_base}/bot{self.bot_token}/{method.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                response_data = resp.read().decode("utf-8")
                return json.loads(response_data)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise RuntimeError(f"Telegram request failed: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram request failed: {e}")

    def send_message(self, chat_id: int, text: str) -> None:
        self._post('sendMessage', {"chat_id": chat_id, "text": text})

    def send_typing_indicator(self, chat_id: int, action: str = 'typing') -> None:
        self._post('sendChatAction', {"chat_id": chat_id, "action": action})

    def send_media_message(self, chat_id: int, media_type: str, link: str, caption: str | None = None) -> dict:
        normalized = (media_type or '').strip().lower()
        if normalized not in {'image', 'video', 'document'}:
            raise ValueError(f'Unsupported media_type: {normalized}')
        if not link:
            raise ValueError('link is required')

        if normalized == 'image':
            method = 'sendPhoto'
            payload: dict = {"chat_id": chat_id, "photo": link}
        elif normalized == 'video':
            method = 'sendVideo'
            payload = {"chat_id": chat_id, "video": link}
        else:
            method = 'sendDocument'
            payload = {"chat_id": chat_id, "document": link}

        if caption:
            payload["caption"] = caption

        return self._post(method, payload)

    def register_webhook(self, webhook_url: str, webhook_id: str = None) -> None:
        """Register webhook with Telegram API"""

        self.delete_webhook()

        url = f"{self.api_base}/bot{self.bot_token}/setWebhook"
        
        if webhook_id:
            full_webhook_url = f"{webhook_url.rstrip('/')}/{webhook_id}"
        else:
            full_webhook_url = webhook_url.rstrip('/')
        
        payload = {
            "url": full_webhook_url,
        }
        
        if self.secret_token:
            payload["secret_token"] = self.secret_token

        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                response = resp.read().decode("utf-8")
                result = json.loads(response)
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram webhook registration failed: {result}")
                return result
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise RuntimeError(f"Telegram webhook registration failed: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram webhook registration failed: {e}")

    def delete_webhook(self) -> None:
        """Delete webhook with Telegram API"""
        url = f"{self.api_base}/bot{self.bot_token}/deleteWebhook"
        
        req = urllib.request.Request(
            url,
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                response = resp.read().decode("utf-8")
                result = json.loads(response)
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram webhook deletion failed: {result}")
                return result
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise RuntimeError(f"Telegram webhook deletion failed: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram webhook deletion failed: {e}")

    def get_webhook_info(self) -> dict:
        """Get current webhook status from Telegram"""
        url = f"{self.api_base}/bot{self.bot_token}/getWebhookInfo"
        
        req = urllib.request.Request(
            url,
            headers={"Content-Type": "application/json"},
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                response = resp.read().decode("utf-8")
                return json.loads(response)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise RuntimeError(f"Failed to get Telegram webhook info: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to get Telegram webhook info: {e}")
