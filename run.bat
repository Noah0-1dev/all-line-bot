@echo off
setlocal
title UniBridge Bot Runner (Python)
cd /d "%~dp0"

REM ================= CONFIG =================
set "DOMAIN=fastness-aftermost-wimp.ngrok-free.dev"
set "PORT=3000"
set "NGROK_DIR=C:\Users\noah\OneDrive\Desktop\all-line-bot\ngrok-v3-stable-windows-amd64"
set "NGROK_EXE=%NGROK_DIR%\ngrok.exe"
set "PYTHON=%~dp0venv\Scripts\python.exe"
REM ==========================================

echo ====================================================
echo 1. KIEM TRA MOI TRUONG PYTHON
echo ====================================================

where python >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python. Hay cai dat Python truoc.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Dang tao moi truong ao venv...
    python -m venv venv
    if errorlevel 1 (
        echo [LOI] Khong tao duoc venv.
        pause
        exit /b 1
    )
)

echo Kiem tra va cai dat thu vien...
"%PYTHON%" -m pip install --disable-pip-version-check flask line-bot-sdk python-dotenv openai

if errorlevel 1 (
    echo [LOI] Cai dat thu vien that bai.
    pause
    exit /b 1
)

echo [OK] Python va thu vien da san sang.
echo.

echo ====================================================
echo 2. KIEM TRA NGROK
echo ====================================================

if not exist "%NGROK_EXE%" (
    echo [LOI] Khong tim thay ngrok.exe:
    echo %NGROK_EXE%
    echo.
    echo Hay kiem tra lai duong dan thu muc Ngrok.
    pause
    exit /b 1
)

echo [OK] Tim thay ngrok.exe
echo Domain: %DOMAIN%
echo Port: %PORT%
echo.

echo ====================================================
echo 3. KHOI DONG NGROK
echo ====================================================

REM Kiem tra Ngrok da chay chua
tasklist /FI "IMAGENAME eq ngrok.exe" 2>nul | find /I "ngrok.exe" >nul
if not errorlevel 1 (
    echo [CANH BAO] Ngrok co the dang chay.
    echo Neu domain bi trung, hay dong cua so Ngrok cu truoc.
) else (
    start "UniBridge Ngrok" /D "%NGROK_DIR%" "%NGROK_EXE%" http --domain=%DOMAIN% %PORT%
)

echo Dang cho Ngrok khoi dong...
timeout /t 3 /nobreak >nul

echo.
echo ====================================================
echo 4. KHOI DONG UNIBRIDGE BOT VA MO TRANG WEB
echo Tu dong khoi dong lai neu app.py dung.
echo Nhan Ctrl+C de dung chuong trinh.
echo ====================================================

if not exist "%~dp0app.py" (
    echo [LOI] Khong tim thay app.py trong:
    echo %~dp0
    pause
    exit /b 1
)

:: Mo trang web trong mot tién trinh rieng biet
echo Dang mo trang web...
start "" "http://localhost:%PORT%"

:loop
echo.
echo [%date% %time%] Dang chay app.py...

"%PYTHON%" "%~dp0app.py"

set "EXITCODE=%ERRORLEVEL%"
echo.
echo [CANH BAO] app.py da dung. Ma loi: %EXITCODE%
echo Tu khoi dong lai sau 5 giay...
timeout /t 5 /nobreak >nul
goto loop