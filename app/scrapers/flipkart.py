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

    async def extract_search_catalog(self, url: str, max_items: int = 40) -> List[Dict[str, Any]]:
        """
        Loads Flipkart search page and unpacks full product cards:
        URLs, Titles, Deal Prices, MRP, and 1664x1664 High-Res Images.
        """
        logger.info(f"[Flipkart] Unpacking search catalog with images & prices: {url}")
        html, error = await self.fetch_page_html(url, wait_selector="a[href*='/p/']")
        if error or not html:
            logger.warning(f"[Flipkart] Could not fetch search page: {error}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        products = []
        seen = set()

        cards = soup.find_all("div", attrs={"data-id": True})
        if not cards:
            cards = [a.find_parent("div") for a in soup.find_all("a", href=re.compile(r'/p/itm|pid='))]

        for c in cards:
            if not c:
                continue

            link_el = c.find("a", href=re.compile(r'/p/itm|pid='))
            if not link_el or not link_el.get("href"):
                continue

            href = link_el["href"]
            full_url = f"https://www.flipkart.com{href}" if href.startswith("/") else href
            norm_url = self.normalize_url(full_url)
            if norm_url in seen or "/search" in norm_url:
                continue
            seen.add(norm_url)

            # Title & Brand
            title_el = c.find("a", title=True) or c.find("a", class_=re.compile(r"WKTcLC|IRpwTa|s1Q9rs"))
            title = title_el.get("title") if title_el and title_el.get("title") else (title_el.get_text(strip=True) if title_el else "")
            brand_el = c.find("div", class_=re.compile(r"syl9yP|_2WkVRV"))
            brand = brand_el.get_text(strip=True) if brand_el else ""
            if brand and brand.lower() not in title.lower():
                title = f"{brand} {title}".strip()
            if not title:
                title = "Flipkart Product"

            # Prices
            prices = [re.sub(r'[^0-9.]', '', s) for s in c.find_all(string=re.compile(r'₹'))]
            prices = [float(p) for p in prices if p and p.replace('.', '', 1).isdigit()]
            deal_price = prices[0] if prices else None
            mrp = prices[1] if len(prices) > 1 else None

            # High-res Image
            img = c.find("img")
            src = img.get("src") or img.get("data-src") if img else ""
            high_res = self._upgrade_image_url(src)
            primary_images = [high_res] if high_res else []

            products.append({
                "product_url": norm_url,
                "title": title,
                "brand": brand or None,
                "deal_price": deal_price,
                "mrp": mrp,
                "primary_images": primary_images
            })

            if len(products) >= max_items:
                break

        logger.info(f"[Flipkart] Discovered {len(products)} full product cards from search catalog.")
        return products

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        items = await self.extract_search_catalog(url, max_items=max_items)
        return [item["product_url"] for item in items]

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
                raw_json = s.get_text(strip=True)
                if not raw_json:
                    continue
                js = json.loads(raw_json)
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
            except Exception as e:
                logger.debug(f"[Flipkart] JSON-LD parse notice: {e}")

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

        # Primary images from gallery thumbnails / product photos
        for img in soup.find_all("img"):
            for attr in ["src", "data-src", "srcset"]:
                val = img.get(attr)
                if val and ("/image/" in val) and ("rukmini" in val or "flixcart" in val):
                    # In case of srcset, take the first link
                    first_link = val.split(",")[0].strip().split(" ")[0]
                    high_res = self._upgrade_image_url(first_link)
                    if high_res and high_res not in product.primary_images:
                        product.primary_images.append(high_res)

        # Failsafe: If still no images found, scan full page HTML for rukmini image URLs
        if not product.primary_images:
            pattern = re.compile(r'https?://[^\s"\'<>]*?(?:rukmini\d*|rukminim\d*)\.flixcart\.com/image/[^\s"\'<>]*?\.(?:jpeg|jpg|png|webp)', re.I)
            for m in pattern.findall(str(soup)):
                high_res = self._upgrade_image_url(m)
                if high_res and high_res not in product.primary_images:
                    product.primary_images.append(high_res)
                if len(product.primary_images) >= 10:
                    break

    def _parse_color_variants(self, soup: BeautifulSoup, product: UniversalProduct):
        # Look for images inside color containers
        color_container = soup.find(string=re.compile(r'Selected Color|Color', re.I))
        if color_container:
            parent = color_container.find_parent("div")
            if parent:
                parent_swatches = parent.find_all("img")
                for img in parent_swatches:
                    alt_name = img.get("alt")
                    src = img.get("src") or img.get("data-src")
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
            def is_swatch_tag(tag):
                if not tag.name or tag.name not in ['li', 'div']:
                    return False
                t_id = tag.get('id') or ''
                if 'swatch' in str(t_id).lower():
                    return True
                classes = ' '.join(tag.get('class') or []).lower()
                return 'swatch' in classes or 'cd6vsq' in classes

            swatch_items = soup.find_all(is_swatch_tag)
            for item in swatch_items:
                img = item.find("img")
                link = item.find("a")
                color_title = item.get("title") or (img.get("alt") if img else None)
                src = img.get("src") or img.get("data-src") if img else None
                if color_title and src:
                    v_url = f"https://www.flipkart.com{link['href']}" if link and link.get("href") else None
                    product.colors.append(ColorVariant(
                        color_name=color_title.strip(),
                        variant_url=v_url,
                        image_urls=[self._upgrade_image_url(src)]
                    ))

    def _upgrade_image_url(self, url: str) -> str:
        """Upgrades Flipkart thumbnail URLs to 1664x1664 high-resolution."""
        if not url:
            return ""
        clean = url.strip().replace("&amp;", "&")
        if clean.startswith("//"):
            clean = "https:" + clean
        return re.sub(r'/image/\d+/\d+/', r'/image/1664/1664/', clean)
