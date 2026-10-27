import json
import re
import logging
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Tuple
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

    async def fetch_page_html(self, url: str, wait_selector: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Fast HTTP/1.1 fetch to avoid Akamai HTTP/2 protocol resets."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, http2=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    return resp.text, None
        except Exception as e:
            logger.debug(f"[Myntra] Fast HTTP fetch notice: {e}")

        return await super().fetch_page_html(url, wait_selector=wait_selector)

    async def extract_search_catalog(self, url: str, max_items: int = 40) -> List[Dict[str, Any]]:
        """Extract full product catalog with titles, prices, and 5 images from Myntra category/search listing."""
        logger.info(f"[Myntra] Unpacking search listing: {url}")
        html, error = await self.fetch_page_html(url)
        if error or not html:
            logger.warning(f"[Myntra] Could not fetch search page: {error}")
            return []

        products = []
        seen = set()

        # 1. Primary: Extract rich product cards from window.__myx script
        idx = html.find("window.__myx")
        if idx != -1:
            try:
                start_pos = html.find("{", idx)
                d, _ = json.JSONDecoder().raw_decode(html, start_pos)
                raw_prods = d.get("searchData", {}).get("results", {}).get("products", [])
                for p in raw_prods:
                    landing = p.get("landingPageUrl")
                    if not landing:
                        continue
                    full_url = f"https://www.myntra.com/{landing.lstrip('/')}" if not landing.startswith("http") else landing
                    norm_url = self.normalize_url(full_url)
                    if norm_url in seen:
                        continue
                    seen.add(norm_url)

                    # Extract up to 5 high-res images
                    raw_imgs = []
                    for img in p.get("images", []):
                        if isinstance(img, dict) and img.get("src"):
                            raw_imgs.append(img.get("src"))
                    if not raw_imgs and p.get("searchImage"):
                        raw_imgs.append(p.get("searchImage"))
                    hi_res_imgs = [self._upgrade_image_url(u) for u in raw_imgs if u][:5]

                    products.append({
                        "product_url": norm_url,
                        "title": p.get("productName") or p.get("product") or "Myntra Product",
                        "brand": p.get("brand"),
                        "deal_price": float(p["price"]) if p.get("price") is not None else None,
                        "mrp": float(p["mrp"]) if p.get("mrp") is not None else None,
                        "primary_images": hi_res_imgs
                    })
                    if len(products) >= max_items:
                        break
            except Exception as e:
                logger.warning(f"[Myntra] Error decoding __myx in search listing: {e}")

        # 2. Fallback: Regex scan for both relative and absolute /buy product URLs
        if not products:
            matches = re.findall(r'(?:https?://(?:www\.)?myntra\.com)?(/[a-zA-Z0-9_/-]+/\d+/buy)', html, re.I)
            for m in matches:
                full_url = f"https://www.myntra.com{m}" if m.startswith("/") else m
                norm_url = self.normalize_url(full_url)
                if norm_url in seen:
                    continue
                seen.add(norm_url)

                slug_match = re.search(r'myntra\.com/(?:[^/]+/)*([^/?#]+)/\d+/buy', norm_url)
                title = slug_match.group(1).replace('-', ' ').title() if slug_match else "Myntra Product"

                products.append({
                    "product_url": norm_url,
                    "title": title,
                    "brand": None,
                    "deal_price": None,
                    "mrp": None,
                    "primary_images": []
                })
                if len(products) >= max_items:
                    break

        logger.info(f"[Myntra] Discovered {len(products)} products from search listing.")
        return products

    async def extract_search_product_urls(self, url: str, max_items: int = 40) -> List[str]:
        items = await self.extract_search_catalog(url, max_items=max_items)
        return [i["product_url"] for i in items]

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

        # 1. Parse window.__myx script from HTML
        pdp_data = self._extract_myx_data(html)
        if pdp_data:
            self._parse_pdp_data(pdp_data, product)

        # 2. Schema.org fallback
        if not product.title or product.title == "Myntra Product":
            self._parse_json_ld(soup, product)

        return product

    def _extract_myx_data(self, html_text: str) -> Optional[Dict[str, Any]]:
        idx = html_text.find("window.__myx")
        if idx != -1:
            try:
                start_pos = html_text.find("{", idx)
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(html_text, start_pos)
                return data.get("pdpData", {})
            except Exception as e:
                logger.warning(f"[Myntra] Failed to raw_decode __myx: {e}")
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

        # Primary Images from Media Albums (up to 5 images)
        albums = pdp.get("media", {}).get("albums", [])
        for album in albums:
            images = album.get("images", [])
            for img in images:
                src = img.get("src") or img.get("imageURL")
                if src:
                    high_res = self._upgrade_image_url(src)
                    if high_res not in product.primary_images:
                        product.primary_images.append(high_res)
                if len(product.primary_images) >= 5:
                    break
            if len(product.primary_images) >= 5:
                break

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
        """Upgrades Myntra images to high-res 1440x1080."""
        if not url:
            return ""
        clean = url.strip()
        if clean.startswith("//"):
            clean = "https:" + clean
        elif clean.startswith("http://"):
            clean = "https://" + clean[7:]
        clean = clean.replace("($height)", "1440").replace("($width)", "1080").replace("($qualityPercentage)", "95")
        clean = re.sub(r'/f_webp[^/]+/', '/h_1440,q_95,w_1080/', clean)
        return clean
