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
    Guarantees the buy link is NEVER cut off.
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
        Uses Groq AI to generate a high-converting caption body,
        then programmatically appends the full buy link so it is NEVER truncated.
        """
        link_block = f"\n\n🛒 <b>Buy Now:</b>\n{product.product_url}"

        if not settings.AI_ENHANCE_POSTS or not settings.GROQ_API_KEY:
            return self._safely_combine_caption(base_caption, link_block)

        client = self._get_client()
        if not client:
            return self._safely_combine_caption(base_caption, link_block)

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
        )

        system_instruction = (
            "You are an expert e-commerce copywriter creating high-converting Telegram channel posts. "
            "Craft an engaging, punchy Telegram post based on the product details provided.\n\n"
            "Rules:\n"
            "1. Hook the reader with a catchy title with emojis.\n"
            "2. Highlight Deal Price (e.g. ₹299) and strikethrough the MRP if available.\n"
            "3. List available colors and sizes.\n"
            "4. Provide 2-3 crisp bullet points highlighting why to buy.\n"
            "5. Add 2-3 trending hashtags (e.g., #Deals #Fashion).\n"
            "6. DO NOT include the buy link or URL (it will be appended automatically).\n"
            "7. CRITICAL: Keep your entire response UNDER 600 characters so it fits Telegram album limits.\n"
            "8. Output ONLY the caption body text without conversational filler."
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
                    max_tokens=300
                )
                body = response.choices[0].message.content.strip()
                if body.startswith("```"):
                    lines = body.split("\n")
                    if len(lines) >= 3 and lines[-1].startswith("```"):
                        body = "\n".join(lines[1:-1]).strip()

                self._active_model = model
                logger.info(f"Successfully generated AI caption with Groq ({model})!")
                return self._safely_combine_caption(body, link_block)
            except Exception as e:
                logger.warning(f"Groq generation failed with {model}: {e}")

        return self._safely_combine_caption(base_caption, link_block)

    def _safely_combine_caption(self, body_text: str, link_block: str, max_total_len: int = 1015) -> str:
        """
        Mathematically guarantees the buy link is never cut off or truncated.
        If the total text exceeds Telegram limits, truncates ONLY the body text.
        """
        # If the body already ended with a broken link attempt, clean it
        clean_body = body_text.strip()
        
        # Calculate available budget for body
        allowed_body_len = max_total_len - len(link_block)
        if len(clean_body) > allowed_body_len:
            clean_body = clean_body[:allowed_body_len - 3].rstrip() + "..."

        return clean_body + link_block

ai_service = GroqAIService()
