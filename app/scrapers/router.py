import logging
from typing import List, Optional
from app.scrapers.base import BaseScraper
from app.scrapers.meesho import MeeshoScraper
from app.scrapers.flipkart import FlipkartScraper
from app.scrapers.myntra import MyntraScraper
from app.scrapers.amazon import AmazonScraper
from app.models.product import UniversalProduct

logger = logging.getLogger("scraper.router")

class ScraperRouter:
    """Dispatches scraping requests to the matching platform adapter."""

    def __init__(self):
        self.scrapers: List[BaseScraper] = [
            MeeshoScraper(),
            FlipkartScraper(),
            MyntraScraper(),
            AmazonScraper()
        ]

    def get_scraper(self, url: str) -> Optional[BaseScraper]:
        for scraper in self.scrapers:
            if scraper.can_handle(url):
                return scraper
        return None

    def normalize_url(self, url: str) -> str:
        scraper = self.get_scraper(url)
        if scraper:
            return scraper.normalize_url(url)
        return url.strip()

    async def scrape(self, url: str) -> UniversalProduct:
        scraper = self.get_scraper(url)
        if not scraper:
            raise ValueError(f"No supported scraper found for URL: {url}. Supported: Meesho, Flipkart, Myntra, Amazon.")
        
        logger.info(f"Routing {url} to {scraper.platform_name} scraper...")
        return await scraper.extract_product(url)

scraper_router = ScraperRouter()
