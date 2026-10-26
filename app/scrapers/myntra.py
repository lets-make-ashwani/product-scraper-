import json
import re
import logging
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from app.scrapers.base import BaseScraper
from app.models.product import UniversalProduct, ColorVariant, ProductPricing, ProductRating

logger = logging.getLogger("scraper.myntra")

class MyntraScraper(BaseScraper):
    platform_name = "myntra"

    def can_handle(self, url: str) -> bool:
        if not url:
            return False
        netloc = urlparse(url.strip().lower()).netloc
        return "myntra.com" in netloc

    def is_search_url(self, url: str) -> bool:
        """Detect Myntra category or search listing pages."""
        if not url:
            return False
        clean = self.clean_generic_url(url)
        is_pdp = bool(re.search(r'/\d+/buy', clean))
        return not is_pdp or "/search" in url.lower() or "q=" in url.lower()

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        logger.info(f"[Myntra] Unpacking search listing: {url}")
        html, error = await self.fetch_page_html(url, wait_selector="li.product-base, a[href*='/buy']")
        if error or not html:
            logger.warning(f"[Myntra] Could not fetch search page: {error}")
            return []

        soup = BeautifulSoup(html, "html.parser")
        product_links = []
        seen = set()

        anchors = soup.find_all("a", href=re.compile(r'/\d+/buy'))
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            full_url = f"https://www.myntra.com/{href.lstrip('/')}" if not href.startswith("http") else href
            norm = self.normalize_url(full_url)
            if norm and norm not in seen:
                seen.add(norm)
                product_links.append(norm)
                if len(product_links) >= max_items:
                    break

        logger.info(f"[Myntra] Discovered {len(product_links)} products from search page.")
        return product_links

    def normalize_url(self, url: str) -> str:
        clean = self.clean_generic_url(url)
        match = re.search(r'(https?://(?:www\.)?myntra\.com/[^?#]+/\d+/buy)', clean)
        if match:
            return match.group(1)
        return clean

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)
        html, error = await self.fetch_page_html(normalized_url)
        if error or not html:
            raise RuntimeError(f"Failed to load Myntra product: {error}")

        soup = BeautifulSoup(html, "html.parser")
        product = UniversalProduct(
            platform=self.platform_name,
            product_url=url,
            normalized_url=normalized_url,
            title="Myntra Product"
        )

        # 1. Parse window.__myx script
        pdp_data = self._extract_myx_data(soup)
        if pdp_data:
            self._parse_pdp_data(pdp_data, product)

        # 2. Schema.org fallback
        if not product.title or product.title == "Myntra Product":
            self._parse_json_ld(soup, product)

        return product

    def _extract_myx_data(self, soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        scripts = soup.find_all("script")
        for s in scripts:
            if s.string and "window.__myx =" in s.string:
                try:
                    match = re.search(r'window\.__myx\s*=\s*({.*?});?</script>', s.string, re.DOTALL)
                    if not match:
                        match = re.search(r'window\.__myx\s*=\s*({.*?});', s.string, re.DOTALL)
                    if match:
                        data = json.loads(match.group(1))
                        return data.get("pdpData", {})
                except Exception as e:
                    logger.warning(f"[Myntra] Failed to parse __myx: {e}")
        return None

    def _parse_pdp_data(self, pdp: Dict[str, Any], product: UniversalProduct):
        product.external_id = str(pdp.get("id") or "")
        product.title = pdp.get("name") or pdp.get("title") or product.title
        product.brand = pdp.get("brand", {}).get("name") if isinstance(pdp.get("brand"), dict) else pdp.get("brand")

        # Pricing
        price_val = pdp.get("price", {}).get("discounted") or pdp.get("price")
        mrp_val = pdp.get("price", {}).get("mrp")
        disc_label = pdp.get("price", {}).get("discountDisplayLabel") or pdp.get("discountDisplayLabel")
        try:
            p_price = float(price_val) if price_val else None
            p_mrp = float(mrp_val) if mrp_val else None
            p_disc = int(re.sub(r'[^0-9]', '', disc_label)) if disc_label else None
        except Exception:
            p_price, p_mrp, p_disc = None, None, None

        product.pricing = ProductPricing(
            deal_price=p_price,
            mrp=p_mrp,
            discount_percent=p_disc,
            discount_label=disc_label
        )

        # Ratings
        ratings_info = pdp.get("ratings", {})
        if ratings_info.get("averageRating"):
            product.rating = ProductRating(
                score=float(ratings_info["averageRating"]),
                review_count=int(ratings_info.get("totalCount", 0))
            )

        # Sizes
        sizes_raw = pdp.get("sizes", [])
        product.sizes = [s.get("label") for s in sizes_raw if isinstance(s, dict) and s.get("label")]

        # Primary Images from Media Albums
        albums = pdp.get("media", {}).get("albums", [])
        for album in albums:
            images = album.get("images", [])
            for img in images:
                src = img.get("src") or img.get("imageURL")
                if src:
                    high_res = self._upgrade_image_url(src)
                    if high_res not in product.primary_images:
                        product.primary_images.append(high_res)

        # Colorways / Variants
        style_options = pdp.get("styleOptions", []) or pdp.get("colours", [])
        for opt in style_options:
            if isinstance(opt, dict):
                c_name = opt.get("colour") or opt.get("name") or "Variant"
                landing_url = opt.get("landingPageUrl")
                full_url = f"https://www.myntra.com/{landing_url}" if landing_url else None
                variant_imgs = []
                if opt.get("image"):
                    variant_imgs.append(self._upgrade_image_url(opt["image"]))

                product.colors.append(ColorVariant(
                    color_name=c_name,
                    variant_url=full_url,
                    image_urls=variant_imgs
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
        """Upgrades Myntra images to high-res WebP/JPG."""
        if not url:
            return ""
        clean = re.sub(r'/f_webp[^/]+/', '/h_1440,q_95,w_1080/', url.strip())
        return clean
