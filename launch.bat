@echo off
cd /d "%~dp0"

:: Set up virtual env
if not exist "venvGNSSProject" (
    echo ============================================================
    echo CRITICAL ERROR: Environment 'venvGNSSProject' is missing.
    echo Please run setup first using: py app.py
    echo ============================================================
    pause
    exit /b 1
)

echo [INFO] 1/2. Ensuring backend dependencies are installed...
call venvGNSSProject\Scripts\activate
python -m pip install -r requirements.txt

echo [INFO] 2/2. Starting server and launching dashboard...
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8000"
python server.py

pause