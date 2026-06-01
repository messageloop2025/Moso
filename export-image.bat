@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

for /f "delims=" %%v in ('python -c "import sys; sys.path.insert(0, '.'); import config; print(config.VERSION)"') do set "VERSION=%%v"
if not defined VERSION set "VERSION=0.0.0"

set "IMAGE=edgeops:v%VERSION%"
set "RELEASE_DIR=build\edgeops-%VERSION%"
set "OUT=%RELEASE_DIR%\edgeops-v%VERSION%.tar"

if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"

echo Checking image: %IMAGE%
docker image inspect "%IMAGE%" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Image not found: %IMAGE%
  echo Build first: python scripts\build_release.py --build-image
  exit /b 1
)

echo.
echo Exporting image to:
echo   %OUT%
docker save -o "%OUT%" "%IMAGE%"
if errorlevel 1 (
  echo.
  echo Export failed.
  exit /b 1
)

echo.
echo Exported: %OUT%
endlocal
