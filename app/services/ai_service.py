import logging
from typing import Optional, Tuple
from app.config import settings
from app.models.product import UniversalProduct

logger = logging.getLogger("services.ai")

try:
    from groq import AsyncGroq
    GROQ_INSTALLED = True
except ImportError:
    GROQ_INSTALLED = False

class GroqAIService:
    """
    AI Service powered by Groq (Llama 3 / Mixtral) for high-converting
    Telegram post copywriting, product highlight extraction, and hashtag generation.
    """

    def __init__(self):
        self._client = None

    def _get_client(self) -> Optional[any]:
        if not GROQ_INSTALLED:
            return None
        if settings.GROQ_API_KEY and not self._client:
            self._client = AsyncGroq(api_key=settings.GROQ_API_KEY.strip())
        return self._client

    async def test_connection(self) -> Tuple[bool, str]:
        """Test Groq API key and model availability."""
        if not settings.GROQ_API_KEY:
            return False, "Groq API Key not configured in settings."

        client = self._get_client()
        if not client:
            return False, "Groq Python SDK not available."

        try:
            res = await client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=[{"role": "user", "content": "Respond with 'OK'"}],
                max_tokens=5
            )
            reply = res.choices[0].message.content.strip()
            return True, f"Groq AI ({settings.GROQ_MODEL}) connected successfully! Response: {reply}"
        except Exception as e:
            return False, f"Groq API connection error: {str(e)}"

    async def enhance_caption(self, product: UniversalProduct, base_caption: str) -> str:
        """
        Uses Groq AI to transform raw product data into a catchy, high-converting
        Telegram shopping post formatted with HTML tags.
        Falls back to base_caption if Groq is not configured or fails.
        """
        if not settings.AI_ENHANCE_POSTS or not settings.GROQ_API_KEY:
            return base_caption

        client = self._get_client()
        if not client:
            return base_caption

        try:
            # Construct context for Groq
            color_names = [c.color_name for c in product.colors if c.color_name]
            p = product.pricing

            prompt = (
                f"Product Title: {product.title}\n"
                f"Platform: {product.platform.title()}\n"
                f"Brand: {product.brand or 'N/A'}\n"
                f"Deal Price: ₹{p.deal_price if p.deal_price else 'N/A'}\n"
                f"MRP: ₹{p.mrp if p.mrp else 'N/A'}\n"
                f"Discount: {p.discount_percent}%\n"
                f"Rating: {product.rating.score if product.rating else 'N/A'}\n"
                f"Available Colors: {', '.join(color_names) if color_names else 'Single Color'}\n"
                f"Sizes: {', '.join(product.sizes) if product.sizes else 'Free Size'}\n"
                f"Raw Description: {product.description or ''}\n"
                f"Product Link: {product.product_url}\n"
            )

            system_instruction = (
                "You are an expert e-commerce copywriter creating high-converting Telegram channel posts. "
                "Craft an engaging, visually appealing Telegram post based on the product details provided.\n\n"
                "Rules:\n"
                "1. Use HTML tags for formatting: <b>bold</b>, <i>italic</i>, <s>strikethrough</s>.\n"
                "2. Hook the reader with a catchy title with emojis.\n"
                "3. Emphasize the Deal Price and strikethrough the MRP (e.g. <b>₹499</b> <s>₹1,499</s> <b>(67% OFF)</b>).\n"
                "4. If available, clearly list the color options and available sizes.\n"
                "5. Provide 3 crisp, compelling bullet points highlighting why to buy.\n"
                "6. Add 3-4 trending shopping hashtags (e.g., #Deals #Shopping #Fashion).\n"
                "7. Include a clear Call To Action at the bottom with the exact Product Link.\n"
                "8. Keep the entire post UNDER 950 characters (Telegram album caption hard limit is 1024).\n"
                "9. Output ONLY the final caption text. Do not include markdown code fences or conversational filler."
            )

            response = await client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=600
            )

            enhanced = response.choices[0].message.content.strip()
            # Remove any markdown code block wrappers if Groq added them
            if enhanced.startswith("```"):
                lines = enhanced.split("\n")
                if len(lines) >= 3 and lines[-1].startswith("```"):
                    enhanced = "\n".join(lines[1:-1]).strip()

            if len(enhanced) > 1010:
                enhanced = enhanced[:1005] + "..."

            logger.info("Successfully generated AI-enhanced Telegram caption with Groq!")
            return enhanced

        except Exception as e:
            logger.warning(f"Groq AI enhancement failed, falling back to base template: {e}")
            return base_caption

ai_service = GroqAIService()
