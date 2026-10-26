import re
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse
from playwright.async_api import async_playwright, Browser, Page
from app.models.product import UniversalProduct
from app.config import settings

logger = logging.getLogger("scraper.base")

class BaseScraper(ABC):
    """Abstract Base Class for E-commerce Platform Scrapers."""

    platform_name: str = "generic"

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True if this scraper can handle the given URL."""
        pass

    @abstractmethod
    def normalize_url(self, url: str) -> str:
        """Strip tracking query parameters and return canonical product URL."""
        pass

    @abstractmethod
    async def extract_product(self, url: str) -> UniversalProduct:
        """Extract product metadata, images, and all color variants."""
        pass

    def is_search_url(self, url: str) -> bool:
        """Determine if the URL is a category or search listing instead of a single product."""
        return False

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        """Extract individual product URLs from a search or category results page."""
        return []

    async def fetch_page_html(self, url: str, wait_selector: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """
        Loads page using Playwright Chromium with anti-bot evasion headers.
        Returns: (html_content, error_message)
        """
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=settings.HEADLESS_BROWSER,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--disable-gpu",
                    "--window-size=1920,1080"
                ]
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US"
            )

            # Prevent webdriver detection
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            page: Page = await context.new_page()

            try:
                logger.info(f"[{self.platform_name}] Loading page: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=5000)
                    except Exception:
                        pass

                # Brief wait for hydration
                await page.wait_for_timeout(2000)
                html = await page.content()
                await browser.close()
                return html, None

            except Exception as e:
                logger.error(f"[{self.platform_name}] Error fetching {url}: {e}")
                await browser.close()
                return "", str(e)

    def clean_generic_url(self, url: str) -> str:
        """Remove query params & hash for general URLs."""
        if not url:
            return ""
        parsed = urlparse(url.strip())
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
