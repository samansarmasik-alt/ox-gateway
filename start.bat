@echo off
REM ox-gateway'i ON PLANDA baslatir (bu pencere acik kalmali).
REM Arka plan / pencere istemiyorsan: start-detached.bat
title ox-gateway
cd /d "%~dp0"
if not exist logs mkdir logs

REM --- Port zaten doluysa KACIS: uvicorn hemen cokup "Press any key" diye
REM takili bir pencere birakiyordu. Artik beklemeden cikiyoruz.
netstat -ano | findstr "127.0.0.1:8756" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Gateway ZATEN calisiyor - http://127.0.0.1:8756
    echo     Bu pencereyi guvenle kapatabilirsin.
    exit /b 0
)

echo [..] ox-gateway baslatiliyor... (durdurmak icin bu pencereyi kapat)
py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756
echo.
echo [!] Gateway DURDU. Yukaridaki mesaji oku.
echo     Arka planda calismasi icin: start-detached.bat
exit /b 0
