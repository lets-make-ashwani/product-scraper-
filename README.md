# 🛍️ Universal Product Scraper, Groq AI & Telegram Engine

An enterprise-grade, 24/7 cloud-native product scraper and Telegram publisher supporting **Meesho, Flipkart, Myntra, and Amazon**. 

Features automatic **color variant discovery**, single combined Telegram album posting, **Groq AI copywriting & post enhancement**, **MongoDB** metadata storage, and **Render ephemeral image staging** with **instant auto-deletion** to maintain 0 MB long-term disk storage.

---

## 🌟 Key Capabilities

- **🤖 Groq AI Copywriting & Optimization**:
  - Leverages lightning-fast LLMs (`llama-3.3-70b-versatile` / `llama-3.1-8b-instant`) at 500+ tokens/sec.
  - Automatically turns raw, cluttered product descriptions into high-converting Telegram posts with bold pricing, discount urgency tags, color options, crisp highlights, and trending shopping hashtags.
- **Multi-Platform Scraper Adapters**:
  - 🌸 **Meesho**: `__NEXT_DATA__` deep hydration + catalog variation extraction.
  - 🛒 **Flipkart**: JSON-LD Schema.org + color swatch selector mapping.
  - 👗 **Myntra**: `window.__myx` object parser + high-res image CDN upgrader.
  - 📦 **Amazon India**: ASIN extraction, variation matrices, and high-resolution swatch galleries.
- **🎨 Color Variant Merger**: Automatically crawls each color variant of a product, downloads high-resolution photos across all colorways, and bundles them into a single Telegram album post (`sendMediaGroup` or Telethon file upload).
- **🍃 MongoDB Document Store**: Full product details, pricing history, specs, and status stored flexibly in MongoDB (supports MongoDB Atlas free tier).
- **⚡ Ephemeral Storage & Instant Auto-Purge**:
  - Images are temporarily downloaded into a scratch directory on the Render server for ~5 seconds.
  - Once posted to Telegram, the entire directory is **instantly deleted** via `shutil.rmtree()` in a `finally:` block.
  - Includes a background **orphan janitor** that guarantees zero disk bloat.
- **📱 Telegram StringSession Authentication**:
  - Uses Telethon's `StringSession` loaded directly from the `TELEGRAM_SESSION_STRING` environment variable.
  - **No local `.session` file required on disk**, ensuring 100% cloud persistence across Render container restarts.
  - Also supports standard Telegram Bot API token fallback.
- **🚀 24/7 Autonomous Deployment on Render**: Dockerfile ready with headless Chromium, Playwright Linux dependencies, and dynamic port binding.

---

## 🏗️ System Architecture

```text
  [ User Submits Product URLs / Webhooks ]
                     │
                     ▼
        [ FastAPI REST & Dashboard UI ]
                     │
                     ▼
          [ MongoDB Jobs Queue ]
                     │
                     ▼
        [ Autonomous Background Worker ]
                     │
    ┌────────────────┼────────────────┐
    ▼                ▼                ▼
[Meesho]        [Flipkart]         [Myntra]      [Amazon]
    │                │                │             │
    └────────────────┴────────┬───────┴─────────────┘
                              ▼
           [ Discover All Color Variants & Images ]
                              │
                              ▼
                 🤖 [ Groq AI Copywriter ]
        (Llama 3.3 70B: High-Converting Caption & Tags)
                              │
                              ▼
         [ Render Ephemeral Temp Scratch: /tmp/... ]
         (Downloads representative images per color)
                              │
                              ▼
           [ Telethon StringSession Telegram Post ]
           (Sends native photo album with colorways)
                              │
                              ▼
          🔥 [ INSTANT DISK AUTO-DELETE PURGE ] 🔥
               (Disk storage returns to 0 MB!)
                              │
                              ▼
            [ Status: PUBLISHED in MongoDB ]
```

---

## ⚙️ Environment Variables Configuration

Create a `.env` file locally or set these in your **Render Environment Settings**:

| Variable | Required | Description | Example |
| :--- | :--- | :--- | :--- |
| `PORT` | Yes | Web server port (Render sets this dynamically) | `8000` |
| `MONGODB_URI` | Yes | MongoDB Atlas or local MongoDB connection string | `mongodb+srv://user:pass@cluster0.mongodb.net/?retryWrites=true` |
| `DATABASE_NAME` | No | Target MongoDB database name | `product_scraper_db` |
| `GROQ_API_KEY` | Recommended | Free API Key from [console.groq.com](https://console.groq.com) | `gsk_abc123...` |
| `GROQ_MODEL` | No | Target Groq model | `llama-3.3-70b-versatile` |
| `AI_ENHANCE_POSTS`| No | Enable/disable AI copywriting | `True` |
| `TELEGRAM_API_ID` | Yes | Telegram API ID from [my.telegram.org](https://my.telegram.org) | `12345678` |
| `TELEGRAM_API_HASH` | Yes | Telegram API Hash from [my.telegram.org](https://my.telegram.org) | `0123456789abcdef0123456789abcdef` |
| `TELEGRAM_SESSION_STRING` | Yes | Telethon StringSession (prevents file session loss) | `1BVtsOGQBu...` |
| `TELEGRAM_CHAT_ID` | Yes | Target Channel Username or Numeric Chat ID | `@your_channel` or `-100123456789` |
| `TELEGRAM_ENABLED` | No | Toggle Telegram publishing | `True` |
| `AUTO_PUBLISH` | No | Automatically publish imported URLs | `True` |
| `HEADLESS_BROWSER` | No | Run Chromium in headless mode | `True` |
| `TEMP_DIR` | No | Ephemeral scratch directory for temporary image files | `/tmp/product_scraper_staging` |

---

## 🔑 How to Generate a Telegram StringSession

You can generate a session string in 1 minute using Python:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = 12345678              # From https://my.telegram.org
API_HASH = "your_api_hash"      # From https://my.telegram.org

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nYOUR TELEGRAM_SESSION_STRING IS:\n")
    print(client.session.save())
```
Copy the printed string and paste it into `TELEGRAM_SESSION_STRING` in Render!

---

## 🚀 24/7 Deployment on Render

### Option A: 1-Click Blueprint (Recommended)
1. Fork or push this repository to your GitHub:
   `https://github.com/lets-make-ashwani/product-scraper-.git`
2. Go to [Render Dashboard](https://dashboard.render.com).
3. Click **New +** $\rightarrow$ **Blueprint**.
4. Connect this GitHub repository.
5. Render will automatically read `render.yaml`, configure Docker, and prompt you to input your `MONGODB_URI`, `GROQ_API_KEY`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION_STRING`.
6. Click **Apply**! Your scraper is now live 24/7.

### Option B: Deploy as a Docker Web Service
1. Click **New +** $\rightarrow$ **Web Service**.
2. Select your repository.
3. Choose **Docker** as the runtime.
4. Set your Environment Variables in the Render settings tab.
5. Deploy!

---

## 💻 Local Development Setup

### 1. Clone Repository
```bash
git clone https://github.com/lets-make-ashwani/product-scraper-.git
cd product-scraper-
```

### 2. Create Virtual Environment & Install Dependencies
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
playwright install --with-deps chromium
```

### 3. Run Application
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Open [http://localhost:8000](http://localhost:8000) in your browser to access the control panel!

---

## 🛡️ License
MIT License. Open-source automation engine.
