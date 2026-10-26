from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class ColorVariant(BaseModel):
    color_name: str
    variant_url: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    in_stock: bool = True
    sizes: List[str] = Field(default_factory=list)

class ProductPricing(BaseModel):
    deal_price: Optional[float] = None
    mrp: Optional[float] = None
    discount_percent: Optional[int] = None
    discount_label: Optional[str] = None

class ProductRating(BaseModel):
    score: Optional[float] = None
    review_count: Optional[int] = None

class UniversalProduct(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    platform: str  # meesho, flipkart, myntra, amazon
    external_id: Optional[str] = None
    product_url: str
    normalized_url: str
    title: str
    brand: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    pricing: ProductPricing = Field(default_factory=ProductPricing)
    rating: Optional[ProductRating] = None
    sizes: List[str] = Field(default_factory=list)
    colors: List[ColorVariant] = Field(default_factory=list)
    primary_images: List[str] = Field(default_factory=list)
    status: str = "PENDING"  # PENDING, SCRAPED, PUBLISHED, FAILED
    telegram_message_ids: List[int] = Field(default_factory=list)
    telegram_media_group_id: Optional[str] = None
    error_message: Optional[str] = None
    scraped_at: Optional[datetime] = None
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Config:
        populate_by_name = True

class Job(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    product_id: str
    job_type: str  # SCRAPE_AND_PUBLISH
    status: str = "PENDING"  # PENDING, PROCESSING, COMPLETED, FAILED, RETRYING
    attempts: int = 0
    max_attempts: int = 3
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Config:
        populate_by_name = True

class URLImportRequest(BaseModel):
    urls: List[str]

class ProductResponse(BaseModel):
    id: str
    platform: str
    product_url: str
    title: str
    price: Optional[float] = None
    mrp: Optional[float] = None
    discount_percent: Optional[int] = None
    rating: Optional[float] = None
    colors_count: int = 0
    images_count: int = 0
    status: str
    created_at: datetime
