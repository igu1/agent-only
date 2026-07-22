import json
import os
import urllib.request
import urllib.error


class InstagramService:
    def __init__(
        self,
        access_token: str | None = None,
        business_account_id: str | None = None,
        verify_token: str | None = None,
        api_version: str | None = None,
        api_base: str | None = None,
        channel_config: dict | None = None,
    ):
        if channel_config:
            self.access_token = channel_config.get('access_token', '').strip()
            self.business_account_id = channel_config.get('business_account_id', '').strip()
            self.verify_token = channel_config.get('verify_token', 'instagram_verify').strip()
            self.api_version = channel_config.get('api_version', 'v18.0').strip()
        else:
            self.access_token = (access_token or '').strip()
            self.business_account_id = (business_account_id or '').strip()
            self.verify_token = (verify_token or 'instagram_verify').strip()
            self.api_version = api_version or 'v18.0'
        
        self.api_base = api_base or os.getenv("INSTAGRAM_API_BASE", "https://graph.instagram.com").rstrip("/")
        
        if not self.access_token:
            raise ValueError("Instagram access_token is required")
        if not self.business_account_id:
            raise ValueError("Instagram business_account_id is required")

    def validate_webhook(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.api_base}/{self.api_version}/{endpoint}"
        
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            },
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
            raise RuntimeError(f"Instagram request failed: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Instagram request failed: {e}")

    def send_message(self, to: str, text: str) -> dict:
        payload = {
            "recipient": {"id": to},
            "message": {"text": text}
        }
        return self._post(f"{self.business_account_id}/messages", payload)

    def send_media_message(
        self,
        to: str,
        media_type: str,
        link: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict:
        media_type = (media_type or "").strip().lower()
        if media_type not in {"image", "video", "document"}:
            raise ValueError(f"Unsupported media_type: {media_type}")
        if not link:
            raise ValueError("link is required")

        media_payload = {"url": link}
        if caption:
            media_payload["caption"] = caption

        payload = {
            "recipient": {"id": to},
            "message": {
                "attachment": {
                    "type": media_type,
                    "payload": media_payload
                }
            }
        }
        return self._post(f"{self.business_account_id}/messages", payload)

    def send_typing_indicator(self, to: str, action: str = 'typing_on') -> dict:
        payload = {
            "recipient": {"id": to},
            "sender_action": action
        }
        return self._post(f"{self.business_account_id}/messages", payload)