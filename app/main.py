import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.mongodb import connect_to_mongo, close_mongo_connection, get_database
from app.services.worker import worker
from app.api.routes import router as api_router
from app.services.log_stream import setup_terminal_logging

# Configure root logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
setup_terminal_logging()
logger = logging.getLogger("main")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Universal Product Scraper starting up...")
    settings.ensure_temp_dir()
    await connect_to_mongo()
    worker.start()
    yield
    # Shutdown
    logger.info("Universal Product Scraper shutting down...")
    worker.stop()
    await close_mongo_connection()

app = FastAPI(
    title="Universal Product Scraper & Telegram Publisher",
    description="Multi-platform scraper (Meesho, Flipkart, Myntra, Amazon) with Color Variant Merger, MongoDB storage, and Render 24/7 ephemeral image auto-delete.",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Router
app.include_router(api_router)

# Healthcheck for Render (supports both GET and HEAD)
@app.api_route("/health", methods=["GET", "HEAD"], tags=["Health"])
async def health_check():
    db = get_database()
    mongo_ok = db is not None
    return {
        "status": "healthy" if mongo_ok else "degraded",
        "mongodb": "connected" if mongo_ok else "disconnected",
        "worker_running": worker.is_running
    }

# Web Dashboard (supports both GET and HEAD, modern Starlette TemplateResponse syntax)
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Dashboard"])
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")
