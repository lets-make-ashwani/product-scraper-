import asyncio
import logging
from datetime import datetime, timezone
from bson import ObjectId
from typing import Optional
from app.config import settings
from app.db.mongodb import get_database
from app.scrapers.router import scraper_router
from app.telegram.composer import telegram_composer
from app.telegram.client import telegram_publisher
from app.services.image_service import image_service
from app.models.product import UniversalProduct

logger = logging.getLogger("worker")

def utc_now():
    return datetime.now(timezone.utc)

class BackgroundWorker:
    """Autonomous 24/7 background worker managing scraping, Telegram posting, and instant image cleanup."""

    def __init__(self):
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._janitor_task: Optional[asyncio.Task] = None

    def start(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._worker_loop())
            self._janitor_task = asyncio.create_task(self._janitor_loop())
            logger.info("Autonomous Background Worker started.")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
        if self._janitor_task:
            self._janitor_task.cancel()
        logger.info("Background Worker stopped.")

    async def _worker_loop(self):
        while self.is_running:
            try:
                db = get_database()
                if db is None:
                    await asyncio.sleep(3.0)
                    continue

                # Find oldest pending job
                job = await db.jobs.find_one_and_update(
                    {"status": "PENDING"},
                    {"$set": {"status": "PROCESSING", "updated_at": utc_now()}}
                )

                if job:
                    await self._process_job(job)
                else:
                    await asyncio.sleep(2.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in worker loop: {e}")
                await asyncio.sleep(5.0)

    async def _process_job(self, job: dict):
        db = get_database()
        job_id = job["_id"]
        product_id = job.get("product_id")
        staging_dir = None

        logger.info(f"Processing job {job_id} for product {product_id}...")

        try:
            # Fetch product record
            product_doc = await db.products.find_one({"_id": ObjectId(product_id) if isinstance(product_id, str) else product_id})
            if not product_doc:
                raise ValueError(f"Product {product_id} not found in database.")

            url = product_doc["product_url"]

            # 1. Scrape product & all color variants
            product: UniversalProduct = await scraper_router.scrape(url)
            product_data = product.model_dump(exclude={"id"})
            product_data["status"] = "SCRAPED"
            product_data["scraped_at"] = utc_now()
            product_data["updated_at"] = utc_now()

            # 2. Ephemeral Image Staging & Telegram Posting
            if settings.AUTO_PUBLISH and settings.TELEGRAM_ENABLED:
                staging_dir = image_service.create_staging_dir(str(job_id))

                try:
                    # Select representative photos across color variants (up to max limit)
                    selected_img_urls = telegram_composer.select_variant_images(product, max_images=settings.MAX_IMAGES_PER_PRODUCT)
                    
                    # Download images temporarily to Render disk
                    downloaded_files = await image_service.download_images(selected_img_urls, staging_dir)
                    caption = telegram_composer.compose_caption(product)

                    # Post to Telegram
                    if downloaded_files:
                        success, msg_ids, err_msg = await telegram_publisher.publish_album(downloaded_files, caption)
                        if success:
                            product_data["status"] = "PUBLISHED"
                            product_data["telegram_message_ids"] = msg_ids
                            product_data["posted_at"] = utc_now()
                            logger.info(f"Product {product_id} published successfully to Telegram!")
                        else:
                            product_data["error_message"] = f"Telegram publish failed: {err_msg}"
                            logger.warning(f"Product {product_id} Telegram failure: {err_msg}")
                    else:
                        logger.warning(f"No valid images downloaded for {product_id}, skipping Telegram album.")

                finally:
                    # INSTANT CLEANUP: Delete temporary staging images immediately!
                    if staging_dir:
                        image_service.purge_staging_dir(staging_dir)

            # Update product in MongoDB
            await db.products.update_one(
                {"_id": product_doc["_id"]},
                {"$set": product_data}
            )

            # Mark job completed
            await db.jobs.update_one(
                {"_id": job_id},
                {"$set": {"status": "COMPLETED", "updated_at": utc_now()}}
            )

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            attempts = job.get("attempts", 0) + 1
            max_attempts = job.get("max_attempts", 3)
            new_status = "RETRYING" if attempts < max_attempts else "FAILED"

            await db.jobs.update_one(
                {"_id": job_id},
                {"$set": {
                    "status": new_status,
                    "attempts": attempts,
                    "error_message": str(e),
                    "updated_at": utc_now()
                }}
            )

            # Ensure cleanup if an unhandled crash happened before finally block
            if staging_dir:
                image_service.purge_staging_dir(staging_dir)

    async def _janitor_loop(self):
        """Runs once every 30 minutes to clean any orphaned temp directories."""
        while self.is_running:
            try:
                await asyncio.sleep(1800)
                image_service.janitor_clean_stale_directories()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Janitor error: {e}")

worker = BackgroundWorker()
