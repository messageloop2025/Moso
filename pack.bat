@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM 从 config.py 读取 VERSION（与 config.py 中 EDGEOPS_VERSION 或默认 "0.1.3" 一致）
for /f "delims=" %%v in ('python -c "import sys; sys.path.insert(0, '.'); import config; print(config.VERSION)"') do set "VERSION=%%v"
if not defined VERSION set "VERSION=0.1.3"

REM 当前目录名（打包的顶层目录名）
for %%A in ("%~dp0.") do set "DIRNAME=%%~nxA"

cd ..
set "OUT=毛竹-%VERSION%.tgz"

REM 使用 tar 打包为 tgz，排除 .git、build、__pycache__、web/fs 及所有 . 开头的文件/目录
tar -acvf "%OUT%" ^
  --exclude="%DIRNAME%/.git" ^
  --exclude="%DIRNAME%/build" ^
  --exclude="%DIRNAME%/build/*" ^
  --exclude="%DIRNAME%/__pycache__" ^
  --exclude="%DIRNAME%/*/__pycache__" ^
  --exclude="%DIRNAME%/*/*/__pycache__" ^
  --exclude="%DIRNAME%/*/*/*/__pycache__" ^
  --exclude="%DIRNAME%/web/fs" ^
  --exclude="%DIRNAME%/.*" ^
  -C "%~dp0.." "%DIRNAME%"

if %ERRORLEVEL% equ 0 (
  echo.
  echo Packed: %OUT%
) else (
  echo Pack failed.
  exit /b 1
)
