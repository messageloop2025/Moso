@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

call "%~dp0scripts\win_find_python.bat"
if errorlevel 1 exit /b 1

call "%~dp0scripts\win_read_version.bat"
if errorlevel 1 exit /b 1
if not defined VERSION set "VERSION=0.0.0"

set "RELEASE_DIR=build\edgeops-%VERSION%"
set "BUNDLE_TGZ=build\edgeops-v%VERSION%.tgz"
set "EDGEOPS_VERSION=%VERSION%"

echo.
echo 毛竹 release bundle v%VERSION%
echo   Python: %PYTHON%
echo   edgeops-v%VERSION%.tgz 解压后:
echo     edgeops-%VERSION%\
echo       docker-compose.yml  run.bat  run.sh  start-compose.*
echo       edgeops-v%VERSION%.tar
echo       data\data  data\fs  data\logs
echo.

echo [1/2] Build image and assemble release directory...
"%PYTHON%" "scripts\build_release.py" --platform linux.x86_64 --mode pyc --build-image --export-tar --tag edgeops:v%VERSION%
if errorlevel 1 (
  echo.
  echo Build failed.
  exit /b 1
)

echo.
echo [2/2] Creating %BUNDLE_TGZ% ...
if not exist "%RELEASE_DIR%" (
  echo Release directory not found: %RELEASE_DIR%
  exit /b 1
)
if exist "%BUNDLE_TGZ%" del /f /q "%BUNDLE_TGZ%"

tar -acf "%BUNDLE_TGZ%" -C "build" "edgeops-%VERSION%"
if errorlevel 1 (
  echo.
  echo Bundle failed.
  exit /b 1
)

echo.
echo Done: %BUNDLE_TGZ%
echo Extract, enter edgeops-%VERSION%%, run start-compose.bat
endlocal
