@echo off
rem WeChat-Claude Bridge - visible logon autostart.
rem 1) bail out if a bridge python is already running (avoid double replies)
rem 2) 45s grace period (WeChat itself starts via HKCU\Run)
rem 3) wait for WeChat.exe (up to ~8 min for manual login)
rem 4) run "python -m bridge" in THIS window; retry for ~10 min on exit
rem Pure ASCII only.
rem delayedexpansion: %date%/%time% inside a for block expand ONCE at parse
rem time (all 30 retries would log the same frozen timestamp); use !date! !time!.
setlocal enabledelayedexpansion
title WeChat Claude Bridge
cd /d "%~dp0.."
if not exist logs mkdir logs

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m bridge*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo [autostart] bridge python is already running, exit to avoid duplicates.
  pause
  exit /b 0
)

echo [autostart] 45s grace period waiting for WeChat...
rem NOTE: timeout.exe exits instantly when stdin is not a console (measured
rem 2026-10-19: retry delay was 0s, 30 attempts burned out in ~2 min). Use ping.
ping 127.0.0.1 -n 46 >nul

echo [autostart] waiting for WeChat.exe process, up to ~8 min...
echo [autostart] launching wechat autologin helper (hidden)
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File tools\wechat_autologin.ps1
for /l %%i in (1,1,24) do (
  tasklist /FI "IMAGENAME eq WeChat.exe" | findstr WeChat.exe >nul
  if not errorlevel 1 goto :wechat_up
  ping 127.0.0.1 -n 21 >nul
)
echo [autostart] WeChat.exe not seen after 8 min, starting bridge anyway.
:wechat_up

rem Deafness probe is launched hourly by Task Scheduler (WxBridgeHourlyProbe),
rem not from this bat: a resident pythonw probe could die silently with nobody
rem left to restart it (2026-10-21 three-process outage).
for /l %%i in (1,1,30) do (
  echo [!date! !time!] autostart attempt %%i
  echo [!date! !time!] autostart attempt %%i >> logs\autostart.log
  .venv\Scripts\python.exe -u -m bridge
  echo [!date! !time!] bridge exited, retry in 20s
  echo [!date! !time!] bridge exited, retry in 20s >> logs\autostart.log
  ping 127.0.0.1 -n 21 >nul
)
echo [autostart] gave up after 30 attempts.
pause
