@echo off
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto trypy
python -B gateway.py
goto :eof
:trypy
where py >nul 2>nul
if errorlevel 1 goto nopython
py -B gateway.py
goto :eof
:nopython
echo python not found, please install Python 3.10+
exit /b 1