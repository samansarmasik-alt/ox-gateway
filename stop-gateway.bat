@echo off
REM ox-gateway'i durdurur. Gorunur terminal birakmaz.
setlocal
cd /d "%~dp0"

py -3 "%~dp0supervisor.py" --stop
echo     Bu pencereyi guvenle kapatabilirsin.
ping -n 4 -w 1000 127.0.0.1 >nul 2>&1
exit /b 0
