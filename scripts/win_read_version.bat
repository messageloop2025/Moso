@echo off
setlocal EnableExtensions
set "VERSION="

if not defined PYTHON (
  call "%~dp0win_find_python.bat"
  if errorlevel 1 exit /b 1
)

pushd "%~dp0.."
set "VERFILE=%TEMP%\edgeops_version_%RANDOM%_%RANDOM%.txt"
"%PYTHON%" -c "import sys; sys.path.insert(0, '.'); import config; print(config.VERSION)" > "%VERFILE%" 2>nul
if errorlevel 1 (
  popd
  endlocal
  exit /b 1
)
for /f "usebackq delims=" %%v in ("%VERFILE%") do set "VERSION=%%v"
del /f /q "%VERFILE%" 2>nul
popd

if not defined VERSION set "VERSION=0.0.0"
for /f "delims=" %%v in ("%VERSION%") do endlocal & set "VERSION=%%~v"
exit /b 0
