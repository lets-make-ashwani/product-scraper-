import sys
import asyncio
import logging
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.scrapers.flipkart import FlipkartScraper
from app.services.ai_service import ai_service
from app.telegram.composer import telegram_composer
from app.telegram.client import telegram_publisher
from app.services.image_service import image_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("batch_uploader")

SEARCH_URL = (
    "https://www.flipkart.com/search?q=western+dresses"
    "&sid=clo%2Codx%2Cmaj%2Cjhy&as=on&as-show=on"
    "&otracker=AS_QueryStore_OrganicAutoSuggest_1_3_na_na_ps"
    "&otracker1=AS_QueryStore_OrganicAutoSuggest_1_3_na_na_ps"
    "&as-pos=1&as-type=RECENT"
    "&suggestionId=western+dresses%7CWomen%27s+Dresses"
    "&requestId=c7abdff9-4c1a-4d5f-82f1-7831b059d590&as-backfill=on"
)

async def run_batch():
    scraper = FlipkartScraper()

    print("=" * 70)
    print("🚀 STARTING AUTOMATED BATCH UPLOAD TO TELEGRAM (@autoforwardlink)")
    print("=" * 70)

    # 1. Unpack products from search listing
    print("\n[Step 1] Crawling Flipkart search catalog...")
    product_urls = await scraper.extract_search_product_urls(SEARCH_URL, max_items=25)
    print(f"✅ Discovered {len(product_urls)} products to process!")

    if not product_urls:
        print("❌ No products discovered.")
        return

    # Skip the 1st one if we already posted it in the previous test
    # Process sequentially
    total = len(product_urls)
    successful = 0

    for idx, p_url in enumerate(product_urls, 1):
        print("\n" + "-" * 70)
        print(f"📦 Processing [{idx}/{total}]: {p_url}")
        print("-" * 70)

        staging_dir = None
        try:
            # 1. Scrape product & variants
            product = await scraper.extract_product(p_url)
            print(f"  • Title: {product.title}")
            print(f"  • Price: ₹{product.pricing.deal_price} (MRP: ₹{product.pricing.mrp})")
            print(f"  • Colors Found: {len(product.colors)} | Primary Images: {len(product.primary_images)}")

            # 2. Generate Groq AI Caption
            base_caption = telegram_composer.compose_caption(product)
            caption = await ai_service.enhance_caption(product, base_caption)

            # 3. Ephemeral Image Staging
            staging_dir = image_service.create_staging_dir(f"batch_{idx}")
            selected_imgs = telegram_composer.select_variant_images(product, max_images=5)
            
            if not selected_imgs:
                print("  ⚠️ No images found for product, skipping upload.")
                continue

            downloaded = await image_service.download_images(selected_imgs, staging_dir)
            print(f"  • Downloaded {len(downloaded)} temp images to {staging_dir.name}")

            if downloaded:
                print("  • Uploading album to Telegram...")
                success, msg_ids, err = await telegram_publisher.publish_album(downloaded, caption)
                if success:
                    successful += 1
                    print(f"  🎉 SUCCESS! Posted to @autoforwardlink! Message IDs: {msg_ids}")
                else:
                    print(f"  ❌ Telegram Publish Failed: {err}")
            else:
                print("  ⚠️ Failed to download valid images.")

        except Exception as e:
            print(f"  ❌ Error processing product {idx}: {e}")

        finally:
            # INSTANT PURGE: Delete temp images immediately after each post!
            if staging_dir:
                image_service.purge_staging_dir(staging_dir)
                print(f"  🧹 Cleaned temp images. Disk storage: 0 MB.")

        # Respectful delay between posts to prevent Telegram FloodWait
        print("  ⏳ Waiting 4 seconds before next item...")
        await asyncio.sleep(4)

    print("\n" + "=" * 70)
    print(f"🏁 BATCH UPLOAD COMPLETE! {successful}/{total} products published to Telegram!")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(run_batch())
