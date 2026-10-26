import json
import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, unquote
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

    def is_search_url(self, url: str) -> bool:
        """Detect Flipkart search or category catalog URLs."""
        if not url:
            return False
        u = url.lower()
        return "/search" in u or "q=" in u or "/pr?sid=" in u or "otracker=" in u or "searchtype=" in u

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        """Loads Flipkart search page and unpacks all individual product links."""
        logger.info(f"[Flipkart] Unpacking search listing: {url}")
        html, error = await self.fetch_page_html(url, wait_selector="a[href*='/p/']")
        if error or not html:
            logger.warning(f"[Flipkart] Could not fetch search page: {error}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        product_links = []
        seen = set()

        anchors = soup.find_all("a", href=re.compile(r'/p/itm|pid='))
        for a in anchors:
            href = a.get("href")
            if not href:
                continue

            full_url = f"https://www.flipkart.com{href}" if href.startswith("/") else href
            norm = self.normalize_url(full_url)

            if ("/p/" in norm or "pid=" in norm) and "/search" not in norm and norm not in seen:
                seen.add(norm)
                product_links.append(norm)
                if len(product_links) >= max_items:
                    break

        logger.info(f"[Flipkart] Discovered {len(product_links)} products from search page.")
        return product_links

    def normalize_url(self, url: str) -> str:
        """Strip affiliate/query tokens and keep canonical path with pid."""
        parsed = urlparse(url.strip())
        qs = parse_qs(parsed.query)
        pid = qs.get("pid", [None])[0]

        clean_path = parsed.path
        if pid:
            return f"https://www.flipkart.com{clean_path}?pid={pid}"
        return f"https://www.flipkart.com{clean_path}"

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)
        # Wait for price or title to ensure JavaScript hydration completes
        html, error = await self.fetch_page_html(normalized_url, wait_selector="div.Nx9bqj, div._30jeq3, h1, span.VU-ZEz, span.B_NuCI")
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

        # 4. Fallback Title from URL slug if still generic
        if not product.title or product.title == "Flipkart Product":
            slug_match = re.search(r'flipkart\.com/([^/?#]+)/p/', normalized_url)
            if slug_match:
                slug_name = slug_match.group(1).replace('-', ' ')
                product.title = unquote(slug_name).title()

        return product

    def _parse_json_ld(self, soup: BeautifulSoup, product: UniversalProduct):
        scripts = soup.find_all("script", type="application/ld+json")
        for s in scripts:
            try:
                js = json.loads(s.string)
                items = js if isinstance(js, list) else [js]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        product.title = item.get("name") or product.title
                        product.description = item.get("description") or product.description
                        if item.get("brand") and isinstance(item["brand"], dict):
                            product.brand = item["brand"].get("name")

                        # Offers / Price
                        offers = item.get("offers")
                        if isinstance(offers, list) and offers:
                            offers = offers[0]
                        if isinstance(offers, dict):
                            price_val = offers.get("price")
                            if price_val:
                                product.pricing.deal_price = float(price_val)

                        # Images
                        imgs = item.get("image", [])
                        if isinstance(imgs, str):
                            imgs = [imgs]
                        for img in imgs:
                            high_res = self._upgrade_image_url(img)
                            if high_res and high_res not in product.primary_images:
                                product.primary_images.append(high_res)
            except Exception:
                pass

    def _parse_dom(self, soup: BeautifulSoup, product: UniversalProduct):
        # Brand & Title
        brand_node = soup.find("span", class_="G6XhRU")
        title_node = (
            soup.find("h1") or
            soup.find("span", class_=re.compile(r"B_NuCI|VU-ZEz|_6EBuvd"))
        )
        if title_node:
            t = title_node.get_text(separator=' ', strip=True)
            if brand_node and brand_node.get_text(strip=True) not in t:
                t = f"{brand_node.get_text(strip=True)} {t}"
            if t and t != "Flipkart Product":
                product.title = t

        # Price
        price_node = (
            soup.find("div", class_=re.compile(r"Nx9bqj|_30jeq3|_16Jk6d")) or
            soup.find("div", class_="Nx9bqj CxhGGd") or
            soup.find(string=re.compile(r'^\u20b9\s*\d+'))
        )
        if price_node and not product.pricing.deal_price:
            raw_text = price_node if isinstance(price_node, str) else price_node.get_text()
            nums = re.sub(r'[^0-9.]', '', raw_text)
            try:
                product.pricing.deal_price = float(nums)
            except Exception:
                pass

        # MRP
        mrp_node = (
            soup.find("div", class_=re.compile(r"yRaY8j|_3I9_wc")) or
            soup.find("div", class_="yRaY8j A68RT5")
        )
        if mrp_node:
            nums = re.sub(r'[^0-9.]', '', mrp_node.get_text())
            try:
                product.pricing.mrp = float(nums)
            except Exception:
                pass

        # Discount
        disc_node = (
            soup.find("div", class_=re.compile(r"UkUFwK|_3Ay6Sb")) or
            soup.find("div", class_="UkUFwK WW8yVX")
        )
        if disc_node:
            match = re.search(r'(\d+)\s*%', disc_node.get_text())
            if match:
                product.pricing.discount_percent = int(match.group(1))

        # Rating
        rating_node = soup.find("div", class_=re.compile(r"XQDdHH|_3LWZlK"))
        if rating_node:
            try:
                product.rating = ProductRating(score=float(rating_node.get_text(strip=True)))
            except Exception:
                pass

        # Sizes
        size_nodes = soup.find_all("a", class_=re.compile(r'CD6vsq|_1fJSAg'))
        for s in size_nodes:
            txt = s.get_text(strip=True)
            if txt and txt not in product.sizes and len(txt) <= 6:
                product.sizes.append(txt)

        # Primary images from gallery thumbnails
        img_nodes = soup.find_all("img", src=re.compile(r'rukminim\d*\.flixcart\.com/image/'))
        for img in img_nodes:
            src = img.get("src") or img.get("data-src")
            if src:
                high_res = self._upgrade_image_url(src)
                if high_res and high_res not in product.primary_images:
                    product.primary_images.append(high_res)

    def _parse_color_variants(self, soup: BeautifulSoup, product: UniversalProduct):
        # Look for swatch color items
        swatch_items = soup.find_all(lambda tag: tag.name in ['li', 'div'] and ('swatch' in tag.get('id', '').lower() or 'swatch' in tag.get('class', [''])[0].lower() or 'cd6vsq' in ' '.join(tag.get('class', [])).lower()))
        
        # Also look for images inside color containers
        color_container = soup.find(string=re.compile(r'Selected Color|Color', re.I))
        if color_container:
            parent = color_container.find_parent("div")
            if parent:
                parent_swatches = parent.find_all("img")
                for img in parent_swatches:
                    alt_name = img.get("alt")
                    src = img.get("src")
                    link = img.find_parent("a")
                    if alt_name and src:
                        v_url = f"https://www.flipkart.com{link['href']}" if link and link.get("href") else None
                        product.colors.append(ColorVariant(
                            color_name=alt_name.strip(),
                            variant_url=v_url,
                            image_urls=[self._upgrade_image_url(src)]
                        ))

        # Fallback to general swatches
        if not product.colors:
            for item in swatch_items:
                img = item.find("img")
                link = item.find("a")
                color_title = item.get("title") or (img.get("alt") if img else None)
                if color_title and img and img.get("src"):
                    v_url = f"https://www.flipkart.com{link['href']}" if link and link.get("href") else None
                    product.colors.append(ColorVariant(
                        color_name=color_title.strip(),
                        variant_url=v_url,
                        image_urls=[self._upgrade_image_url(img["src"])]
                    ))

    def _upgrade_image_url(self, url: str) -> str:
        """Upgrades Flipkart thumbnail URLs to 1664x1664 high-resolution."""
        if not url:
            return ""
        return re.sub(r'/image/\d+/\d+/', r'/image/1664/1664/', url.strip())
