import json
import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from app.scrapers.base import BaseScraper
from app.models.product import UniversalProduct, ColorVariant, ProductPricing, ProductRating

logger = logging.getLogger("scraper.amazon")

class AmazonScraper(BaseScraper):
    platform_name = "amazon"

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        netloc = urlparse(url.strip().lower()).netloc
        return "amazon.in" in netloc or "amazon.com" in netloc or "amzn.to" in netloc or "amzn.in" in netloc

    def normalize_url(self, url: str) -> str:
        clean = self.clean_generic_url(url)
        # Extract ASIN (10 alphanumeric characters e.g. B08N5WRWNW)
        match = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})', clean)
        if match:
            return f"https://www.amazon.in/dp/{match.group(1)}"
        return clean

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)
        html, error = await self.fetch_page_html(normalized_url, wait_selector="#productTitle")
        if error or not html:
            raise RuntimeError(f"Failed to load Amazon product: {error}")

        soup = BeautifulSoup(html, "html.parser")
        product = UniversalProduct(
            platform=self.platform_name,
            product_url=url,
            normalized_url=normalized_url,
            title="Amazon Product"
        )

        # 1. ASIN External ID
        asin_match = re.search(r'/dp/([A-Z0-9]{10})', normalized_url)
        if asin_match:
            product.external_id = asin_match.group(1)

        # 2. Title
        title_node = soup.find(id="productTitle")
        if title_node:
            product.title = title_node.get_text(strip=True)

        # 3. Brand
        byline = soup.find(id="bylineInfo")
        if byline:
            product.brand = re.sub(r'^(Brand:\s*|Visit the\s*|\s*Store)', '', byline.get_text(strip=True)).strip()

        # 4. Pricing & MRP
        self._parse_pricing(soup, product)

        # 5. Rating & Reviews
        self._parse_ratings(soup, product)

        # 6. Description / Feature Bullets
        bullets = soup.select("#feature-bullets ul li span.a-list-item")
        if bullets:
            bullet_texts = [b.get_text(strip=True) for b in bullets if b.get_text(strip=True)]
            product.description = "\n".join(f"• {t}" for t in bullet_texts[:5])

        # 7. High-res Images from colorImages script
        self._parse_images(soup, html, product)

        # 8. Color Variants from swatches
        self._parse_color_swatches(soup, product)

        return product

    def _parse_pricing(self, soup: BeautifulSoup, product: UniversalProduct):
        # Deal price
        price_node = soup.select_one(".priceToPay .a-price-whole") or soup.select_one(".apexPriceToPay .a-offscreen") or soup.select_one(".a-price .a-offscreen")
        if price_node:
            raw_price = re.sub(r'[^0-9.]', '', price_node.get_text())
            try:
                product.pricing.deal_price = float(raw_price)
            except Exception:
                pass

        # MRP / List Price
        mrp_node = soup.select_one(".basisPrice .a-text-price .a-offscreen") or soup.select_one(".a-text-strike")
        if mrp_node:
            raw_mrp = re.sub(r'[^0-9.]', '', mrp_node.get_text())
            try:
                product.pricing.mrp = float(raw_mrp)
            except Exception:
                pass

        # Discount Percentage
        if product.pricing.deal_price and product.pricing.mrp and product.pricing.mrp > product.pricing.deal_price:
            calc_disc = round(((product.pricing.mrp - product.pricing.deal_price) / product.pricing.mrp) * 100)
            product.pricing.discount_percent = calc_disc

    def _parse_ratings(self, soup: BeautifulSoup, product: UniversalProduct):
        rating_node = soup.select_one("#acrPopover") or soup.select_one(".a-icon-star .a-icon-alt")
        if rating_node:
            match = re.search(r'([0-9.]+)\s+out of', rating_node.get_text())
            if match:
                score = float(match.group(1))
                rev_node = soup.select_one("#acrCustomerReviewText")
                rev_cnt = None
                if rev_node:
                    rev_match = re.search(r'([0-9,]+)', rev_node.get_text())
                    if rev_match:
                        rev_cnt = int(rev_match.group(1).replace(",", ""))
                product.rating = ProductRating(score=score, review_count=rev_cnt)

    def _parse_images(self, soup: BeautifulSoup, html: str, product: UniversalProduct):
        # Try finding colorImages JSON in scripts
        img_match = re.search(r"'colorImages':\s*\{\s*'initial':\s*(\[.*?\])\s*\},", html, re.DOTALL)
        if img_match:
            try:
                img_data = json.loads(img_match.group(1))
                for item in img_data:
                    hi_res = item.get("hiRes") or item.get("large")
                    if hi_res and hi_res not in product.primary_images:
                        product.primary_images.append(hi_res)
            except Exception:
                pass

        # Fallback to main landing image
        if not product.primary_images:
            landing_img = soup.select_one("#landingImage")
            if landing_img and landing_img.get("data-old-hires"):
                product.primary_images.append(landing_img["data-old-hires"])
            elif landing_img and landing_img.get("src"):
                product.primary_images.append(landing_img["src"])

    def _parse_color_swatches(self, soup: BeautifulSoup, product: UniversalProduct):
        # Color variants list
        swatches = soup.select("#variation_color_name li")
        for s in swatches:
            img = s.find("img")
            color_name = s.get("title", "") or (img.get("alt") if img else "")
            color_name = re.sub(r'^Click to select\s*', '', color_name).strip()

            asin = s.get("data-defaultasin")
            variant_url = f"https://www.amazon.in/dp/{asin}" if asin else None

            images = []
            if img and img.get("src"):
                # Amazon high-res swatch transformation
                high_res_swatch = re.sub(r'\._[A-Z0-9_,]+_\.', '.', img["src"])
                images.append(high_res_swatch)

            if color_name:
                product.colors.append(ColorVariant(
                    color_name=color_name,
                    variant_url=variant_url,
                    image_urls=images
                ))
