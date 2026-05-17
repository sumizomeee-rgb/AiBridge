@echo off
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo [AiBridge] Node.js was not found in PATH.
  echo Please install Node.js first.
  pause
  exit /b 1
)

set "NEED_NPM_INSTALL="
if not exist "node_modules\" set "NEED_NPM_INSTALL=1"
if not exist "node_modules\playwright\" set "NEED_NPM_INSTALL=1"

if defined NEED_NPM_INSTALL (
  echo [AiBridge] Installing server dependencies...
  call npm install
  if errorlevel 1 (
    echo [AiBridge] npm install failed.
    pause
    exit /b 1
  )
)

if not exist "data\config.json" if exist "data\config.example.json" (
  echo [AiBridge] Creating local runtime config...
  copy "data\config.example.json" "data\config.json" >nul
)

set "AIBRIDGE_BROWSER_CHANNEL="
if "%AIBRIDGE_BROWSER_CHANNEL%"=="" if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  set "AIBRIDGE_BROWSER_CHANNEL=chrome"
  echo [AiBridge] Found local Google Chrome.
)

if "%AIBRIDGE_BROWSER_CHANNEL%"=="" if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
  set "AIBRIDGE_BROWSER_CHANNEL=chrome"
  echo [AiBridge] Found local Google Chrome.
)

if "%AIBRIDGE_BROWSER_CHANNEL%"=="" (
  dir /b "%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win64\chrome.exe" >nul 2>nul
  if not errorlevel 1 (
    set "AIBRIDGE_BROWSER_CHANNEL=bundled"
    echo [AiBridge] Found Playwright Chromium.
  )
)

if "%AIBRIDGE_BROWSER_CHANNEL%"=="" (
  echo [AiBridge] Browser not found. Installing Playwright Chromium...
  call npx playwright install chromium
  if errorlevel 1 (
    echo [AiBridge] Playwright Chromium install failed.
    pause
    exit /b 1
  )
  set "AIBRIDGE_BROWSER_CHANNEL=bundled"
)

echo [AiBridge] Starting server...
call npm start
pause
