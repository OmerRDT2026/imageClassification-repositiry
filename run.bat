@echo off
setlocal

REM Double-click this file to start the app.
REM It checks Python is available, installs any missing dependencies,
REM starts the backend server in a visible window (so errors are seen),
REM waits until it actually responds, then opens your browser.

set "PROJECT_DIR=%~dp0"
set "BACKEND_DIR=%PROJECT_DIR%backend"
set "REQUIREMENTS_FILE=%PROJECT_DIR%requirements.txt"

echo Checking for Python...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: Python was not found on this computer.
    echo Install Python from https://python.org and try again.
    echo ^(During install, tick "Add Python to PATH".^)
    echo.
    pause
    exit /b 1
)

if not exist "%BACKEND_DIR%\app.py" (
    echo.
    echo ERROR: Could not find "%BACKEND_DIR%\app.py"
    echo Make sure run.bat is in the project root, alongside index.html.
    echo.
    pause
    exit /b 1
)

if exist "%REQUIREMENTS_FILE%" (
    echo Checking dependencies ^(this can take a minute the first time^)...
    REM Force-reinstall fiona at the pinned version to prevent the
    REM "module 'fiona' has no attribute 'path'" conflict that happens
    REM when a newer incompatible fiona is already installed.
    python -m pip install fiona==1.9.5 --quiet --disable-pip-version-check
    python -m pip install -r "%REQUIREMENTS_FILE%" --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install required packages. Scroll up for details.
        echo.
        pause
        exit /b 1
    )
) else (
    echo WARNING: requirements.txt not found — skipping dependency check.
)

echo Starting the server...
start "Multispectral Image Analysis Server - Keep This Window Open" cmd /k "cd /d "%BACKEND_DIR%" && python app.py"

echo Waiting for the server to be ready...
set "READY=0"
for /l %%i in (1,1,30) do (
    powershell -Command "try { Invoke-WebRequest -Uri http://localhost:5000/api/health -UseBasicParsing -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
    if not errorlevel 1 (
        set "READY=1"
        goto :done_waiting
    )
    timeout /t 1 /nobreak >nul
)
:done_waiting

if "%READY%"=="1" (
    echo Server is up. Opening browser...
    start "" http://localhost:5000
) else (
    echo.
    echo The server did not respond after 30 seconds.
    echo Check the "Multispectral Image Analysis Server" window for errors.
    echo.
)

pause