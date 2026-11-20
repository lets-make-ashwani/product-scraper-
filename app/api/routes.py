import re
import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse, parse_qs
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from app.db.mongodb import get_database
from app.models.product import URLImportRequest, UniversalProduct
from app.scrapers.router import scraper_router
from app.telegram.client import telegram_publisher
from app.services.ai_service import ai_service
from app.services.log_stream import terminal_logger_handler
from app.services.autopilot import autopilot_engine
from app.config import settings

router = APIRouter(prefix="/api", tags=["API"])

def utc_now():
    return datetime.now(timezone.utc)

@router.post("/products/import")
async def import_urls(payload: URLImportRequest):
    """Import URLs from Meesho, Flipkart, Myntra, or Amazon (Supports both Single Products & Search/Category pages)."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    imported = []
    skipped = []

    # Cleanly expand URLs even if pasted across newlines, spaces, or with comments
    expanded_urls = []
    for raw_item in payload.urls:
        if not raw_item:
            continue
        parts = re.findall(r'https?://[^\s,]+', raw_item.strip())
        if parts:
            expanded_urls.extend(parts)
        elif raw_item.strip().startswith("http"):
            expanded_urls.append(raw_item.strip())

    for clean_url in expanded_urls:
        scraper = scraper_router.get_scraper(clean_url)
        if not scraper:
            skipped.append({"url": clean_url, "reason": "Unsupported platform"})
            continue

        # Check if the URL is a Search or Category Listing
        if scraper.is_search_url(clean_url):
            catalog_items = await scraper.extract_search_catalog(clean_url, max_items=40)
            if not catalog_items:
                skipped.append({"url": clean_url, "reason": "No products could be extracted from search listing"})
                continue

            for item in catalog_items:
                product_url = item["product_url"]
                normalized_url = scraper.normalize_url(product_url)

                # Extract external_id (e.g. Flipkart PID)
                parsed = urlparse(normalized_url)
                qs = parse_qs(parsed.query)
                pid = qs.get("pid", [None])[0]

                query_filters = [{"normalized_url": normalized_url}]
                if pid:
                    query_filters.append({"platform": scraper.platform_name, "external_id": pid})

                existing = await db.products.find_one({"$or": query_filters})
                if existing:
                    if existing.get("status") == "FAILED":
                        p_id = str(existing["_id"])
                        await db.products.update_one(
                            {"_id": existing["_id"]},
                            {"$set": {"status": "PENDING", "error_message": None, "updated_at": utc_now()}}
                        )
                        await db.jobs.update_one(
                            {"product_id": p_id},
                            {"$set": {"status": "PENDING", "attempts": 0, "error_message": None, "updated_at": utc_now()}},
                            upsert=True
                        )
                        imported.append({"id": p_id, "url": product_url, "platform": scraper.platform_name})
                    else:
                        skipped.append({"url": product_url, "reason": f"Already exists ({existing.get('status')})"})
                    continue

                doc = {
                    "platform": scraper.platform_name,
                    "product_url": product_url,
                    "normalized_url": normalized_url,
                    "external_id": pid,
                    "title": item.get("title") or f"Queued {scraper.platform_name.title()} Product",
                    "brand": item.get("brand"),
                    "status": "PENDING",
                    "pricing": {
                        "deal_price": item.get("deal_price"),
                        "mrp": item.get("mrp"),
                        "discount_percent": None,
                        "discount_label": None
                    },
                    "sizes": [],
                    "colors": [],
                    "primary_images": item.get("primary_images", []),
                    "created_at": utc_now(),
                    "updated_at": utc_now()
                }

                res = await db.products.insert_one(doc)
                product_id = res.inserted_id

                job_doc = {
                    "product_id": str(product_id),
                    "job_type": "SCRAPE_AND_PUBLISH",
                    "status": "PENDING",
                    "attempts": 0,
                    "max_attempts": settings.MAX_JOB_ATTEMPTS,
                    "created_at": utc_now(),
                    "updated_at": utc_now()
                }
                await db.jobs.insert_one(job_doc)
                imported.append({"id": str(product_id), "url": product_url, "platform": scraper.platform_name})

        else:
            product_url = clean_url
            normalized_url = scraper.normalize_url(product_url)

            # Extract external_id
            parsed = urlparse(normalized_url)
            qs = parse_qs(parsed.query)
            pid = qs.get("pid", [None])[0]

            query_filters = [{"normalized_url": normalized_url}]
            if pid:
                query_filters.append({"platform": scraper.platform_name, "external_id": pid})

            # Check existing
            existing = await db.products.find_one({"$or": query_filters})
            if existing:
                if existing.get("status") == "FAILED":
                    p_id = str(existing["_id"])
                    await db.products.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {"status": "PENDING", "error_message": None, "updated_at": utc_now()}}
                    )
                    await db.jobs.update_one(
                        {"product_id": p_id},
                        {"$set": {"status": "PENDING", "attempts": 0, "error_message": None, "updated_at": utc_now()}},
                        upsert=True
                    )
                    imported.append({"id": p_id, "url": product_url, "platform": scraper.platform_name})
                else:
                    skipped.append({"url": product_url, "reason": f"Already exists ({existing.get('status')})"})
                continue

            # Create initial product document
            doc = {
                "platform": scraper.platform_name,
                "product_url": product_url,
                "normalized_url": normalized_url,
                "external_id": pid,
                "title": f"Queued {scraper.platform_name.title()} Product",
                "status": "PENDING",
                "pricing": {},
                "sizes": [],
                "colors": [],
                "primary_images": [],
                "created_at": utc_now(),
                "updated_at": utc_now()
            }

            res = await db.products.insert_one(doc)
            product_id = res.inserted_id

            # Create background Job
            job_doc = {
                "product_id": str(product_id),
                "job_type": "SCRAPE_AND_PUBLISH",
                "status": "PENDING",
                "attempts": 0,
                "max_attempts": settings.MAX_JOB_ATTEMPTS,
                "created_at": utc_now(),
                "updated_at": utc_now()
            }
            await db.jobs.insert_one(job_doc)
            imported.append({"id": str(product_id), "url": product_url, "platform": scraper.platform_name})

    return {"imported": imported, "skipped": skipped, "total_imported": len(imported)}

@router.get("/products")
async def get_products(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100
):
    """Retrieve scraped products from MongoDB."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    query = {}
    if platform and platform != "all":
        query["platform"] = platform.lower()
    if status and status != "all":
        query["status"] = status.upper()

    cursor = db.products.find(query).sort("created_at", -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        items.append(doc)

    total = await db.products.count_documents(query)
    return {"total": total, "items": items}

@router.get("/products/{product_id}")
async def get_product_detail(product_id: str):
    """Get single product details with full color variants."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    try:
        oid = ObjectId(product_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Product ID format.")

    doc = await db.products.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found.")

    doc["_id"] = str(doc["_id"])
    return doc

@router.delete("/products/{product_id}")
async def delete_product(product_id: str):
    """Delete a single product and any of its queued or active jobs."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    try:
        oid = ObjectId(product_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Product ID format.")

    res = await db.products.delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found.")

    # Remove any associated jobs
    await db.jobs.delete_many({"product_id": product_id})
    return {"success": True, "message": "Product and associated jobs successfully deleted."}

@router.delete("/products")
async def bulk_delete_products(delete_all: bool = False, status: Optional[str] = None):
    """Bulk delete products: delete all, or delete by status (e.g. SCRAPED, PUBLISHED, FAILED)."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    if delete_all:
        p_res = await db.products.delete_many({})
        j_res = await db.jobs.delete_many({})
        return {
            "success": True,
            "deleted_products": p_res.deleted_count,
            "deleted_jobs": j_res.deleted_count,
            "message": f"Successfully cleared all {p_res.deleted_count} products and {j_res.deleted_count} jobs."
        }

    if status and status != "all":
        stat_upper = status.upper()
        cursor = db.products.find({"status": stat_upper}, {"_id": 1})
        p_ids = [str(doc["_id"]) async for doc in cursor]

        p_res = await db.products.delete_many({"status": stat_upper})
        j_res = await db.jobs.delete_many({"product_id": {"$in": p_ids}}) if p_ids else None
        return {
            "success": True,
            "deleted_products": p_res.deleted_count,
            "deleted_jobs": j_res.deleted_count if j_res else 0,
            "message": f"Deleted {p_res.deleted_count} products with status {stat_upper}."
        }

    raise HTTPException(status_code=400, detail="Specify either delete_all=true or status=...")

@router.post("/products/{product_id}/publish")
async def publish_single_product(product_id: str):
    """Manually queue or re-try publishing a specific product to Telegram."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    try:
        oid = ObjectId(product_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Product ID format.")

    prod = await db.products.find_one({"_id": oid})
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found.")

    # Reset product status to PENDING
    await db.products.update_one({"_id": oid}, {"$set": {"status": "PENDING", "error_message": None, "updated_at": utc_now()}})

    # Insert or update background job
    job = await db.jobs.find_one({"product_id": product_id})
    if job:
        await db.jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "PENDING", "attempts": 0, "error_message": None, "updated_at": utc_now()}}
        )
    else:
        await db.jobs.insert_one({
            "product_id": product_id,
            "job_type": "SCRAPE_AND_PUBLISH",
            "status": "PENDING",
            "attempts": 0,
            "max_attempts": settings.MAX_JOB_ATTEMPTS,
            "created_at": utc_now(),
            "updated_at": utc_now()
        })

    return {"success": True, "message": f"Product queued for scraping & Telegram publishing."}

@router.post("/products/publish-all")
async def publish_all_unposted():
    """Queue all unposted products (SCRAPED, FAILED, or PENDING) for publishing."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    cursor = db.products.find({"status": {"$ne": "PUBLISHED"}})
    queued = 0
    async for prod in cursor:
        p_id = str(prod["_id"])
        job = await db.jobs.find_one({"product_id": p_id})
        if job:
            await db.jobs.update_one(
                {"_id": job["_id"]},
                {"$set": {"status": "PENDING", "attempts": 0, "error_message": None, "updated_at": utc_now()}}
            )
        else:
            await db.jobs.insert_one({
                "product_id": p_id,
                "job_type": "SCRAPE_AND_PUBLISH",
                "status": "PENDING",
                "attempts": 0,
                "max_attempts": settings.MAX_JOB_ATTEMPTS,
                "created_at": utc_now(),
                "updated_at": utc_now()
            })
        await db.products.update_one({"_id": prod["_id"]}, {"$set": {"status": "PENDING", "error_message": None, "updated_at": utc_now()}})
        queued += 1

    return {"success": True, "queued_count": queued, "message": f"Queued {queued} products for publishing."}

@router.post("/products/retry-failed")
async def retry_failed_products():
    """Reset all FAILED products back to PENDING and re-enqueue them for publishing."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    cursor = db.products.find({"status": "FAILED"})
    failed_pids = []
    async for prod in cursor:
        failed_pids.append(str(prod["_id"]))

    if not failed_pids:
        return {"success": True, "count": 0, "message": "No failed products found to retry."}

    await db.products.update_many(
        {"_id": {"$in": [ObjectId(pid) for pid in failed_pids]}},
        {"$set": {"status": "PENDING", "error_message": None, "updated_at": utc_now()}}
    )

    for pid in failed_pids:
        await db.jobs.update_one(
            {"product_id": pid},
            {"$set": {"status": "PENDING", "attempts": 0, "error_message": None, "updated_at": utc_now()}},
            upsert=True
        )

    return {
        "success": True,
        "count": len(failed_pids),
        "message": f"Successfully re-queued {len(failed_pids)} failed products for publishing."
    }

@router.get("/logs")
async def get_logs(since_id: int = 0):
    """Fetch live terminal logs from memory ring-buffer."""
    logs = terminal_logger_handler.get_logs(since_id=since_id)
    return {"logs": logs, "total": len(logs)}

@router.delete("/logs")
async def clear_logs():
    """Clear the live terminal logs buffer."""
    terminal_logger_handler.clear()
    return {"success": True, "message": "Terminal logs cleared."}

@router.get("/jobs")
async def get_jobs(limit: int = 30):
    """Get background worker jobs."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    cursor = db.jobs.find().sort("created_at", -1).limit(limit)
    jobs = []
    async for j in cursor:
        j["_id"] = str(j["_id"])
        jobs.append(j)
    return {"jobs": jobs}

@router.get("/stats")
async def get_stats():
    """Get summary statistics for dashboard."""
    db = get_database()
    if db is None:
        return {"status": "MongoDB offline", "total_products": 0, "published": 0, "pending_queue": 0}

    total_products = await db.products.count_documents({})
    published = await db.products.count_documents({"status": "PUBLISHED"})
    scraped = await db.products.count_documents({"status": "SCRAPED"})
    failed = await db.products.count_documents({"status": "FAILED"})
    pending_products = await db.products.count_documents({"status": "PENDING"})

    pending_jobs = await db.jobs.count_documents({"status": "PENDING"})
    processing_jobs = await db.jobs.count_documents({"status": "PROCESSING"})

    # Platform counts
    pipeline = [{"$group": {"_id": "$platform", "count": {"$sum": 1}}}]
    platforms = {}
    async for p in db.products.aggregate(pipeline):
        if p["_id"]:
            platforms[p["_id"]] = p["count"]

    return {
        "total_products": total_products,
        "published_telegram": published,
        "scraped_count": scraped,
        "failed_count": failed,
        "pending_count": pending_products,
        "pending_queue": pending_jobs,
        "processing_queue": processing_jobs,
        "platforms": platforms,
        "groq_ai_enabled": bool(settings.GROQ_API_KEY and settings.AI_ENHANCE_POSTS),
        "telegram_channel": settings.TELEGRAM_CHAT_ID
    }

@router.get("/autopilot/status")
async def get_autopilot_status():
    """Get the current state and metrics of the 24/7 Autonomous Discovery Engine."""
    db = get_database()
    if db is None:
        return {"enabled": False, "status": "OFFLINE"}

    state = await db.autopilot_state.find_one({"key": "global_state"}) or {}
    state.pop("_id", None)
    return {
        "engine_running": autopilot_engine.is_running,
        "config_enabled": settings.AUTOPILOT_ENABLED,
        "state": state
    }

@router.post("/autopilot/toggle")
async def toggle_autopilot(payload: dict):
    """Enable or pause the 24/7 Autonomous Discovery Engine."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="Database offline")

    enable = bool(payload.get("enable", True))
    await db.autopilot_state.update_one(
        {"key": "global_state"},
        {"$set": {"enabled": enable, "updated_at": utc_now()}},
        upsert=True
    )

    if enable and not autopilot_engine.is_running:
        autopilot_engine.start()
    elif not enable and autopilot_engine.is_running:
        autopilot_engine.stop()

    return {"success": True, "autopilot_enabled": enable}

@router.post("/autopilot/trigger")
async def trigger_autopilot_now():
    """Manually trigger one full discovery cycle immediately."""
    asyncio.create_task(autopilot_engine.run_discovery_cycle())
    return {"success": True, "message": "Autonomous discovery sweep triggered in background."}

@router.post("/telegram/test")
async def test_telegram():
    """Verify Telegram session or bot connection."""
    success, message = await telegram_publisher.test_connection()
    return {"success": success, "message": message}

@router.post("/ai/test")
async def test_ai():
    """Verify Groq AI connection and model availability."""
    success, message = await ai_service.test_connection()
    return {"success": success, "message": message}
