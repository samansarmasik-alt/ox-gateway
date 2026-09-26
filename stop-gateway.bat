@echo off
REM ox-gateway'i durdurur (supervisor dahil).
title ox-gateway stop

for /f "tokens=5" %%p in ('netstat -ano ^| findstr "127.0.0.1:8756" ^| findstr "LISTENING"') do (
    echo Port 8756 sahibi: PID %%p
    taskkill /PID %%p /T /F >nul 2>&1
)

REM supervisor penceresini de kapat
taskkill /FI "WINDOWTITLE eq ox-gateway supervisor" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ox-gateway" /T /F >nul 2>&1

echo ox-gateway durduruldu.
timeout /t 2 /nobreak >nul
