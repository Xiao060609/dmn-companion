@echo off
rem ASCII-only batch: cmd reads batch files in the ANSI code page, so no
rem Chinese bytes allowed here. All Chinese text lives in restart.ps1.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1"
set "RC=%ERRORLEVEL%"
if /i "%~1"=="/nopause" exit /b %RC%
rem Auto-close once the restart is done. restart.ps1 hands the "start" phase to a
rem hidden detached process, so closing this window can no longer abort it - this
rem is only about not leaving a dead console behind.
rem timeout /nobreak: always waits the full 10s, and exits immediately instead of
rem hanging when stdin is redirected (choice /t never times out without a console).
if not "%RC%"=="0" (
    echo.
    echo Something failed - keeping this window open.
    pause
    exit /b %RC%
)
echo.
echo Window closes in 10s.
timeout /t 10 /nobreak >nul 2>&1
exit /b %RC%
