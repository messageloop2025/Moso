@echo off
chcp 65001 >nul
title Moso
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

python -c "import mcp" 2>nul
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo pip install failed. Run: pip install -r requirements.txt
        pause
        exit /b 1
    )
)

rem Default single worker (same as config).
rem Multi-worker may duplicate scheduled jobs and cause SQLite lock contention.
rem On Windows, app.py enforces single worker even if EDGEOPS_WORKERS is set.
set EDGEOPS_WORKERS=1
echo Starting Moso at http://127.0.0.1:8010
echo Press Ctrl+C to stop.
echo.
python app.py
if errorlevel 1 (
    echo.
    echo Startup failed. Run: pip install -r requirements.txt
    pause
)
