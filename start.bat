@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0server"

if not exist ".venv\Scripts\python.exe" (
  echo [AiBridge] Creating local Python environment...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 goto :error
)

echo [AiBridge] Checking dependencies...
".venv\Scripts\python.exe" -m pip install -q -r "server\requirements.txt"
if errorlevel 1 goto :error

echo [AiBridge] Admin:  http://127.0.0.1:7009
echo [AiBridge] Gateway: http://0.0.0.0:9600
".venv\Scripts\python.exe" -m aibridge.runner
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo [AiBridge] Startup failed. See the error above.
pause
exit /b 1
