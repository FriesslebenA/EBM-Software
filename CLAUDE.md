# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Desktop sequence optimizer for EBM (Electron Beam Melting) additive manufacturing. Loads ABS, B99, ZIP, and STEP/STP inputs, reorders the scan sequence using several scientific optimization modes, and exports results as ZIP packages.

## Running and building

```powershell
# Run directly
python abs_path_optimizer.py

# Run with pre-selected files and a specific mode
python abs_path_optimizer.py file1.txt --mode dispersion_maximisation

# Build SequenceOptimiser.exe (produces dist/SequenceOptimiser.exe + .zip)
build_abs_path_optimizer.bat

# Build StepLayerGenerator.exe (requires uv; uses Python 3.12 + cadquery)
build_step_layer_generator.bat
```

The `start_abs_path_optimizer.bat` launcher auto-detects `.venv\Scripts\python.exe` before falling back to system Python.

**Dependencies:**
```powershell
pip install -r requirements.txt          # numpy, Pillow, PySide6  (main app)
# cadquery (step helper) is NOT installed here — see STEP Import below
```

## Self-tests

`run_self_tests()` in `abs_path_optimizer.py` and `run_selftest()` in `step_layer_generator.py` both run automatically on startup. To invoke manually:

```powershell
python -c "import abs_path_optimizer as m; m.run_self_tests(); print('OK')"
python -c "import step_layer_generator as m; m.run_selftest(); print('OK')"
```

## Architecture

### Files

| File | Purpose |
|---|---|
| `abs_path_optimizer.py` | Main application (~7 300 lines) — UI, optimization, viewer |
| `step_layer_generator.py` | STEP/STP slicer helper (~834 lines) — runs as subprocess |
| `abs_path_optimizer.spec` | PyInstaller spec for SequenceOptimiser.exe |
| `step_layer_generator.spec` | PyInstaller spec for StepLayerGenerator.exe |
| `requirements.txt` | Main app deps (numpy, PySide6) |
| `requirements-step-helper.txt` | STEP helper dep (cadquery==2.7.0) |

### Two separate UIs

**Main window** — `ComparisonApp(tk.Tk)`. A tkinter desktop app for file selection, optimization, statistics comparison, and ZIP export. Heavy work (optimization, animation plan) runs in `ProcessPoolExecutor` with a `spawn` context so tkinter state never leaks into worker processes.

**Interactive viewer** — `run_interactive_viewer()`. A PySide6 window that is always launched as a *separate subprocess* (re-invoking the same script with `--viewer-payload <json-file>`). The viewer backend can be controlled via `--viewer-backend` (raster/opengl/auto) or the `SEQUENCE_VIEWER_BACKEND` environment variable. Default is `raster`.

### Viewer rendering backends

Two backends, with automatic fallback:

- **Raster** (default): QPainter-based pixel rendering. The static background point cloud is pre-rendered in a background `ThreadPoolExecutor` thread as a `QImage`; overlay (trail, head) is painted on top each frame. Idle redraw is triggered after 200 ms with more detail (`VIEWER_RASTER_IDLE_REDRAW_MS`).
- **OpenGL**: `QtOpenGLWidgets.QOpenGLWidget` with raw GL calls, batched in chunks of 1 000 000 points (`VIEWER_GL_MAX_BATCH_POINTS`). If the driver fails to produce a visible image the window sets `request_backend_fallback = True` and the launcher retries with raster.

Note: `pyqtgraph` is no longer a dependency in V2. The viewer is pure PySide6.

### STEP import pipeline

STEP/STP files cannot be sliced inside the main Python environment because `cadquery` requires Python 3.12 and has a heavy OCC dependency. The integration is therefore always out-of-process:

- **Source mode**: `resolve_step_helper_command()` returns a `uv run --python 3.12 --with cadquery==2.7.0 -- python step_layer_generator.py` invocation. `uv` must be on PATH.
- **Frozen mode** (EXE): the function looks for `StepLayerGenerator.exe` next to `SequenceOptimiser.exe` and calls it directly.

`generate_step_layer_artifact()` writes the result to a temp directory as a ZIP + JSON manifest, which the main app then reads and feeds into the existing optimization pipeline.

Inside `step_layer_generator.py`, slicing works as:
1. `cadquery` opens the STEP file and slices it at each Z height.
2. `collect_face_rings()` → `generate_boundary_points_for_rings()` produces closed boundary contours.
3. `generate_infill_points_for_rings()` / `generate_infill_points_for_faces()` generates scan-line infill via even-odd rule (`_point_in_rings_odd_even`).
4. Optional support layers (`build_support_layers()`, `apply_support_layers()`) use the first part outline as boundary and fill with an alternating hedge pattern.
5. `write_layer_archive()` packs all layers as B99-format text entries into a ZIP.

### Data flow (ABS/B99 path)

1. **Input**: plain `.txt`/`.abs` files, or ZIP archives with B99 entries (name pattern `(\d+)0(\d)1`; second digit = type: 1=infill, 2=boundary, 3=combo).
2. **Parsing**: `load_points_from_file` / `load_points_from_zip_entry` → `_parse_points_from_lines` → `List[(x, y)]` in normalized coordinates.
3. **Optimization**: `optimize_path()` dispatches to the selected algorithm.
4. **Output reconstruction**: `build_output_lines()` splices optimized points back into the original file (preserving headers/comments).
5. **Export**: `save_results_as_zip()` writes a new ZIP; entries under `figure files/` keep their original names unchanged (`_archive_member_keeps_original_name`).

### Optimization modes

| Canonical ID | Description |
|---|---|
| `direct_visualisation` | No reordering — reference/inspection |
| `local_greedy` | Greedy nearest-neighbour with memory repulsion |
| `dispersion_maximisation` | Maximises distance from recent visit history |
| `deterministic_grid_dispersion` | Virtual lattice, deterministic bucket order |
| `stochastic_grid_dispersion` | Same lattice, stochastic intra-bucket sampling |
| `density_adaptive_sampling` | Density-aware stochastic, penalises crowded zones |
| `ghost_beam_scanning` | Reorders within stripes to mimic primary + delayed beam |
| `interlaced_stripe_scanning` | Interlaced forward/backward jumps inside detected stripes |
| `raster_zigzag` | Boustrophedon row sort — alternating scan direction per row |
| `hilbert_curve` | Hilbert space-filling curve traversal; `hilbert_order` (2–7) controls grid resolution |
| `spot_ordered` | Raster pre-sort split into `spot_skip+1` interleaved passes; `spot_skip` (1–20) |
| `island_raster` | Chessboard macro-cell segmentation (size = `grid_spacing`), two-phase traversal |

Legacy aliases (e.g. `greedy`, `spread`, `grid_spread`) are mapped via `MODE_ALIASES`.

### Performance notes

- **Canvas reset** (`_reset_canvas_raster`): Uses numpy + Pillow to build the background image as a single PNG → base64 → `tk.PhotoImage`, replacing N×`photo.put()` Tcl roundtrips. Incremental animation calls still use `photo.put()`.
- **dispersion_maximisation candidate selection** (`_collect_farthest_candidate_ids`): O(N) numpy `argpartition` instead of O(N log N) `sorted()`.
- **Scoring loop** (`optimize_path` greedy/dispersion): numpy broadcasting over all candidates replaces per-candidate Python loop calls to `_score_candidate` / `_spread_score_candidate`.

### Coordinate system

ABS files use normalized coordinates. Multiply by `DISPLAY_COORDINATE_SCALE_MM = 60.0` to get mm. Build plate is 120 × 120 mm. `step_layer_generator.py` works internally in mm and converts to ABS units via `MM_TO_ABS_SCALE = 1/60`.

### Key dataclasses

- `ModeSpec` — immutable mode descriptor (label, description, visible parameters).
- `InputSource` — one file to process (kind, path, archive member).
- `ProcessedFileResult` — original + optimized points with computed statistics.
- `AnimationPlan` — pre-computed frame schedule for viewer playback.
- `SliceLayerResult` — one sliced Z-layer from the STEP helper (boundary + infill in mm).
- `SupportFootprint` — boundary contour + ring list for support layer generation.
