@echo off
cd /d "%~dp0"

:: had to chnage from port 8000 to a less common port to try and avoid overlap of running servers.
set PORT=8421

:: Set up virtual env
if not exist "venvGNSSProject" (
    echo ============================================================
    echo CRITICAL ERROR: Environment 'venvGNSSProject' is missing.
    echo Please run setup first using: py app.py
    echo ============================================================
    pause
    exit /b 1
)

echo [INFO] 1/3. Ensuring backend dependencies are installed...
call venvGNSSProject\Scripts\activate
python -m pip install -r requirements.txt

echo [INFO] 2/3. Checking port %PORT% is free...
netstat -aon | findstr ":%PORT%" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo ============================================================
    echo WARNING: Port %PORT% is already in use by another program.
    echo This is almost always a leftover server from a DIFFERENT
    echo project still running in another terminal window.
    echo.
    echo Close that other terminal window, then run launch.bat again.
    echo ============================================================
    pause
    exit /b 1
)
 
echo [INFO] 3/3. Starting server and launching dashboard...
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:%PORT%"
python server.py
 
pause