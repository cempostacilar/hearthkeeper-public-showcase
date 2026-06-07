@echo off
setlocal

REM Run from repo root: launches frontend dev server from correct folder.
cd /d "%~dp0frontend" || (
  echo [ERROR] Could not enter frontend folder.
  exit /b 1
)

if not exist package.json (
  echo [ERROR] package.json not found in %cd%
  exit /b 1
)

echo [INFO] Working directory: %cd%

if not exist node_modules (
  echo [INFO] Installing npm dependencies...
  npm install || exit /b 1
)

echo [INFO] Starting Vite dev server...
npm run dev
