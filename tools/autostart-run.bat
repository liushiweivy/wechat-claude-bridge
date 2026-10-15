@echo off
rem WeChat-Claude Bridge - hidden autostart launcher with retry loop.
rem Pure ASCII only. python -m bridge stays resident while WeChat is up;
rem if it exits (WeChat not logged in yet, disconnect) we retry for ~10 min.
cd /d "%~dp0.."
if not exist logs mkdir logs

for /l %%i in (1,1,30) do (
  echo [%date% %time%] autostart attempt %%i >> logs\autostart.log
  .venv\Scripts\python.exe -m bridge >> logs\autostart.log 2>&1
  echo [%date% %time%] bridge exited, retry in 20s >> logs\autostart.log
  timeout /t 20 /nobreak >nul
)
echo [%date% %time%] autostart gave up after 30 attempts >> logs\autostart.log
