@echo off
REM ox-gateway'i baslatir
cd /d "%~dp0"
py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756
pause
