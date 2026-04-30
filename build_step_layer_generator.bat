@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo.
echo Installiere uv (falls nicht vorhanden)...
"%PYTHON_EXE%" -m pip install uv

echo.
echo Baue StepLayerGenerator mit uv + Python 3.12...
"%PYTHON_EXE%" -m uv run --python 3.12 --with pyinstaller==6.19.0 --with cadquery==2.7.0 python -m PyInstaller --clean --noconfirm step_layer_generator.spec

if errorlevel 1 (
    echo.
    echo Fehler: StepLayerGenerator-Build fehlgeschlagen.
    exit /b 1
)

if not exist "dist\StepLayerGenerator.exe" (
    echo.
    echo Fehler: dist\StepLayerGenerator.exe wurde nicht erzeugt.
    exit /b 1
)

echo.
echo Fertig.
echo EXE: dist\StepLayerGenerator.exe
