@echo off
rem ASCII-only batch: cmd reads batch files in the ANSI code page,
rem so no non-ASCII bytes are allowed here.
rem
rem Rewritten 2026-11-04 (P1-2).
rem
rem The old version did: "if pid file exists -> taskkill /F that pid,
rem without checking whether that pid is still the gateway".
rem gateway.py's `finally: os.remove(pid)` only runs on CLEAN exit, and the
rem log shows many hard kills, so the pid file is often stale. Once Windows
rem reuses that PID for another program, the next run of this script kills an
rem unrelated process, and /F gives no chance to confirm.
rem
rem New version: NEVER trust the pid file alone. Verify by port AND by
rem command line. Better to check twice than to kill the wrong thing.
rem
rem WHY NOT `netstat` (found the hard way, same day):
rem   `netstat -ano` did NOT show a LISTENING line for the python services on
rem   8898 / 8848 on this machine (only timewait rows), while
rem   `Get-NetTCPConnection -LocalPort 8898` DID report the listener.
rem   So a first version built on `netstat | findstr LISTENING` silently
rem   decided "no listener" and fell through to the pid-file path.
rem   Get-NetTCPConnection is the reliable source here, so use that.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "GWPID="

rem ---- 1) find the owner by port (the only trustworthy evidence) ----
rem Get-NetTCPConnection prints the owning pid; State can be blank for some
rem listeners, so do NOT filter on State.
for /f "usebackq tokens=*" %%L in (`powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8898 -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -ne 0 } | Select-Object -First 1; if ($c) { $c.OwningProcess }"`) do (
    if not defined GWPID set "GWPID=%%L"
)

if not defined GWPID (
    echo no listener on 8898
    rem Port is gone but the process may still be alive (zombie).
    rem Use the pid file only as a HINT, and still verify the command line.
    if exist "data\gateway.pid" (
        set /p CANDIDATE=<"data\gateway.pid"
        call :kill_if_gateway !CANDIDATE!
        del "data\gateway.pid" >nul 2>nul
    )
    goto :eof
)

call :kill_if_gateway !GWPID!
rem Clear the pid file either way: its content is no longer trustworthy.
del "data\gateway.pid" >nul 2>nul
goto :eof

rem ---------------------------------------------------------------------------
rem :kill_if_gateway <pid>
rem   Kill only if the command line actually contains gateway.py.
rem ---------------------------------------------------------------------------
:kill_if_gateway
set "TPID=%~1"
if "%TPID%"=="" goto :eof
set "CMDLINE="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=%TPID%').CommandLine" 2^>nul`) do set "CMDLINE=%%c"
if not defined CMDLINE (
    echo pid %TPID% not found
    goto :eof
)
echo !CMDLINE! | findstr /i "gateway.py" >nul
if errorlevel 1 (
    echo SKIP pid %TPID%: command line is not gateway.py
    goto :eof
)
taskkill /PID %TPID% /F >nul 2>nul
if errorlevel 1 (
    echo FAILED to kill pid %TPID%
) else (
    echo stopped pid=%TPID%
)
goto :eof
