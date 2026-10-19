@echo off
rem WeChat-Claude Bridge - hidden autostart launcher with retry loop.
rem Pure ASCII only. python -m bridge stays resident while WeChat is up;
rem if it exits (WeChat not logged in yet, disconnect) we retry for ~10 min.
rem delayedexpansion: %date%/%time% inside a for block expand ONCE at parse
rem time (all 30 retries would log the same frozen timestamp); use !date! !time!.
setlocal enabledelayedexpansion
cd /d "%~dp0.."
if not exist logs mkdir logs
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File tools\wechat_autologin.ps1

for /l %%i in (1,1,30) do (
  echo [!date! !time!] autostart attempt %%i >> logs\autostart.log
  powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*bridge_probe.py*' }) { exit 1 } else { exit 0 }"
  if errorlevel 1 (
    echo [!date! !time!] probe already running >> logs\autostart.log
  ) else (
    echo [!date! !time!] starting probe >> logs\autostart.log
    start "" .venv\Scripts\pythonw.exe tools\bridge_probe.py
  )
  .venv\Scripts\python.exe -m bridge >> logs\autostart.log 2>&1
  echo [!date! !time!] bridge exited, retry in 20s >> logs\autostart.log
  rem timeout.exe exits instantly when stdin is not a console; use ping instead.
  ping 127.0.0.1 -n 21 >nul
)
echo [!date! !time!] autostart gave up after 30 attempts >> logs\autostart.log
