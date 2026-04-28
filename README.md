# EBM-Software

Sequence optimiser is a desktop tool for loading ABS, B99, ZIP, and STEP inputs, reordering the scan sequence with several scientific optimisation modes, comparing path statistics, and exporting the optimised results as ZIP packages.

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

The main window handles file selection, processing, statistics, and ZIP export. The visual inspection runs in a separate Qt viewer that is launched from the application and provides:

- mouse-wheel zoom
- left-mouse panning
- arrow-key navigation
- `Home` to reset the view
- animation playback with speed and trail-length controls
- exact raster rendering as the default backend
- optional OpenGL rendering path when the graphics driver behaves reliably
- a short navigation preview in raster mode, followed by an exact redraw after idle

## STEP Import

STEP and STP files can be sliced directly inside the app. The import dialog asks for:

- point spacing in mm
- layer height in mm
- optional support layer count below the part

The helper generates B99 layer ZIP archives with separate infill and boundary files, then feeds them into the existing optimisation pipeline. Optional support layers reuse the first part outline as the support boundary and fill that footprint with an alternating hedge pattern before the shifted model layers.

## Build

Install the dependencies and create the Windows executable with:

```powershell
python -m pip install -r requirements.txt
build_abs_path_optimizer.bat
```

The PyInstaller build produces:

- `dist/SequenceOptimiser.exe`
- `dist/StepLayerGenerator.exe`
- `dist/SequenceOptimiser_portable.zip`

For source-mode STEP import, `uv` must be available so the app can launch the Python-3.12 STEP helper on demand.
