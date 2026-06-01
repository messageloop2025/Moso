@echo off
setlocal
cd /d "%~dp0"
call npm run pack
if errorlevel 1 exit /b 1
echo.
echo Pack done. Tarball is in this folder ^(edgeops-claw-ops-*.tgz^).
