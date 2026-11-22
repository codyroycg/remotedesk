@echo off
rem Start the RemoteDesk server (Windows). Double-click this file.
cd /d "%~dp0server"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js is required. Install it from https://nodejs.org ^(version 18 or newer^), then run this again.
  pause
  exit /b 1
)
if not exist node_modules call npm install --omit=dev --no-fund --no-audit
node server.js
pause
