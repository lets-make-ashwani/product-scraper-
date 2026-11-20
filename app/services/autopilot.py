import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs
from bson import ObjectId

from app.config import settings
from app.db.mongodb import get_database
from app.scrapers.router import scraper_router

logger = logging.getLogger("autopilot")

def utc_now():
    return datetime.now(timezone.utc)

# Curated Flipkart categories focusing on all types of women's dresses, ethnic wear, sarees, and kurtas
FLIPKART_DRESS_CATEGORIES = [
    {
        "name": "Women Kurta Sharara & Ethnic Sets",
        "url_template": "https://www.flipkart.com/search?q=sharara+for+women&sid=clo%2Ccfv%2Citg%2Ctys&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Western Dresses & Gowns",
        "url_template": "https://www.flipkart.com/search?q=women+dresses&sid=clo%2Codx%2Cmaj%2Cjhy&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Sarees",
        "url_template": "https://www.flipkart.com/search?q=sarees+for+women&sid=clo%2C8on%2Czpd%2C9og&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Anarkali & Maxi Kurta Sets",
        "url_template": "https://www.flipkart.com/search?q=anarkali+kurta+set+women&sid=clo%2Ccfv%2Citg%2Ctys&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Lehenga Choli",
        "url_template": "https://www.flipkart.com/search?q=lehenga+choli+for+women&sid=clo%2Ch95%2Ca57&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Kurtis & Tunics",
        "url_template": "https://www.flipkart.com/search?q=kurtis+for+women&sid=clo%2Ccfv%2Ccjr%2Cvbe&sort=recency_desc&page={page}"
    },
    {
        "name": "Women Party Wear Frocks & Midis",
        "url_template": "https://www.flipkart.com/search?q=party+wear+dress+women&sid=clo%2Codx%2Cmaj%2Cjhy&sort=recency_desc&page={page}"
    }
]

class AutopilotEngine:
    """
    Autonomous 24/7 Discovery Engine:
    Continuously monitors Flipkart for all dress categories, extracts fresh listings,
    and enqueues newly discovered products through strict URL & PID deduplication checks.
    """

    def __init__(self):
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if not self.is_running and settings.AUTOPILOT_ENABLED:
            self.is_running = True
            self._task = asyncio.create_task(self._autopilot_loop())
            logger.info("Autonomous 24/7 Discovery Engine started.")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
        logger.info("Autonomous Discovery Engine stopped.")

    async def _autopilot_loop(self):
        # Initial stagger on startup to let server and db settle
        await asyncio.sleep(10.0)

        while self.is_running:
            try:
                db = get_database()
                if db is None:
                    await asyncio.sleep(5.0)
                    continue

                # Check if autopilot is paused via DB state
                state_doc = await db.autopilot_state.find_one({"key": "global_state"})
                if state_doc and state_doc.get("enabled") is False:
                    await asyncio.sleep(15.0)
                    continue

                # Run a full discovery cycle across all dress categories
                await self.run_discovery_cycle()

                # Sleep interval between discovery cycles (e.g., 20 minutes)
                sleep_sec = settings.AUTOPILOT_INTERVAL_MINUTES * 60
                logger.info(f"[AutoPilot] Cycle complete. Resting for {settings.AUTOPILOT_INTERVAL_MINUTES} minutes before next sweep...")
                await asyncio.sleep(sleep_sec)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AutoPilot] Unexpected error in discovery loop: {e}", exc_info=True)
                await asyncio.sleep(60.0)

    async def run_discovery_cycle(self) -> Dict[str, Any]:
        """
        Runs one discovery sweep across Flipkart dress categories,
        discovers new products, applies URL & PID deduplication, and enqueues new items.
        """
        db = get_database()
        if db is None:
            return {"error": "Database unavailable"}

        total_discovered = 0
        total_new_enqueued = 0
        total_skipped_duplicates = 0

        logger.info("[AutoPilot] Starting autonomous Flipkart dress discovery sweep...")

        # Update last run state
        await db.autopilot_state.update_one(
            {"key": "global_state"},
            {"$set": {
                "key": "global_state",
                "last_run_at": utc_now(),
                "status": "RUNNING"
            }},
            upsert=True
        )

        scraper = scraper_router.get_scraper("https://www.flipkart.com")
        if not scraper:
            logger.warning("[AutoPilot] Flipkart scraper not available.")
            return {"error": "Flipkart scraper missing"}

        for category in FLIPKART_DRESS_CATEGORIES:
            if not self.is_running:
                break

            cat_name = category["name"]
            logger.info(f"[AutoPilot] Scanning category: '{cat_name}'")

            for page_num in range(1, settings.AUTOPILOT_MAX_PAGES_PER_RUN + 1):
                if not self.is_running:
                    break

                target_url = category["url_template"].format(page=page_num)
                try:
                    catalog_items = await scraper.extract_search_catalog(
                        target_url,
                        max_items=settings.AUTOPILOT_ITEMS_PER_CATEGORY
                    )

                    if not catalog_items:
                        logger.info(f"[AutoPilot] No more items found on page {page_num} for '{cat_name}'.")
                        break

                    for item in catalog_items:
                        total_discovered += 1
                        raw_url = item["product_url"]
                        norm_url = scraper.normalize_url(raw_url)

                        # Extract canonical Product ID (PID)
                        parsed = urlparse(norm_url)
                        qs = parse_qs(parsed.query)
                        pid = qs.get("pid", [None])[0]

                        # STRICT DEDUPLICATION CHECK:
                        # 1. Skip if product was already PUBLISHED (zero Telegram duplicates!)
                        # 2. Skip if product is currently PENDING or PROCESSING in queue
                        query_filters = [{"normalized_url": norm_url}]
                        if pid:
                            query_filters.append({"platform": "flipkart", "external_id": pid})

                        existing_active = await db.products.find_one({
                            "$or": query_filters,
                            "status": {"$in": ["PUBLISHED", "PENDING", "PROCESSING"]}
                        })
                        if existing_active:
                            total_skipped_duplicates += 1
                            continue

                        # Check if product previously FAILED (e.g. temporary network error or image timeout)
                        # If found again, REVIVE it for a fresh publishing attempt!
                        failed_existing = await db.products.find_one({
                            "$or": query_filters,
                            "status": "FAILED"
                        })
                        if failed_existing:
                            logger.info(f"[AutoPilot] Rediscovered previously failed product {pid or norm_url}. Reviving for Telegram publishing!")
                            await db.products.update_one(
                                {"_id": failed_existing["_id"]},
                                {"$set": {
                                    "status": "PENDING",
                                    "error_message": None,
                                    "primary_images": item.get("primary_images", failed_existing.get("primary_images", [])),
                                    "updated_at": utc_now()
                                }}
                            )
                            await db.jobs.update_one(
                                {"product_id": str(failed_existing["_id"])},
                                {"$set": {
                                    "status": "PENDING",
                                    "attempts": 0,
                                    "error_message": None,
                                    "updated_at": utc_now()
                                }},
                                upsert=True
                            )
                            total_new_enqueued += 1
                            continue

                        # New unique product found! Create product and job document
                        product_doc = {
                            "platform": "flipkart",
                            "product_url": raw_url,
                            "normalized_url": norm_url,
                            "external_id": pid,
                            "title": item.get("title") or "Flipkart Dress",
                            "brand": item.get("brand"),
                            "status": "PENDING",
                            "pricing": {
                                "deal_price": item.get("deal_price"),
                                "mrp": item.get("mrp"),
                                "discount_percent": None,
                                "discount_label": None
                            },
                            "sizes": [],
                            "description": None,
                            "primary_images": item.get("primary_images", []),
                            "colors": [],
                            "rating": None,
                            "discovered_by": "AUTOPILOT",
                            "category_seed": cat_name,
                            "created_at": utc_now(),
                            "updated_at": utc_now()
                        }

                        res = await db.products.insert_one(product_doc)
                        product_id = res.inserted_id

                        job_doc = {
                            "product_id": str(product_id),
                            "status": "PENDING",
                            "attempts": 0,
                            "max_attempts": settings.MAX_JOB_ATTEMPTS,
                            "error": None,
                            "created_at": utc_now(),
                            "updated_at": utc_now()
                        }
                        await db.jobs.insert_one(job_doc)
                        total_new_enqueued += 1

                    # Short polite delay between pagination requests
                    await asyncio.sleep(random.uniform(2.5, 4.0))

                except Exception as page_err:
                    logger.warning(f"[AutoPilot] Error scraping page {page_num} of '{cat_name}': {page_err}")
                    await asyncio.sleep(3.0)

            # Delay between categories to stay undetectable
            await asyncio.sleep(random.uniform(3.0, 5.0))

        logger.info(
            f"[AutoPilot] Sweep finished: {total_discovered} products discovered, "
            f"{total_new_enqueued} NEW unique dresses queued, "
            f"{total_skipped_duplicates} duplicates safely filtered."
        )

        summary = {
            "status": "IDLE",
            "last_run_at": utc_now(),
            "total_discovered": total_discovered,
            "total_new_enqueued": total_new_enqueued,
            "total_skipped_duplicates": total_skipped_duplicates
        }

        await db.autopilot_state.update_one(
            {"key": "global_state"},
            {"$set": summary},
            upsert=True
        )

        return summary

autopilot_engine = AutopilotEngine()
