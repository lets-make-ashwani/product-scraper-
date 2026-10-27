import os
import tempfile
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Server
    HOST: str = "127.0.0.1"
    PORT: int = int(os.getenv("PORT", "8000"))
    DEBUG: bool = False

    # MongoDB
    MONGODB_URI: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "product_scraper_db"

    # Telegram Session Configuration (Telethon StringSession)
    TELEGRAM_API_ID: Optional[int] = None
    TELEGRAM_API_HASH: Optional[str] = None
    TELEGRAM_SESSION_STRING: Optional[str] = None
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ENABLED: bool = True
    TELEGRAM_CAPTION_TEMPLATE: str = (
        "🛍️ <b>{title}</b>\n\n"
        "💰 <b>Deal Price:</b> ₹{price} {mrp_info} {discount_info}\n"
        "{rating_info}"
        "{colors_info}"
        "{sizes_info}\n"
        "✨ <b>Highlights:</b>\n{description}\n\n"
        "🔗 <b>Buy Link:</b>\n{buy_url}"
    )

    # Groq AI Configuration
    GROQ_API_KEY: Optional[str] = None
    GROQ_MODEL: str = "qwen/qwen3.8-27b"
    AI_ENHANCE_POSTS: bool = True

    # Scraper & Worker
    REQUEST_DELAY_SECONDS: float = 4.5
    MAX_IMAGES_PER_PRODUCT: int = 10
    MAX_JOB_ATTEMPTS: int = 3
    AUTO_PUBLISH: bool = True
    HEADLESS_BROWSER: bool = True

    # Cross-platform Ephemeral Temp Directory (Windows & Linux compatible)
    TEMP_DIR: str = os.getenv("TEMP_DIR", os.path.join(tempfile.gettempdir(), "product_scraper_staging"))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def ensure_temp_dir(self):
        Path(self.TEMP_DIR).mkdir(parents=True, exist_ok=True)

settings = Settings()
