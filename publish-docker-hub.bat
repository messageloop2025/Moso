@echo off
setlocal
cd /d "%~dp0"

rem Load docker-hub.env (copy from docker-hub.env.example)
set "DOCKERHUB_USER="
set "DOCKERHUB_REPO=moso"
set "PUSH_LATEST=1"

if exist "docker-hub.env" (
    for /f "tokens=1,* delims==" %%a in ('findstr /b /i /c:"DOCKERHUB_USER=" docker-hub.env') do set "DOCKERHUB_USER=%%b"
    for /f "tokens=1,* delims==" %%a in ('findstr /b /i /c:"DOCKERHUB_REPO=" docker-hub.env') do set "DOCKERHUB_REPO=%%b"
    for /f "tokens=1,* delims==" %%a in ('findstr /b /i /c:"PUSH_LATEST=" docker-hub.env') do set "PUSH_LATEST=%%b"
)

if not defined DOCKERHUB_USER (
    echo.
    echo ERROR: DOCKERHUB_USER is not set.
    echo   1. Copy docker-hub.env.example to docker-hub.env
    echo   2. Set your hub.docker.com username in docker-hub.env
    echo   3. Run docker login first
    echo.
    pause
    exit /b 1
)

if not defined DOCKERHUB_REPO set "DOCKERHUB_REPO=moso"

call "%~dp0scripts\win_find_python.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
call "%~dp0scripts\win_read_version.bat"
if errorlevel 1 (
    echo.
    echo ERROR: Cannot read VERSION from config.py ^(Python: %PYTHON%^)
    pause
    exit /b 1
)
if not defined VERSION set "VERSION=0.0.0"

set "EDGEOPS_VERSION=%VERSION%"
set "LOCAL_TAG=edgeops:%VERSION%"
set "REMOTE=%DOCKERHUB_USER%/%DOCKERHUB_REPO%"
set "REMOTE_TAG=%REMOTE%:%VERSION%"

echo.
echo Moso - publish Docker image to Docker Hub
echo   version:         %VERSION%
echo   Python:          %PYTHON%
echo   build local tag: %LOCAL_TAG%
echo   push remote tag: %REMOTE_TAG%
if "%PUSH_LATEST%"=="1" echo   also push:       %REMOTE%:latest
echo.

docker version >nul 2>nul
if errorlevel 1 (
    echo ERROR: docker not found. Start Docker Desktop first.
    pause
    exit /b 1
)

echo [1/4] docker compose build ...
docker compose -f docker/docker-compose.yml build
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo [2/4] docker tag ...
docker image inspect "%LOCAL_TAG%" >nul 2>nul
if errorlevel 1 (
    echo ERROR: local image not found: %LOCAL_TAG%
    echo   EDGEOPS_VERSION=%VERSION%
    pause
    exit /b 1
)

docker tag "%LOCAL_TAG%" "%REMOTE_TAG%"
if errorlevel 1 (
    echo Tag failed: %REMOTE_TAG%
    pause
    exit /b 1
)

if "%PUSH_LATEST%"=="1" (
    docker tag "%LOCAL_TAG%" "%REMOTE%:latest"
    if errorlevel 1 (
        echo Tag failed: %REMOTE%:latest
        pause
        exit /b 1
    )
)

echo.
echo [3/4] docker push ...
docker push "%REMOTE_TAG%"
if errorlevel 1 (
    echo.
    echo Push failed. Check docker login and repo %REMOTE% for user %DOCKERHUB_USER%.
    pause
    exit /b 1
)

if "%PUSH_LATEST%"=="1" (
    docker push "%REMOTE%:latest"
    if errorlevel 1 (
        echo.
        echo Push latest failed. Version tag %REMOTE_TAG% may already be on Hub.
        pause
        exit /b 1
    )
)

echo.
echo [4/4] Done
echo.
echo   docker pull %REMOTE_TAG%
if "%PUSH_LATEST%"=="1" echo   docker pull %REMOTE%:latest
echo.
echo   deploy from repo root:
echo     docker compose --env-file docker-hub.env pull
echo     docker compose --env-file docker-hub.env up -d
echo.

endlocal
