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
echo Baue Sequence optimiser mit PyInstaller...
"%PYTHON_EXE%" -m PyInstaller --clean --noconfirm abs_path_optimizer.spec
if errorlevel 1 (
    echo.
    echo Fehler: PyInstaller-Build fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo Baue StepLayerGenerator...
call build_step_layer_generator.bat
if errorlevel 1 (
    echo.
    echo Fehler: StepLayerGenerator-Build fehlgeschlagen.
    pause
    exit /b 1
)

if exist "dist\SequenceOptimiser\SequenceOptimiser.exe" if exist "dist\StepLayerGenerator.exe" (
    echo.
    echo Erzeuge ZIP-Paket...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\\SequenceOptimiser','dist\\StepLayerGenerator.exe' -DestinationPath 'dist\\SequenceOptimiser_portable.zip' -Force"
)

echo.
echo Fertig.
echo EXE-Ordner: dist\SequenceOptimiser
if exist "dist\StepLayerGenerator.exe" echo STEP-Helper: dist\StepLayerGenerator.exe
if exist "dist\SequenceOptimiser_portable.zip" echo ZIP: dist\SequenceOptimiser_portable.zip
echo.
pause
