import json
import re
import logging
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse
from app.scrapers.base import BaseScraper
from app.models.product import UniversalProduct, ColorVariant, ProductPricing, ProductRating

logger = logging.getLogger("scraper.meesho")

class MeeshoScraper(BaseScraper):
    platform_name = "meesho"
    custom_user_agent = "Meesho/14.8.1 (com.meesho.supply; build:14810; Android 14)"

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        netloc = urlparse(url.strip().lower()).netloc
        return "meesho.com" in netloc

    def is_search_url(self, url: str) -> bool:
        if not url:
            return False
        u = url.lower()
        return "/search" in u or "/category/" in u or "/pl/" in u or "q=" in u or "searchtype=" in u

    async def fetch_page_html(self, url: str, wait_selector: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Direct fast fetch with Meesho App User-Agent and geolocation headers."""
        headers = {
            "User-Agent": self.custom_user_agent,
            "X-Forwarded-For": "103.211.205.10",
            "X-Real-IP": "103.211.205.10",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8"
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, http2=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 4000:
                    title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.I)
                    title_text = title_match.group(1).lower() if title_match else ""
                    if "access denied" not in title_text and "edgesuite.net" not in resp.text.lower():
                        return resp.text, None
        except Exception as e:
            logger.debug(f"[Meesho] Fast fetch notice: {e}")

        html, err = await super().fetch_page_html(url, wait_selector=wait_selector)
        if html and ("access denied" in html.lower() or "edgesuite.net" in html.lower()):
            return "", "Akamai Access Denied on cloud host IP"
        return html, err

    async def extract_search_catalog(self, url: str, max_items: int = 40) -> List[Dict[str, Any]]:
        """Extract full product cards (URL, title, price, images) from a Meesho listing or search page."""
        logger.info(f"[Meesho] Unpacking search catalog: {url}")
        html, error = await self.fetch_page_html(url)
        if error or not html:
            logger.warning(f"[Meesho] Could not fetch listing page: {error}")
            return []

        products = []
        seen = set()

        # 1. Primary: Parse Next.js Hydration data (__NEXT_DATA__)
        try:
            soup = BeautifulSoup(html, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                data = json.loads(next_script.string)
                init = data.get("props", {}).get("pageProps", {}).get("initialState", {})
                listing_obj = (
                    init.get("plpListing", {}).get("listing", {}) or
                    init.get("searchListing", {}).get("listing", {}) or
                    init.get("clpListing", {}).get("listing", {}) or
                    init.get("collectionListing", {}).get("listing", {})
                )
                prods_pages = listing_obj.get("products", [])
                for page_data in prods_pages:
                    for p in page_data.get("products", []):
                        slug = p.get("slug") or p.get("original_slug")
                        pid = p.get("product_id") or p.get("id")
                        if slug and pid:
                            clean_slug = slug.strip("/")
                            if "/p/" in clean_slug:
                                p_url = f"https://www.meesho.com/{clean_slug}"
                            else:
                                p_url = f"https://www.meesho.com/{clean_slug}/p/{pid}"
                        elif pid:
                            p_url = f"https://www.meesho.com/s/p-{pid}"
                        else:
                            continue

                        norm_url = self.normalize_url(p_url)
                        if norm_url in seen:
                            continue
                        seen.add(norm_url)

                        raw_imgs = p.get("images", []) or ([p.get("image")] if p.get("image") else [])
                        hi_imgs = [self._upgrade_image_url(u) for u in raw_imgs if u][:5]

                        products.append({
                            "product_url": norm_url,
                            "title": p.get("hero_product_name") or p.get("name") or "Meesho Product",
                            "brand": None,
                            "deal_price": float(p["min_product_price"]) if p.get("min_product_price") else (float(p["min_catalog_price"]) if p.get("min_catalog_price") else (float(p["price"]) if p.get("price") else None)),
                            "mrp": float(p["original_price"]) if p.get("original_price") else (float(p["mrp"]) if p.get("mrp") else None),
                            "primary_images": hi_imgs
                        })
                        if len(products) >= max_items:
                            break
                    if len(products) >= max_items:
                        break
        except Exception as e:
            logger.warning(f"[Meesho] Error parsing __NEXT_DATA__ in search catalog: {e}")

        # 2. Fallback: Parse HTML DOM Anchor Tags
        if not products:
            soup = BeautifulSoup(html, "html.parser")
            anchors = soup.find_all("a", href=re.compile(r'/p/[a-zA-Z0-9]+|/s/p-[a-zA-Z0-9]+'))
            for a in anchors:
                href = a.get("href")
                if not href:
                    continue
                full_url = f"https://www.meesho.com{href}" if href.startswith("/") else href
                norm_url = self.normalize_url(full_url)
                if norm_url in seen or "/search" in norm_url:
                    continue
                seen.add(norm_url)

                title_text = a.get_text(separator=' ', strip=True)
                clean_title = re.sub(r'^\+\s*\d+\s*More\s*(?:\d+h\s*:\s*\d+m\s*:\s*\d+s)?', '', title_text).strip()
                clean_title = clean_title.split("₹")[0].strip() or "Meesho Product"

                prices = [re.sub(r'[^0-9.]', '', p) for p in re.findall(r'₹\s*([0-9,]+)', title_text)]
                valid_prices = [float(p) for p in prices if p and p.replace('.', '', 1).isdigit()]
                deal_price = valid_prices[0] if valid_prices else None
                mrp = valid_prices[1] if len(valid_prices) > 1 else None

                img = a.find("img")
                img_src = img.get("src") if img else None
                high_res = self._upgrade_image_url(img_src) if img_src else ""

                products.append({
                    "product_url": norm_url,
                    "title": clean_title,
                    "brand": None,
                    "deal_price": deal_price,
                    "mrp": mrp,
                    "primary_images": [high_res] if high_res else []
                })

                if len(products) >= max_items:
                    break

        logger.info(f"[Meesho] Discovered {len(products)} products from listing.")
        return products

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        items = await self.extract_search_catalog(url, max_items=max_items)
        return [i["product_url"] for i in items]

    def normalize_url(self, url: str) -> str:
        """Extract canonical product URL (e.g. https://www.meesho.com/name/p/123456)."""
        clean = self.clean_generic_url(url)
        match = re.search(r'(https?://(?:www\.)?meesho\.com/[^/?#]+/p/[a-zA-Z0-9]+)', clean)
        if match:
            return match.group(1)
        # Also handle /s/p-123456
        s_match = re.search(r'(https?://(?:www\.)?meesho\.com/s/p-[a-zA-Z0-9]+)', clean)
        if s_match:
            return s_match.group(1)
        return clean

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)
        html, error = await self.fetch_page_html(normalized_url)
        if error or not html:
            raise RuntimeError(f"Failed to load Meesho product: {error}")

        soup = BeautifulSoup(html, "html.parser")
        product = UniversalProduct(
            platform=self.platform_name,
            product_url=url,
            normalized_url=normalized_url,
            title="Meesho Product"
        )

        # 1. Next.js Hydration Data
        next_script = soup.find("script", id="__NEXT_DATA__")
        raw_json = next_script.get_text(strip=True) if next_script else None
        if raw_json:
            try:
                data = json.loads(raw_json)
                self._parse_next_data(data, product)
            except Exception as e:
                logger.warning(f"[Meesho] Next.js parsing warning: {e}")

        # 2. Schema.org JSON-LD fallback
        if not product.title or product.title == "Meesho Product":
            self._parse_json_ld(soup, product)

        # 3. DOM Fallbacks
        if not product.title or product.title == "Meesho Product":
            title_tag = soup.find("h1") or soup.find("title")
            if title_tag:
                t = title_tag.get_text(strip=True)
                if t.lower() not in ["access denied", "403 forbidden", "error"]:
                    product.title = t

        if product.title.lower() in ["access denied", "403 forbidden", "error"]:
            raise RuntimeError(f"Invalid product title received from CDN: {product.title}")

        return product

    def _parse_next_data(self, data: Dict[str, Any], product: UniversalProduct):
        props = data.get("props", {}).get("pageProps", {})
        initial_state = props.get("initialState", {})

        p_data = (
            props.get("productData") or 
            props.get("product") or 
            initial_state.get("product", {}).get("details", {}).get("data") or
            initial_state.get("product", {}).get("details", {})
        )

        if not isinstance(p_data, dict):
            return

        product.external_id = str(p_data.get("id") or p_data.get("product_id") or "")
        product.title = p_data.get("name") or p_data.get("title") or product.title
        product.brand = p_data.get("brand") or None
        product.category = p_data.get("category_name") or None
        product.description = p_data.get("description") or None

        # Pricing
        deal_price = p_data.get("price") or p_data.get("discounted_price")
        mrp = p_data.get("mrp") or p_data.get("original_price")
        discount = p_data.get("discount")
        try:
            p_price = float(deal_price) if deal_price else None
            p_mrp = float(mrp) if mrp else None
            p_disc = int(re.sub(r'[^0-9]', '', str(discount))) if discount else None
        except Exception:
            p_price, p_mrp, p_disc = None, None, None

        product.pricing = ProductPricing(
            deal_price=p_price,
            mrp=p_mrp,
            discount_percent=p_disc
        )

        # Ratings
        rating_val = p_data.get("rating") or p_data.get("average_rating")
        review_cnt = p_data.get("rating_count") or p_data.get("review_count")
        if rating_val:
            product.rating = ProductRating(
                score=float(rating_val),
                review_count=int(review_cnt) if review_cnt else None
            )

        # Primary Images (up to 5 images)
        raw_images = p_data.get("images") or p_data.get("media") or []
        for img in raw_images[:5]:
            url_str = img if isinstance(img, str) else img.get("url")
            if url_str:
                high_res = self._upgrade_image_url(url_str)
                if high_res not in product.primary_images:
                    product.primary_images.append(high_res)

        # Color Variants (Meesho catalog / sibling items)
        catalogs = (
            p_data.get("catalogs") or 
            p_data.get("variations") or 
            initial_state.get("product", {}).get("related_catalogs", [])
        )
        if isinstance(catalogs, list):
            for cat in catalogs:
                if isinstance(cat, dict):
                    color_name = cat.get("color") or cat.get("name") or "Default Color"
                    cat_imgs = cat.get("images") or []
                    color_img_urls = [self._upgrade_image_url(i if isinstance(i, str) else i.get("url")) for i in cat_imgs if i]
                    variant_url = f"https://www.meesho.com/s/p-{cat.get('id')}" if cat.get("id") else None

                    product.colors.append(ColorVariant(
                        color_name=color_name,
                        variant_url=variant_url,
                        image_urls=color_img_urls[:5]
                    ))

    def _parse_json_ld(self, soup: BeautifulSoup, product: UniversalProduct):
        scripts = soup.find_all("script", type="application/ld+json")
        for s in scripts:
            try:
                raw_json = s.get_text(strip=True)
                if not raw_json:
                    continue
                js = json.loads(raw_json)
                if isinstance(js, dict) and js.get("@type") == "Product":
                    product.title = js.get("name") or product.title
                    product.description = js.get("description") or product.description
                    imgs = js.get("image", [])
                    if isinstance(imgs, str):
                        imgs = [imgs]
                    for img in imgs:
                        high_res = self._upgrade_image_url(img)
                        if high_res not in product.primary_images:
                            product.primary_images.append(high_res)
            except Exception:
                pass

    def _upgrade_image_url(self, url: str) -> str:
        """Resolves Meesho thumbnail URLs to max resolution."""
        if not url:
            return ""
        return re.sub(r'/(\d{3,4})\.(jpg|jpeg|png|webp)', r'/1024.\2', url.strip())
