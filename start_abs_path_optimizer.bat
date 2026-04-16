@echo off
setlocal

cd /d "%~dp0"

set "SCRIPT=abs_path_optimizer.py"

if not exist "%SCRIPT%" (
    echo Die Datei "%SCRIPT%" fuer Sequence optimiser wurde im Startordner nicht gefunden.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%SCRIPT%"
    set "EXITCODE=%ERRORLEVEL%"
    if "%EXITCODE%"=="0" exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%"
    set "EXITCODE=%ERRORLEVEL%"
    if "%EXITCODE%"=="0" exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%"
    set "EXITCODE=%ERRORLEVEL%"
    if "%EXITCODE%"=="0" exit /b 0
)

echo.
echo Sequence optimiser konnte nicht gestartet werden.
echo Geprueft wurden:
echo   1. .venv\Scripts\python.exe
echo   2. py -3
echo   3. python
echo.
echo Bitte pruefen Sie, ob Python mit Tkinter korrekt installiert ist.
pause
exit /b 1
