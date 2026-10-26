import json
import logging
from pathlib import Path
from typing import List, Tuple, Optional
import httpx
from app.config import settings

logger = logging.getLogger("telegram.client")

# Attempt Telethon import for StringSession support
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False

class TelegramPublisher:
    """
    Dual-mode Telegram Publisher.
    Prefers Telethon StringSession (ideal for Render envs with 0 disk footprint).
    Falls back to Telegram Bot API (sendMediaGroup) if Bot Token is provided.
    """

    def __init__(self):
        self._telethon_client = None

    async def _get_telethon_client(self):
        if not TELETHON_AVAILABLE:
            return None

        if settings.TELEGRAM_SESSION_STRING and settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH:
            if not self._telethon_client:
                session = StringSession(settings.TELEGRAM_SESSION_STRING.strip())
                self._telethon_client = TelegramClient(
                    session,
                    int(settings.TELEGRAM_API_ID),
                    settings.TELEGRAM_API_HASH.strip()
                )
                await self._telethon_client.connect()
                logger.info("Connected to Telegram using Telethon StringSession.")
            return self._telethon_client
        return None

    async def test_connection(self) -> Tuple[bool, str]:
        """Test authentication with configured Telegram credentials."""
        # 1. Test Telethon StringSession
        client = await self._get_telethon_client()
        if client:
            try:
                me = await client.get_me()
                name = getattr(me, "first_name", "") or getattr(me, "username", "User")
                return True, f"Telethon StringSession authenticated as: {name}"
            except Exception as e:
                return False, f"Telethon StringSession failed: {str(e)}"

        # 2. Test Bot Token Fallback
        if settings.TELEGRAM_BOT_TOKEN:
            try:
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    res = await http_client.get(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe")
                    data = res.json()
                    if data.get("ok"):
                        bot_user = data.get("result", {}).get("username")
                        return True, f"Telegram Bot API connected as @{bot_user}"
                    return False, f"Bot Token rejected: {data.get('description')}"
            except Exception as e:
                return False, f"Bot API connection error: {str(e)}"

        return False, "No Telegram credentials configured (provide TELEGRAM_SESSION_STRING or TELEGRAM_BOT_TOKEN)."

    async def publish_album(
        self,
        image_paths: List[Path],
        caption: str,
        chat_id: Optional[str] = None
    ) -> Tuple[bool, List[int], Optional[str]]:
        """
        Publishes images as a native album post with caption to the target Telegram chat/channel.
        Returns: (success, message_ids, error_message)
        """
        target_chat = chat_id or settings.TELEGRAM_CHAT_ID
        if not target_chat:
            return False, [], "No TELEGRAM_CHAT_ID configured in settings."

        # Strategy 1: Telethon StringSession (User or Bot session)
        telethon_client = await self._get_telethon_client()
        if telethon_client:
            try:
                logger.info(f"Uploading {len(image_paths)} images via Telethon StringSession to {target_chat}...")
                file_paths = [str(p) for p in image_paths]
                
                # Telethon send_file with multiple files automatically creates an album
                messages = await telethon_client.send_file(
                    target_chat,
                    file=file_paths,
                    caption=caption,
                    parse_mode="html"
                )

                msg_ids = []
                if isinstance(messages, list):
                    msg_ids = [m.id for m in messages]
                elif hasattr(messages, "id"):
                    msg_ids = [messages.id]

                logger.info(f"Successfully posted album via StringSession! Message IDs: {msg_ids}")
                return True, msg_ids, None
            except Exception as e:
                logger.error(f"Telethon StringSession publish error: {e}")
                # Fall through to Bot API fallback if available

        # Strategy 2: Telegram Bot API (sendMediaGroup)
        if settings.TELEGRAM_BOT_TOKEN:
            return await self._publish_via_bot_api(image_paths, caption, target_chat)

        return False, [], "Publishing failed: No valid Telegram session or bot token connected."

    async def _publish_via_bot_api(
        self,
        image_paths: List[Path],
        caption: str,
        chat_id: str
    ) -> Tuple[bool, List[int], Optional[str]]:
        try:
            url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMediaGroup"

            media_json = []
            files_dict = {}

            for idx, path in enumerate(image_paths):
                attach_name = f"photo_{idx}"
                item = {
                    "type": "photo",
                    "media": f"attach://{attach_name}"
                }
                if idx == 0:
                    item["caption"] = caption
                    item["parse_mode"] = "HTML"

                media_json.append(item)
                files_dict[attach_name] = (path.name, open(path, "rb"), "image/jpeg")

            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.post(
                    url,
                    data={"chat_id": chat_id, "media": json.dumps(media_json)},
                    files=files_dict
                )
                
                # Close file handles
                for f_tuple in files_dict.values():
                    try:
                        f_tuple[1].close()
                    except Exception:
                        pass

                data = res.json()
                if data.get("ok"):
                    msg_ids = [msg["message_id"] for msg in data.get("result", [])]
                    return True, msg_ids, None
                else:
                    return False, [], f"Bot API error: {data.get('description')}"

        except Exception as e:
            return False, [], f"Bot API upload error: {str(e)}"

telegram_publisher = TelegramPublisher()
