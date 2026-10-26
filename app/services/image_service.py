import os
import shutil
import time
import logging
from pathlib import Path
from typing import List, Tuple
import httpx
from PIL import Image
from app.config import settings

logger = logging.getLogger("services.image")

class EphemeralImageService:
    """
    Manages temporary image staging on Render local disk.
    Ensures immediate auto-deletion after Telegram posting to keep disk usage at 0 MB.
    """

    def __init__(self):
        settings.ensure_temp_dir()

    def create_staging_dir(self, identifier: str) -> Path:
        """Create a dedicated temporary folder for a scraping/publishing task."""
        staging_dir = Path(settings.TEMP_DIR) / f"staging_{identifier}_{int(time.time())}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        return staging_dir

    async def download_images(self, urls: List[str], staging_dir: Path, max_images: int = 10) -> List[Path]:
        """
        Downloads up to max_images to the ephemeral staging folder.
        Validates image integrity via PIL.
        """
        valid_paths: List[Path] = []
        if not urls:
            return valid_paths

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for idx, img_url in enumerate(urls[:max_images], start=1):
                clean_url = img_url.strip()
                if not clean_url:
                    continue

                dest_file = staging_dir / f"img_{idx:02d}.jpg"
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.flipkart.com/"
                    }
                    res = await client.get(clean_url, headers=headers)
                    if res.status_code == 200 and len(res.content) > 500:
                        # Save & Validate with PIL
                        with open(dest_file, "wb") as f:
                            f.write(res.content)

                        # Test image integrity
                        with Image.open(dest_file) as im:
                            im.verify()

                        valid_paths.append(dest_file)
                        logger.info(f"Downloaded image #{idx}: {dest_file.name} ({len(res.content) // 1024} KB)")
                    else:
                        logger.warning(f"Image {clean_url} returned HTTP {res.status_code} (size: {len(res.content)} bytes)")
                except Exception as e:
                    logger.warning(f"Failed to download image {clean_url}: {e}")
                    if dest_file.exists():
                        try:
                            dest_file.unlink()
                        except Exception:
                            pass

        return valid_paths

    def purge_staging_dir(self, staging_dir: Path):
        """
        INSTANT AUTO-DELETE: Purges temporary directory and all staged images
        immediately after Telegram upload is completed.
        """
        if staging_dir and staging_dir.exists():
            try:
                shutil.rmtree(staging_dir, ignore_errors=True)
                logger.info(f"[Auto-Clean] Successfully purged ephemeral staging folder: {staging_dir}")
            except Exception as e:
                logger.error(f"Error purging staging directory {staging_dir}: {e}")

    def janitor_clean_stale_directories(self, max_age_seconds: int = 1200):
        """
        Failsafe orphan cleaner: Deletes any temp folder older than 20 minutes
        in case of unexpected server crashes or unhandled exceptions.
        """
        base = Path(settings.TEMP_DIR)
        if not base.exists():
            return

        now = time.time()
        for item in base.glob("staging_*"):
            if item.is_dir():
                try:
                    mtime = item.stat().st_mtime
                    if now - mtime > max_age_seconds:
                        shutil.rmtree(item, ignore_errors=True)
                        logger.info(f"[Janitor] Cleaned orphan staging folder: {item.name}")
                except Exception as e:
                    logger.error(f"[Janitor] Failed cleaning {item.name}: {e}")

image_service = EphemeralImageService()
