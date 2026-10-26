from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import logging
from typing import Optional
from app.config import settings

logger = logging.getLogger("mongodb")

class MongoDB:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None

mongodb = MongoDB()

async def connect_to_mongo():
    """Connect to MongoDB and ensure essential indexes."""
    try:
        logger.info(f"Connecting to MongoDB at: {settings.MONGODB_URI[:25]}...")
        mongodb.client = AsyncIOMotorClient(
            settings.MONGODB_URI,
            serverSelectionTimeoutMS=5000
        )
        mongodb.db = mongodb.client[settings.DATABASE_NAME]

        # Verify connection
        await mongodb.client.admin.command('ping')
        logger.info(f"Connected to MongoDB database: {settings.DATABASE_NAME}")

        # Ensure Indexes
        await mongodb.db.products.create_index("normalized_url", unique=True)
        await mongodb.db.products.create_index("platform")
        await mongodb.db.products.create_index("status")
        await mongodb.db.products.create_index("created_at")

        await mongodb.db.jobs.create_index("status")
        await mongodb.db.jobs.create_index("created_at")
        await mongodb.db.jobs.create_index("product_id")

        await mongodb.db.logs.create_index("created_at")
        logger.info("MongoDB collections and indexes initialized successfully.")

    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        # Note: In development or testing without local mongo, fallback can continue with warning
        pass

async def close_mongo_connection():
    """Close MongoDB connection gracefully."""
    if mongodb.client:
        mongodb.client.close()
        logger.info("MongoDB connection closed.")

def get_database() -> AsyncIOMotorDatabase:
    """Return active MongoDB database instance."""
    return mongodb.db
