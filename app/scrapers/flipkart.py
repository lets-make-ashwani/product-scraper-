import json
import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, urlunparse
from app.scrapers.base import BaseScraper
from app.models.product import UniversalProduct, ColorVariant, ProductPricing, ProductRating

logger = logging.getLogger("scraper.flipkart")

class FlipkartScraper(BaseScraper):
    platform_name = "flipkart"

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        netloc = urlparse(url.strip().lower()).netloc
        return "flipkart.com" in netloc or "dl.flipkart.com" in netloc

    def normalize_url(self, url: str) -> str:
        """Strip affiliate/query tokens and keep canonical path with pid."""
        parsed = urlparse(url.strip())
        qs = parse_qs(parsed.query)
        pid = qs.get("pid", [None])[0]

        # Standard Flipkart clean path
        clean_path = parsed.path
        if pid:
            return f"https://www.flipkart.com{clean_path}?pid={pid}"
        return f"https://www.flipkart.com{clean_path}"

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)
        html, error = await self.fetch_page_html(normalized_url)
        if error or not html:
            raise RuntimeError(f"Failed to load Flipkart product: {error}")

        soup = BeautifulSoup(html, "html.parser")
        product = UniversalProduct(
            platform=self.platform_name,
            product_url=url,
            normalized_url=normalized_url,
            title="Flipkart Product"
        )

        # 1. Parse Schema.org JSON-LD
        self._parse_json_ld(soup, product)

        # 2. Extract DOM Title, Price, MRP, Rating
        self._parse_dom(soup, product)

        # 3. Extract Color Variants & Swatches
        self._parse_color_variants(soup, product)

        return product

    def _parse_json_ld(self, soup: BeautifulSoup, product: UniversalProduct):
        scripts = soup.find_all("script", type="application/ld+json")
        for s in scripts:
            try:
                js = json.loads(s.string)
                if isinstance(js, list):
                    js = js[0] if js else {}
                if isinstance(js, dict) and js.get("@type") == "Product":
                    product.title = js.get("name") or product.title
                    product.description = js.get("description") or product.description
                    if js.get("brand") and isinstance(js["brand"], dict):
                        product.brand = js["brand"].get("name")

                    # Offers / Price
                    offers = js.get("offers")
                    if isinstance(offers, dict):
                        price_val = offers.get("price")
                        if price_val:
                            product.pricing.deal_price = float(price_val)

                    # Images
                    imgs = js.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    for img in imgs:
                        high_res = self._upgrade_image_url(img)
                        if high_res and high_res not in product.primary_images:
                            product.primary_images.append(high_res)
            except Exception:
                pass

    def _parse_dom(self, soup: BeautifulSoup, product: UniversalProduct):
        # Title
        if not product.title or product.title == "Flipkart Product":
            title_node = soup.find("h1") or soup.find("span", class_=re.compile(r"B_NuCI|VU-ZEz"))
            if title_node:
                product.title = title_node.get_text(strip=True)

        # Price
        price_node = soup.find("div", class_=re.compile(r"_30jeq3|Nx9bqj"))
        if price_node and not product.pricing.deal_price:
            raw_text = re.sub(r'[^0-9.]', '', price_node.get_text())
            try:
                product.pricing.deal_price = float(raw_text)
            except Exception:
                pass

        # MRP
        mrp_node = soup.find("div", class_=re.compile(r"_3I9_wc|yRaY8j"))
        if mrp_node:
            raw_text = re.sub(r'[^0-9.]', '', mrp_node.get_text())
            try:
                product.pricing.mrp = float(raw_text)
            except Exception:
                pass

        # Discount
        disc_node = soup.find("div", class_=re.compile(r"_3Ay6Sb|UkUFwK"))
        if disc_node:
            disc_text = disc_node.get_text(strip=True)
            match = re.search(r'(\d+)\s*%', disc_text)
            if match:
                product.pricing.discount_percent = int(match.group(1))

        # Rating
        rating_node = soup.find("div", class_=re.compile(r"_3LWZlK|XQDdHH"))
        if rating_node:
            try:
                product.rating = ProductRating(score=float(rating_node.get_text(strip=True)))
            except Exception:
                pass

        # Primary images from gallery thumbnails if JSON-LD was sparse
        img_nodes = soup.find_all("img", src=re.compile(r'rukminim\d*\.flixcart\.com/image/'))
        for img in img_nodes:
            src = img.get("src") or img.get("data-src")
            if src:
                high_res = self._upgrade_image_url(src)
                if high_res and high_res not in product.primary_images:
                    product.primary_images.append(high_res)

    def _parse_color_variants(self, soup: BeautifulSoup, product: UniversalProduct):
        # Flipkart Color Swatches are represented as list items or thumbnails
        swatch_containers = soup.find_all("li", id=re.compile(r'swatch-\d+-color|swatch'))
        if not swatch_containers:
            swatch_containers = soup.find_all("div", class_=re.compile(r'_331-kn|_10Ermr'))

        for container in swatch_containers:
            img = container.find("img")
            link = container.find("a")
            color_title = container.get("title") or (img.get("alt") if img else None)

            if color_title:
                variant_url = f"https://www.flipkart.com{link['href']}" if (link and link.get("href")) else None
                variant_images = []
                if img and img.get("src"):
                    variant_images.append(self._upgrade_image_url(img["src"]))

                product.colors.append(ColorVariant(
                    color_name=color_title.strip(),
                    variant_url=variant_url,
                    image_urls=variant_images
                ))

    def _upgrade_image_url(self, url: str) -> str:
        """Upgrades Flipkart thumbnail URLs to 1664x1664 high-resolution."""
        if not url:
            return ""
        return re.sub(r'/image/\d+/\d+/', r'/image/1664/1664/', url.strip())
