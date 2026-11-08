@echo off
rem 本机执行策略会拦新建的 .ps1（提示“未数字签名”），所以从这里走：
rem -ExecutionPolicy Bypass 只作用于本次进程，不改系统设置。
rem 参数：/nopause  跑完不等待按键（给脚本或快捷方式调用）
setlocal
set "PS1="
for %%f in ("%~dp0*.ps1") do if not defined PS1 set "PS1=%%~ff"
if not defined PS1 (
  echo [launcher] 同目录下找不到 .ps1 启动脚本。
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%ERRORLEVEL%"
if /i "%~1"=="/nopause" exit /b %RC%
echo.
pause
exit /b %RC%
