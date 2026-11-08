@echo off
chcp 65001 >nul
cd /d "%~dp0"
start /b python server.py
timeout /t 4 >nul
start "" http://127.0.0.1:8848
echo.
echo Server:  http://127.0.0.1:8848
echo Press Ctrl+C or close this window to stop.
echo.
pause