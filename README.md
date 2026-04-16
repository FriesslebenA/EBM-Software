# EBM-Software

Sequence optimiser is a desktop tool for loading ABS and B99 point files, reordering the scan sequence with several scientific optimisation modes, comparing path statistics, and exporting the optimised results as ZIP packages.

## Start

Use the launcher:

```bat
start_abs_path_optimizer.bat
```

Or start it directly with Python:

```powershell
python abs_path_optimizer.py
```

## Interactive Viewer

The main window handles file selection, processing, statistics, and ZIP export. The visual inspection runs in a separate Qt/PyQtGraph viewer that is launched from the application and provides:

- mouse-wheel zoom
- left-mouse panning
- arrow-key navigation
- `Home` to reset the view
- animation playback with speed and trail-length controls

## Build

Install the dependencies and create the Windows executable with:

```powershell
python -m pip install -r requirements.txt
build_abs_path_optimizer.bat
```

The PyInstaller build produces:

- `dist/SequenceOptimiser.exe`
- `dist/SequenceOptimiser_portable.zip`
