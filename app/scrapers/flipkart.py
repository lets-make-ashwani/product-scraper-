import json
import re
import logging
import asyncio
import httpx
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

    async def _extract_via_rome_api(self, url: str) -> Optional[UniversalProduct]:
        """
        Fast Rome API extractor: queries Flipkart's internal single-page-app API.
        Extracts up to 10-18 high-resolution 1664x1664 gallery images, prices, titles, and sizes.
        Bypasses standard HTML blocks and browser overhead.
        """
        try:
            parsed = urlparse(url.strip())
            qs = parse_qs(parsed.query)
            pid = qs.get("pid", [None])[0]

            page_uri = parsed.path
            if pid:
                page_uri += f"?pid={pid}"

            api_url = "https://2.rome.api.flipkart.com/api/4/page/fetch"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "X-User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 FKUA/website/42/website/Desktop"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": "https://www.flipkart.com/"
            }

            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.post(api_url, json={"pageUri": page_uri}, headers=headers)
                if res.status_code != 200:
                    logger.debug(f"[Flipkart] Rome API returned status {res.status_code}")
                    return None

                data = res.json()
                raw_text = json.dumps(data)

                # 1. Gallery Images (1664x1664 high-resolution)
                img_pattern = re.compile(
                    r'https?://[^\s"\'<>]+?\.flixcart\.com/image/[^\s"\'<>]+?\.(?:jpeg|jpg|png|webp)',
                    re.I
                )
                raw_imgs = img_pattern.findall(raw_text)
                gallery_images = []
                for img in raw_imgs:
                    if "cms-rpd-images" in img or "icon" in img.lower() or "placeholder" in img.lower():
                        continue
                    high = self._upgrade_image_url(img)
                    if high and high not in gallery_images:
                        gallery_images.append(high)

                if not gallery_images:
                    return None

                # 2. Title & Brand
                title = None
                brand = None
                slots = data.get("RESPONSE", {}).get("slots", [])
                for s in slots:
                    w = s.get("widget", {})
                    w_data = w.get("data", {})
                    dls = json.dumps(w_data)

                    # Title search
                    if not title:
                        m_title = re.search(r'"(?:superTitle|title)":\s*\{"text":\s*"([^"]+)"\}', dls)
                        if m_title and len(m_title.group(1)) > 3:
                            title = m_title.group(1)

                    # Brand search
                    if not brand:
                        m_brand = re.search(r'"brand":\s*\{"text":\s*"([^"]+)"\}', dls)
                        if m_brand:
                            brand = m_brand.group(1)

                if not title:
                    slug_match = re.search(r'flipkart\.com/([^/?#]+)/p/', url)
                    if slug_match:
                        title = unquote(slug_match.group(1).replace('-', ' ')).title()
                    else:
                        title = "Flipkart Product"

                if brand and brand.lower() not in title.lower():
                    title = f"{brand} {title}".strip()

                product = UniversalProduct(
                    platform=self.platform_name,
                    product_url=url,
                    normalized_url=self.normalize_url(url),
                    title=title,
                    brand=brand,
                    primary_images=gallery_images[:5]
                )

                # 3. Prices
                found_price = False
                for s in slots:
                    w = s.get("widget", {})
                    dls_data = w.get("data", {}).get("dlsData", {})
                    pd = dls_data.get("price_description_0", {}).get("value", {})
                    if pd:
                        l2 = str(pd.get("label_2", {}))
                        m2 = re.search(r'₹\s*([0-9,]+)', l2)
                        if m2:
                            product.pricing.deal_price = float(m2.group(1).replace(",", ""))
                            found_price = True

                        l1 = str(pd.get("label_1", {}))
                        m1 = re.search(r'([0-9,]+)', l1)
                        if m1:
                            product.pricing.mrp = float(m1.group(1).replace(",", ""))

                        l0 = str(pd.get("label_0", {}))
                        m0 = re.search(r'([0-9]+)\s*%', l0)
                        if m0:
                            product.pricing.discount_percent = int(m0.group(1))
                        break

                if not found_price:
                    prices = [re.sub(r'[^0-9.]', '', p) for p in re.findall(r'₹\s*([0-9,]+)', raw_text)]
                    valid_prices = [float(p) for p in prices if p and p.replace('.', '', 1).isdigit() and float(p) > 50]
                    if valid_prices:
                        product.pricing.deal_price = min(valid_prices)
                        higher_prices = [p for p in valid_prices if p > product.pricing.deal_price]
                        if higher_prices:
                            product.pricing.mrp = max(higher_prices)
                            product.pricing.discount_percent = int(
                                round((product.pricing.mrp - product.pricing.deal_price) / product.pricing.mrp * 100)
                            )

                # 4. Rating
                m_avg = re.search(r'"average":\s*([0-9.]+)', raw_text)
                m_cnt = re.search(r'"ratingCount":\s*([0-9]+)', raw_text)
                if m_avg:
                    try:
                        product.rating = ProductRating(
                            score=float(m_avg.group(1)),
                            review_count=int(m_cnt.group(1)) if m_cnt else None
                        )
                    except Exception:
                        pass

                # 5. Color variants
                v_matches = re.findall(r'"url":\s*"(/[^"]*?[?&]pid=[^"]+)"', raw_text)
                seen_variants = set()
                for v_path in v_matches:
                    v_url = f"https://www.flipkart.com{v_path}"
                    norm_v = self.normalize_url(v_url)
                    if norm_v != product.normalized_url and norm_v not in seen_variants:
                        seen_variants.add(norm_v)
                        c_match = re.search(r'flipkart\.com/([^/?#]+)/p/', norm_v)
                        c_name = unquote(c_match.group(1).replace('-', ' ')).title() if c_match else f"Color {len(product.colors)+1}"
                        product.colors.append(ColorVariant(
                            color_name=c_name,
                            variant_url=norm_v,
                            image_urls=[]
                        ))
                    if len(product.colors) >= 8:
                        break

                logger.info(f"[Flipkart] Rome API extracted {len(product.primary_images)} gallery images and ₹{product.pricing.deal_price} for '{product.title[:35]}...'")
                return product

        except Exception as e:
            logger.debug(f"[Flipkart] Rome API parsing error: {e}")
            return None

    async def extract_product(self, url: str) -> UniversalProduct:
        normalized_url = self.normalize_url(url)

        # 1. High-speed Rome API attempt (retrieves all 10-18 gallery images without HTML blocks)
        try:
            rome_prod = await self._extract_via_rome_api(normalized_url)
            if rome_prod and len(rome_prod.primary_images) >= 1:
                return rome_prod
        except Exception as e:
            logger.debug(f"[Flipkart] Rome API fast extraction bypassed: {e}")

        # 2. Fallback to full HTML fetch & DOM parsing
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
                if len(product.primary_images) >= 5:
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
        clean = clean.replace("{@width}", "1664").replace("{@height}", "1664").replace("{@quality}", "90")
        return re.sub(r'/image/[^/]+/[^/]+/', r'/image/1664/1664/', clean)
