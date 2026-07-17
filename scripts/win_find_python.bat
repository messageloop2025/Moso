@echo off
setlocal EnableExtensions
set "PYTHON="

pushd "%~dp0.." 2>nul
if errorlevel 1 (
  echo.
  echo Cannot resolve project root from %~dp0
  popd 2>nul
  exit /b 1
)
set "ROOT=%CD%"
popd

if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
  goto :verify
)
if exist "%ROOT%\venv\Scripts\python.exe" (
  set "PYTHON=%ROOT%\venv\Scripts\python.exe"
  goto :verify
)

for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%p"
if defined PYTHON goto :verify

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON=python"
)
if defined PYTHON goto :verify

echo.
echo Python 3 not found.
echo   1. py -3 -m venv .venv ^& .venv\Scripts\pip install -r requirements.txt
echo   2. Or install Python 3.11+ and disable Microsoft Store "python" app alias
echo      Settings ^> Apps ^> Advanced app settings ^> App execution aliases
endlocal
exit /b 1

:verify
"%PYTHON%" -c "import sys; print(sys.version)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python found but not runnable: %PYTHON%
  endlocal
  exit /b 1
)

for /f "delims=" %%p in ("%PYTHON%") do endlocal & set "PYTHON=%%~p"
exit /b 0
