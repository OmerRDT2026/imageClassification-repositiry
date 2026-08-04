@echo off
setlocal EnableDelayedExpansion

REM ═══════════════════════════════════════════════════════════════════
REM  Multispectral Image Analysis — Launcher
REM  Double-click this file. That's it.
REM ═══════════════════════════════════════════════════════════════════

set "PROJECT_DIR=%~dp0"
set "BACKEND_DIR=%PROJECT_DIR%backend"
set "REQUIREMENTS_FILE=%PROJECT_DIR%requirements.txt"
set "RUNTIME_DIR=%PROJECT_DIR%python_runtime"
set "PYTHON_EXE="

REM ── STEP 1: Check for bundled Python runtime ─────────────────────
REM The installer may place python.exe in a subfolder, so search
REM all likely locations inside python_runtime\.
if exist "%RUNTIME_DIR%\python.exe" (
    echo Found bundled Python runtime.
    set "PYTHON_EXE=%RUNTIME_DIR%\python.exe"
    goto :deps_check
)
if exist "%RUNTIME_DIR%\Python311\python.exe" (
    echo Found bundled Python runtime in Python311 subfolder.
    set "PYTHON_EXE=%RUNTIME_DIR%\Python311\python.exe"
    goto :deps_check
)
if exist "%RUNTIME_DIR%\Python\python.exe" (
    echo Found bundled Python runtime in Python subfolder.
    set "PYTHON_EXE=%RUNTIME_DIR%\Python\python.exe"
    goto :deps_check
)
REM Deep search: walk the runtime dir for any python.exe
for /r "%RUNTIME_DIR%" %%F in (python.exe) do (
    if "!PYTHON_EXE!"=="" (
        echo Found bundled Python at %%F
        set "PYTHON_EXE=%%F"
    )
)
if not "!PYTHON_EXE!"=="" goto :deps_check

REM ── STEP 2: Check for a REAL system Python ───────────────────────
python --version >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%V in ('python --version 2^>^&1') do set "PY_VER=%%V"
    echo !PY_VER! | findstr /i "Python 3" >nul
    if not errorlevel 1 (
        echo Found real system Python: !PY_VER!
        set "PYTHON_EXE=python"
        goto :deps_check
    )
)

py --version >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%V in ('py --version 2^>^&1') do set "PY_VER=%%V"
    echo !PY_VER! | findstr /i "Python 3" >nul
    if not errorlevel 1 (
        echo Found Python via py launcher: !PY_VER!
        set "PYTHON_EXE=py"
        goto :deps_check
    )
)

REM ── STEP 3: No Python found — download and install ───────────────
echo.
echo No Python installation was found on this computer.
echo Downloading Python 3.11.9 (~25 MB, one-time only)...
echo This requires an internet connection.
echo.

ping -n 1 8.8.8.8 >nul 2>nul
if errorlevel 1 (
    echo ERROR: No internet connection detected.
    echo Please connect to the internet and try again.
    pause
    exit /b 1
)

mkdir "%RUNTIME_DIR%" 2>nul

if exist "%ProgramFiles(x86)%" (
    set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
) else (
    set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe"
)

set "PY_INSTALLER=%RUNTIME_DIR%\python_installer.exe"
echo Downloading from %PY_URL%...
powershell -Command "& { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' }"

if not exist "%PY_INSTALLER%" (
    echo ERROR: Download failed. Check your internet connection.
    pause
    exit /b 1
)

echo Installing Python into "%RUNTIME_DIR%"...
echo This will take about 60 seconds, please wait.
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 TargetDir="%RUNTIME_DIR%" Include_launcher=0 Include_test=0

REM Wait a moment for installer to fully finish writing files
timeout /t 5 /nobreak >nul

del "%PY_INSTALLER%" >nul 2>nul

REM Find python.exe wherever the installer put it
for /r "%RUNTIME_DIR%" %%F in (python.exe) do (
    if "!PYTHON_EXE!"=="" (
        set "PYTHON_EXE=%%F"
    )
)

if "!PYTHON_EXE!"=="" (
    echo.
    echo ERROR: Python was downloaded and installed but python.exe
    echo could not be found inside "%RUNTIME_DIR%".
    echo Contents of runtime folder:
    dir "%RUNTIME_DIR%" /b
    echo.
    echo Please install Python manually from https://python.org
    pause
    exit /b 1
)

echo Python installed successfully at !PYTHON_EXE!

REM ── STEP 4: Verify backend exists ────────────────────────────────
:deps_check
if "!PYTHON_EXE!"=="" (
    echo ERROR: No Python found. Cannot continue.
    pause
    exit /b 1
)

if not exist "%BACKEND_DIR%\app.py" (
    echo ERROR: Could not find "%BACKEND_DIR%\app.py"
    echo Make sure run.bat is in the project root alongside index.html.
    pause
    exit /b 1
)

REM ── STEP 5: Install dependencies if not already present ──────────
"!PYTHON_EXE!" -c "import flask, geopandas, rasterio, sklearn" >nul 2>nul
if not errorlevel 1 (
    echo Dependencies already installed. Starting server...
    goto :start_server
)

if not exist "%REQUIREMENTS_FILE%" (
    echo WARNING: requirements.txt not found - skipping dependency install.
    goto :start_server
)

echo.
echo Installing dependencies (one-time, may take 2-5 minutes)...
echo Please wait - do not close this window.
echo.

"!PYTHON_EXE!" -m pip install fiona==1.9.5 --quiet --disable-pip-version-check --no-warn-script-location
"!PYTHON_EXE!" -m pip install -r "%REQUIREMENTS_FILE%" --quiet --disable-pip-version-check --no-warn-script-location
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install packages.
    echo Run this manually to see the full error:
    echo "!PYTHON_EXE!" -m pip install -r "%REQUIREMENTS_FILE%"
    echo.
    pause
    exit /b 1
)
echo Dependencies installed successfully.

REM ── STEP 6: Start server ─────────────────────────────────────────
:start_server
echo.
echo Starting the server...
start "Multispectral Image Analysis - Keep This Window Open" cmd /k "cd /d "%BACKEND_DIR%" && "!PYTHON_EXE!" app.py"

echo Waiting for server to be ready...
set "READY=0"
for /l %%i in (1,1,40) do (
    powershell -Command "try{Invoke-WebRequest -Uri http://localhost:5000/api/health -UseBasicParsing -TimeoutSec 1|Out-Null;exit 0}catch{exit 1}" >nul 2>nul
    if not errorlevel 1 (
        set "READY=1"
        goto :done_waiting
    )
    timeout /t 1 /nobreak >nul
)
:done_waiting

if "!READY!"=="1" (
    echo.
    echo ============================================================
    echo   Server is ready^^!
    echo   ALWAYS use this address - do NOT open index.html directly:
    echo   http://localhost:5000
    echo ============================================================
    echo.
    set "OPENED=0"
    if "!OPENED!"=="0" if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
        start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" "http://localhost:5000"
        set "OPENED=1"
    )
    if "!OPENED!"=="0" if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
        start "" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "http://localhost:5000"
        set "OPENED=1"
    )
    if "!OPENED!"=="0" if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
        start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "http://localhost:5000"
        set "OPENED=1"
    )
    if "!OPENED!"=="0" if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
        start "" "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" "http://localhost:5000"
        set "OPENED=1"
    )
    if "!OPENED!"=="0" (
        start "" "http://localhost:5000"
    )
) else (
    echo.
    echo Server did not respond after 40 seconds.
    echo Check the server window for error details.
    echo If it looks like it started, open your browser manually and go to:
    echo http://localhost:5000
    echo.
)

pause
