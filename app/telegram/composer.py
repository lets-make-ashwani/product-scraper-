import logging
from typing import List, Tuple
from app.models.product import UniversalProduct
from app.config import settings

logger = logging.getLogger("telegram.composer")

class TelegramComposer:
    """Composes merged color variant posts and selects representative images under the 10-media limit."""

    @staticmethod
    def compose_caption(product: UniversalProduct) -> str:
        # Price and MRP
        p = product.pricing
        price_str = f"{p.deal_price:,.0f}" if p.deal_price else "Check link"
        mrp_info = f"<s>₹{p.mrp:,.0f}</s>" if p.mrp and p.mrp > (p.deal_price or 0) else ""
        discount_info = f"({p.discount_percent}% OFF)" if p.discount_percent else ""

        # Rating
        rating_info = ""
        if product.rating and product.rating.score:
            rev_text = f" ({product.rating.review_count:,} reviews)" if product.rating.review_count else ""
            rating_info = f"⭐ <b>Rating:</b> {product.rating.score}/5{rev_text}\n"

        # Color Variants
        colors_info = ""
        if product.colors:
            color_names = [f"• {c.color_name}" for c in product.colors if c.color_name]
            if color_names:
                colors_info = f"\n🎨 <b>Available in {len(color_names)} Colors:</b>\n" + "\n".join(color_names[:6]) + "\n"

        # Sizes
        sizes_info = ""
        if product.sizes:
            sizes_info = f"📏 <b>Sizes:</b> {', '.join(product.sizes[:6])}\n"

        # Description / Highlights
        desc = (product.description or "High quality product with premium finish.").strip()
        if len(desc) > 300:
            desc = desc[:297] + "..."

        caption = settings.TELEGRAM_CAPTION_TEMPLATE.format(
            title=product.title,
            price=price_str,
            mrp_info=mrp_info,
            discount_info=discount_info,
            rating_info=rating_info,
            colors_info=colors_info,
            sizes_info=sizes_info,
            description=desc,
            buy_url=product.product_url
        )

        # Telegram caption length maximum is 1024 characters for media groups
        if len(caption) > 1020:
            caption = caption[:1015] + "..."

        return caption

    @staticmethod
    def select_variant_images(product: UniversalProduct, max_images: int = 10) -> List[str]:
        """
        Gathers images from primary product and each color variant.
        Evenly balances pictures across color variants to produce a single merged album.
        """
        selected_images: List[str] = []

        # If there are color variants, pull representative images from each
        if product.colors:
            # 1 photo from primary product
            if product.primary_images:
                selected_images.append(product.primary_images[0])

            # Then round-robin from each color variant
            slots_remaining = max_images - len(selected_images)
            if slots_remaining > 0:
                num_colors = len(product.colors)
                imgs_per_color = max(1, slots_remaining // num_colors)

                for variant in product.colors:
                    for img in variant.image_urls[:imgs_per_color]:
                        if img not in selected_images:
                            selected_images.append(img)
                        if len(selected_images) >= max_images:
                            break
                    if len(selected_images) >= max_images:
                        break

        # Fallback to remaining primary images if slots remain
        for img in product.primary_images:
            if img not in selected_images:
                selected_images.append(img)
            if len(selected_images) >= max_images:
                break

        return selected_images[:max_images]

telegram_composer = TelegramComposer()
