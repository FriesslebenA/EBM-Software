@echo off
setlocal
echo === EBM Slicer Build Fix v2 ===

:: Try to create venv with uv first (it's faster)
echo Erstelle virtuelle Umgebung...
uv venv .venv --python 3.12
if errorlevel 1 (
    echo uv venv fehlgeschlagen, versuche python -m venv...
    python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo FEHLER: .venv konnte nicht erstellt werden.
    pause
    exit /b 1
)

:: Install dependencies using uv pip (more reliable)
echo Installiere Abhängigkeiten...
.venv\Scripts\python -m uv pip install numpy Pillow PySide6 pyinstaller==6.20.0
if errorlevel 1 (
    echo uv pip fehlgeschlagen, versuche standard pip...
    .venv\Scripts\python -m pip install numpy Pillow PySide6 pyinstaller==6.20.0
)

:: Run Build
echo.
echo Starte Build...
.venv\Scripts\python -m PyInstaller --clean --noconfirm abs_path_optimizer.spec

if errorlevel 1 (
    echo.
    echo Fehler: Build fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo Build erfolgreich!
echo EXE-Ordner: dist\SequenceOptimiser
echo.
pause
