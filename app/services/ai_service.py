import logging
from typing import Optional, Tuple, List
from app.config import settings
from app.models.product import UniversalProduct

logger = logging.getLogger("services.ai")

try:
    from groq import AsyncGroq
    GROQ_INSTALLED = True
except ImportError:
    GROQ_INSTALLED = False

CANDIDATE_MODELS = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant"
]

class GroqAIService:
    """
    AI Service powered by Groq for high-converting Telegram post copywriting,
    product highlight extraction, and hashtag generation.
    """

    def __init__(self):
        self._client = None
        self._active_model: Optional[str] = None

    def _get_client(self) -> Optional[any]:
        if not GROQ_INSTALLED:
            return None
        if settings.GROQ_API_KEY and not self._client:
            self._client = AsyncGroq(api_key=settings.GROQ_API_KEY.strip())
        return self._client

    async def test_connection(self) -> Tuple[bool, str]:
        """Test Groq API key and auto-detect available working model."""
        if not settings.GROQ_API_KEY:
            return False, "Groq API Key not configured in settings."

        client = self._get_client()
        if not client:
            return False, "Groq Python SDK not available."

        models_to_try = [settings.GROQ_MODEL] + [m for m in CANDIDATE_MODELS if m != settings.GROQ_MODEL]

        for model in models_to_try:
            try:
                res = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Say OK"}],
                    max_tokens=5
                )
                self._active_model = model
                return True, f"Groq AI connected successfully with model: {model}"
            except Exception as e:
                logger.debug(f"Model {model} unavailable: {e}")

        return False, "Could not authenticate with any candidate Groq models."

    async def enhance_caption(self, product: UniversalProduct, base_caption: str) -> str:
        """
        Uses Groq AI to transform raw product data into a catchy, high-converting
        Telegram shopping post formatted with HTML tags.
        """
        if not settings.AI_ENHANCE_POSTS or not settings.GROQ_API_KEY:
            return base_caption

        client = self._get_client()
        if not client:
            return base_caption

        # Detect active model if not yet detected
        models_to_try = [self._active_model] if self._active_model else ([settings.GROQ_MODEL] + [m for m in CANDIDATE_MODELS if m != settings.GROQ_MODEL])

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
            "1. Hook the reader with a catchy title with emojis.\n"
            "2. Emphasize the Deal Price and strikethrough the MRP.\n"
            "3. If available, clearly list the color options and available sizes.\n"
            "4. Provide 3 crisp, compelling bullet points highlighting why to buy.\n"
            "5. Add 3-4 trending shopping hashtags (e.g., #Deals #Shopping #Fashion).\n"
            "6. Include a clear Call To Action at the bottom with the exact Product Link.\n"
            "7. Keep the entire post UNDER 950 characters.\n"
            "8. Output ONLY the final caption text."
        )

        for model in [m for m in models_to_try if m]:
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    max_tokens=600
                )
                enhanced = response.choices[0].message.content.strip()
                if enhanced.startswith("```"):
                    lines = enhanced.split("\n")
                    if len(lines) >= 3 and lines[-1].startswith("```"):
                        enhanced = "\n".join(lines[1:-1]).strip()

                if len(enhanced) > 1010:
                    enhanced = enhanced[:1005] + "..."

                self._active_model = model
                logger.info(f"Successfully generated AI-enhanced caption using Groq ({model})!")
                return enhanced
            except Exception as e:
                logger.warning(f"Groq generation failed with {model}: {e}")

        return base_caption

ai_service = GroqAIService()
