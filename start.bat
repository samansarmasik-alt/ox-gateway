@echo off
REM ox-gateway'i baslatir (ON PLANDA - pencereyi kapatma, gateway olur).
REM Kalici calistirmak icin: start-detached.bat  (otomatik yeniden baslatir)
title ox-gateway
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756
echo.
echo [!] Gateway DURDU. Yukari mesaji oku, sonra bu pencereyi KAPATMA diye bir sey yapma.
pause
