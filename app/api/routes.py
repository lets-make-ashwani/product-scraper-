from datetime import datetime, timezone
from typing import List, Optional
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from app.db.mongodb import get_database
from app.models.product import URLImportRequest, UniversalProduct
from app.scrapers.router import scraper_router
from app.telegram.client import telegram_publisher
from app.services.ai_service import ai_service
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
        target_urls = [clean_url]
        if scraper.is_search_url(clean_url):
            discovered = await scraper.extract_search_product_urls(clean_url, max_items=40)
            if not discovered:
                skipped.append({"url": clean_url, "reason": "No products could be extracted from search listing"})
                continue
            target_urls = discovered

        for product_url in target_urls:
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
    limit: int = 50
):
    """Retrieve scraped products from MongoDB."""
    db = get_database()
    if db is None:
        raise HTTPException(status_code=503, detail="MongoDB not connected.")

    query = {}
    if platform:
        query["platform"] = platform.lower()
    if status:
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
        "pending_queue": pending_jobs,
        "processing_queue": processing_jobs,
        "platforms": platforms,
        "groq_ai_enabled": bool(settings.GROQ_API_KEY and settings.AI_ENHANCE_POSTS)
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
