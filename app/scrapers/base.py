import re
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse
import httpx
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
        Hybrid Loader:
        1. Ultra-fast direct HTTP fetch (<1s, 0 MB browser RAM footprint).
        2. Seamless fallback to Playwright Chromium with anti-bot stealth evasions.
        """
        # 1. Fast HTTP attempt
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1"
            }
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 10000:
                    text_lower = resp.text.lower()
                    if "captcha" not in text_lower and "access denied" not in text_lower:
                        logger.info(f"[{self.platform_name}] Fast HTTP fetched {len(resp.text)} bytes successfully.")
                        return resp.text, None
        except Exception as e:
            logger.debug(f"[{self.platform_name}] Fast HTTP attempt bypassed: {e}")

        # 2. Playwright Chromium Fallback
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
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'hi'] });
            """)

            page: Page = await context.new_page()

            try:
                logger.info(f"[{self.platform_name}] Playwright loading page: {url}")
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
