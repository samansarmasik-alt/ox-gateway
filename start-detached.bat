@echo off
REM ox-gateway'i arka planda baslatir ve cokerse otomatik yeniden baslatir.
REM Kullanim: start-detached.bat   (pencereyi KAPATMA - sadece bu cokerse)
title ox-gateway supervisor

cd /d "%~dp0"

REM zaten calisiyorsa cik
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "127.0.0.1:8756" ^| findstr "LISTENING"') do (
    echo ox-gateway zaten calisiyor ^(PID %%p^). Kapatmak icin: stop-gateway.bat
    exit /b 0
)

:loop
echo [%date% %time%] ox-gateway baslatiliyor...
py -3 -m uvicorn gateway:app --host 127.0.0.1 --port 8756 >> logs\gateway.log 2>&1
echo [%date% %time%] ox-gateway DURDU ^(exit %errorlevel%^). 3 sn sonra yeniden baslatilacak...
timeout /t 3 /nobreak >nul
goto loop
