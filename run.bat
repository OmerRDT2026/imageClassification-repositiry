@echo off
setlocal EnableDelayedExpansion

REM ═══════════════════════════════════════════════════════════════════
REM  Multispectral Image Analysis — Launcher
REM  Double-click this file. That's it.
REM
REM  What this does:
REM  1. Looks for Python bundled in the "python_runtime" folder first
REM  2. Falls back to any system Python if found
REM  3. If neither exists, downloads the embedded Python runtime
REM     automatically (requires internet, one-time only, ~25MB)
REM  4. Installs all dependencies into the bundled runtime
REM  5. Starts the server and opens the browser
REM ═══════════════════════════════════════════════════════════════════

set "PROJECT_DIR=%~dp0"
set "BACKEND_DIR=%PROJECT_DIR%backend"
set "REQUIREMENTS_FILE=%PROJECT_DIR%requirements.txt"
set "RUNTIME_DIR=%PROJECT_DIR%python_runtime"
set "PYTHON_EXE=%RUNTIME_DIR%\python.exe"
set "PIP_EXE=%RUNTIME_DIR%\Scripts\pip.exe"

REM ── STEP 1: Check for bundled Python runtime ──────────────────────
if exist "%PYTHON_EXE%" (
    echo Found bundled Python runtime.
    goto :deps_check
)

REM ── STEP 2: Check for system Python ──────────────────────────────
where python >nul 2>nul
if not errorlevel 1 (
    REM Only trust system Python if its version is known to have
    REM prebuilt packages ("wheels") for our dependencies (rasterio,
    REM fiona/pyogrio, Pillow, etc). Very new Python releases often
    REM do not yet, which forces pip to compile from source and
    REM fail (needs GDAL headers, a C compiler, and other things a
    REM normal machine does not have installed).
    python -c "import sys; exit(0 if (3,9) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>nul
    if not errorlevel 1 (
        echo Found compatible system Python.
        set "PYTHON_EXE=python"
        set "PIP_EXE=python -m pip"
        goto :deps_check
    ) else (
        echo System Python found but its version is not supported
        echo by this app's dependencies ^(needs Python 3.9-3.12^).
        echo Using a bundled, known-compatible Python instead.
    )
)

REM ── STEP 3: No Python found — download embedded runtime ──────────
echo.
echo Python was not found on this computer.
echo Downloading a self-contained Python runtime (~25 MB, one-time only)...
echo This requires an internet connection.
echo.

REM Check internet connectivity first. Using HTTP rather than ping: many
REM institutional networks (schools, universities, corporate) block outbound
REM ICMP for security reasons even when normal HTTP/internet access works
REM fine, which would make a ping-based check falsely report no connection.
powershell -Command "try{Invoke-WebRequest -Uri https://www.python.org -UseBasicParsing -TimeoutSec 5|Out-Null;exit 0}catch{exit 1}" >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: No internet connection detected.
    echo Please either:
    echo   A^) Connect to the internet and run this again, OR
    echo   B^) Install Python from https://python.org
    echo      ^(tick "Add Python to PATH" during install^)
    echo.
    pause
    exit /b 1
)

REM Create runtime dir
mkdir "%RUNTIME_DIR%" 2>nul

REM Determine Windows architecture
reg Query "HKLM\Hardware\Description\System\CentralProcessor\0" /v "Identifier" | find /i "x86" >nul 2>nul
if errorlevel 1 (
    set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    set "PY_INSTALLER=%RUNTIME_DIR%\python_installer.exe"
) else (
    set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe"
    set "PY_INSTALLER=%RUNTIME_DIR%\python_installer.exe"
)

echo Downloading Python 3.11.9...
powershell -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' }"
if not exist "%PY_INSTALLER%" (
    echo.
    echo ERROR: Download failed. Check your internet connection and try again.
    echo If the problem persists, install Python manually from https://python.org
    echo.
    pause
    exit /b 1
)

echo Installing Python into "%RUNTIME_DIR%"...
echo ^(This will take about 60 seconds^)
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 ^
    TargetDir="%RUNTIME_DIR%" Include_launcher=0 Include_test=0
if errorlevel 1 (
    echo.
    echo ERROR: Python installation failed.
    echo Try installing manually from https://python.org
    echo.
    del "%PY_INSTALLER%" >nul 2>nul
    pause
    exit /b 1
)
del "%PY_INSTALLER%" >nul 2>nul
set "PYTHON_EXE=%RUNTIME_DIR%\python.exe"
set "PIP_EXE=%RUNTIME_DIR%\Scripts\pip.exe"
echo Python installed successfully.

REM ── STEP 4: Install dependencies ─────────────────────────────────
:deps_check
if not exist "%BACKEND_DIR%\app.py" (
    echo.
    echo ERROR: Could not find "%BACKEND_DIR%\app.py"
    echo Make sure run.bat is in the project root alongside index.html.
    echo.
    pause
    exit /b 1
)

REM Check if deps are already installed by testing flask import
"%PYTHON_EXE%" -c "import flask, geopandas, rasterio, sklearn" >nul 2>nul
if not errorlevel 1 (
    echo Dependencies already installed. Skipping...
    goto :start_server
)

if not exist "%REQUIREMENTS_FILE%" (
    echo WARNING: requirements.txt not found — skipping dependency install.
    goto :start_server
)

echo Installing dependencies ^(one-time, may take 2-5 minutes^)...
echo Please wait — do not close this window.
echo.

REM No forced fiona install here -- app.py uses the pyogrio engine
REM for reading vector files, which does not need fiona at all.
"%PYTHON_EXE%" -m pip install -r "%REQUIREMENTS_FILE%" --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install packages. Check your internet connection.
    echo.
    pause
    exit /b 1
)
echo Dependencies installed successfully.

REM ── STEP 5: Start server ──────────────────────────────────────────
:start_server
echo.
echo Starting the server...
start "Multispectral Image Analysis - Keep This Window Open" cmd /k ^
    "cd /d "%BACKEND_DIR%" && "%PYTHON_EXE%" app.py"

echo Waiting for server to be ready...
set "READY=0"
for /l %%i in (1,1,40) do (
    powershell -Command ^
        "try{Invoke-WebRequest -Uri http://localhost:5000/api/health -UseBasicParsing -TimeoutSec 1|Out-Null;exit 0}catch{exit 1}" ^
        >nul 2>nul
    if not errorlevel 1 (
        set "READY=1"
        goto :done_waiting
    )
    timeout /t 1 /nobreak >nul
)
:done_waiting

if "!READY!"=="1" (
    echo Server is ready. Opening browser...
    start "" http://localhost:5000
) else (
    echo.
    echo Server did not respond after 40 seconds.
    echo Check the server window that opened for error details.
    echo.
)

pause