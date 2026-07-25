@echo off
cd /d "%~dp0"

echo === RMAX Quote Tracker ===

REM Install dependencies if needed
pip install -r requirements.txt --quiet

REM Import existing quotes (safe to run multiple times)
python migrate.py

REM Start the server
echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn backend:app --host 0.0.0.0 --port 8000 --reload
