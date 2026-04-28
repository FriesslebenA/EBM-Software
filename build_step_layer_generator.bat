@echo off
setlocal

cd /d "%~dp0"

echo.
echo Baue StepLayerGenerator mit uv + Python 3.12...
uv run --python 3.12 --with pyinstaller==6.19.0 --with cadquery==2.7.0 pyinstaller --clean --noconfirm step_layer_generator.spec
if errorlevel 1 (
    echo.
    echo Fehler: StepLayerGenerator-Build fehlgeschlagen.
    exit /b 1
)

if not exist "dist\\StepLayerGenerator.exe" (
    echo.
    echo Fehler: dist\\StepLayerGenerator.exe wurde nicht erzeugt.
    exit /b 1
)

echo.
echo Fertig.
echo EXE: dist\StepLayerGenerator.exe
