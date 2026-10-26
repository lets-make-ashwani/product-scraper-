@echo off
title Universal Product Scraper & Telegram Engine
echo ==================================================================
echo      Universal Product Scraper, Groq AI & Telegram Publisher
echo ==================================================================
echo.
echo Starting FastAPI Backend, AI Worker, and Web Dashboard...
echo Local Dashboard: http://127.0.0.1:8000
echo.

start http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

pause
