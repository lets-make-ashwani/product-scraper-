import json
import logging
from pathlib import Path
from typing import List, Tuple, Optional
import httpx
from app.config import settings

logger = logging.getLogger("telegram.client")

# Optional imports for Pyrogram and Telethon
try:
    from pyrogram import Client as PyrogramClient
    from pyrogram.types import InputMediaPhoto
    PYROGRAM_AVAILABLE = True
except ImportError:
    PYROGRAM_AVAILABLE = False

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False


class TelegramPublisher:
    """
    Multi-engine Telegram Publisher:
    1. Pyrogram StringSession (session string starting with 'BQ...')
    2. Telethon StringSession (session string starting with '1B...')
    3. Telegram Bot API fallback (sendMediaGroup HTTP endpoint)
    """

    def __init__(self):
        self._pyrogram_client = None
        self._telethon_client = None

    def _is_pyrogram_session(self) -> bool:
        s = (settings.TELEGRAM_SESSION_STRING or "").strip()
        return s.startswith("BQ")

    async def _get_pyrogram_client(self):
        if not PYROGRAM_AVAILABLE:
            return None
        if self._is_pyrogram_session() and settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH:
            if not self._pyrogram_client:
                self._pyrogram_client = PyrogramClient(
                    "user_session",
                    session_string=settings.TELEGRAM_SESSION_STRING.strip(),
                    api_id=int(settings.TELEGRAM_API_ID),
                    api_hash=settings.TELEGRAM_API_HASH.strip(),
                    in_memory=True,
                    no_updates=True
                )
                await self._pyrogram_client.start()
                logger.info("Connected to Telegram using Pyrogram StringSession.")
            return self._pyrogram_client
        return None

    async def _get_telethon_client(self):
        if not TELETHON_AVAILABLE:
            return None
        s = (settings.TELEGRAM_SESSION_STRING or "").strip()
        if s and not s.startswith("BQ") and settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH:
            if not self._telethon_client:
                session = StringSession(s)
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
        """Test authentication with configured Telegram session or bot credentials."""
        # 1. Pyrogram Test
        if self._is_pyrogram_session():
            try:
                client = await self._get_pyrogram_client()
                if client:
                    me = await client.get_me()
                    name = getattr(me, "first_name", "") or getattr(me, "username", "User")
                    return True, f"Pyrogram StringSession authenticated as: {name} (ID: {me.id})"
            except Exception as e:
                return False, f"Pyrogram StringSession failed: {str(e)}"

        # 2. Telethon Test
        try:
            client = await self._get_telethon_client()
            if client:
                me = await client.get_me()
                name = getattr(me, "first_name", "") or getattr(me, "username", "User")
                return True, f"Telethon StringSession authenticated as: {name}"
        except Exception as e:
            pass

        # 3. Bot Token Test
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

        return False, "No Telegram credentials configured."

    async def publish_album(
        self,
        image_paths: List[Path],
        caption: str,
        chat_id: Optional[str] = None
    ) -> Tuple[bool, List[int], Optional[str]]:
        """
        Publishes images as a native album post with caption to the target Telegram chat/channel.
        """
        target_chat = chat_id or settings.TELEGRAM_CHAT_ID
        if not target_chat:
            return False, [], "No TELEGRAM_CHAT_ID configured in settings."

        # Engine 1: Pyrogram StringSession
        if self._is_pyrogram_session():
            try:
                client = await self._get_pyrogram_client()
                if client:
                    media_list = []
                    for idx, p in enumerate(image_paths):
                        if idx == 0:
                            media_list.append(InputMediaPhoto(media=str(p), caption=caption, parse_mode=None))
                        else:
                            media_list.append(InputMediaPhoto(media=str(p)))

                    sent_messages = await client.send_media_group(chat_id=target_chat, media=media_list)
                    msg_ids = [m.id for m in sent_messages] if isinstance(sent_messages, list) else [sent_messages.id]
                    logger.info(f"Successfully posted album via Pyrogram StringSession! Message IDs: {msg_ids}")
                    return True, msg_ids, None
            except Exception as e:
                logger.error(f"Pyrogram publish error: {e}")

        # Engine 2: Telethon StringSession
        try:
            telethon_client = await self._get_telethon_client()
            if telethon_client:
                file_paths = [str(p) for p in image_paths]
                messages = await telethon_client.send_file(
                    target_chat,
                    file=file_paths,
                    caption=caption,
                    parse_mode="html"
                )
                msg_ids = [m.id for m in messages] if isinstance(messages, list) else [messages.id]
                logger.info(f"Successfully posted album via Telethon StringSession! Message IDs: {msg_ids}")
                return True, msg_ids, None
        except Exception as e:
            logger.error(f"Telethon publish error: {e}")

        # Engine 3: Bot API Fallback
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
