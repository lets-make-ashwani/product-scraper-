import json
import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from app.scrapers.base import BaseScraper
from app.models.product import UniversalProduct, ColorVariant, ProductPricing, ProductRating

logger = logging.getLogger("scraper.meesho")

class MeeshoScraper(BaseScraper):
    platform_name = "meesho"

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        netloc = urlparse(url.strip().lower()).netloc
        return "meesho.com" in netloc

    def is_search_url(self, url: str) -> bool:
        if not url:
            return False
        u = url.lower()
        return "/search" in u or "/category/" in u or "q=" in u or "searchtype=" in u

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        logger.info(f"[Meesho] Unpacking search listing: {url}")
        html, error = await self.fetch_page_html(url)
        if error or not html:
            logger.warning(f"[Meesho] Could not fetch search page: {error}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        product_links = []
        seen = set()

        # 1. Check Next.js state for catalogs
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                data = json.loads(next_script.string)
                props = data.get("props", {}).get("pageProps", {})
                catalogs = props.get("initialState", {}).get("search", {}).get("catalogs", [])
                if not catalogs:
                    catalogs = props.get("catalogs", [])

                for cat in catalogs:
                    cid = cat.get("id") or cat.get("catalog_id")
                    if cid:
                        p_url = f"https://www.meesho.com/s/p-{cid}"
                        if p_url not in seen:
                            seen.add(p_url)
                            product_links.append(p_url)
                            if len(product_links) >= max_items:
                                break
            except Exception as e:
                logger.warning(f"[Meesho] Error parsing search Next.js data: {e}")

        # 2. Check DOM anchor links
        if len(product_links) < max_items:
            anchors = soup.find_all("a", href=re.compile(r'/p/[a-zA-Z0-9]+|/s/p-[a-zA-Z0-9]+'))
            for a in anchors:
                href = a.get("href")
                if not href:
                    continue
                full_url = f"https://www.meesho.com{href}" if href.startswith("/") else href
                norm = self.normalize_url(full_url)
                if norm not in seen and "/search" not in norm:
                    seen.add(norm)
                    product_links.append(norm)
                    if len(product_links) >= max_items:
                        break

        logger.info(f"[Meesho] Discovered {len(product_links)} products from search page.")
        return product_links

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
        if next_script and next_script.string:
            try:
                data = json.loads(next_script.string)
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
                product.title = title_tag.get_text(strip=True)

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

        # Primary Images
        raw_images = p_data.get("images") or p_data.get("media") or []
        for img in raw_images:
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
                js = json.loads(s.string)
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
