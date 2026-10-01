@echo off
REM ox-gateway'i ON PLANDA baslatir (bu pencere acik kalmali).
REM Arka plan / pencere istemiyorsan: start-detached.bat
title ox-gateway (on plan - kapatma)
cd /d "%~dp0"
if not exist logs mkdir logs

REM --- Port zaten doluysa KACIS: uvicorn hemen cokup takili pencere birakmasin.
netstat -ano | findstr "127.0.0.1:8756" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Gateway ZATEN calisiyor - http://127.0.0.1:8756
    echo     Bu pencereyi guvenle kapatabilirsin.
    ping -n 4 -w 1000 127.0.0.1 >nul 2>&1
    exit /b 0
)

echo.
echo  [!] ON PLAN MODU: bu pencereyi KAPATIRSAN gateway DURUR.
echo      Log yok, otomatik yeniden baslatma yok, hata da basilmaz.
echo      Arka planda calistirmak icin: start-detached.bat
echo      Kanit biraksin diye cikti logs\gateway-console.log'a yaziliyor.
echo.

py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756 >>logs\gateway-console.log 2>&1

echo.
echo [!] Gateway DURDU. Yukaridaki mesaji oku.
echo     Kanit: logs\gateway-console.log
echo     Kalici calistirma: start-detached.bat
ping -n 6 -w 1000 127.0.0.1 >nul 2>&1
exit /b 0
