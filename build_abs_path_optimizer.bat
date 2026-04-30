@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo.
echo Verwende Python: %PYTHON_EXE%
echo.
echo Baue Sequence optimiser mit uv + Python 3.12...
"%PYTHON_EXE%" -m uv run --python 3.12 --with pyinstaller==6.20.0 --with numpy --with pillow --with PySide6 python -m PyInstaller --clean --noconfirm abs_path_optimizer.spec
if errorlevel 1 (
    echo.
    echo Fehler: PyInstaller-Build fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo Fertig.
echo EXE-Ordner: dist\SequenceOptimiser
echo.
pause
