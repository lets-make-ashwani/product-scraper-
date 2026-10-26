from datetime import datetime, timezone
from typing import List, Optional
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from app.db.mongodb import get_database
from app.models.product import URLImportRequest, UniversalProduct
from app.scrapers.router import scraper_router
from app.telegram.client import telegram_publisher
from app.services.ai_service import ai_service
from app.services.log_stream import terminal_logger_handler
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

    for raw_url in payload.urls:
        clean_url = raw_url.strip()
        if not clean_url:
            continue

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

                existing = await db.products.find_one({"normalized_url": normalized_url})
                if existing:
                    skipped.append({"url": product_url, "reason": "Already exists in database"})
                    continue

                doc = {
                    "platform": scraper.platform_name,
                    "product_url": product_url,
                    "normalized_url": normalized_url,
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

            # Check existing
            existing = await db.products.find_one({"normalized_url": normalized_url})
            if existing:
                skipped.append({"url": product_url, "reason": "Already exists in database"})
                continue

            # Create initial product document
            doc = {
                "platform": scraper.platform_name,
                "product_url": product_url,
                "normalized_url": normalized_url,
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
