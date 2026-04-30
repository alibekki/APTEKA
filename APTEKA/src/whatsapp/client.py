"""Wazzup24 API client for sending WhatsApp messages."""

import logging
import ssl

import aiohttp
import certifi

from src.config import WazzupConfig

logger = logging.getLogger(__name__)

# WhatsApp limits per Meta spec (Wazzup passes these through)
WA_TEXT_LIMIT = 4096
WA_MAX_REPLY_BUTTONS = 3


class WazzupClient:
    """Async client for Wazzup24 WhatsApp API (v3)."""

    def __init__(self, config: WazzupConfig):
        self._config = config
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                connector=connector,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _split_text(text: str, limit: int = WA_TEXT_LIMIT) -> list[str]:
        """Split long text on newline boundaries under WhatsApp's 4096 limit."""
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        soft_limit = limit - 96  # small buffer
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, soft_limit)
            if split_at == -1:
                split_at = soft_limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        return chunks

    async def _post_message(self, payload: dict, chat_id: str) -> dict | None:
        """Internal POST to /message with unified error handling."""
        session = await self._get_session()
        url = f"{self._config.base_url}/message"
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                body = await resp.text()
                logger.error(
                    "Wazzup send failed (HTTP %d) to %s: %s",
                    resp.status, chat_id, body[:300],
                )
                return None
        except Exception as e:
            logger.error("Wazzup send error to %s: %s", chat_id, e)
            return None

    async def send_message(self, chat_id: str, text: str,
                           buttons: list[str] | None = None) -> dict | None:
        """Send a text message. If `buttons` provided (up to 3), attaches them
        as WhatsApp reply buttons via Wazzup's `buttonsObject` — when the user
        taps, the button text is sent back as an inbound text message.

        Args:
            chat_id: Recipient phone number (international format without +)
            text: Message text (will be auto-split if >4096 chars)
            buttons: Optional list of button labels (max 3). If >3 passed, we
                silently truncate — WhatsApp hard limit.

        Returns:
            API response dict with messageId, or None on failure.
        """
        chunks = self._split_text(text)
        last_result: dict | None = None

        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            payload: dict = {
                "channelId": self._config.channel_id,
                "chatType": "whatsapp",
                "chatId": chat_id,
                "text": chunk,
            }
            # Attach buttons only to the final chunk so they appear at the end
            if is_last and buttons:
                trimmed = [b[:20] for b in buttons[:WA_MAX_REPLY_BUTTONS]]
                payload["buttonsObject"] = {
                    "buttons": [{"text": b, "type": "text"} for b in trimmed]
                }

            result = await self._post_message(payload, chat_id)
            if result is None:
                return None
            last_result = result

        return last_result

    async def mark_read(self, chat_id: str) -> bool:
        """Send a read receipt — best-effort, no error on failure."""
        session = await self._get_session()
        url = f"{self._config.base_url}/chats/mark_read"
        try:
            async with session.post(url, json={
                "channelId": self._config.channel_id,
                "chatType": "whatsapp",
                "chatId": chat_id,
            }) as resp:
                return resp.status in (200, 201, 204)
        except Exception:
            return False

    async def fetch_media(self, media_url: str) -> bytes | None:
        """Download a media file from Wazzup's CDN (for photos/audio)."""
        session = await self._get_session()
        try:
            async with session.get(media_url) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning("Media fetch failed HTTP %d: %s", resp.status, media_url)
                return None
        except Exception as e:
            logger.warning("Media fetch error: %s", e)
            return None

    async def setup_webhook(self, webhook_url: str) -> bool:
        """Register webhook URL with Wazzup to receive incoming messages.

        Args:
            webhook_url: Publicly accessible HTTPS URL (e.g. https://yourdomain.com/wazzup/webhook)

        Returns:
            True if webhook was set successfully.
        """
        session = await self._get_session()
        url = f"{self._config.base_url}/webhooks"

        try:
            async with session.patch(url, json={
                "webhooksUri": webhook_url,
                "subscriptions": {
                    "messagesAndStatuses": True,
                    "contactsAndDealsCreation": False,
                    "channelsUpdates": False,
                    "templateStatus": False,
                },
            }) as resp:
                if resp.status in (200, 201, 204):
                    logger.info("Wazzup webhook set to: %s", webhook_url)
                    return True
                body = await resp.text()
                logger.error("Wazzup webhook setup failed (HTTP %d): %s", resp.status, body[:200])
                return False
        except Exception as e:
            logger.error("Wazzup webhook setup error: %s", e)
            return False
