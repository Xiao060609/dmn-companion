@echo off
chcp 65001 >nul
rem Pass the app path as an absolute argument instead of ".".
rem If the working directory is not ...\desktop\desktop, Electron
rem looks for package.json in ...\desktop and fails with
rem "Unable to find Electron app at ...\desktop".
set "APPDIR=%~dp0desktop"

if not exist "%APPDIR%\package.json" (
  echo [ERROR] package.json not found in %APPDIR%
  pause
  exit /b 1
)
if not exist "%APPDIR%\node_modules\electron\dist\electron.exe" (
  echo [ERROR] Electron not installed. Run: cd /d "%APPDIR%" ^&^& npm install
  pause
  exit /b 1
)

start "" "%APPDIR%\node_modules\electron\dist\electron.exe" "%APPDIR%"
exit /b 0
