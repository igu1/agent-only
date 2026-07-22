import json
import os
import urllib.request
import urllib.error


class WhatsAppService:
    """
    WhatsApp Business Cloud API Service
    
    Handles sending and receiving WhatsApp messages via Meta's Cloud API.
    Requires:
    - PHONE_NUMBER_ID: WhatsApp Business Phone Number ID
    - PERMANENT_ACCESS_TOKEN: Meta permanent access token
    - VERIFY_TOKEN: Webhook verification token
    """
    
    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
        verify_token: str | None = None,
        api_version: str | None = None,
        api_base: str | None = None,
        channel_config: dict | None = None,
    ):
        # If channel_config is provided, use it (from database)
        if channel_config:
            self.phone_number_id = channel_config.get('phone_number_id', '').strip()
            self.access_token = channel_config.get('access_token', '').strip()
            self.verify_token = channel_config.get('verify_token', 'whatsapp_verify').strip()
            self.api_version = channel_config.get('api_version', 'v21.0').strip()
            self.api_base = api_base or os.getenv("WHATSAPP_API_BASE", "https://graph.facebook.com").rstrip("/")
        
        if not self.phone_number_id:
            raise ValueError("PHONE_NUMBER_ID is required")
        if not self.access_token:
            raise ValueError("PERMANENT_ACCESS_TOKEN is required")

    def _post(self, payload: dict) -> dict:
        url = f"{self.api_base}/{self.api_version}/{self.phone_number_id}/messages"

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
            raise RuntimeError(f"WhatsApp request failed: HTTP {e.code} {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"WhatsApp request failed: {e}")
    
    def validate_webhook(self, mode: str | None, token: str | None, challenge: str | None) -> str | None:
        """
        Validate webhook verification request from Meta.
        
        Args:
            mode: Should be "subscribe"
            token: Should match VERIFY_TOKEN
            challenge: Challenge string to return
            
        Returns:
            Challenge string if validation succeeds, None otherwise
        """
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None
    
    def parse_webhook(self, payload: dict) -> dict | None:
        """
        Parse incoming webhook payload from WhatsApp.
        
        Args:
            payload: Webhook payload from Meta
            
        Returns:
            Parsed message data or None if not a valid message
        """
        try:
            # Extract entry from webhook payload
            entry = payload.get("entry", [])
            if not entry:
                return None
            
            # Get changes from first entry
            changes = entry[0].get("changes", [])
            if not changes:
                return None
            
            # Get value from first change
            value = changes[0].get("value", {})
            
            # Extract messages
            messages = value.get("messages", [])
            if not messages:
                return None
            
            # Get first message
            message = messages[0]
            
            # Extract message details
            from_number = message.get("from")
            message_id = message.get("id")
            timestamp = message.get("timestamp")
            message_type = message.get("type")
            
            # Extract text content
            text_content = None
            if message_type == "text":
                text_obj = message.get("text", {})
                text_content = text_obj.get("body")
            
            # Extract contact info if available
            contacts = value.get("contacts", [])
            contact_name = None
            if contacts:
                profile = contacts[0].get("profile", {})
                contact_name = profile.get("name")
            
            if not from_number or not text_content:
                return None
            
            return {
                "from": from_number,
                "message_id": message_id,
                "timestamp": timestamp,
                "text": text_content,
                "type": message_type,
                "contact_name": contact_name,
            }
            
        except (KeyError, IndexError, TypeError):
            return None
    
    def send_message(self, to: str, text: str) -> dict:
        """
        Send a text message via WhatsApp Business API.
        
        Args:
            to: Recipient phone number (with country code, no + sign)
            text: Message text to send
            
        Returns:
            Response data from API
            
        Raises:
            RuntimeError: If the API request fails
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text
            }
        }
        return self._post(payload)

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

        media_payload: dict = {"link": link}
        if caption:
            media_payload["caption"] = caption
        if media_type == "document" and filename:
            media_payload["filename"] = filename

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: media_payload,
        }
        return self._post(payload)
    
    def send_template_message(self, to: str, template_name: str, language_code: str = "en") -> dict:
        """
        Send a template message via WhatsApp Business API.
        
        Template messages are pre-approved messages that can be sent outside the 24-hour window.
        
        Args:
            to: Recipient phone number (with country code, no + sign)
            template_name: Name of the approved template
            language_code: Language code (default: "en")
            
        Returns:
            Response data from API
            
        Raises:
            RuntimeError: If the API request fails
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language_code
                }
            }
        }
        return self._post(payload)
    
    def mark_message_read(self, message_id: str) -> dict:
        """
        Mark a message as read.
        
        Args:
            message_id: ID of the message to mark as read
            
        Returns:
            Response data from API
            
        Raises:
            RuntimeError: If the API request fails
        """
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }
        return self._post(payload)
    
    def send_typing_indicator(self, to: str, message_id: str = None, indicator_type: str = "text") -> dict:
        """
        Send a typing indicator to show the user that the bot is typing.
        
        Args:
            to: Recipient phone number (with country code, no + sign)
            message_id: Optional message ID to mark as read while typing
            indicator_type: Type of indicator ("text" for typing, "audio" for recording)
            
        Returns:
            Response data from API
            
        Raises:
            RuntimeError: If the API request fails
        """
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id or "0",  # Use 0 if no message_id provided
            "typing_indicator": {
                "type": indicator_type
            }
        }
        return self._post(payload)
