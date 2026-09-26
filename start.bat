@echo off
REM ox-gateway'i baslatir (ON PLANDA - bu pencere acik kalmali).
REM Kalici/otomatik calistirmak icin: start-detached.bat
title ox-gateway
cd /d "%~dp0"
if not exist logs mkdir logs

REM --- Port zaten doluysa KACIS: uvicorn hemen cokup "Press any key" diye
REM takili bir pencere birakiyordu. Onun yerine net mesaj ver ve cik.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "127.0.0.1:8756" ^| findstr "LISTENING"') do (
    echo.
    echo [OK] Gateway ZATEN calisiyor ^(PID %%p^) - http://127.0.0.1:8756
    echo      Bu pencereyi guvenle kapatabilirsin.
    echo.
    timeout /t 4 /nobreak >nul
    exit /b 0
)

echo [..] ox-gateway baslatiliyor... (durdurmak icin bu pencereyi kapat)
py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756
echo.
echo [!] Gateway DURDU. Yukaridaki mesaji oku.
echo     Yeniden baslatmak icin bu pencereye tikla ve start.bat'i tekrar calistir.
pause
