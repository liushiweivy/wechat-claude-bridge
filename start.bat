@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
  echo [setup] .venv not found, creating virtual environment...
  py -3.12 -m venv .venv || goto err
  .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto err
)

if not exist config.json (
  copy config.example.json config.json >nul
  echo [setup] config.json created from example. Edit workspace/allowed_dirs, then run again.
  pause
  exit /b 0
)

.venv\Scripts\python.exe -m bridge
pause
exit /b 0

:err
echo [error] setup failed. Check Python 3.12 and network.
pause
exit /b 1
