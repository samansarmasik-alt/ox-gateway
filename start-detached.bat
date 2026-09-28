@echo off
REM ox-gateway'i ARKA PLANDA baslatir. HICBIR gorunur pencere acmaz.
REM Kullanim: start-detached.bat  (sonra bu pencereyi rahatca kapatabilirsin)
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

REM zaten calisiyorsa hicbir sey yapma, sadece cik
netstat -ano | findstr "127.0.0.1:8756" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Gateway zaten calisiyor - http://127.0.0.1:8756
    goto done
)

del /q logs\gateway.stop >nul 2>&1

REM pythonw.exe = KONSOLSZ. Gorunur terminal acilmaz, o pencere de kapanir.
set PY=
for %%P in (
    "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
    "%LocalAppData%\Programs\Python\Python312\pythonw.exe"
    "%LocalAppData%\Programs\Python\Python311\pythonw.exe"
) do if exist %%P if not defined PY set PY=%%~P
if not defined PY (
    py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" > "%TEMP%\_oxgw.txt" 2>nul
    set /p PY=<"%TEMP%\_oxgw.txt"
    del /q "%TEMP%\_oxgw.txt" >nul 2>&1
)
if not defined PY if not exist "%PY%" (
    echo [!] pythonw.exe bulunamadi. start.bat'i elle calistir.
    exit /b 1
)

REM pythonw.exe = KONSOLSZ. Gorunur terminal acilmaz, o pencere de kapanir.
REM >>>/>>2&1 KRITIK: bu yonlendirme olmadan arka plandaki surec ebeveynin
REM stdout pipe'ini acik tutuyor ve cagiran shell (orn. agent terminali)
REM hic kapanmiyor. Yonlendirme ile cikis akisi serbest birakiliyor.
REM NOT: dogrudan "start pythonw" KULLANILMAZ. Cagiran kabuk kapaninca
REM Windows surec agacini da olduruyor -> gateway rastgele dustu.
REM launcher.py DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB ile
REM supervisor'i kabuktan tamamen ayiriyor.
py -3 "%~dp0launcher.py"

REM supervisor + uvicorn ayaga kalkana kadar bekle (en fazla 15 sn)
REM NOT: 'timeout' konsolsuz shell'de "Input redirection is not supported"
REM verip donguyu bozuyor; ping her ortamda sessizce 1 sn bekler.
set /a TRIES=0
:wait
ping -n 2 -w 1000 127.0.0.1 >nul 2>&1
set /a TRIES+=1
netstat -ano | findstr "127.0.0.1:8756" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Gateway arka planda calisiyor - http://127.0.0.1:8756
    goto done
)
if %TRIES% lss 15 goto wait
echo [!] Gateway 15 sn icinde ayaga kalkmadi. logs\gateway.log'a bak.
exit /b 1

:done
REM Pencere aninda kapanirsa kullanici "calismadi" saniyor; sonucu
REM okuyabilmesi icin ~3 sn bekletip SONRA kapat.
echo.
echo     Bu pencereyi guvenle kapatabilirsin.
ping -n 4 -w 1000 127.0.0.1 >nul 2>&1
exit /b 0
