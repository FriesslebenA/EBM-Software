import argparse
import json
import math
import multiprocessing as mp
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from array import array
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import mean, median, pstdev
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
try:
    from PySide6 import QtCore, QtGui, QtOpenGL, QtOpenGLWidgets, QtWidgets
except ImportError:
    class Dummy: pass
    QtCore = QtGui = QtOpenGL = QtOpenGLWidgets = QtWidgets = Dummy


Point = Tuple[float, float]
Stats = Dict[str, float]
ProgressCallback = Optional[Callable[[float, str], None]]

W1_DEFAULT = 1.0
W2_DEFAULT = 0.5
MEMORY_DEFAULT = 4
GRID_SPREAD_DEFAULT_SPACING = 0.1
GRID_SPREAD_DEFAULT_RECENT_PERCENT = 10.0
GRID_SPREAD_AGE_DECAY_DEFAULT = 0.9
GHOST_BEAM_DEFAULT_DELAY = 2
INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP = 3
INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP = 2
APP_DISPLAY_NAME = "Sequence optimiser"
APP_BUILD_NAME = "SequenceOptimiser"
VIEWER_PAYLOAD_PREFIX = "sequence_viewer_payload_"
VIEWER_POINTS_PER_SECOND_BASE = 3.0
VIEWER_POINTS_PER_SECOND_MAX = 3000.0
VIEWER_TIMER_INTERVAL_MS = 33
VIEWER_DEFAULT_GRADIENT_WINDOW = 1000
VIEWER_PLAYBACK_GRADIENT_CAP = VIEWER_DEFAULT_GRADIENT_WINDOW
DISPLAY_COORDINATE_SCALE_MM = 60.0
BUILD_PLATE_WIDTH_MM = 120.0
BUILD_PLATE_DEPTH_MM = 120.0
DISPLAY_POINT_SPACING_MM = 0.1
STEP_POINT_SPACING_MM_DEFAULT = DISPLAY_POINT_SPACING_MM
STEP_LAYER_HEIGHT_MM_DEFAULT = 0.1
STEP_SUPPORT_LAYER_COUNT_DEFAULT = 0
VIEWER_POINT_SIZE_MM_MIN = 0.05
VIEWER_POINT_SIZE_MM_MAX = 5.0
VIEWER_POINT_SIZE_MM_DEFAULT = DISPLAY_POINT_SPACING_MM
VIEWER_POINT_SIZE_SLIDER_SCALE = 1000
VIEWER_POINT_SIZE_INPUT_SCALE_UM = 1000.0
VIEWER_VISITED_SIZE_MULTIPLIER = 1.15
VIEWER_TRAIL_SIZE_MULTIPLIER = 1.35
VIEWER_HEAD_SIZE_MULTIPLIER = 1.80
VIEWER_BACKGROUND_COLOR = "#232323"
VIEWER_VISITED_COLOR = "#666666"
VIEWER_HEAD_COLOR = "#ff3030"
VIEWER_NAVIGATION_DYNAMIC_MARKERS = 750
VIEWER_PLAYBACK_DYNAMIC_MARKERS = 2000
VIEWER_IDLE_DYNAMIC_MARKERS = 10000
VIEWER_NAVIGATION_BACKGROUND_MARKERS = 2500
VIEWER_PLAYBACK_BACKGROUND_MARKERS = 6000
VIEWER_AUTO_RESUME_DELAY_MS = 300
VIEWER_SLIDER_DRAG_REFRESH_HZ = 10
VIEWER_SLIDER_DRAG_INTERVAL_MS = max(1, int(round(1000 / VIEWER_SLIDER_DRAG_REFRESH_HZ)))
MODE_PARAMETER_MEMORY = "memory"
MODE_PARAMETER_GRID_SPACING = "grid_spacing"
MODE_PARAMETER_RECENT_PERCENT = "recent_percent"
MODE_PARAMETER_AGE_DECAY = "age_decay"
MODE_PARAMETER_GHOST_DELAY = "ghost_delay"
MODE_PARAMETER_FORWARD_JUMP = "forward_jump"
MODE_PARAMETER_BACKWARD_JUMP = "backward_jump"
MODE_PARAMETER_HILBERT_ORDER = "hilbert_order"
MODE_PARAMETER_SPOT_SKIP = "spot_skip"
MODE_PARAMETER_SPIRAL_DIRECTION = "spiral_direction"
MODE_PARAMETER_HATCH_SPACING = "hatch_spacing"
HILBERT_ORDER_DEFAULT = 4
SPOT_SKIP_DEFAULT = 2
SPIRAL_DIRECTION_DEFAULT = "inward"
HATCH_SPACING_UM_DEFAULT = 200.0
ANIMATION_BASE_POINTS_PER_SECOND = 3.0
ANIMATION_MIN_MULTIPLIER = 1
ANIMATION_MAX_MULTIPLIER = 1000
ANIMATION_MAX_FPS = 30.0
TRAIL_MIN_POINTS = 1
TRAIL_MAX_POINTS = 64
TRAIL_DEFAULT_POINTS = 4
MAX_RENDER_GRADIENT_BINS = 48
BACKGROUND_POINT_HALF_SIZE = 1
VISITED_POINT_HALF_SIZE = 2
HEAD_POINT_RADIUS = 5
ZIP_ENTRY_NAME_PATTERN = re.compile(r"^(\d+)0(\d)1$", re.IGNORECASE)
ZIP_ENTRY_TYPES = ("infill", "boundary", "combo")
STEP_GENERATED_ZIP_ENTRY_TYPES = ("infill", "boundary")
ZIP_ENTRY_TYPE_LABELS = {
    "infill": "Infill",
    "boundary": "Boundary",
    "combo": "Kombi",
}
STEP_HELPER_BUILD_NAME = "StepLayerGenerator"
STEP_HELPER_SOURCE_PYTHON = "3.12"
STEP_HELPER_CADQUERY_SPEC = "cadquery==2.7.0"


@dataclass(frozen=True)
class ModeSpec:
    canonical_id: str
    label: str
    description: str
    visible_parameters: Tuple[str, ...]


@dataclass(frozen=True)
class ViewerRenderPolicy:
    name: str
    dynamic_marker_budget: int
    background_marker_budget: Optional[int]


@dataclass(frozen=True)
class ViewerRenderStats:
    requested_trail_count: int
    requested_gradient_count: int
    displayed_trail_count: int
    displayed_gradient_count: int
    policy_name: str


VIEWER_RENDER_POLICY_NAVIGATION = ViewerRenderPolicy(
    "navigation",
    VIEWER_NAVIGATION_DYNAMIC_MARKERS,
    VIEWER_NAVIGATION_BACKGROUND_MARKERS,
)
VIEWER_RENDER_POLICY_PLAYBACK = ViewerRenderPolicy(
    "playback",
    VIEWER_PLAYBACK_DYNAMIC_MARKERS,
    VIEWER_PLAYBACK_BACKGROUND_MARKERS,
)
VIEWER_RENDER_POLICY_IDLE = ViewerRenderPolicy("idle_refine", VIEWER_IDLE_DYNAMIC_MARKERS, None)
VIEWER_EMPTY_RENDER_STATS = ViewerRenderStats(
    requested_trail_count=0,
    requested_gradient_count=0,
    displayed_trail_count=0,
    displayed_gradient_count=0,
    policy_name=VIEWER_RENDER_POLICY_IDLE.name,
)


MODE_SPECS: Dict[str, ModeSpec] = {
    "direct_visualisation": ModeSpec(
        canonical_id="direct_visualisation",
        label="Direct Visualisation (Unchanged)",
        description=(
            "This mode preserves the source sequence exactly as provided and performs no spatial reordering at all. "
            "It is intended as a reference condition for direct visual inspection, metric comparison and viewer-based "
            "analysis of the unmodified scan path."
        ),
        visible_parameters=(),
    ),
    "local_greedy": ModeSpec(
        canonical_id="local_greedy",
        label="Local Greedy Optimisation",
        description=(
            "This mode minimises the immediate travel distance while retaining a short-term memory of recently "
            "visited locations. The repulsive memory term discourages immediate revisits to nearby regions, "
            "which yields a locally efficient yet still spatially stabilised traversal."
        ),
        visible_parameters=(MODE_PARAMETER_MEMORY,),
    ),
    "dispersion_maximisation": ModeSpec(
        canonical_id="dispersion_maximisation",
        label="Dispersion Maximisation",
        description=(
            "This mode emphasises spatial separation from the recent visitation history and therefore promotes "
            "global dispersion before local consolidation. It is suited to scan orders where broad area coverage "
            "is more important than the shortest immediate jump."
        ),
        visible_parameters=(MODE_PARAMETER_MEMORY,),
    ),
    "deterministic_grid_dispersion": ModeSpec(
        canonical_id="deterministic_grid_dispersion",
        label="Deterministic Grid Dispersion",
        description=(
            "This mode projects points onto a virtual lattice and traverses occupied grid buckets in a deterministic, "
            "spatially balanced order. Within each bucket, candidates are selected to maximise distance from the "
            "recently visited subset, producing reproducible large-scale dispersion."
        ),
        visible_parameters=(MODE_PARAMETER_GRID_SPACING, MODE_PARAMETER_RECENT_PERCENT),
    ),
    "stochastic_grid_dispersion": ModeSpec(
        canonical_id="stochastic_grid_dispersion",
        label="Stochastic Grid Dispersion",
        description=(
            "This mode uses the same virtual lattice as the deterministic variant but injects controlled stochasticity "
            "when sampling candidates inside each bucket. The result preserves macroscopic dispersion while reducing "
            "systematic ordering artefacts on densely populated grid cells."
        ),
        visible_parameters=(MODE_PARAMETER_GRID_SPACING, MODE_PARAMETER_RECENT_PERCENT),
    ),
    "density_adaptive_sampling": ModeSpec(
        canonical_id="density_adaptive_sampling",
        label="Density-Adaptive Sampling",
        description=(
            "This mode performs a density-aware stochastic traversal that penalises locally crowded regions and "
            "prioritises under-sampled spatial zones. It is intended for sequences where adaptive coverage and "
            "reduced clustering are scientifically preferable to strict deterministic repeatability."
        ),
        visible_parameters=(MODE_PARAMETER_GRID_SPACING, MODE_PARAMETER_AGE_DECAY),
    ),
    "ghost_beam_scanning": ModeSpec(
        canonical_id="ghost_beam_scanning",
        label="Ghost Beam Scanning",
        description=(
            "This mode adapts the paper's primary and delayed secondary beam concept to the existing point cloud by "
            "reordering only the already present points inside each detected stripe. A configurable ghost delay "
            "interleaves forward-leading and delayed-following stripe segments to mimic local reheating behaviour "
            "without generating any new exposure points."
        ),
        visible_parameters=(MODE_PARAMETER_GHOST_DELAY,),
    ),
    "interlaced_stripe_scanning": ModeSpec(
        canonical_id="interlaced_stripe_scanning",
        label="Interlaced Stripe Scanning",
        description=(
            "This mode preserves the stripe topology already encoded in the source sequence and only reorders points "
            "within each detected stripe. The intra-stripe traversal is interlaced in deterministic forward/backward "
            "jump blocks to reduce immediately adjacent consecutive exposures without duplicating any point."
        ),
        visible_parameters=(MODE_PARAMETER_FORWARD_JUMP, MODE_PARAMETER_BACKWARD_JUMP),
    ),
    "raster_zigzag": ModeSpec(
        canonical_id="raster_zigzag",
        label="Raster Zig-Zag",
        description=(
            "This mode reorders points in a boustrophedon raster pattern by detecting the natural scan-line spacing "
            "from the source point cloud and sorting alternating rows in opposite X directions. It serves as a "
            "deterministic O(N log N) reference strategy for uniform area coverage without spatial dispersion."
        ),
        visible_parameters=(),
    ),
    "spot_ordered": ModeSpec(
        canonical_id="spot_ordered",
        label="Spot Ordered (Multipass)",
        description=(
            "This mode applies a raster pre-sort and then splits the sequence into interleaved passes separated by "
            "a configurable skip distance. Adjacent spots in the original raster order are exposed in different "
            "passes, introducing a controlled dwell interval between neighbouring melt events to reduce thermal "
            "accumulation without sacrificing deterministic coverage."
        ),
        visible_parameters=(MODE_PARAMETER_SPOT_SKIP,),
    ),
    "hilbert_curve": ModeSpec(
        canonical_id="hilbert_curve",
        label="Hilbert Curve",
        description=(
            "This mode projects the point cloud onto a two-dimensional Hilbert space-filling curve and visits "
            "points in the resulting index order. The Hilbert mapping maximises spatial locality and cache "
            "coherence while providing a deterministic, reproducible traversal that covers the build area "
            "uniformly at progressively finer scales. The grid resolution is controlled by the order parameter."
        ),
        visible_parameters=(MODE_PARAMETER_HILBERT_ORDER,),
    ),
    "island_raster": ModeSpec(
        canonical_id="island_raster",
        label="Island Raster (Chessboard)",
        description=(
            "This mode partitions the point cloud into a chessboard of square islands and processes them in two "
            "alternating phases: phase-A islands (even diagonal index) are completed before phase-B islands "
            "(odd diagonal index), mimicking the thermal isolation strategy used in industrial island scanning. "
            "Within each island the points are sorted in boustrophedon raster order. The island edge length is "
            "set by the grid spacing parameter."
        ),
        visible_parameters=(MODE_PARAMETER_GRID_SPACING,),
    ),
    "spiral_scan": ModeSpec(
        canonical_id="spiral_scan",
        label="Spirale",
        description=(
            "Sortiert Punkte ringweise nach Abstand zum Schwerpunkt. Der Ring-Index ergibt sich aus "
            "Abstand / Hatch-Abstand. Innerhalb jedes Rings wird nach Winkel sortiert. "
            "Die Richtung kann von außen nach innen oder innen nach außen gewählt werden."
        ),
        visible_parameters=(MODE_PARAMETER_SPIRAL_DIRECTION, MODE_PARAMETER_HATCH_SPACING),
    ),
    "peano_curve": ModeSpec(
        canonical_id="peano_curve",
        label="Peano-Kurve",
        description=(
            "Boustrophedon-Näherung auf einem 3^n × 3^n-Gitter. Punkte werden quantisiert und "
            "zeilenweise sortiert – gerade Zeilen links→rechts, ungerade rechts→links. "
            "Ähnlich der Peano-Kurve ohne vollen rekursiven Aufwand. Auflösung via Ordnung (3^n)."
        ),
        visible_parameters=(MODE_PARAMETER_HILBERT_ORDER,),
    ),
}
MODE_ALIASES = {
    "greedy": "local_greedy",
    "basic": "local_greedy",
    "spread": "dispersion_maximisation",
    "grid_spread": "deterministic_grid_dispersion",
    "random_grid": "stochastic_grid_dispersion",
    "random_noise": "density_adaptive_sampling",
}
OPTIMIZATION_MODES = tuple(MODE_SPECS)


# ---------------------------------------------------------------------------
# Makro-Segmentierungstypen (Stufe 1)
# ---------------------------------------------------------------------------
MACRO_NONE = "keine_segmentierung"
MACRO_CHESSBOARD = "schachbrett"
MACRO_STRIPES = "streifen"
MACRO_HEXAGONAL = "hexagonal"
MACRO_SPIRAL_ZONES = "spiralzonen"

MACRO_STRATEGIES: Dict[str, str] = {
    MACRO_NONE: "Keine Segmentierung",
    MACRO_CHESSBOARD: "Schachbrett (Island)",
    MACRO_STRIPES: "Streifen (Stripe)",
    MACRO_HEXAGONAL: "Hexagonal",
    MACRO_SPIRAL_ZONES: "Spiralzonen (Konzentrisch)",
}
MACRO_STRATEGY_IDS = tuple(MACRO_STRATEGIES)
MACRO_DEFAULT = MACRO_NONE
MACRO_DEFAULT_SEG_SIZE_MM = 5.0
MACRO_DEFAULT_SEG_OVERLAP_UM = 100.0
MACRO_DEFAULT_ROTATION_DEG = 67.0
MACRO_SEGMENT_ORDERS = (
    "Schachbrett (schwarz→weiß)",
    "Spirale (außen→innen)",
    "Spirale (innen→außen)",
    "Zufällig",
    "Sequentiell (links→rechts)",
)
MACRO_DEFAULT_SEG_ORDER = MACRO_SEGMENT_ORDERS[0]


@dataclass
class ProcessedFileResult:
    source_path: Path
    source_label: str
    archive_member: Optional[str]
    output_name: str
    original_lines: List[str]
    original_points: List[Point]
    optimized_points: List[Point]
    original_stats: Stats
    optimized_stats: Stats
    output_text: str
    processing_seconds: float


@dataclass
class AnimationPlan:
    progress_values: array
    frame_count: int
    fps: float
    points_per_second: float
    speed_multiplier: int


@dataclass
class GridPreviewData:
    source_path: Path
    source_label: str
    archive_member: Optional[str]
    point_count: int
    sampled_points: List[Point]
    bounds: Tuple[float, float, float, float]


@dataclass(frozen=True)
class InputSource:
    source_kind: str
    source_path: str
    source_label: str
    output_name: str
    archive_member: Optional[str] = None


@dataclass(frozen=True)
class StepGeneratedArtifact:
    source_step_path: Path
    generated_zip_path: Path
    manifest_json_path: Path
    manifest_data: Dict[str, Any]


class CancelledWorkError(RuntimeError):
    """Raised when a background calculation was cancelled by the user."""


def raise_if_cancelled(cancel_event: object = None) -> None:
    """Abort cooperative worker tasks when the shared cancel flag is set."""
    if cancel_event is not None and bool(cancel_event.is_set()):
        raise CancelledWorkError("Vorgang wurde abgebrochen.")


def is_cancelled_exception(exc: BaseException) -> bool:
    """Detect user-triggered cooperative cancellation exceptions."""
    return isinstance(exc, CancelledWorkError) or "abgebrochen" in str(exc).lower()


def _parse_abs_line(line: str) -> Optional[Point]:
    """Parse one ABS line and return the point, otherwise None."""
    stripped = line.strip()
    if not stripped.startswith("ABS"):
        return None

    parts = stripped.split()
    if len(parts) < 3 or parts[0] != "ABS":
        raise ValueError(f"Ungueltige ABS-Zeile: {line!r}")

    try:
        x = float(parts[1])
        y = float(parts[2])
    except ValueError as exc:
        raise ValueError(f"ABS-Zeile enthaelt keine gueltigen Floats: {line!r}") from exc

    return (x, y)


def _parse_points_from_lines(raw_lines: Sequence[str], source_label: str) -> Tuple[List[str], List[Point]]:
    """Parse ABS points from already loaded text lines."""
    original_lines: List[str] = []
    points: List[Point] = []

    for line_number, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.rstrip("\r\n")
        original_lines.append(line)

        try:
            point = _parse_abs_line(line)
        except ValueError as exc:
            raise ValueError(f"Fehler in Zeile {line_number}: {exc}") from exc

        if point is not None:
            points.append(point)

    if not points:
        raise ValueError(f"Keine ABS-Zeilen gefunden in: {source_label}")

    return original_lines, points


def _decode_text_bytes(raw_bytes: bytes) -> str:
    """Decode text bytes from files or ZIP entries with a small fallback chain."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def load_points_from_file(file_path: str) -> Tuple[List[str], List[Point]]:
    """Read one file and return all original lines plus parsed ABS points."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Eingabedatei nicht gefunden: {file_path}")

    with open(file_path, "rb") as handle:
        raw = handle.read()
    text = _decode_text_bytes(raw)
    return _parse_points_from_lines(text.splitlines(keepends=True), file_path)


def load_points_from_zip_entry(zip_path: str, archive_member: str) -> Tuple[List[str], List[Point]]:
    """Read one B99-like text entry from a ZIP archive."""
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"ZIP-Datei nicht gefunden: {zip_path}")

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            raw_bytes = archive.read(archive_member)
    except KeyError as exc:
        raise FileNotFoundError(f"ZIP-Eintrag nicht gefunden: {archive_member}") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Ungueltige ZIP-Datei: {zip_path}") from exc

    text = _decode_text_bytes(raw_bytes)
    return _parse_points_from_lines(text.splitlines(), f"{zip_path}!{archive_member}")


def get_mode_spec(mode: str) -> ModeSpec:
    """Return the canonical mode specification for a user or legacy mode value."""
    normalized_mode = MODE_ALIASES.get(mode, mode)
    spec = MODE_SPECS.get(normalized_mode)
    if spec is None:
        raise ValueError(f"Unbekannter Optimierungsmodus: {mode!r}")
    return spec


def get_mode_label(mode: str) -> str:
    """Return the scientific user-facing label for one optimization mode."""
    return get_mode_spec(mode).label


def _build_plain_output_name(file_name: str, mode: str) -> str:
    """Build one optimised output filename from a plain file name."""
    spec = get_mode_spec(mode)
    name_path = Path(file_name)
    stem = name_path.stem if name_path.stem else "sequence"
    return f"{stem}_optimised_{spec.canonical_id}{name_path.suffix}"


def _archive_member_keeps_original_name(archive_member: str) -> bool:
    """Return whether one archive member must keep its original file name."""
    member_path = PurePosixPath(str(archive_member).replace("\\", "/"))
    return any(part.casefold() == "figure files" for part in member_path.parent.parts)


def build_output_name_for_source(input_source: "InputSource", mode: str) -> str:
    """Build the output member/file name while preserving ZIP parent folders."""
    if input_source.archive_member:
        if _archive_member_keeps_original_name(input_source.archive_member):
            return input_source.archive_member
        member_path = PurePosixPath(input_source.archive_member)
        renamed_member = _build_plain_output_name(member_path.name, mode)
        if str(member_path.parent) == ".":
            return renamed_member
        return str(member_path.parent / renamed_member)
    return _build_plain_output_name(input_source.output_name, mode)


def build_default_zip_name(results: Sequence["ProcessedFileResult"], mode: str) -> str:
    """Suggest a ZIP name based on one or many processed outputs."""
    spec = get_mode_spec(mode)
    if len(results) == 1:
        output_path = PurePosixPath(results[0].output_name.replace("\\", "/"))
        base_stem = Path(output_path.name).stem or "sequence"
        return f"{base_stem}.zip"
    return f"sequence_optimised_{spec.canonical_id}.zip"


def normalize_input_source(source: Union[str, InputSource]) -> InputSource:
    """Normalize plain file paths and prepared input sources into InputSource objects."""
    if isinstance(source, InputSource):
        return source

    source_path = str(source)
    path_obj = Path(source_path)
    suffix = path_obj.suffix or ".txt"
    return InputSource(
        source_kind="file",
        source_path=source_path,
        source_label=path_obj.name,
        output_name=path_obj.name if path_obj.name else f"optimized{suffix}",
        archive_member=None,
    )


def normalize_zip_entry_types(zip_entry_types: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    """Normalize one or many ZIP entry types while preserving the configured display order."""
    if zip_entry_types is None:
        normalized = ("infill",)
    else:
        seen: Set[str] = set()
        ordered: List[str] = []
        for entry_type in zip_entry_types:
            safe_type = str(entry_type)
            if safe_type not in ZIP_ENTRY_TYPES or safe_type in seen:
                continue
            seen.add(safe_type)
            ordered.append(safe_type)
        normalized = tuple(ordered)

    if not normalized:
        raise ValueError("Mindestens ein ZIP-Eintragstyp muss ausgewaehlt sein.")
    return normalized


def is_step_file_path(file_path: Union[str, Path]) -> bool:
    return Path(file_path).suffix.lower() in {".step", ".stp"}


def _step_helper_source_script_path() -> Path:
    return Path(__file__).resolve().with_name("step_layer_generator.py")


def _step_helper_executable_path() -> Path:
    return Path(sys.executable).resolve().with_name(f"{STEP_HELPER_BUILD_NAME}.exe")


def resolve_step_helper_command() -> List[str]:
    """Resolve the external STEP helper invocation for source and frozen runs."""
    if getattr(sys, "frozen", False):
        helper_executable = _step_helper_executable_path()
        if not helper_executable.is_file():
            raise FileNotFoundError(
                f"STEP-Helfer nicht gefunden: {helper_executable}. "
                "Bitte das Release mit StepLayerGenerator.exe neu bauen."
            )
        return [str(helper_executable)]

    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise FileNotFoundError(
            "uv wurde nicht gefunden. Bitte uv installieren oder das gebaute Release mit StepLayerGenerator.exe verwenden."
        )

    helper_script = _step_helper_source_script_path()
    if not helper_script.is_file():
        raise FileNotFoundError(f"STEP-Helferskript nicht gefunden: {helper_script}")

    return [
        uv_executable,
        "run",
        "--python",
        STEP_HELPER_SOURCE_PYTHON,
        "--with",
        STEP_HELPER_CADQUERY_SPEC,
        "--",
        "python",
        str(helper_script),
    ]


def generate_step_layer_artifact(
    step_file_path: Union[str, Path],
    point_spacing_mm: float,
    layer_height_mm: float,
    support_layer_count: int = STEP_SUPPORT_LAYER_COUNT_DEFAULT,
) -> StepGeneratedArtifact:
    """Generate one temporary ZIP+manifest pair from a STEP file via the external helper."""
    step_path = Path(step_file_path).resolve()
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", step_path.stem).strip("._-") or "step_model"
    temp_dir = Path(tempfile.mkdtemp(prefix=f"{safe_stem}_step_layers_"))
    output_zip_path = temp_dir / f"{safe_stem}_layers.zip"
    manifest_json_path = temp_dir / f"{safe_stem}_layers_manifest.json"
    command = resolve_step_helper_command() + [
        "--step-file",
        str(step_path),
        "--output-zip",
        str(output_zip_path),
        "--manifest-json",
        str(manifest_json_path),
        "--point-spacing-mm",
        f"{float(point_spacing_mm):.9g}",
        "--layer-height-mm",
        f"{float(layer_height_mm):.9g}",
        "--support-layer-count",
        str(max(0, int(support_layer_count))),
    ]
    helper_env = os.environ.copy()
    for env_key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        helper_env.pop(env_key, None)
    completed = subprocess.run(
        command,
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=helper_env,
    )
    if completed.returncode != 0:
        stderr_text = (completed.stderr or "").strip()
        stdout_text = (completed.stdout or "").strip()
        detail = stderr_text or stdout_text or f"Helper-Exitcode {completed.returncode}"
        raise RuntimeError(f"STEP-Helfer fehlgeschlagen fuer {step_path.name}: {detail}")

    if not output_zip_path.is_file():
        raise FileNotFoundError(f"STEP-Helfer hat kein ZIP erzeugt: {output_zip_path}")
    if not manifest_json_path.is_file():
        raise FileNotFoundError(f"STEP-Helfer hat kein Manifest erzeugt: {manifest_json_path}")

    manifest_data = json.loads(manifest_json_path.read_text(encoding="utf-8"))
    return StepGeneratedArtifact(
        source_step_path=step_path,
        generated_zip_path=output_zip_path,
        manifest_json_path=manifest_json_path,
        manifest_data=manifest_data,
    )


def _classify_b99_type(type_digit: int) -> str:
    """Classify the type digit from a B99 filename.

    Convention per object:
      Object 1: contour=1, infill=2
      Object 2: contour=3, infill=4
      Object 3: contour=5, infill=6
      Object 4: contour=7, infill=8
      Object 5: infill=9 (no contour file exists for this object)

    Even digits and digit 9 are infill; odd digits (except 9) are boundary.
    """
    if type_digit == 9:
        return "infill"
    if type_digit % 2 == 0:
        return "infill"
    return "boundary"


def parse_b99_archive_entry_name(entry_name: str) -> Optional[Tuple[int, str]]:
    """Extract layer and type classification from one ZIP entry name."""
    entry_path = Path(entry_name)
    if entry_path.suffix.lower() != ".b99":
        return None

    match = ZIP_ENTRY_NAME_PATTERN.fullmatch(entry_path.stem)
    if match is None:
        return None

    layer_value = int(match.group(1))
    type_digit = int(match.group(2))
    return (layer_value, _classify_b99_type(type_digit))


def build_input_sources(
    selected_paths: Sequence[str],
    zip_entry_types: Optional[Sequence[str]] = None,
    zip_support_end_layer: int = 0,
) -> Tuple[List[InputSource], List[str]]:
    """Resolve selected files and ZIP archives into concrete processable input sources."""
    resolved_sources: List[InputSource] = []
    errors: List[str] = []
    selected_entry_types = set(normalize_zip_entry_types(zip_entry_types))

    for selected_path in selected_paths:
        path_obj = Path(selected_path)
        if path_obj.suffix.lower() != ".zip":
            resolved_sources.append(normalize_input_source(selected_path))
            continue

        try:
            with zipfile.ZipFile(selected_path, "r") as archive:
                matching_entries: List[Tuple[str, int, str]] = []
                for archive_member in archive.namelist():
                    parsed = parse_b99_archive_entry_name(archive_member)
                    if parsed is None:
                        continue
                    layer_value, entry_type = parsed
                    if layer_value <= zip_support_end_layer:
                        continue
                    if entry_type not in selected_entry_types:
                        continue
                    matching_entries.append((archive_member, layer_value, entry_type))
        except zipfile.BadZipFile as exc:
            errors.append(f"{selected_path}: Ungueltige ZIP-Datei ({exc})")
            continue
        except Exception as exc:
            errors.append(f"{selected_path}: {exc}")
            continue

        if not matching_entries:
            selected_label = ", ".join(
                ZIP_ENTRY_TYPE_LABELS.get(entry_type, entry_type) for entry_type in normalize_zip_entry_types(zip_entry_types)
            )
            errors.append(
                f"{selected_path}: Keine passenden .B99-Eintraege fuer Typ "
                f"'{selected_label}' oberhalb Schicht {zip_support_end_layer} gefunden."
            )
            continue

        matching_entries.sort(key=lambda item: (item[1], item[0].lower()))
        for archive_member, _, _ in matching_entries:
            resolved_sources.append(
                InputSource(
                    source_kind="zip_entry",
                    source_path=str(path_obj),
                    source_label=f"{path_obj.name} :: {archive_member}",
                    output_name=archive_member,
                    archive_member=archive_member,
                )
            )

    return resolved_sources, errors


def load_points_from_input_source(source: Union[str, InputSource]) -> Tuple[List[str], List[Point]]:
    """Load points from a normal file or from one selected ZIP entry."""
    input_source = normalize_input_source(source)
    if input_source.source_kind == "zip_entry":
        assert input_source.archive_member is not None
        return load_points_from_zip_entry(input_source.source_path, input_source.archive_member)
    return load_points_from_file(input_source.source_path)


def dist(a: Point, b: Point) -> float:
    """Return the Euclidean distance between two points."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def scale_distance_for_display(distance_value: float, coordinate_scale: float = DISPLAY_COORDINATE_SCALE_MM) -> float:
    """Convert one relative distance value into its displayed millimetre distance."""
    return float(distance_value) * float(coordinate_scale)


def scale_point_for_display(point: Point, coordinate_scale: float = DISPLAY_COORDINATE_SCALE_MM) -> Point:
    """Convert one relative coordinate pair into displayed millimetres."""
    return (
        scale_distance_for_display(point[0], coordinate_scale),
        scale_distance_for_display(point[1], coordinate_scale),
    )


def scale_points_for_display(points: Sequence[Point], coordinate_scale: float = DISPLAY_COORDINATE_SCALE_MM) -> List[Point]:
    """Convert a point sequence into displayed millimetre coordinates."""
    return [scale_point_for_display(point, coordinate_scale) for point in points]


def clamp_viewer_point_size_mm(point_size_mm: float) -> float:
    """Clamp the viewer point size to the supported metric range."""
    return min(max(float(point_size_mm), VIEWER_POINT_SIZE_MM_MIN), VIEWER_POINT_SIZE_MM_MAX)


def viewer_point_size_slider_to_mm(slider_value: int) -> float:
    """Convert the point-size slider value into millimetres."""
    return clamp_viewer_point_size_mm(float(slider_value) / VIEWER_POINT_SIZE_SLIDER_SCALE)


def viewer_point_size_mm_to_slider(point_size_mm: float) -> int:
    """Convert a metric point size into the slider range."""
    return int(round(clamp_viewer_point_size_mm(point_size_mm) * VIEWER_POINT_SIZE_SLIDER_SCALE))


def viewer_point_size_mm_to_input_um(point_size_mm: float) -> int:
    """Convert the internal metric point size into the displayed micrometre value."""
    return int(round(clamp_viewer_point_size_mm(point_size_mm) * VIEWER_POINT_SIZE_INPUT_SCALE_UM))


def viewer_point_size_input_um_to_mm(point_size_um: float) -> float:
    """Convert a displayed micrometre input back into internal millimetres."""
    return clamp_viewer_point_size_mm(float(point_size_um) / VIEWER_POINT_SIZE_INPUT_SCALE_UM)


def normalize_mode(mode: str) -> str:
    """Normalize user-facing and legacy optimization mode names."""
    return get_mode_spec(mode).canonical_id


def _cell_key(point: Point, min_x: float, min_y: float, cell_size: float) -> Tuple[int, int]:
    """Map a point to a grid cell for fast neighborhood lookup."""
    return (
        int(math.floor((point[0] - min_x) / cell_size)),
        int(math.floor((point[1] - min_y) / cell_size)),
    )


def _score_candidate(candidate: Point, current: Point, recent_points: List[Point], w1: float, w2: float) -> float:
    """Calculate the basic greedy score for one candidate point."""
    score = w1 * dist(candidate, current)
    if recent_points:
        score -= w2 * sum(dist(candidate, recent) for recent in recent_points)
    return score


def _spread_score_candidate(candidate: Point, current: Point, recent_points: List[Point], w1: float, w2: float) -> float:
    """Calculate the spread score for one candidate point."""
    score = w1 * dist(candidate, current)
    if recent_points:
        score += w2 * sum(dist(candidate, recent) for recent in recent_points)
    return score


def _weighted_recent_score(candidate: Point, recent_ids: List[int], points: List[Point], age_decay: float) -> float:
    """Score one candidate by its weighted distance to the newest recent points."""
    score = 0.0
    for age, point_id in enumerate(reversed(recent_ids)):
        score += (age_decay ** age) * dist(candidate, points[point_id])
    return score


def _grid_bucket_key(point: Point, min_x: float, min_y: float, grid_spacing: float) -> Tuple[int, int]:
    """Map a point to the nearest virtual grid intersection."""
    return (
        int(round((point[0] - min_x) / grid_spacing)),
        int(round((point[1] - min_y) / grid_spacing)),
    )


def _grid_bucket_target(
    bucket_key: Tuple[int, int],
    min_x: float,
    min_y: float,
    grid_spacing: float,
) -> Point:
    """Return the virtual grid-intersection point for one grid bucket."""
    grid_x, grid_y = bucket_key
    return (
        min_x + grid_x * grid_spacing,
        min_y + grid_y * grid_spacing,
    )


def _build_grid_bucket_order(bucket_keys: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Return a snake-like traversal order over occupied grid buckets."""
    row_map: Dict[int, List[int]] = defaultdict(list)
    for grid_x, grid_y in bucket_keys:
        row_map[grid_y].append(grid_x)

    ordered_keys: List[Tuple[int, int]] = []
    for row_index, grid_y in enumerate(sorted(row_map)):
        xs = sorted(row_map[grid_y])
        if row_index % 2 == 1:
            xs.reverse()
        ordered_keys.extend((grid_x, grid_y) for grid_x in xs)

    return ordered_keys


def choose_next_bucket(
    grid_buckets: Dict[Tuple[int, int], List[int]],
    bucket_last_used_step: Dict[Tuple[int, int], int],
    bucket_order_index: Dict[Tuple[int, int], int],
    current_point: Optional[Point],
    current_step: int,
    min_x: float,
    min_y: float,
    grid_spacing: float,
) -> Tuple[int, int]:
    """Choose the next grid bucket while keeping the traversal spatially spread out."""
    if not grid_buckets:
        raise ValueError("Es gibt keine Grid-Buckets mehr zur Auswahl.")

    def bucket_score(bucket_key: Tuple[int, int]) -> Tuple[int, int, int, float, int]:
        remaining_count = len(grid_buckets[bucket_key])
        last_used_step = bucket_last_used_step.get(bucket_key)
        never_used = 1 if last_used_step is None else 0
        age_since_use = current_step + 1 if last_used_step is None else current_step - last_used_step
        bucket_target = _grid_bucket_target(bucket_key, min_x, min_y, grid_spacing)
        distance_score = 0.0 if current_point is None else dist(current_point, bucket_target)
        # Earlier snake-order buckets win the final tie to keep the traversal deterministic.
        order_score = -bucket_order_index.get(bucket_key, 0)
        return (never_used, age_since_use, remaining_count, distance_score, order_score)

    return max(grid_buckets, key=bucket_score)


def select_bucket_candidates(
    bucket_ids: Sequence[int],
    candidate_limit: int,
    randomized: bool = False,
    rng: Optional[random.Random] = None,
) -> List[int]:
    """Return either the first N or a random N candidates from one bucket."""
    safe_limit = max(1, min(len(bucket_ids), candidate_limit))
    if safe_limit >= len(bucket_ids):
        return list(bucket_ids)
    if randomized:
        random_source = rng if rng is not None else random
        return list(random_source.sample(list(bucket_ids), safe_limit))
    return list(bucket_ids[:safe_limit])


def sample_points_for_preview(points: Sequence[Point], max_points: int = 5000) -> List[Point]:
    """Return a bounded point sample for fast static preview rendering."""
    if len(points) <= max_points:
        return list(points)

    step = max(1, len(points) // max_points)
    sampled = list(points[::step])
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled[: max_points + 1]


def compute_point_bounds(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    """Return min/max bounds for an arbitrary point list."""
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), max(xs), min(ys), max(ys))


def map_point_to_bounds(
    point: Point,
    bounds: Tuple[float, float, float, float],
    width: int,
    height: int,
    margin: int = 28,
) -> Tuple[float, float]:
    """Map one point into a canvas rectangle while keeping aspect ratio."""
    min_x, max_x, min_y, max_y = bounds
    span_x = max(max_x - min_x, 1e-12)
    span_y = max(max_y - min_y, 1e-12)

    scale = min((width - margin * 2) / span_x, (height - margin * 2) / span_y)
    content_width = span_x * scale
    content_height = span_y * scale
    offset_x = (width - content_width) / 2
    offset_y = (height - content_height) / 2

    x_pos = offset_x + (point[0] - min_x) * scale
    y_pos = height - (offset_y + (point[1] - min_y) * scale)
    return (x_pos, y_pos)


def build_grid_preview_data(
    input_sources: Sequence[Union[str, InputSource]],
    max_points_per_file: int = 5000,
) -> Tuple[List[GridPreviewData], List[str]]:
    """Load a light-weight preview representation for the selected files or ZIP entries."""
    preview_items: List[GridPreviewData] = []
    errors: List[str] = []

    for source in input_sources:
        input_source = normalize_input_source(source)
        try:
            _, points = load_points_from_input_source(input_source)
        except Exception as exc:
            errors.append(f"{input_source.source_label}: {exc}")
            continue

        preview_items.append(
            GridPreviewData(
                source_path=Path(input_source.source_path),
                source_label=input_source.source_label,
                archive_member=input_source.archive_member,
                point_count=len(points),
                sampled_points=sample_points_for_preview(points, max_points=max_points_per_file),
                bounds=compute_point_bounds(points),
            )
        )

    return preview_items, errors


def _collect_candidate_ids(
    current: Point,
    points: List[Point],
    active_ids: Set[int],
    cell_points: Dict[Tuple[int, int], Set[int]],
    min_x: float,
    min_y: float,
    cell_size: float,
    target_candidates: int,
) -> List[int]:
    """Collect a bounded candidate set near the current point."""
    if not active_ids:
        return []

    current_cell_x, current_cell_y = _cell_key(current, min_x, min_y, cell_size)
    candidate_ids: List[int] = []
    seen_ids: Set[int] = set()
    radius = 0
    max_radius = 10

    while radius <= max_radius and len(candidate_ids) < target_candidates:
        for cell_x in range(current_cell_x - radius, current_cell_x + radius + 1):
            for cell_y in range(current_cell_y - radius, current_cell_y + radius + 1):
                if radius > 0:
                    is_border = (
                        cell_x == current_cell_x - radius
                        or cell_x == current_cell_x + radius
                        or cell_y == current_cell_y - radius
                        or cell_y == current_cell_y + radius
                    )
                    if not is_border:
                        continue

                for point_id in cell_points.get((cell_x, cell_y), ()):
                    if point_id in active_ids and point_id not in seen_ids:
                        candidate_ids.append(point_id)
                        seen_ids.add(point_id)
        radius += 1

    if len(candidate_ids) < min(target_candidates, len(active_ids)):
        # Fallback: broaden the search with a bounded sample from the remaining points.
        sample_limit = max(target_candidates * 4, 256)
        for point_id in active_ids:
            if point_id not in seen_ids:
                candidate_ids.append(point_id)
                seen_ids.add(point_id)
            if len(candidate_ids) >= sample_limit:
                break

    if not candidate_ids:
        return [next(iter(active_ids))]

    candidate_ids.sort(key=lambda point_id: dist(points[point_id], current))
    return candidate_ids[: max(target_candidates, 1)]


def _collect_farthest_candidate_ids(
    current: Point,
    points: List[Point],
    active_ids: Set[int],
    target_candidates: int,
) -> List[int]:
    """Collect a set of far-away candidates from the remaining points using numpy argpartition."""
    if len(active_ids) <= target_candidates:
        return list(active_ids)
    import numpy as np
    active_list = list(active_ids)
    pts = np.array([points[i] for i in active_list], dtype=np.float64)
    cur = np.array(current, dtype=np.float64)
    dists = np.sqrt(np.sum((pts - cur) ** 2, axis=1))
    k = min(target_candidates, len(active_list))
    top_k = np.argpartition(dists, -k)[-k:]
    return [active_list[int(i)] for i in top_k]


def _cell_center(cell_key: Tuple[int, int], min_x: float, min_y: float, cell_size: float) -> Point:
    """Return the center point of one spatial lookup cell."""
    return (
        min_x + (cell_key[0] + 0.5) * cell_size,
        min_y + (cell_key[1] + 0.5) * cell_size,
    )


def _neighbor_density(cell_key: Tuple[int, int], cell_points: Dict[Tuple[int, int], Set[int]]) -> int:
    """Estimate local density by summing active points in the surrounding 3x3 cells."""
    cell_x, cell_y = cell_key
    density = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            density += len(cell_points.get((cell_x + dx, cell_y + dy), ()))
    return density


def _approx_distance_to_visited_cells(
    cell_key: Tuple[int, int],
    visited_cell_keys: Set[Tuple[int, int]],
    min_x: float,
    min_y: float,
    cell_size: float,
    rng: random.Random,
) -> float:
    """Approximate spacing to the visited area using nearby occupied visited cells."""
    if not visited_cell_keys:
        return cell_size * 2.0

    target_center = _cell_center(cell_key, min_x, min_y, cell_size)
    cell_x, cell_y = cell_key
    best_distance = float("inf")
    max_radius = 6

    for radius in range(max_radius + 1):
        found_in_radius = False
        for search_x in range(cell_x - radius, cell_x + radius + 1):
            for search_y in range(cell_y - radius, cell_y + radius + 1):
                if radius > 0:
                    is_border = (
                        search_x == cell_x - radius
                        or search_x == cell_x + radius
                        or search_y == cell_y - radius
                        or search_y == cell_y + radius
                    )
                    if not is_border:
                        continue

                search_key = (search_x, search_y)
                if search_key not in visited_cell_keys:
                    continue
                found_in_radius = True
                best_distance = min(
                    best_distance,
                    dist(target_center, _cell_center(search_key, min_x, min_y, cell_size)),
                )

        if found_in_radius and best_distance <= (radius + 1.5) * cell_size:
            return best_distance

    if len(visited_cell_keys) <= 128:
        sampled_keys = list(visited_cell_keys)
    else:
        sampled_keys = rng.sample(list(visited_cell_keys), 128)

    for search_key in sampled_keys:
        best_distance = min(
            best_distance,
            dist(target_center, _cell_center(search_key, min_x, min_y, cell_size)),
        )

    return best_distance


def _weighted_random_choice(
    candidate_ids: Sequence[object],
    weights: Sequence[float],
    rng: random.Random,
) -> object:
    """Pick one candidate by positive weights with a stable fallback."""
    if not candidate_ids:
        raise ValueError("Es wurden keine Kandidaten fuer die Zufallsauswahl uebergeben.")

    safe_weights = [max(0.0, float(weight)) for weight in weights]
    total_weight = sum(safe_weights)
    if total_weight <= 0.0:
        return candidate_ids[rng.randrange(len(candidate_ids))]

    threshold = rng.random() * total_weight
    running_weight = 0.0
    for candidate_id, weight in zip(candidate_ids, safe_weights):
        running_weight += weight
        if running_weight >= threshold:
            return candidate_id

    return candidate_ids[-1]


def detect_source_stripe_ranges(points: Sequence[Point]) -> List[Tuple[int, int]]:
    """Detect stripe boundaries from the original traversal by looking for large reverse reset jumps."""
    point_count = len(points)
    if point_count <= 0:
        return []
    if point_count == 1:
        return [(0, 0)]

    deltas_x = [points[index + 1][0] - points[index][0] for index in range(point_count - 1)]
    deltas_y = [points[index + 1][1] - points[index][1] for index in range(point_count - 1)]
    scan_axis = 0 if sum(abs(delta) for delta in deltas_x) >= sum(abs(delta) for delta in deltas_y) else 1
    scan_deltas = deltas_x if scan_axis == 0 else deltas_y
    nonzero_scan_deltas = [delta for delta in scan_deltas if abs(delta) > 1e-12]
    if not nonzero_scan_deltas:
        return [(0, point_count - 1)]

    forward_direction = median(nonzero_scan_deltas)
    if abs(forward_direction) <= 1e-12:
        forward_direction = sum(nonzero_scan_deltas)
    if abs(forward_direction) <= 1e-12:
        forward_direction = nonzero_scan_deltas[0]
    forward_sign = 1.0 if forward_direction >= 0.0 else -1.0

    forward_steps = [abs(delta) for delta in nonzero_scan_deltas if delta * forward_sign > 0.0]
    if forward_steps:
        median_forward_step = float(median(forward_steps))
    else:
        median_forward_step = float(median(abs(delta) for delta in nonzero_scan_deltas))

    scan_values = [point[scan_axis] for point in points]
    scan_axis_span = max(scan_values) - min(scan_values)
    reset_threshold = max(5.0 * median_forward_step, 0.02 * scan_axis_span, 1e-12)

    stripe_ranges: List[Tuple[int, int]] = []
    stripe_start = 0
    for delta_index, delta_scan in enumerate(scan_deltas):
        if delta_scan * forward_sign < 0.0 and abs(delta_scan) >= reset_threshold:
            stripe_ranges.append((stripe_start, delta_index))
            stripe_start = delta_index + 1

    stripe_ranges.append((stripe_start, point_count - 1))
    return stripe_ranges


def detect_interlaced_stripe_ranges(points: Sequence[Point]) -> List[Tuple[int, int]]:
    """Backward-compatible wrapper for the generic source-stripe detection."""
    return detect_source_stripe_ranges(points)


def build_interlaced_block_order(block_size: int, forward_jump: int) -> List[int]:
    """Build the deterministic modular visit order for one full interlaced block."""
    safe_block_size = max(1, int(block_size))
    safe_forward_jump = max(1, int(forward_jump))
    order: List[int] = []
    seen: Set[int] = set()

    for start_index in range(safe_block_size):
        if start_index in seen:
            continue
        current_index = start_index
        while current_index not in seen:
            seen.add(current_index)
            order.append(current_index)
            current_index = (current_index + safe_forward_jump) % safe_block_size

    return order


def reorder_interlaced_stripe_indices(
    stripe_indices: Sequence[int],
    forward_jump: int,
    backward_jump: int,
) -> List[int]:
    """Reorder one detected stripe blockwise without duplicating any point index."""
    stripe_list = list(stripe_indices)
    if not stripe_list:
        return []

    safe_forward_jump = max(1, int(forward_jump))
    safe_backward_jump = max(1, int(backward_jump))
    block_size = safe_forward_jump + safe_backward_jump
    full_block_order = build_interlaced_block_order(block_size, safe_forward_jump)
    reordered_indices: List[int] = []

    for block_start in range(0, len(stripe_list), block_size):
        block = stripe_list[block_start : block_start + block_size]
        filtered_order = [position for position in full_block_order if position < len(block)]
        reordered_indices.extend(block[position] for position in filtered_order)

    return reordered_indices


def reorder_ghost_beam_stripe_indices(
    stripe_indices: Sequence[int],
    ghost_delay: int,
) -> List[int]:
    """Interleave delayed stripe segments to mimic a primary/secondary ghost-beam traversal without duplicates."""
    stripe_list = list(stripe_indices)
    if not stripe_list:
        return []

    safe_delay = max(1, int(ghost_delay))
    block_size = max(2, safe_delay * 2)
    reordered_indices: List[int] = []

    for block_start in range(0, len(stripe_list), block_size):
        block = stripe_list[block_start : block_start + block_size]
        if len(block) <= 1:
            reordered_indices.extend(block)
            continue

        lag = min(safe_delay, len(block) - 1)
        leading_segment = block[lag:]
        trailing_segment = block[:lag]

        for position in range(max(len(leading_segment), len(trailing_segment))):
            if position < len(leading_segment):
                reordered_indices.append(leading_segment[position])
            if position < len(trailing_segment):
                reordered_indices.append(trailing_segment[position])

    return reordered_indices


def _xy2d_hilbert(n: int, x: int, y: int) -> int:
    """Convert (x, y) grid coordinates to a Hilbert curve index for an n×n grid."""
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


def _raster_zigzag_indices(points: Sequence[Point]) -> List[int]:
    """Return point indices sorted in boustrophedon raster order."""
    import numpy as np
    if not points:
        return []
    if len(points) == 1:
        return [0]
    pts = np.array(points, dtype=np.float64)
    ys = pts[:, 1]
    unique_ys = np.unique(np.round(ys, 9))
    if len(unique_ys) < 2:
        return np.argsort(pts[:, 0]).tolist()
    resolution = float(np.min(np.diff(unique_ys))) * 0.5
    y_rounded = np.round(ys / resolution) * resolution
    unique_y_rounded = np.unique(y_rounded)
    result: List[int] = []
    for row_index, yc in enumerate(unique_y_rounded):
        mask = np.abs(y_rounded - yc) < resolution * 0.1
        row_indices = np.where(mask)[0]
        order = row_indices[np.argsort(pts[row_indices, 0])]
        if row_index % 2 == 1:
            order = order[::-1]
        result.extend(order.tolist())
    return result


def _optimize_raster_zigzag(points: List[Point]) -> List[Point]:
    """Reorder points in boustrophedon raster order using the natural scan-line grid."""
    return [points[i] for i in _raster_zigzag_indices(points)]


def _optimize_spot_ordered(points: List[Point], spot_skip: int) -> List[Point]:
    """Raster pre-sort followed by interleaved multipass splitting for controlled cooling."""
    base_indices = _raster_zigzag_indices(points)
    safe_skip = max(1, int(spot_skip))
    passes = [base_indices[offset :: safe_skip + 1] for offset in range(safe_skip + 1)]
    result: List[int] = []
    for p in passes:
        result.extend(p)
    return [points[i] for i in result]


def _optimize_hilbert_curve(points: List[Point], order: int) -> List[Point]:
    """Reorder points along a Hilbert space-filling curve of the given order."""
    import numpy as np
    if len(points) < 2:
        return list(points)
    n = 2 ** max(1, min(int(order), 7))
    pts = np.array(points, dtype=np.float64)
    min_x, max_x = float(pts[:, 0].min()), float(pts[:, 0].max())
    min_y, max_y = float(pts[:, 1].min()), float(pts[:, 1].max())
    eps = 1e-12
    ix = np.clip(((pts[:, 0] - min_x) / (max_x - min_x + eps) * (n - 1)).astype(int), 0, n - 1)
    iy = np.clip(((pts[:, 1] - min_y) / (max_y - min_y + eps) * (n - 1)).astype(int), 0, n - 1)
    h_indices = [_xy2d_hilbert(n, int(x), int(y)) for x, y in zip(ix.tolist(), iy.tolist())]
    return [points[i] for i in np.argsort(h_indices).tolist()]


def _optimize_island_raster(points: List[Point], island_size: float) -> List[Point]:
    """Chessboard island segmentation with boustrophedon raster sort within each island."""
    import numpy as np
    if len(points) < 2:
        return list(points)
    safe_size = max(float(island_size), 1e-9)
    pts = np.array(points, dtype=np.float64)
    min_x, min_y = float(pts[:, 0].min()), float(pts[:, 1].min())
    col_idx = ((pts[:, 0] - min_x) / safe_size).astype(int)
    row_idx = ((pts[:, 1] - min_y) / safe_size).astype(int)
    result: List[int] = []
    for phase in (0, 1):
        phase_point_ids = np.where(((row_idx + col_idx) % 2) == phase)[0].tolist()
        if not phase_point_ids:
            continue
        cells: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for pid in phase_point_ids:
            cells[(int(row_idx[pid]), int(col_idx[pid]))].append(pid)
        rows_present = sorted({r for r, _c in cells})
        for row in rows_present:
            cols = sorted(c for r, c in cells if r == row)
            if row % 2 == 1:
                cols = cols[::-1]
            for col in cols:
                orig_ids = cells[(row, col)]
                cell_pts = [points[i] for i in orig_ids]
                local_order = _raster_zigzag_indices(cell_pts)
                result.extend(orig_ids[local_i] for local_i in local_order)
    return [points[i] for i in result]


# ---------------------------------------------------------------------------
# Makro-Segmentierungsfunktionen (Stufe 1)
# ---------------------------------------------------------------------------

def _macro_order_cells(cells: List[Tuple[int, int]], seg_order: str) -> List[Tuple[int, int]]:
    """Sort cell tuples (row, col) according to the chosen segment order."""
    if not cells:
        return cells
    if "außen" in seg_order and "innen" in seg_order:
        rows = [r for r, c in cells]
        cols = [c for r, c in cells]
        cr = sum(rows) / len(rows)
        cc = sum(cols) / len(cols)
        dists = [((r - cr) ** 2 + (c - cc) ** 2, (r, c)) for r, c in cells]
        rev = "außen→innen" in seg_order
        return [cell for _, cell in sorted(dists, reverse=rev)]
    if "Zufällig" in seg_order:
        import random as _rnd
        rng = _rnd.Random(42)
        arr = list(cells)
        rng.shuffle(arr)
        return arr
    return sorted(cells, key=lambda rc: (rc[0], rc[1]))


def segment_points_macro(
    points: List[Point],
    macro_type: str,
    seg_size_mm: float = MACRO_DEFAULT_SEG_SIZE_MM,
    seg_overlap_um: float = MACRO_DEFAULT_SEG_OVERLAP_UM,
    seg_order: str = MACRO_DEFAULT_SEG_ORDER,
    rotation_deg: float = 0.0,
) -> List[List[Point]]:
    """Split a point cloud into spatial segments according to the macro strategy.

    Returns a list of point lists; each inner list is one segment to be
    independently sorted by the micro strategy.
    """
    import numpy as np

    if macro_type == MACRO_NONE or len(points) < 2:
        return [list(points)]

    pts = np.array(points, dtype=np.float64)
    overlap_mm = max(0.0, seg_overlap_um) / 1000.0
    safe_size = max(float(seg_size_mm), 0.1)

    if macro_type == MACRO_CHESSBOARD:
        return _segment_chessboard_np(pts, safe_size, seg_order, overlap_mm)
    if macro_type == MACRO_STRIPES:
        return _segment_stripes_np(pts, safe_size, rotation_deg, seg_order, overlap_mm)
    if macro_type == MACRO_HEXAGONAL:
        return _segment_hexagonal_np(pts, safe_size, seg_order)
    if macro_type == MACRO_SPIRAL_ZONES:
        return _segment_spiral_zones_np(pts, safe_size, seg_order)

    return [list(points)]


def _segment_chessboard_np(pts, seg_size: float, seg_order: str, overlap_mm: float) -> List[List[Point]]:
    """Chessboard segmentation: phase A (even diag) then phase B (odd diag)."""
    import numpy as np
    min_x, min_y = float(pts[:, 0].min()), float(pts[:, 1].min())
    x_rel = pts[:, 0] - min_x
    y_rel = pts[:, 1] - min_y
    half_ov = overlap_mm / 2.0

    col_idx = (x_rel / seg_size).astype(int)
    row_idx = (y_rel / seg_size).astype(int)

    cell_keys_arr = np.stack([row_idx, col_idx], axis=1)
    unique_cells = list(set(map(tuple, cell_keys_arr.tolist())))

    phase_a = [(r, c) for (r, c) in unique_cells if (r + c) % 2 == 0]
    phase_b = [(r, c) for (r, c) in unique_cells if (r + c) % 2 == 1]
    phase_a = _macro_order_cells(phase_a, seg_order)
    phase_b = _macro_order_cells(phase_b, seg_order)
    ordered = phase_a + phase_b

    segments: List[List[Point]] = []
    for (r, c) in ordered:
        if half_ov <= 0.0:
            mask = (row_idx == r) & (col_idx == c)
        else:
            mask = (
                (x_rel >= c * seg_size - half_ov) & (x_rel < (c + 1) * seg_size + half_ov) &
                (y_rel >= r * seg_size - half_ov) & (y_rel < (r + 1) * seg_size + half_ov)
            )
        sel = pts[mask]
        if len(sel) > 0:
            segments.append([tuple(p) for p in sel.tolist()])
    return segments


def _segment_stripes_np(pts, seg_size: float, rotation: float, seg_order: str, overlap_mm: float) -> List[List[Point]]:
    """Stripe segmentation: parallel bands perpendicular to the hatch direction."""
    import numpy as np
    cos_r = math.cos(math.radians(rotation))
    sin_r = math.sin(math.radians(rotation))
    cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    rel_x = pts[:, 0] - cx
    rel_y = pts[:, 1] - cy
    rot_y = rel_x * sin_r + rel_y * cos_r

    min_ry = float(rot_y.min())
    rot_y_rel = rot_y - min_ry
    half_ov = overlap_mm / 2.0

    stripe_idx = (rot_y_rel / seg_size).astype(int)
    unique_stripes = sorted(set(stripe_idx.tolist()))

    if "Zufällig" in seg_order:
        import random as _rnd
        rng = _rnd.Random(42)
        unique_stripes = list(unique_stripes)
        rng.shuffle(unique_stripes)

    segments: List[List[Point]] = []
    for s in unique_stripes:
        if half_ov <= 0.0:
            mask = stripe_idx == s
        else:
            mask = (rot_y_rel >= s * seg_size - half_ov) & (rot_y_rel < (s + 1) * seg_size + half_ov)
        sel = pts[mask]
        if len(sel) > 0:
            segments.append([tuple(p) for p in sel.tolist()])
    return segments


def _segment_hexagonal_np(pts, seg_size: float, seg_order: str) -> List[List[Point]]:
    """Hexagonal segmentation: offset honeycomb grid, alternating phase A/B."""
    import numpy as np
    h = seg_size * math.sqrt(3)
    v = seg_size * 1.5

    min_x, min_y = float(pts[:, 0].min()), float(pts[:, 1].min())
    row_idx = ((pts[:, 1] - min_y) / v).astype(int)
    x_offset = np.where(row_idx % 2 == 1, h / 2.0, 0.0)
    col_idx = ((pts[:, 0] - min_x - x_offset) / h).astype(int)

    cell_keys_arr = np.stack([row_idx, col_idx], axis=1)
    unique_cells = list(set(map(tuple, cell_keys_arr.tolist())))

    phase_a = [(r, c) for (r, c) in unique_cells if (r + c) % 2 == 0]
    phase_b = [(r, c) for (r, c) in unique_cells if (r + c) % 2 == 1]
    ordered = _macro_order_cells(phase_a, seg_order) + _macro_order_cells(phase_b, seg_order)

    segments: List[List[Point]] = []
    for (r, c) in ordered:
        mask = (row_idx == r) & (col_idx == c)
        sel = pts[mask]
        if len(sel) > 0:
            segments.append([tuple(p) for p in sel.tolist()])
    return segments


def _segment_spiral_zones_np(pts, seg_size: float, seg_order: str) -> List[List[Point]]:
    """Spiral zone segmentation: concentric rings around the centroid."""
    import numpy as np
    cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    dist = np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2)
    ring_idx = (dist / seg_size).astype(int)
    unique_rings = sorted(set(ring_idx.tolist()))

    if "außen" in seg_order:
        unique_rings = sorted(unique_rings, reverse=True)

    segments: List[List[Point]] = []
    for r in unique_rings:
        sel = pts[ring_idx == r]
        if len(sel) > 0:
            segments.append([tuple(p) for p in sel.tolist()])
    return segments


def _optimize_spiral(points: List[Point], direction: str = "inward", hatch_spacing_um: float = 200.0) -> List[Point]:
    """Spiral traversal: sort by ring index (distance/hatch_spacing), then by angle within each ring."""
    if not points:
        return []
    pts = np.asarray(points, dtype=np.float64)
    hatch_mm = hatch_spacing_um / 1000.0
    cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
    dx = pts[:, 0] - cx
    dy = pts[:, 1] - cy
    dist = np.sqrt(dx ** 2 + dy ** 2)
    angle = np.arctan2(dy, dx)
    ring_idx = np.round(dist / max(hatch_mm, 1e-9)).astype(int)
    if direction == "inward":
        order_idx = np.lexsort((angle, -ring_idx))
    else:
        order_idx = np.lexsort((angle, ring_idx))
    return [tuple(p) for p in pts[order_idx].tolist()]


def _optimize_peano(points: List[Point], order: int = 4) -> List[Point]:
    """Peano-curve approximation: boustrophedon traversal on a 3^n grid."""
    if not points:
        return []
    pts = np.asarray(points, dtype=np.float64)
    n = 3 ** min(max(order, 1), 5)
    minx, miny = pts[:, 0].min(), pts[:, 1].min()
    maxx, maxy = pts[:, 0].max(), pts[:, 1].max()
    eps = 1e-9
    ix = np.clip(((pts[:, 0] - minx) / (maxx - minx + eps) * (n - 1)).astype(int), 0, n - 1)
    iy = np.clip(((pts[:, 1] - miny) / (maxy - miny + eps) * (n - 1)).astype(int), 0, n - 1)
    peano_x = np.where(iy % 2 == 0, ix, n - 1 - ix)
    peano_key = iy * n + peano_x
    order_idx = np.argsort(peano_key, kind="stable")
    return [tuple(p) for p in pts[order_idx].tolist()]


def optimize_path(
    points: List[Point],
    w1: float = W1_DEFAULT,
    w2: float = W2_DEFAULT,
    memory: int = MEMORY_DEFAULT,
    mode: str = "local_greedy",
    progress_callback: ProgressCallback = None,
    grid_spacing: float = GRID_SPREAD_DEFAULT_SPACING,
    recent_percent: float = GRID_SPREAD_DEFAULT_RECENT_PERCENT,
    age_decay: float = GRID_SPREAD_AGE_DECAY_DEFAULT,
    ghost_delay: int = GHOST_BEAM_DEFAULT_DELAY,
    forward_jump: int = INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
    backward_jump: int = INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
    hilbert_order: int = HILBERT_ORDER_DEFAULT,
    spot_skip: int = SPOT_SKIP_DEFAULT,
    spiral_direction: str = SPIRAL_DIRECTION_DEFAULT,
    hatch_spacing: float = HATCH_SPACING_UM_DEFAULT,
    cancel_event: object = None,
    macro_strategy: str = MACRO_NONE,
    macro_seg_size_mm: float = MACRO_DEFAULT_SEG_SIZE_MM,
    macro_seg_overlap_um: float = MACRO_DEFAULT_SEG_OVERLAP_UM,
    macro_seg_order: str = MACRO_DEFAULT_SEG_ORDER,
    macro_rotation_deg: float = 0.0,
) -> List[Point]:
    """Optimize point order with the selected two-stage strategy."""
    if not points:
        return []

    normalized_mode = normalize_mode(mode)

    if memory < 0:
        raise ValueError("memory darf nicht negativ sein.")

    if grid_spacing <= 0.0:
        raise ValueError("grid_spacing muss groesser als 0 sein.")

    if not 0.0 < recent_percent <= 100.0:
        raise ValueError("recent_percent muss zwischen 0 und 100 liegen.")

    if not 0.0 < age_decay <= 1.0:
        raise ValueError("age_decay muss zwischen 0 und 1 liegen.")

    if ghost_delay < 1:
        raise ValueError("ghost_delay muss mindestens 1 sein.")

    if forward_jump < 1:
        raise ValueError("forward_jump muss mindestens 1 sein.")

    if backward_jump < 1:
        raise ValueError("backward_jump muss mindestens 1 sein.")

    if normalized_mode == "direct_visualisation":
        if progress_callback is not None:
            progress_callback(1.0, f"{len(points)} / {len(points)} Punkte unveraendert uebernommen")
        return points.copy()

    if len(points) == 1:
        if progress_callback is not None:
            progress_callback(1.0, "1 / 1 Punkte optimiert")
        return points.copy()

    # --- Stufe 1: Makro-Segmentierung ---
    if macro_strategy != MACRO_NONE:
        segments = segment_points_macro(
            points,
            macro_type=macro_strategy,
            seg_size_mm=macro_seg_size_mm,
            seg_overlap_um=macro_seg_overlap_um,
            seg_order=macro_seg_order,
            rotation_deg=macro_rotation_deg,
        )
        if progress_callback is not None:
            progress_callback(0.05, f"{len(segments)} Segmente erstellt")

        # --- Stufe 2: Mikro-Strategie pro Segment ---
        combined: List[Point] = []
        for seg_index, segment in enumerate(segments):
            raise_if_cancelled(cancel_event)
            if not segment:
                continue
            # For ghost_beam_scanning: only pre-sort with raster per segment;
            # the ghost interleaving is applied after combining all segments.
            if normalized_mode == "ghost_beam_scanning":
                combined.extend(_optimize_raster_zigzag(segment))
            else:
                seg_result = optimize_path(
                    segment,
                    w1=w1,
                    w2=w2,
                    memory=memory,
                    mode=mode,
                    progress_callback=None,
                    grid_spacing=grid_spacing,
                    recent_percent=recent_percent,
                    age_decay=age_decay,
                    ghost_delay=ghost_delay,
                    forward_jump=forward_jump,
                    backward_jump=backward_jump,
                    hilbert_order=hilbert_order,
                    spot_skip=spot_skip,
                    spiral_direction=spiral_direction,
                    hatch_spacing=hatch_spacing,
                    cancel_event=cancel_event,
                    macro_strategy=MACRO_NONE,
                )
                combined.extend(seg_result)

            if progress_callback is not None:
                frac = 0.05 + 0.90 * ((seg_index + 1) / len(segments))
                progress_callback(
                    frac,
                    f"Segment {seg_index + 1} / {len(segments)} fertig ({len(combined)} Punkte)",
                )

        # Ghost beam on the combined path (after merging all segments)
        if normalized_mode == "ghost_beam_scanning":
            total_steps = len(combined)
            ghost_result: List[Point] = []
            stripe_ranges = detect_source_stripe_ranges(combined)
            for stripe_start, stripe_end in stripe_ranges:
                raise_if_cancelled(cancel_event)
                stripe_indices = list(range(stripe_start, stripe_end + 1))
                reordered = reorder_ghost_beam_stripe_indices(stripe_indices, ghost_delay=ghost_delay)
                ghost_result.extend(combined[i] for i in reordered)
            combined = ghost_result

        if progress_callback is not None:
            progress_callback(1.0, f"{len(combined)} / {len(combined)} Punkte optimiert")
        return combined

    if normalized_mode == "ghost_beam_scanning":
        total_steps = len(points)
        report_every = max(1, total_steps // 200)
        optimized_indices: List[int] = []
        stripe_ranges = detect_source_stripe_ranges(points)

        for stripe_start, stripe_end in stripe_ranges:
            raise_if_cancelled(cancel_event)
            stripe_indices = list(range(stripe_start, stripe_end + 1))
            optimized_indices.extend(
                reorder_ghost_beam_stripe_indices(
                    stripe_indices,
                    ghost_delay=ghost_delay,
                )
            )
            finished_steps = len(optimized_indices)
            if (
                progress_callback is not None
                and (finished_steps == total_steps or finished_steps % report_every == 0)
            ):
                progress_callback(
                    finished_steps / total_steps,
                    f"{finished_steps} / {total_steps} Punkte mit {get_mode_label(normalized_mode)} optimiert",
                )

        return [points[point_id] for point_id in optimized_indices]

    if normalized_mode == "interlaced_stripe_scanning":
        total_steps = len(points)
        report_every = max(1, total_steps // 200)
        optimized_indices: List[int] = []
        stripe_ranges = detect_source_stripe_ranges(points)

        for stripe_start, stripe_end in stripe_ranges:
            raise_if_cancelled(cancel_event)
            stripe_indices = list(range(stripe_start, stripe_end + 1))
            optimized_indices.extend(
                reorder_interlaced_stripe_indices(
                    stripe_indices,
                    forward_jump=forward_jump,
                    backward_jump=backward_jump,
                )
            )
            finished_steps = len(optimized_indices)
            if (
                progress_callback is not None
                and (finished_steps == total_steps or finished_steps % report_every == 0)
            ):
                progress_callback(
                    finished_steps / total_steps,
                    f"{finished_steps} / {total_steps} Punkte mit {get_mode_label(normalized_mode)} optimiert",
                )

        return [points[point_id] for point_id in optimized_indices]

    if normalized_mode == "raster_zigzag":
        if progress_callback is not None:
            progress_callback(0.5, f"{len(points)} Punkte werden sortiert")
        result = _optimize_raster_zigzag(points)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte mit {get_mode_label(normalized_mode)} sortiert")
        return result

    if normalized_mode == "spot_ordered":
        if progress_callback is not None:
            progress_callback(0.5, f"{len(points)} Punkte werden sortiert")
        result = _optimize_spot_ordered(points, spot_skip)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte mit {get_mode_label(normalized_mode)} sortiert")
        return result

    if normalized_mode == "hilbert_curve":
        if progress_callback is not None:
            progress_callback(0.5, f"{len(points)} Punkte werden auf Hilbert-Kurve projiziert")
        result = _optimize_hilbert_curve(points, hilbert_order)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte mit {get_mode_label(normalized_mode)} sortiert")
        return result

    if normalized_mode == "island_raster":
        if progress_callback is not None:
            progress_callback(0.5, f"{len(points)} Punkte werden in Inseln segmentiert")
        result = _optimize_island_raster(points, grid_spacing)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte mit {get_mode_label(normalized_mode)} sortiert")
        return result

    if normalized_mode == "spiral_scan":
        result = _optimize_spiral(points, direction=spiral_direction, hatch_spacing_um=hatch_spacing)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte spiralförmig sortiert")
        return result

    if normalized_mode == "peano_curve":
        result = _optimize_peano(points, order=hilbert_order)
        if progress_callback is not None:
            progress_callback(1.0, f"{len(result)} / {len(result)} Punkte nach Peano-Kurve sortiert")
        return result

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    span_x = max(max_x - min_x, 1e-12)
    span_y = max(max_y - min_y, 1e-12)

    density_scale = math.sqrt((span_x * span_y) / max(len(points), 1))
    cell_size = max(density_scale, max(span_x, span_y) / max(64.0, math.sqrt(len(points))), 1e-9)

    cell_points: Dict[Tuple[int, int], Set[int]] = defaultdict(set)
    for point_id, point in enumerate(points):
        cell_points[_cell_key(point, min_x, min_y, cell_size)].add(point_id)

    active_ids: Set[int] = set(range(len(points)))
    active_cell_keys: Set[Tuple[int, int]] = {cell_key for cell_key, ids in cell_points.items() if ids}
    optimized_ids: List[int] = []
    total_steps = len(points)
    report_every = max(1, total_steps // 200)

    def report_progress(detail: str) -> None:
        if progress_callback is None:
            return
        finished_steps = len(optimized_ids)
        if finished_steps == 0:
            return
        if finished_steps == total_steps or finished_steps % report_every == 0:
            progress_callback(finished_steps / total_steps, detail)

    def register_point(point_id: int) -> None:
        optimized_ids.append(point_id)
        active_ids.remove(point_id)
        point_cell_key = _cell_key(points[point_id], min_x, min_y, cell_size)
        cell_points[point_cell_key].discard(point_id)
        if not cell_points[point_cell_key]:
            active_cell_keys.discard(point_cell_key)

    if normalized_mode in {"deterministic_grid_dispersion", "stochastic_grid_dispersion"}:
        grid_buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for point_id, point in enumerate(points):
            bucket_key = _grid_bucket_key(point, min_x, min_y, grid_spacing)
            grid_buckets[bucket_key].append(point_id)

        for bucket_key, bucket_ids in grid_buckets.items():
            target_point = _grid_bucket_target(bucket_key, min_x, min_y, grid_spacing)
            bucket_ids.sort(key=lambda point_id: (dist(points[point_id], target_point), point_id))

        bucket_order = _build_grid_bucket_order(tuple(grid_buckets))
        bucket_order_index = {bucket_key: index for index, bucket_key in enumerate(bucket_order)}
        bucket_last_used_step: Dict[Tuple[int, int], int] = {}

        while active_ids and grid_buckets:
            raise_if_cancelled(cancel_event)
            current_point = points[optimized_ids[-1]] if optimized_ids else None
            bucket_key = choose_next_bucket(
                grid_buckets=grid_buckets,
                bucket_last_used_step=bucket_last_used_step,
                bucket_order_index=bucket_order_index,
                current_point=current_point,
                current_step=len(optimized_ids),
                min_x=min_x,
                min_y=min_y,
                grid_spacing=grid_spacing,
            )

            bucket_ids = grid_buckets[bucket_key]
            if not bucket_ids:
                del grid_buckets[bucket_key]
                continue

            recent_count = max(1, int(len(optimized_ids) * (recent_percent / 100.0)))
            recent_ids = optimized_ids[-recent_count:] if optimized_ids else []
            target_point = _grid_bucket_target(bucket_key, min_x, min_y, grid_spacing)
            candidate_limit = min(len(bucket_ids), 64)
            candidate_ids = select_bucket_candidates(
                bucket_ids,
                candidate_limit=candidate_limit,
                randomized=(normalized_mode == "stochastic_grid_dispersion"),
            )

            if recent_ids:
                best_id = max(
                    candidate_ids,
                    key=lambda point_id: (
                        _weighted_recent_score(points[point_id], recent_ids, points, age_decay),
                        -dist(points[point_id], target_point),
                        -point_id,
                    ),
                )
            else:
                best_id = min(
                    candidate_ids,
                    key=lambda point_id: (dist(points[point_id], target_point), point_id),
                )

            register_point(best_id)
            bucket_ids.remove(best_id)
            bucket_last_used_step[bucket_key] = len(optimized_ids) - 1
            if not bucket_ids:
                del grid_buckets[bucket_key]
            report_progress(
                f"{len(optimized_ids)} / {total_steps} Punkte mit "
                f"{get_mode_label(normalized_mode)} optimiert"
            )

        return [points[point_id] for point_id in optimized_ids]

    if normalized_mode == "density_adaptive_sampling":
        if len(points) < 2:
            if progress_callback is not None:
                progress_callback(1.0, f"{len(points)} / {len(points)} Punkte unveraendert uebernommen")
            return list(points)
        rng = random.Random()
        visited_cell_keys: Set[Tuple[int, int]] = set()
        start_id = rng.randrange(len(points))
        register_point(start_id)
        visited_cell_keys.add(_cell_key(points[start_id], min_x, min_y, cell_size))
        report_progress(f"{len(optimized_ids)} / {total_steps} Punkte mit {get_mode_label(normalized_mode)} optimiert")

        while active_ids:
            raise_if_cancelled(cancel_event)
            candidate_cell_pool = list(active_cell_keys)
            if len(candidate_cell_pool) > 128:
                candidate_cell_pool = rng.sample(candidate_cell_pool, 128)

            cell_weights: List[float] = []
            for candidate_cell_key in candidate_cell_pool:
                local_density = _neighbor_density(candidate_cell_key, cell_points)
                spacing_score = _approx_distance_to_visited_cells(
                    candidate_cell_key,
                    visited_cell_keys,
                    min_x,
                    min_y,
                    cell_size,
                    rng,
                )
                # Dense regions are penalized, distant regions get higher probability.
                cell_weight = ((spacing_score + cell_size * 0.35) ** 2) / (1.0 + math.sqrt(max(local_density, 1)))
                cell_weights.append(cell_weight)

            chosen_cell_key = candidate_cell_pool[0]
            if candidate_cell_pool:
                chosen_cell_key = _weighted_random_choice(candidate_cell_pool, cell_weights, rng)

            bucket_ids = list(cell_points.get(chosen_cell_key, ()))
            if not bucket_ids:
                active_cell_keys.discard(chosen_cell_key)
                continue

            candidate_ids = select_bucket_candidates(
                bucket_ids,
                candidate_limit=min(len(bucket_ids), 64),
                randomized=True,
                rng=rng,
            )
            local_density = _neighbor_density(chosen_cell_key, cell_points)
            point_weights = []
            for point_id in candidate_ids:
                point_cell_key = _cell_key(points[point_id], min_x, min_y, cell_size)
                spacing_score = _approx_distance_to_visited_cells(
                    point_cell_key,
                    visited_cell_keys,
                    min_x,
                    min_y,
                    cell_size,
                    rng,
                )
                point_weight = ((spacing_score + cell_size * 0.15) ** 2) / (1.0 + max(local_density - 1, 0) * 0.35)
                point_weights.append(point_weight)

            best_id = _weighted_random_choice(candidate_ids, point_weights, rng)
            register_point(best_id)
            visited_cell_keys.add(_cell_key(points[best_id], min_x, min_y, cell_size))
            report_progress(
                f"{len(optimized_ids)} / {total_steps} Punkte mit {get_mode_label(normalized_mode)} optimiert"
            )

        return [points[point_id] for point_id in optimized_ids]

    register_point(0)
    report_progress(f"{len(optimized_ids)} / {total_steps} Punkte optimiert")

    import numpy as _np_opt

    while active_ids:
        raise_if_cancelled(cancel_event)
        current_id = optimized_ids[-1]
        current = points[current_id]
        recent_ids_slice = optimized_ids[-memory:] if memory > 0 else []
        target_candidates = min(max(96, memory * 24), len(active_ids))

        if normalized_mode == "local_greedy":
            candidate_ids = _collect_candidate_ids(
                current=current,
                points=points,
                active_ids=active_ids,
                cell_points=cell_points,
                min_x=min_x,
                min_y=min_y,
                cell_size=cell_size,
                target_candidates=target_candidates,
            )
        else:
            candidate_ids = _collect_farthest_candidate_ids(
                current=current,
                points=points,
                active_ids=active_ids,
                target_candidates=target_candidates,
            )

        cand_pts = _np_opt.array([points[pid] for pid in candidate_ids], dtype=_np_opt.float64)
        cur_arr = _np_opt.array(current, dtype=_np_opt.float64)
        dist_to_current = _np_opt.sqrt(_np_opt.sum((cand_pts - cur_arr) ** 2, axis=1))
        scores = w1 * dist_to_current
        if recent_ids_slice and w2 > 0.0:
            for rid in recent_ids_slice:
                rec = _np_opt.array(points[rid], dtype=_np_opt.float64)
                rep = _np_opt.sqrt(_np_opt.sum((cand_pts - rec) ** 2, axis=1))
                if normalized_mode == "local_greedy":
                    scores -= w2 * rep
                else:
                    scores += w2 * rep

        if normalized_mode == "local_greedy":
            best_id = candidate_ids[int(_np_opt.argmin(scores))]
        else:
            best_id = candidate_ids[int(_np_opt.argmax(scores))]

        register_point(best_id)
        report_progress(f"{len(optimized_ids)} / {total_steps} Punkte optimiert")

    return [points[point_id] for point_id in optimized_ids]


def analyze_path(points: List[Point]) -> Stats:
    """Calculate jump statistics for a point order."""
    if len(points) < 2:
        return {
            "mean_jump": 0.0,
            "max_jump": 0.0,
            "min_jump": 0.0,
            "std_jump": 0.0,
            "count_jumps": 0.0,
        }

    jumps = [dist(points[index], points[index + 1]) for index in range(len(points) - 1)]

    return {
        "mean_jump": mean(jumps),
        "max_jump": max(jumps),
        "min_jump": min(jumps),
        "std_jump": pstdev(jumps),
        "count_jumps": float(len(jumps)),
    }


def print_points_as_abs(points: List[Point]) -> List[str]:
    """Format points as ABS lines without changing any coordinate values."""
    return [f"ABS {x:.17g} {y:.17g}" for x, y in points]


def build_output_lines(original_lines: List[str], optimized_points: List[Point]) -> List[str]:
    """Return all output lines while only replacing the order of ABS lines."""
    abs_lines = print_points_as_abs(optimized_points)
    abs_index = 0
    output_lines: List[str] = []

    for line in original_lines:
        if _parse_abs_line(line) is not None:
            if abs_index >= len(abs_lines):
                raise ValueError("Zu wenige optimierte ABS-Zeilen fuer den Export.")
            output_lines.append(abs_lines[abs_index])
            abs_index += 1
        else:
            output_lines.append(line)

    if abs_index != len(abs_lines):
        raise ValueError("Zu viele optimierte ABS-Zeilen fuer den Export.")

    return output_lines


def build_output_text(original_lines: List[str], optimized_points: List[Point]) -> str:
    """Build the optimized file content as one text block."""
    return "\n".join(build_output_lines(original_lines, optimized_points)) + "\n"


def save_points(original_lines: List[str], optimized_points: List[Point], output_path: str) -> None:
    """Save one optimized text file to disk."""
    output_text = build_output_text(original_lines, optimized_points)
    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(output_text)


def process_file(
    source: Union[str, InputSource],
    w1: float,
    w2: float,
    memory: int,
    mode: str,
    progress_callback: ProgressCallback = None,
    grid_spacing: float = GRID_SPREAD_DEFAULT_SPACING,
    recent_percent: float = GRID_SPREAD_DEFAULT_RECENT_PERCENT,
    age_decay: float = GRID_SPREAD_AGE_DECAY_DEFAULT,
    ghost_delay: int = GHOST_BEAM_DEFAULT_DELAY,
    forward_jump: int = INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
    backward_jump: int = INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
    hilbert_order: int = HILBERT_ORDER_DEFAULT,
    spot_skip: int = SPOT_SKIP_DEFAULT,
    spiral_direction: str = SPIRAL_DIRECTION_DEFAULT,
    hatch_spacing: float = HATCH_SPACING_UM_DEFAULT,
    cancel_event: object = None,
    macro_strategy: str = MACRO_NONE,
    macro_seg_size_mm: float = MACRO_DEFAULT_SEG_SIZE_MM,
    macro_seg_overlap_um: float = MACRO_DEFAULT_SEG_OVERLAP_UM,
    macro_seg_order: str = MACRO_DEFAULT_SEG_ORDER,
    macro_rotation_deg: float = 0.0,
) -> ProcessedFileResult:
    """Load, optimize and analyze one ABS file."""
    input_source = normalize_input_source(source)
    started_at = time.perf_counter()
    raise_if_cancelled(cancel_event)

    if progress_callback is not None:
        progress_callback(0.02, "Datei wird eingelesen")

    original_lines, original_points = load_points_from_input_source(input_source)
    raise_if_cancelled(cancel_event)

    if progress_callback is not None:
        progress_callback(0.08, f"{len(original_points)} ABS-Punkte geladen")

    source_points = list(original_points)
    optimized_points = optimize_path(
        list(source_points),
        w1=w1,
        w2=w2,
        memory=memory,
        mode=mode,
        progress_callback=(
            None
            if progress_callback is None
            else lambda fraction, detail: progress_callback(0.08 + 0.84 * fraction, detail)
        ),
        grid_spacing=grid_spacing,
        recent_percent=recent_percent,
        age_decay=age_decay,
        ghost_delay=ghost_delay,
        forward_jump=forward_jump,
        backward_jump=backward_jump,
        hilbert_order=hilbert_order,
        spot_skip=spot_skip,
        spiral_direction=spiral_direction,
        hatch_spacing=hatch_spacing,
        cancel_event=cancel_event,
        macro_strategy=macro_strategy,
        macro_seg_size_mm=macro_seg_size_mm,
        macro_seg_overlap_um=macro_seg_overlap_um,
        macro_seg_order=macro_seg_order,
        macro_rotation_deg=macro_rotation_deg,
    )
    raise_if_cancelled(cancel_event)

    if progress_callback is not None:
        progress_callback(0.95, "Analysiere Reihenfolgen")

    original_stats = analyze_path(original_points)
    optimized_stats = analyze_path(optimized_points)
    raise_if_cancelled(cancel_event)

    output_text = build_output_text(original_lines, optimized_points)

    return ProcessedFileResult(
        source_path=Path(input_source.source_path),
        source_label=input_source.source_label,
        archive_member=input_source.archive_member,
        output_name=build_output_name_for_source(input_source, mode),
        original_lines=original_lines,
        original_points=source_points,
        optimized_points=optimized_points,
        original_stats=original_stats,
        optimized_stats=optimized_stats,
        output_text=output_text,
        processing_seconds=time.perf_counter() - started_at,
    )


def process_files(
    input_sources: Sequence[Union[str, InputSource]],
    w1: float,
    w2: float,
    memory: int,
    mode: str,
    grid_spacing: float = GRID_SPREAD_DEFAULT_SPACING,
    recent_percent: float = GRID_SPREAD_DEFAULT_RECENT_PERCENT,
    age_decay: float = GRID_SPREAD_AGE_DECAY_DEFAULT,
    ghost_delay: int = GHOST_BEAM_DEFAULT_DELAY,
    forward_jump: int = INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
    backward_jump: int = INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
    hilbert_order: int = HILBERT_ORDER_DEFAULT,
    spot_skip: int = SPOT_SKIP_DEFAULT,
) -> Tuple[List[ProcessedFileResult], List[str]]:
    """Process all selected files and collect readable errors."""
    results: List[ProcessedFileResult] = []
    errors: List[str] = []

    for input_source in input_sources:
        try:
            result = process_file(
                input_source,
                w1=w1,
                w2=w2,
                memory=memory,
                mode=mode,
                grid_spacing=grid_spacing,
                recent_percent=recent_percent,
                age_decay=age_decay,
                ghost_delay=ghost_delay,
                forward_jump=forward_jump,
                backward_jump=backward_jump,
                hilbert_order=hilbert_order,
                spot_skip=spot_skip,
            )
        except Exception as exc:
            source_label = normalize_input_source(input_source).source_label
            errors.append(f"{source_label}: {exc}")
            continue

        results.append(result)
        print_analysis_for_file(result)

    return results, errors


def _clone_zip_info(zip_info: zipfile.ZipInfo, filename: Optional[str] = None) -> zipfile.ZipInfo:
    """Create a writable copy of a ZipInfo entry while preserving metadata."""
    cloned = zipfile.ZipInfo(filename or zip_info.filename, date_time=zip_info.date_time)
    cloned.compress_type = zip_info.compress_type
    cloned.comment = zip_info.comment
    cloned.extra = zip_info.extra
    cloned.create_system = zip_info.create_system
    cloned.create_version = zip_info.create_version
    cloned.extract_version = zip_info.extract_version
    cloned.flag_bits = zip_info.flag_bits
    cloned.volume = zip_info.volume
    cloned.internal_attr = zip_info.internal_attr
    cloned.external_attr = zip_info.external_attr
    return cloned


def save_results_as_zip(results: Sequence[ProcessedFileResult], zip_path: str) -> None:
    """Write all optimized files into one ZIP archive."""
    if not results:
        raise ValueError("Es gibt keine optimierten Dateien zum Speichern.")

    results_by_archive: Dict[Path, Dict[str, ProcessedFileResult]] = defaultdict(dict)
    plain_results: List[ProcessedFileResult] = []
    used_output_names: Set[str] = set()

    for result in results:
        if result.archive_member is not None:
            existing = results_by_archive[result.source_path].get(result.archive_member)
            if existing is not None:
                raise ValueError(f"Doppelter ZIP-Eintrag im Export: {result.archive_member}")
            results_by_archive[result.source_path][result.archive_member] = result
        else:
            plain_results.append(result)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_zip_path, replacement_map in results_by_archive.items():
            with zipfile.ZipFile(source_zip_path, "r") as source_archive:
                for zip_info in source_archive.infolist():
                    replacement = replacement_map.get(zip_info.filename)
                    target_name = replacement.output_name if replacement is not None else zip_info.filename
                    if target_name in used_output_names:
                        raise ValueError(
                            f"Namenskonflikt beim ZIP-Export: {target_name} kommt in mehreren Quellen vor."
                        )

                    cloned_info = _clone_zip_info(zip_info, filename=target_name)
                    if zip_info.is_dir():
                        archive.writestr(cloned_info, b"")
                    elif replacement is not None:
                        archive.writestr(cloned_info, replacement.output_text.encode("utf-8"))
                    else:
                        archive.writestr(cloned_info, source_archive.read(zip_info.filename))
                    used_output_names.add(target_name)

        for result in plain_results:
            target_name = result.output_name
            if target_name in used_output_names:
                raise ValueError(
                    f"Namenskonflikt beim ZIP-Export: {target_name} existiert bereits im Ausgabearchiv."
                )
            archive.writestr(target_name, result.output_text.encode("utf-8"))
            used_output_names.add(target_name)


def format_stats(stats: Stats, distance_scale: float = 1.0, distance_unit: str = "") -> str:
    """Format jump statistics for console output and the GUI."""
    safe_scale = float(distance_scale)
    unit_suffix = str(distance_unit)
    return (
        f"mean_jump : {stats['mean_jump'] * safe_scale:.6f}{unit_suffix}\n"
        f"max_jump  : {stats['max_jump'] * safe_scale:.6f}{unit_suffix}\n"
        f"min_jump  : {stats['min_jump'] * safe_scale:.6f}{unit_suffix}\n"
        f"std_jump  : {stats['std_jump'] * safe_scale:.6f}{unit_suffix}\n"
        f"count_jumps: {int(stats['count_jumps'])}"
    )


def format_duration(seconds: float, include_tenths: bool = False) -> str:
    """Format a duration in a compact readable form."""
    safe_seconds = max(0.0, float(seconds))
    if include_tenths:
        total_tenths = int(round(safe_seconds * 10))
        hours, rem = divmod(total_tenths, 36000)
        minutes, rem = divmod(rem, 600)
        whole_seconds, tenths = divmod(rem, 10)
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{tenths}"

    total_seconds = int(round(safe_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, whole_seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


def _truncate_middle(s: str, max_len: int) -> str:
    """Shorten a string by replacing its middle with an ellipsis."""
    if len(s) <= max_len:
        return s
    half = (max_len - 3) // 2
    return s[:half] + "..." + s[-(max_len - half - 3):]

def print_analysis_for_file(result: ProcessedFileResult) -> None:
    """Print one file summary to the console."""
    print(f"\nDatei: {result.source_label}")
    print(f"Berechnungszeit: {format_duration(result.processing_seconds, include_tenths=True)}")
    print("Originale Reihenfolge")
    print("---------------------")
    print(format_stats(result.original_stats))
    print()
    print("Optimierte Reihenfolge")
    print("----------------------")
    print(format_stats(result.optimized_stats))



# --- Viewer Classes (Outdented) ---


GL_BLEND = 0x0BE2
GL_COLOR_BUFFER_BIT = 0x00004000
GL_FLOAT = 0x1406
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_POINTS = 0x0000
GL_PROGRAM_POINT_SIZE = 0x8642
GL_POINT_SPRITE = 0x8861
GL_SRC_ALPHA = 0x0302
VIEWER_GL_MAX_BATCH_POINTS = 1_000_000
VIEWER_RASTER_IDLE_REDRAW_MS = 200
VIEWER_DEFAULT_BACKGROUND_COLOR = "#0a0a0a"

def hex_to_qcolor(color_value: str, alpha: float = 1.0) -> QtGui.QColor:
    color = QtGui.QColor(color_value)
    color.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return color

def hex_to_rgba(color_value: str, alpha: float = 1.0) -> Tuple[float, float, float, float]:
    color = hex_to_qcolor(color_value, alpha)
    return (color.redF(), color.greenF(), color.blueF(), color.alphaF())

def build_trail_rgba(count: int) -> List[Tuple[float, float, float, float]]:
    return [hex_to_rgba(color) for color in build_trail_colors(count)]

def map_points_to_screen(
    points: Any,
    center_x_mm: float,
    center_y_mm: float,
    pixels_per_mm: float,
    width_px: int,
    height_px: int,
) -> Any:
    if len(points) == 0:
        return np.empty((0, 2), dtype=np.float32)
    mapped = np.empty((len(points), 2), dtype=np.float32)
    mapped[:, 0] = (points[:, 0] - center_x_mm) * pixels_per_mm + (width_px * 0.5)
    mapped[:, 1] = (height_px * 0.5) - (points[:, 1] - center_y_mm) * pixels_per_mm
    return mapped

def mapped_points_to_polygon(mapped_points: Any) -> QtGui.QPolygonF:
    polygon = QtGui.QPolygonF()
    for x_pos, y_pos in mapped_points:
        polygon.append(QtCore.QPointF(float(x_pos), float(y_pos)))
    return polygon

def draw_mapped_points(
    painter: QtGui.QPainter,
    mapped_points: Any,
    color: QtGui.QColor,
    diameter_px: float,
) -> None:
    if len(mapped_points) == 0:
        return
    pen = QtGui.QPen(color)
    pen.setWidthF(max(1.0, float(diameter_px)))
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPoints(mapped_points_to_polygon(mapped_points))

def build_frame_state(
    points: Any,
    progress: float,
    trail_length: int,
    gradient_window: int,
    policy_name: str,
) -> Dict[str, Any]:
    point_count = len(points)
    if point_count <= 0:
        return {
            "base_index": -1,
            "head_point": None,
            "gray_range": None,
            "gradient_ranges": [],
            "gradient_color_count": 0,
            "stats": VIEWER_EMPTY_RENDER_STATS,
        }

    safe_progress = max(0.0, min(float(progress), point_count - 1))
    base_index = min(max(int(safe_progress), 0), point_count - 1)
    safe_trail_length = max(1, min(int(trail_length), point_count))
    safe_gradient_window = max(1, min(int(gradient_window), point_count))
    next_index = min(base_index + 1, point_count - 1)
    segment_fraction = max(0.0, min(1.0, safe_progress - base_index))
    start_point = points[base_index]
    end_point = points[next_index]
    head_point = (
        float(start_point[0] + (end_point[0] - start_point[0]) * segment_fraction),
        float(start_point[1] + (end_point[1] - start_point[1]) * segment_fraction),
    )
    gray_range, gradient_range = compute_viewer_trail_ranges(
        point_count=point_count,
        base_index=base_index,
        trail_length=safe_trail_length,
        gradient_window=safe_gradient_window,
    )
    gradient_ranges: List[Tuple[int, int]] = []
    gradient_color_count = 0
    if gradient_range is not None:
        gradient_count = gradient_range[1] - gradient_range[0] + 1
        gradient_color_count = min(MAX_RENDER_GRADIENT_BINS, gradient_count)
        gradient_ranges = build_gradient_bin_ranges(
            gradient_range[0],
            gradient_range[1],
            gradient_color_count,
        )
    gray_count = 0 if gray_range is None else gray_range[1] - gray_range[0] + 1
    gradient_count = 0 if gradient_range is None else gradient_range[1] - gradient_range[0] + 1
    stats = ViewerRenderStats(
        requested_trail_count=gray_count + gradient_count,
        requested_gradient_count=gradient_count,
        displayed_trail_count=gray_count + gradient_count,
        displayed_gradient_count=gradient_count,
        policy_name=policy_name,
    )
    return {
        "base_index": base_index,
        "head_point": head_point,
        "gray_range": gray_range,
        "gradient_ranges": gradient_ranges,
        "gradient_color_count": gradient_color_count,
        "stats": stats,
    }

def make_surface_format() -> QtGui.QSurfaceFormat:
    fmt = QtGui.QSurfaceFormat()
    fmt.setRenderableType(QtGui.QSurfaceFormat.RenderableType.OpenGL)
    fmt.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.NoProfile)
    fmt.setVersion(2, 1)
    fmt.setSwapBehavior(QtGui.QSurfaceFormat.SwapBehavior.DoubleBuffer)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    return fmt

def probe_opengl() -> None:
    offscreen_surface = QtGui.QOffscreenSurface()
    offscreen_surface.setFormat(make_surface_format())
    offscreen_surface.create()
    if not offscreen_surface.isValid():
        raise RuntimeError("Qt konnte keine gueltige Offscreen-OpenGL-Oberflaeche erzeugen.")
    context = QtGui.QOpenGLContext()
    context.setFormat(make_surface_format())
    if not context.create():
        raise RuntimeError("Qt konnte keinen OpenGL-Kontext erzeugen.")
    if not context.makeCurrent(offscreen_surface):
        raise RuntimeError("Der erzeugte OpenGL-Kontext konnte nicht aktiviert werden.")
    context.doneCurrent()

def render_raster_background_image(points: Any, snapshot: Dict[str, Any]) -> QtGui.QImage:
    width_px = max(1, int(snapshot["width_px"]))
    height_px = max(1, int(snapshot["height_px"]))
    image = QtGui.QImage(width_px, height_px, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(hex_to_qcolor(VIEWER_DEFAULT_BACKGROUND_COLOR))
    if len(points) <= 0:
        return image

    mapped_points = map_points_to_screen(
        points,
        snapshot["center_x_mm"],
        snapshot["center_y_mm"],
        snapshot["pixels_per_mm"],
        width_px,
        height_px,
    )
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
    draw_mapped_points(
        painter,
        mapped_points,
        hex_to_qcolor(VIEWER_BACKGROUND_COLOR),
        snapshot["point_size_px"],
    )
    painter.end()
    return image

class SharedTransformState(QtCore.QObject):
    changed = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.center_x_mm = 0.0
        self.center_y_mm = 0.0
        self.pixels_per_mm = 6.0

    def set_transform(
        self,
        center_x_mm: float,
        center_y_mm: float,
        pixels_per_mm: float,
        reason: str = "programmatic",
    ) -> None:
        new_scale = max(1e-6, float(pixels_per_mm))
        changed = (
            not math.isclose(self.center_x_mm, float(center_x_mm), rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(self.center_y_mm, float(center_y_mm), rel_tol=0.0, abs_tol=1e-12)
            or not math.isclose(self.pixels_per_mm, new_scale, rel_tol=0.0, abs_tol=1e-12)
        )
        self.center_x_mm = float(center_x_mm)
        self.center_y_mm = float(center_y_mm)
        self.pixels_per_mm = new_scale
        if changed:
            self.changed.emit(reason)

    def shift_world(self, dx_mm: float, dy_mm: float, reason: str = "pan") -> None:
        self.set_transform(
            self.center_x_mm + float(dx_mm),
            self.center_y_mm + float(dy_mm),
            self.pixels_per_mm,
            reason,
        )

    def pan_pixels(self, dx_px: float, dy_px: float, reason: str = "pan") -> None:
        scale = max(1e-6, self.pixels_per_mm)
        self.set_transform(
            self.center_x_mm - (float(dx_px) / scale),
            self.center_y_mm + (float(dy_px) / scale),
            scale,
            reason,
        )

    def zoom_about(self, anchor_world: Tuple[float, float], factor: float, reason: str = "zoom") -> None:
        safe_factor = max(0.05, min(40.0, float(factor)))
        old_scale = max(1e-6, self.pixels_per_mm)
        new_scale = max(1e-6, min(old_scale * safe_factor, 1_000_000.0))
        anchor_x, anchor_y = anchor_world
        scale_ratio = old_scale / new_scale
        self.set_transform(
            anchor_x - ((anchor_x - self.center_x_mm) * scale_ratio),
            anchor_y - ((anchor_y - self.center_y_mm) * scale_ratio),
            new_scale,
            reason,
        )

    def fit_bounds(self, bounds: Tuple[float, float, float, float], viewport_size: QtCore.QSize, reason: str = "home") -> None:
        min_x, max_x, min_y, max_y = bounds
        width_px = max(1, int(viewport_size.width()))
        height_px = max(1, int(viewport_size.height()))
        span_x = max(float(max_x - min_x), 1e-6)
        span_y = max(float(max_y - min_y), 1e-6)
        padding_factor = 0.92
        pixels_per_mm = min(
            (width_px * padding_factor) / span_x,
            (height_px * padding_factor) / span_y,
        )
        if not math.isfinite(pixels_per_mm) or pixels_per_mm <= 0.0:
            pixels_per_mm = 1.0
        self.set_transform(
            (float(min_x) + float(max_x)) * 0.5,
            (float(min_y) + float(max_y)) * 0.5,
            pixels_per_mm,
            reason,
        )

class SequenceViewMixin:
    def _init_sequence_view(
        self,
        transform_state: SharedTransformState,
        coordinate_unit: str,
        build_plate_width_mm: float,
        build_plate_depth_mm: float,
        origin_reference: str,
        backend_name: str,
        navigation_callback: Callable[[], None],
    ) -> None:
        self.transform_state = transform_state
        self.coordinate_unit = coordinate_unit
        self.build_plate_width_mm = float(build_plate_width_mm)
        self.build_plate_depth_mm = float(build_plate_depth_mm)
        self.origin_reference = origin_reference
        self.backend_name = backend_name
        self.navigation_callback = navigation_callback
        self.points = np.empty((0, 2), dtype=np.float32)
        self.point_size_mm = VIEWER_POINT_SIZE_MM_DEFAULT
        self.base_index = -1
        self.head_point: Optional[Tuple[float, float]] = None
        self.gray_range: Optional[Tuple[int, int]] = None
        self.gradient_ranges: List[Tuple[int, int]] = []
        self.gradient_rgba_cache: Dict[int, List[Tuple[float, float, float, float]]] = {}
        self.gradient_qcolor_cache: Dict[int, List[QtGui.QColor]] = {}
        self.current_stats = VIEWER_EMPTY_RENDER_STATS
        self._drag_active = False
        self._last_drag_pos: Optional[QtCore.QPointF] = None
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.transform_state.changed.connect(self._on_transform_changed)

    def _on_transform_changed(self, _reason: str) -> None:
        self.update()

    def _set_points_common(self, points: Any) -> None:
        if len(points) <= 0:
            self.points = np.empty((0, 2), dtype=np.float32)
        else:
            self.points = np.ascontiguousarray(points, dtype=np.float32)
        self.base_index = -1
        self.head_point = None
        self.gray_range = None
        self.gradient_ranges = []
        self.current_stats = VIEWER_EMPTY_RENDER_STATS

    def _set_progress_common(
        self,
        progress: float,
        trail_length: int,
        gradient_window: int,
        render_policy: ViewerRenderPolicy,
    ) -> ViewerRenderStats:
        state = build_frame_state(
            self.points,
            progress,
            trail_length,
            gradient_window,
            render_policy.name,
        )
        self.base_index = state["base_index"]
        self.head_point = state["head_point"]
        self.gray_range = state["gray_range"]
        self.gradient_ranges = state["gradient_ranges"]
        self.current_stats = state["stats"]
        return self.current_stats

    def current_point_text(self) -> str:
        point_count = len(self.points)
        if point_count <= 0:
            return "Point 0 / 0"
        if self.base_index < 0:
            return f"Point 1 / {point_count}"
        return f"Point {self.base_index + 1} / {point_count}"

    def _point_diameter_px(self, multiplier: float = 1.0) -> float:
        return max(1.0, self.transform_state.pixels_per_mm * self.point_size_mm * float(multiplier))

    def world_to_screen(self, x_mm: float, y_mm: float) -> QtCore.QPointF:
        return QtCore.QPointF(
            ((float(x_mm) - self.transform_state.center_x_mm) * self.transform_state.pixels_per_mm) + (self.width() * 0.5),
            (self.height() * 0.5) - ((float(y_mm) - self.transform_state.center_y_mm) * self.transform_state.pixels_per_mm),
        )

    def screen_to_world(self, point: QtCore.QPointF) -> Tuple[float, float]:
        scale = max(1e-6, self.transform_state.pixels_per_mm)
        return (
            self.transform_state.center_x_mm + ((float(point.x()) - (self.width() * 0.5)) / scale),
            self.transform_state.center_y_mm - ((float(point.y()) - (self.height() * 0.5)) / scale),
        )

    def visible_world_bounds(self) -> Tuple[float, float, float, float]:
        scale = max(1e-6, self.transform_state.pixels_per_mm)
        half_width_mm = self.width() * 0.5 / scale
        half_height_mm = self.height() * 0.5 / scale
        return (
            self.transform_state.center_x_mm - half_width_mm,
            self.transform_state.center_x_mm + half_width_mm,
            self.transform_state.center_y_mm - half_height_mm,
            self.transform_state.center_y_mm + half_height_mm,
        )

    def pan_by_fraction(self, x_fraction: float, y_fraction: float) -> None:
        min_x, max_x, min_y, max_y = self.visible_world_bounds()
        self.transform_state.shift_world(
            (max_x - min_x) * float(x_fraction),
            (max_y - min_y) * float(y_fraction),
            "pan",
        )

    def _trail_qcolors(self, count: int) -> List[QtGui.QColor]:
        cached = self.gradient_qcolor_cache.get(count)
        if cached is None:
            cached = [hex_to_qcolor(color) for color in build_trail_colors(count)]
            self.gradient_qcolor_cache[count] = cached
        return cached

    def _trail_rgba(self, count: int) -> List[Tuple[float, float, float, float]]:
        cached = self.gradient_rgba_cache.get(count)
        if cached is None:
            cached = build_trail_rgba(count)
            self.gradient_rgba_cache[count] = cached
        return cached

    def _nice_grid_step_mm(self) -> float:
        target_pixels = 80.0
        target_mm = target_pixels / max(1e-6, self.transform_state.pixels_per_mm)
        exponent = math.floor(math.log10(max(target_mm, 1e-6)))
        base = 10.0 ** exponent
        for multiplier in (1.0, 2.0, 5.0, 10.0):
            candidate = base * multiplier
            if candidate >= target_mm:
                return candidate
        return base * 10.0

    def _plate_bounds(self) -> Tuple[float, float, float, float]:
        if self.origin_reference == "build_plate_centre":
            return (
                -(self.build_plate_width_mm * 0.5),
                self.build_plate_width_mm * 0.5,
                -(self.build_plate_depth_mm * 0.5),
                self.build_plate_depth_mm * 0.5,
            )
        return (0.0, self.build_plate_width_mm, 0.0, self.build_plate_depth_mm)

    def _paint_overlay(self, painter: QtGui.QPainter) -> None:
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        grid_pen = QtGui.QPen(hex_to_qcolor("#ffffff", 0.08))
        grid_pen.setWidthF(1.0)
        painter.setPen(grid_pen)
        min_x, max_x, min_y, max_y = self.visible_world_bounds()
        grid_step = self._nice_grid_step_mm()

        x_value = math.floor(min_x / grid_step) * grid_step
        while x_value <= max_x + 1e-9:
            screen_point = self.world_to_screen(x_value, 0.0)
            painter.drawLine(
                QtCore.QPointF(screen_point.x(), 0.0),
                QtCore.QPointF(screen_point.x(), float(self.height())),
            )
            x_value += grid_step

        y_value = math.floor(min_y / grid_step) * grid_step
        while y_value <= max_y + 1e-9:
            screen_point = self.world_to_screen(0.0, y_value)
            painter.drawLine(
                QtCore.QPointF(0.0, screen_point.y()),
                QtCore.QPointF(float(self.width()), screen_point.y()),
            )
            y_value += grid_step

        plate_pen = QtGui.QPen(hex_to_qcolor("#f0f0f0", 0.30))
        plate_pen.setWidthF(1.25)
        painter.setPen(plate_pen)
        plate_min_x, plate_max_x, plate_min_y, plate_max_y = self._plate_bounds()
        top_left = self.world_to_screen(plate_min_x, plate_max_y)
        bottom_right = self.world_to_screen(plate_max_x, plate_min_y)
        painter.drawRect(QtCore.QRectF(top_left, bottom_right).normalized())

        label_pen = QtGui.QPen(hex_to_qcolor("#f0f0f0", 0.70))
        painter.setPen(label_pen)
        label_font = painter.font()
        label_font.setPointSize(max(8, label_font.pointSize()))
        painter.setFont(label_font)

        x_label = f"X ({self.coordinate_unit})"
        y_label = f"Y ({self.coordinate_unit})"
        painter.drawText(QtCore.QRectF(12.0, float(self.height()) - 28.0, 160.0, 20.0), x_label)
        painter.save()
        painter.translate(24.0, 24.0)
        painter.rotate(-90.0)
        painter.drawText(QtCore.QRectF(-120.0, -16.0, 120.0, 20.0), y_label)
        painter.restore()

        badge_rect = QtCore.QRectF(12.0, 12.0, 132.0, 24.0)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(hex_to_qcolor("#101010", 0.72))
        painter.drawRoundedRect(badge_rect, 6.0, 6.0)
        painter.setPen(QtGui.QPen(hex_to_qcolor("#f0f0f0", 0.92)))
        painter.drawText(badge_rect.adjusted(8.0, 0.0, -8.0, 0.0), QtCore.Qt.AlignmentFlag.AlignVCenter, self.backend_name)

        if self.head_point is not None:
            head_screen = self.world_to_screen(self.head_point[0], self.head_point[1])
            head_radius = self._point_diameter_px(VIEWER_HEAD_SIZE_MULTIPLIER) * 0.5
            painter.setBrush(hex_to_qcolor(VIEWER_HEAD_COLOR))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(head_screen, head_radius, head_radius)

        painter.restore()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.navigation_callback()
            self._drag_active = True
            self._last_drag_pos = event.position()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_active and self._last_drag_pos is not None:
            self.navigation_callback()
            current_pos = event.position()
            delta = current_pos - self._last_drag_pos
            self.transform_state.pan_pixels(delta.x(), delta.y(), "pan")
            self._last_drag_pos = current_pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._drag_active:
            self._drag_active = False
            self._last_drag_pos = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: Any) -> None:
        self.navigation_callback()
        anchor_world = self.screen_to_world(event.position())
        delta_steps = event.angleDelta().y() / 120.0
        if delta_steps != 0.0:
            self.transform_state.zoom_about(anchor_world, 1.15 ** delta_steps, "zoom")
        event.accept()

class SequenceGLView(SequenceViewMixin, QtOpenGLWidgets.QOpenGLWidget):
    def __init__(
        self,
        transform_state: SharedTransformState,
        coordinate_unit: str,
        build_plate_width_mm: float,
        build_plate_depth_mm: float,
        origin_reference: str,
        navigation_callback: Callable[[], None],
    ) -> None:
        super().__init__()
        self._init_sequence_view(
            transform_state,
            coordinate_unit,
            build_plate_width_mm,
            build_plate_depth_mm,
            origin_reference,
            "OpenGL",
            navigation_callback,
        )
        self._gl_program: Optional[QtOpenGL.QOpenGLShaderProgram] = None
        self._gl_functions: Any = None
        self._gl_batches: List[Dict[str, Any]] = []
        self._gl_ready = False
        self._position_attribute = -1
        self._center_uniform = -1
        self._viewport_uniform = -1
        self._scale_uniform = -1
        self._color_uniform = -1
        self._point_size_uniform = -1
        self.setMinimumSize(320, 320)

    def set_points(self, points: Any) -> None:
        self._set_points_common(points)
        if self._gl_ready:
            self.makeCurrent()
            try:
                self._upload_batches()
            finally:
                self.doneCurrent()
        self.update()

    def set_marker_size_mm(self, point_size_mm: float) -> None:
        self.point_size_mm = clamp_viewer_point_size_mm(point_size_mm)
        self.update()

    def update_progress(
        self,
        progress: float,
        trail_length: int,
        gradient_window: int,
        render_policy: ViewerRenderPolicy,
    ) -> ViewerRenderStats:
        stats = self._set_progress_common(progress, trail_length, gradient_window, render_policy)
        self.update()
        return stats

    def _build_program(self) -> QtOpenGL.QOpenGLShaderProgram:
        program = QtOpenGL.QOpenGLShaderProgram(self.context())
        vertex_source = """
            attribute vec2 a_position_mm;
            uniform vec2 u_center_mm;
            uniform vec2 u_viewport_px;
            uniform float u_pixels_per_mm;
            uniform float u_point_size_px;
            void main() {
                vec2 relative_mm = a_position_mm - u_center_mm;
                vec2 clip = vec2(
                    (relative_mm.x * 2.0 * u_pixels_per_mm) / max(u_viewport_px.x, 1.0),
                    (relative_mm.y * 2.0 * u_pixels_per_mm) / max(u_viewport_px.y, 1.0)
                );
                gl_Position = vec4(clip, 0.0, 1.0);
                gl_PointSize = u_point_size_px;
            }
        """
        fragment_source = """
            uniform vec4 u_color_rgba;
            void main() {
                bool hasPointCoord = (
                    gl_PointCoord.x >= 0.0 && gl_PointCoord.x <= 1.0 &&
                    gl_PointCoord.y >= 0.0 && gl_PointCoord.y <= 1.0
                );
                if (hasPointCoord) {
                    vec2 delta = gl_PointCoord - vec2(0.5, 0.5);
                    if (dot(delta, delta) > 0.25) {
                        discard;
                    }
                }
                gl_FragColor = u_color_rgba;
            }
        """
        if not program.addShaderFromSourceCode(QtOpenGL.QOpenGLShader.ShaderTypeBit.Vertex, vertex_source):
            raise RuntimeError(program.log() or "Vertex-Shader konnte nicht kompiliert werden.")
        if not program.addShaderFromSourceCode(QtOpenGL.QOpenGLShader.ShaderTypeBit.Fragment, fragment_source):
            raise RuntimeError(program.log() or "Fragment-Shader konnte nicht kompiliert werden.")
        if not program.link():
            raise RuntimeError(program.log() or "OpenGL-Shader konnten nicht gelinkt werden.")
        return program

    def _destroy_batches(self) -> None:
        for batch in self._gl_batches:
            try:
                batch["buffer"].destroy()
            except Exception:
                pass
        self._gl_batches = []

    def _upload_batches(self) -> None:
        self._destroy_batches()
        if len(self.points) <= 0 or self._gl_program is None:
            return
        for batch_start in range(0, len(self.points), VIEWER_GL_MAX_BATCH_POINTS):
            batch_points = self.points[batch_start : batch_start + VIEWER_GL_MAX_BATCH_POINTS]
            buffer = QtOpenGL.QOpenGLBuffer(QtOpenGL.QOpenGLBuffer.Type.VertexBuffer)
            if not buffer.create():
                raise RuntimeError("OpenGL-VBO konnte nicht erzeugt werden.")
            if not buffer.bind():
                raise RuntimeError("OpenGL-VBO konnte nicht gebunden werden.")
            try:
                contiguous_points = np.ascontiguousarray(batch_points, dtype=np.float32)
                buffer.allocate(contiguous_points.tobytes(), int(contiguous_points.nbytes))
            finally:
                buffer.release()
            self._gl_batches.append(
                {
                    "buffer": buffer,
                    "global_start": batch_start,
                    "count": len(batch_points),
                }
            )

    def initializeGL(self) -> None:
        self._gl_functions = self.context().extraFunctions()
        self._gl_program = self._build_program()
        self._position_attribute = self._gl_program.attributeLocation("a_position_mm")
        self._center_uniform = self._gl_program.uniformLocation("u_center_mm")
        self._viewport_uniform = self._gl_program.uniformLocation("u_viewport_px")
        self._scale_uniform = self._gl_program.uniformLocation("u_pixels_per_mm")
        self._color_uniform = self._gl_program.uniformLocation("u_color_rgba")
        self._point_size_uniform = self._gl_program.uniformLocation("u_point_size_px")
        self._gl_functions.glEnable(GL_BLEND)
        self._gl_functions.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        self._gl_functions.glEnable(GL_PROGRAM_POINT_SIZE)
        try:
            self._gl_functions.glEnable(GL_POINT_SPRITE)
        except Exception:
            # Modern drivers may not expose GL_POINT_SPRITE separately, but older
            # compatibility contexts need it so gl_PointCoord becomes valid.
            pass
        self._gl_ready = True
        self._upload_batches()

    def _set_common_uniforms(self) -> None:
        if self._gl_program is None:
            return
        self._gl_program.setUniformValue(
            self._center_uniform,
            QtGui.QVector2D(float(self.transform_state.center_x_mm), float(self.transform_state.center_y_mm)),
        )
        self._gl_program.setUniformValue(
            self._viewport_uniform,
            QtGui.QVector2D(float(max(1, self.width())), float(max(1, self.height()))),
        )
        self._gl_program.setUniformValue(self._scale_uniform, float(self.transform_state.pixels_per_mm))

    def _draw_range(
        self,
        start_index: int,
        end_index: int,
        rgba: Tuple[float, float, float, float],
        point_diameter_px: float,
    ) -> None:
        if self._gl_program is None or start_index > end_index or len(self._gl_batches) <= 0:
            return
        self._gl_program.setUniformValue(
            self._color_uniform,
            QtGui.QVector4D(float(rgba[0]), float(rgba[1]), float(rgba[2]), float(rgba[3])),
        )
        self._gl_program.setUniformValue(self._point_size_uniform, float(max(1.0, point_diameter_px)))
        for batch in self._gl_batches:
            batch_start = int(batch["global_start"])
            batch_end = batch_start + int(batch["count"]) - 1
            overlap_start = max(start_index, batch_start)
            overlap_end = min(end_index, batch_end)
            if overlap_end < overlap_start:
                continue
            local_start = overlap_start - batch_start
            draw_count = overlap_end - overlap_start + 1
            buffer = batch["buffer"]
            if not buffer.bind():
                continue
            try:
                self._gl_program.enableAttributeArray(self._position_attribute)
                self._gl_program.setAttributeBuffer(self._position_attribute, GL_FLOAT, 0, 2, 8)
                self._gl_functions.glDrawArrays(GL_POINTS, int(local_start), int(draw_count))
                self._gl_program.disableAttributeArray(self._position_attribute)
            finally:
                buffer.release()

    def paintGL(self) -> None:
        if self._gl_functions is None:
            return
        background_color = hex_to_qcolor(VIEWER_DEFAULT_BACKGROUND_COLOR)
        self._gl_functions.glViewport(0, 0, int(max(1, self.width())), int(max(1, self.height())))
        self._gl_functions.glClearColor(background_color.redF(), background_color.greenF(), background_color.blueF(), 1.0)
        self._gl_functions.glClear(GL_COLOR_BUFFER_BIT)
        if self._gl_program is None or len(self.points) <= 0:
            return
        self._gl_program.bind()
        try:
            self._set_common_uniforms()
            self._draw_range(
                0,
                len(self.points) - 1,
                hex_to_rgba(VIEWER_BACKGROUND_COLOR),
                self._point_diameter_px(1.0),
            )
            if self.gray_range is not None:
                self._draw_range(
                    self.gray_range[0],
                    self.gray_range[1],
                    hex_to_rgba(VIEWER_VISITED_COLOR),
                    self._point_diameter_px(VIEWER_VISITED_SIZE_MULTIPLIER),
                )
            if self.gradient_ranges:
                trail_colors = self._trail_rgba(len(self.gradient_ranges))
                for range_index, point_range in enumerate(self.gradient_ranges):
                    self._draw_range(
                        point_range[0],
                        point_range[1],
                        trail_colors[range_index],
                        self._point_diameter_px(VIEWER_TRAIL_SIZE_MULTIPLIER),
                    )
        finally:
            self._gl_program.release()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        self._paint_overlay(painter)
        painter.end()

    def closeEvent(self, event: Any) -> None:
        if self._gl_ready:
            self.makeCurrent()
            try:
                self._destroy_batches()
                if self._gl_program is not None:
                    self._gl_program.removeAllShaders()
            finally:
                self.doneCurrent()
        super().closeEvent(event)

    def framebuffer_has_non_background_pixels(self) -> bool:
        if not self.isValid() or self.width() <= 0 or self.height() <= 0 or len(self.points) <= 0:
            return True
        image = self.grabFramebuffer()
        if image.isNull():
            return False
        converted = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        pixel_bytes = converted.bits().tobytes(converted.sizeInBytes())
        pixels = np.frombuffer(pixel_bytes, dtype=np.uint8)
        if pixels.size <= 0:
            return False
        pixels = pixels.reshape((converted.height(), converted.width(), 4))
        background = hex_to_qcolor(VIEWER_DEFAULT_BACKGROUND_COLOR)
        background_rgb = np.array([background.red(), background.green(), background.blue()], dtype=np.int16)
        rgb_pixels = pixels[:, :, :3].astype(np.int16)
        diff = np.abs(rgb_pixels - background_rgb)
        return bool(np.any(np.max(diff, axis=2) > 2))

class RasterRenderBridge(QtCore.QObject):
    rendered = QtCore.Signal(int, object, object)

class SequenceRasterView(SequenceViewMixin, QtWidgets.QWidget):
    def __init__(
        self,
        transform_state: SharedTransformState,
        coordinate_unit: str,
        build_plate_width_mm: float,
        build_plate_depth_mm: float,
        origin_reference: str,
        navigation_callback: Callable[[], None],
    ) -> None:
        super().__init__()
        self._init_sequence_view(
            transform_state,
            coordinate_unit,
            build_plate_width_mm,
            build_plate_depth_mm,
            origin_reference,
            "Exact Raster",
            navigation_callback,
        )
        self._bridge = RasterRenderBridge()
        self._bridge.rendered.connect(self._on_background_rendered)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sequence-raster-view")
        self._background_image: Optional[QtGui.QImage] = None
        self._background_snapshot: Optional[Dict[str, Any]] = None
        self._pending_snapshot: Optional[Dict[str, Any]] = None
        self._render_job_id = 0
        self._idle_redraw_timer = QtCore.QTimer(self)
        self._idle_redraw_timer.setSingleShot(True)
        self._idle_redraw_timer.setInterval(VIEWER_RASTER_IDLE_REDRAW_MS)
        self._idle_redraw_timer.timeout.connect(self._submit_exact_render)
        self.setMinimumSize(320, 320)

    def _background_snapshot_key(self, snapshot: Optional[Dict[str, Any]]) -> Optional[Tuple[Any, ...]]:
        if snapshot is None:
            return None
        return (
            int(snapshot["width_px"]),
            int(snapshot["height_px"]),
            round(float(snapshot["center_x_mm"]), 9),
            round(float(snapshot["center_y_mm"]), 9),
            round(float(snapshot["pixels_per_mm"]), 9),
            round(float(snapshot["point_size_px"]), 6),
            int(snapshot["point_count"]),
        )

    def _current_snapshot(self) -> Dict[str, Any]:
        return {
            "width_px": max(1, int(self.width())),
            "height_px": max(1, int(self.height())),
            "center_x_mm": float(self.transform_state.center_x_mm),
            "center_y_mm": float(self.transform_state.center_y_mm),
            "pixels_per_mm": float(self.transform_state.pixels_per_mm),
            "point_size_px": float(self._point_diameter_px(1.0)),
            "point_count": len(self.points),
        }

    def _schedule_exact_render(self, immediate: bool) -> None:
        if immediate:
            self._idle_redraw_timer.stop()
            self._submit_exact_render()
            return
        self._idle_redraw_timer.start()

    def _submit_exact_render(self) -> None:
        snapshot = self._current_snapshot()
        self._pending_snapshot = snapshot
        self._render_job_id += 1
        job_id = self._render_job_id
        points_copy = np.ascontiguousarray(self.points, dtype=np.float32)

        def on_done(future: Future, expected_job_id: int, expected_snapshot: Dict[str, Any]) -> None:
            try:
                image = future.result()
            except Exception:
                return
            self._bridge.rendered.emit(expected_job_id, image, expected_snapshot)

        future = self._executor.submit(render_raster_background_image, points_copy, snapshot)
        future.add_done_callback(lambda future, jid=job_id, snap=snapshot: on_done(future, jid, snap))
        self.update()

    def _on_background_rendered(self, job_id: int, image: Any, snapshot: Any) -> None:
        if job_id != self._render_job_id:
            return
        self._background_image = image
        self._background_snapshot = snapshot
        self.update()

    def _on_transform_changed(self, _reason: str) -> None:
        self._schedule_exact_render(immediate=False)
        self.update()

    def set_points(self, points: Any) -> None:
        self._set_points_common(points)
        self._background_image = None
        self._background_snapshot = None
        self._schedule_exact_render(immediate=True)

    def set_marker_size_mm(self, point_size_mm: float) -> None:
        self.point_size_mm = clamp_viewer_point_size_mm(point_size_mm)
        self._schedule_exact_render(immediate=True)

    def update_progress(
        self,
        progress: float,
        trail_length: int,
        gradient_window: int,
        render_policy: ViewerRenderPolicy,
    ) -> ViewerRenderStats:
        stats = self._set_progress_common(progress, trail_length, gradient_window, render_policy)
        self.update()
        return stats

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._schedule_exact_render(immediate=False)

    def _preview_target_rect(self, current_snapshot: Dict[str, Any], cached_snapshot: Dict[str, Any]) -> QtCore.QRectF:
        old_scale = max(1e-6, float(cached_snapshot["pixels_per_mm"]))
        new_scale = max(1e-6, float(current_snapshot["pixels_per_mm"]))
        scale_ratio = new_scale / old_scale
        width_old = float(cached_snapshot["width_px"])
        height_old = float(cached_snapshot["height_px"])
        translation_x = (
            (float(cached_snapshot["center_x_mm"]) - float(current_snapshot["center_x_mm"])) * new_scale
            + (float(current_snapshot["width_px"]) * 0.5)
            - (width_old * 0.5 * scale_ratio)
        )
        translation_y = (
            (float(current_snapshot["center_y_mm"]) - float(cached_snapshot["center_y_mm"])) * new_scale
            + (float(current_snapshot["height_px"]) * 0.5)
            - (height_old * 0.5 * scale_ratio)
        )
        return QtCore.QRectF(
            translation_x,
            translation_y,
            width_old * scale_ratio,
            height_old * scale_ratio,
        )

    def _draw_point_range(self, painter: QtGui.QPainter, point_range: Tuple[int, int], color: QtGui.QColor, multiplier: float) -> None:
        start_index, end_index = point_range
        if end_index < start_index or len(self.points) <= 0:
            return
        mapped_points = map_points_to_screen(
            self.points[start_index : end_index + 1],
            self.transform_state.center_x_mm,
            self.transform_state.center_y_mm,
            self.transform_state.pixels_per_mm,
            max(1, self.width()),
            max(1, self.height()),
        )
        draw_mapped_points(painter, mapped_points, color, self._point_diameter_px(multiplier))

    def paintEvent(self, _event: Any) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), hex_to_qcolor(VIEWER_DEFAULT_BACKGROUND_COLOR))

        current_snapshot = self._current_snapshot()
        exact_key = self._background_snapshot_key(self._background_snapshot)
        current_key = self._background_snapshot_key(current_snapshot)
        if self._background_image is not None and self._background_snapshot is not None:
            if exact_key == current_key:
                painter.drawImage(0.0, 0.0, self._background_image)
            else:
                painter.drawImage(
                    self._preview_target_rect(current_snapshot, self._background_snapshot),
                    self._background_image,
                )
        elif self._pending_snapshot is not None:
            painter.setPen(QtGui.QPen(hex_to_qcolor("#f0f0f0", 0.85)))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Rendering exact raster view...")

        if self.gray_range is not None:
            self._draw_point_range(
                painter,
                self.gray_range,
                hex_to_qcolor(VIEWER_VISITED_COLOR),
                VIEWER_VISITED_SIZE_MULTIPLIER,
            )
        if self.gradient_ranges:
            trail_colors = self._trail_qcolors(len(self.gradient_ranges))
            for range_index, point_range in enumerate(self.gradient_ranges):
                self._draw_point_range(
                    painter,
                    point_range,
                    trail_colors[range_index],
                    VIEWER_TRAIL_SIZE_MULTIPLIER,
                )

        self._paint_overlay(painter)
        painter.end()

    def closeEvent(self, event: Any) -> None:
        self._idle_redraw_timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)

class SequencePanel(QtWidgets.QWidget):
    def __init__(
        self,
        title: str,
        transform_state: SharedTransformState,
        coordinate_unit: str,
        build_plate_width_mm: float,
        build_plate_depth_mm: float,
        origin_reference: str,
        backend_kind: str,
        navigation_callback: Callable[[], None],
    ) -> None:
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title_label = QtWidgets.QLabel(title)
        title_font = title_label.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        if backend_kind == "opengl":
            self.view_widget: Union[SequenceGLView, SequenceRasterView] = SequenceGLView(
                transform_state,
                coordinate_unit,
                build_plate_width_mm,
                build_plate_depth_mm,
                origin_reference,
                navigation_callback,
            )
        else:
            self.view_widget = SequenceRasterView(
                transform_state,
                coordinate_unit,
                build_plate_width_mm,
                build_plate_depth_mm,
                origin_reference,
                navigation_callback,
            )
        layout.addWidget(self.view_widget, 1)

        self.point_label = QtWidgets.QLabel("Point 0 / 0")
        layout.addWidget(self.point_label)

    def set_points(self, points: Any) -> None:
        self.view_widget.set_points(points)
        self.point_label.setText(self.view_widget.current_point_text())

    def set_marker_size_mm(self, point_size_mm: float) -> None:
        self.view_widget.set_marker_size_mm(point_size_mm)

    def update_progress(
        self,
        progress: float,
        trail_length: int,
        gradient_window: int,
        render_policy: ViewerRenderPolicy,
    ) -> ViewerRenderStats:
        stats = self.view_widget.update_progress(progress, trail_length, gradient_window, render_policy)
        self.point_label.setText(self.view_widget.current_point_text())
        return stats

    def pan_by_fraction(self, x_fraction: float, y_fraction: float) -> None:
        self.view_widget.pan_by_fraction(x_fraction, y_fraction)

    def viewport_size(self) -> QtCore.QSize:
        return self.view_widget.size()

class SequenceViewerWindow(QtWidgets.QWidget):
    def __init__(self, payload_data: Dict[str, Any], backend_kind: str):
        super().__init__()
        self.payload = payload_data
        self.backend_kind = backend_kind
        self.backend_name = "OpenGL" if backend_kind == "opengl" else "Exact Raster"
        self.coordinate_scale_mm = float(payload_data.get("coordinate_scale_mm", DISPLAY_COORDINATE_SCALE_MM))
        self.coordinate_unit = str(payload_data.get("coordinate_unit", "mm"))
        self.build_plate_width_mm = float(payload_data.get("build_plate_width_mm", BUILD_PLATE_WIDTH_MM))
        self.build_plate_depth_mm = float(payload_data.get("build_plate_depth_mm", BUILD_PLATE_DEPTH_MM))
        self.origin_reference = str(payload_data.get("origin_reference", "build_plate_centre"))
        self.point_spacing_mm = float(payload_data.get("point_spacing_mm", DISPLAY_POINT_SPACING_MM))
        self.results = [
            {
                **item,
                "original_points_np": np.asarray(item["original_points"], dtype=np.float32) * self.coordinate_scale_mm,
                "optimized_points_np": np.asarray(item["optimized_points"], dtype=np.float32) * self.coordinate_scale_mm,
            }
            for item in payload_data["results"]
        ]
        self.current_index = 0
        self.current_progress = 0.0
        self.last_render_stats = VIEWER_EMPTY_RENDER_STATS
        self.heavy_slider_drag_count = 0
        self.user_paused = False
        self.auto_paused_for_navigation = False
        self.navigation_active = False
        self.playback_start_progress = 0.0
        self.playback_points_per_second = VIEWER_POINTS_PER_SECOND_BASE
        self.playback_elapsed = QtCore.QElapsedTimer()
        self.point_size_mm = VIEWER_POINT_SIZE_MM_DEFAULT
        self.transform_state = SharedTransformState()
        self._home_reset_scheduled = False
        self._initial_show_handled = False
        self.request_backend_fallback = False
        self.backend_fallback_reason = ""

        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(14, 14, 14, 14)
        outer_layout.setSpacing(10)

        control_row = QtWidgets.QHBoxLayout()
        outer_layout.addLayout(control_row)

        control_row.addWidget(QtWidgets.QLabel("Result"))
        self.result_box = QtWidgets.QComboBox()
        self.result_box.addItems([item["source_label"] for item in self.results])
        self.result_box.currentIndexChanged.connect(self._on_result_changed)
        control_row.addWidget(self.result_box, 1)

        self.play_button = QtWidgets.QPushButton("Pause")
        self.play_button.clicked.connect(self._toggle_playback)
        control_row.addWidget(self.play_button)

        control_row.addWidget(QtWidgets.QLabel("Speed"))
        self.speed_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, ANIMATION_MAX_MULTIPLIER)
        self.speed_slider.setValue(60)
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        control_row.addWidget(self.speed_slider)
        self.speed_input = QtWidgets.QDoubleSpinBox()
        self.speed_input.setDecimals(1)
        self.speed_input.setRange(VIEWER_POINTS_PER_SECOND_BASE, VIEWER_POINTS_PER_SECOND_MAX)
        self.speed_input.setSingleStep(VIEWER_POINTS_PER_SECOND_BASE)
        self.speed_input.setSuffix(" pts/s")
        self.speed_input.setAccelerated(True)
        self.speed_input.setFixedWidth(120)
        self.speed_input.valueChanged.connect(self._on_speed_input_changed)
        control_row.addWidget(self.speed_input)

        control_row.addWidget(QtWidgets.QLabel("Trail length"))
        self.trail_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.trail_slider.setRange(1, 1)
        self.trail_slider.setValue(1)
        self.trail_slider.valueChanged.connect(self._on_trail_slider_changed)
        self.trail_slider.sliderPressed.connect(self._on_heavy_slider_pressed)
        self.trail_slider.sliderReleased.connect(self._on_heavy_slider_released)
        control_row.addWidget(self.trail_slider)
        self.trail_input = QtWidgets.QSpinBox()
        self.trail_input.setRange(1, 1)
        self.trail_input.setAccelerated(True)
        self.trail_input.setFixedWidth(90)
        self.trail_input.valueChanged.connect(self._on_trail_input_changed)
        control_row.addWidget(self.trail_input)
        self.trail_label = QtWidgets.QLabel()
        control_row.addWidget(self.trail_label)

        control_row.addWidget(QtWidgets.QLabel("Gradient window"))
        self.gradient_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.gradient_slider.setRange(1, 1)
        self.gradient_slider.setValue(1)
        self.gradient_slider.valueChanged.connect(self._on_gradient_slider_changed)
        self.gradient_slider.sliderPressed.connect(self._on_heavy_slider_pressed)
        self.gradient_slider.sliderReleased.connect(self._on_heavy_slider_released)
        control_row.addWidget(self.gradient_slider)
        self.gradient_input = QtWidgets.QSpinBox()
        self.gradient_input.setRange(1, 1)
        self.gradient_input.setAccelerated(True)
        self.gradient_input.setFixedWidth(90)
        self.gradient_input.valueChanged.connect(self._on_gradient_input_changed)
        control_row.addWidget(self.gradient_input)
        self.gradient_label = QtWidgets.QLabel()
        control_row.addWidget(self.gradient_label)

        control_row.addWidget(QtWidgets.QLabel("Point size"))
        self.point_size_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.point_size_slider.setRange(
            viewer_point_size_mm_to_slider(VIEWER_POINT_SIZE_MM_MIN),
            viewer_point_size_mm_to_slider(VIEWER_POINT_SIZE_MM_MAX),
        )
        self.point_size_slider.setValue(viewer_point_size_mm_to_slider(self.point_size_mm))
        self.point_size_slider.valueChanged.connect(self._on_point_size_slider_changed)
        control_row.addWidget(self.point_size_slider)
        self.point_size_input = QtWidgets.QSpinBox()
        self.point_size_input.setRange(
            viewer_point_size_mm_to_input_um(VIEWER_POINT_SIZE_MM_MIN),
            viewer_point_size_mm_to_input_um(VIEWER_POINT_SIZE_MM_MAX),
        )
        self.point_size_input.setSingleStep(10)
        self.point_size_input.setSuffix(" um")
        self.point_size_input.setAccelerated(True)
        self.point_size_input.setFixedWidth(124)
        self.point_size_input.valueChanged.connect(self._on_point_size_input_changed)
        control_row.addWidget(self.point_size_input)

        self.reset_button = QtWidgets.QPushButton("Home")
        self.reset_button.clicked.connect(self._reset_views)
        control_row.addWidget(self.reset_button)

        self.info_label = QtWidgets.QLabel()
        self.info_label.setWordWrap(True)
        outer_layout.addWidget(self.info_label)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        outer_layout.addWidget(splitter, 1)

        self.original_panel = SequencePanel(
            "Original",
            self.transform_state,
            self.coordinate_unit,
            self.build_plate_width_mm,
            self.build_plate_depth_mm,
            self.origin_reference,
            self.backend_kind,
            self._on_manual_range_change,
        )
        self.optimized_panel = SequencePanel(
            "Optimised",
            self.transform_state,
            self.coordinate_unit,
            self.build_plate_width_mm,
            self.build_plate_depth_mm,
            self.origin_reference,
            self.backend_kind,
            self._on_manual_range_change,
        )
        splitter.addWidget(self.original_panel)
        splitter.addWidget(self.optimized_panel)
        splitter.setSizes([780, 780])

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(VIEWER_TIMER_INTERVAL_MS)
        self.timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.navigation_resume_timer = QtCore.QTimer(self)
        self.navigation_resume_timer.setSingleShot(True)
        self.navigation_resume_timer.setInterval(VIEWER_AUTO_RESUME_DELAY_MS)
        self.navigation_resume_timer.timeout.connect(self._on_navigation_idle)
        self.slider_refresh_timer = QtCore.QTimer(self)
        self.slider_refresh_timer.setSingleShot(True)
        self.slider_refresh_timer.setInterval(VIEWER_SLIDER_DRAG_INTERVAL_MS)
        self.slider_refresh_timer.timeout.connect(self._apply_throttled_slider_update)
        self.opengl_diagnostic_timer = QtCore.QTimer(self)
        self.opengl_diagnostic_timer.setSingleShot(True)
        self.opengl_diagnostic_timer.setInterval(600)
        self.opengl_diagnostic_timer.timeout.connect(self._diagnose_opengl_output)

        self._sync_speed_controls_from_slider()
        self._sync_trail_inputs()
        self._sync_point_size_controls_from_slider()
        self._apply_point_size_to_panels()
        self._update_render_feedback()

        initial_index = int(payload_data.get("selected_index", 0))
        if not self.results:
            self.info_label.setText("Keine Ergebnisse fuer die Visualisierung vorhanden.")
        else:
            if not 0 <= initial_index < len(self.results):
                initial_index = 0
            self.result_box.blockSignals(True)
            self.result_box.setCurrentIndex(initial_index)
            self.result_box.blockSignals(False)
            self._set_result(initial_index)
            self._start_playback()
            self._schedule_opengl_diagnostic()

    def _schedule_home_reset(self) -> None:
        if self._home_reset_scheduled:
            return
        self._home_reset_scheduled = True
        QtCore.QTimer.singleShot(0, self._perform_scheduled_home_reset)

    def _perform_scheduled_home_reset(self) -> None:
        self._home_reset_scheduled = False
        self._reset_views()
        self._schedule_opengl_diagnostic()

    def _schedule_opengl_diagnostic(self) -> None:
        if self.backend_kind == "opengl" and self.results:
            self.opengl_diagnostic_timer.start()

    def _diagnose_opengl_output(self) -> None:
        if self.backend_kind != "opengl" or not self.results or not self.isVisible():
            return
        original_view = getattr(self.original_panel, "view_widget", None)
        optimized_view = getattr(self.optimized_panel, "view_widget", None)
        panels_to_check: List[SequenceGLView] = []
        if isinstance(original_view, SequenceGLView) and len(original_view.points) > 0:
            panels_to_check.append(original_view)
        if isinstance(optimized_view, SequenceGLView) and len(optimized_view.points) > 0:
            panels_to_check.append(optimized_view)
        if not panels_to_check:
            return
        if all(not panel.framebuffer_has_non_background_pixels() for panel in panels_to_check):
            self.request_backend_fallback = True
            self.backend_fallback_reason = (
                "Der OpenGL-Viewer hat trotz vorhandener Punkte ein leeres Framebuffer-Bild geliefert."
            )
            self.close()

    def _current_result_bounds(self) -> Tuple[float, float, float, float]:
        if not self.results:
            return (-1.0, 1.0, -1.0, 1.0)
        result = self.results[self.current_index]
        point_sets = [result["original_points_np"], result["optimized_points_np"]]
        non_empty_sets = [points for points in point_sets if len(points) > 0]
        if not non_empty_sets:
            return (-1.0, 1.0, -1.0, 1.0)
        min_x = min(float(points[:, 0].min()) for points in non_empty_sets)
        max_x = max(float(points[:, 0].max()) for points in non_empty_sets)
        min_y = min(float(points[:, 1].min()) for points in non_empty_sets)
        max_y = max(float(points[:, 1].max()) for points in non_empty_sets)
        return (min_x, max_x, min_y, max_y)

    def _current_viewport_size(self) -> QtCore.QSize:
        original_size = self.original_panel.viewport_size()
        optimized_size = self.optimized_panel.viewport_size()
        return QtCore.QSize(
            max(640, original_size.width(), optimized_size.width()),
            max(640, original_size.height(), optimized_size.height()),
        )

    def _current_point_count(self) -> int:
        if not self.results:
            return 0
        result = self.results[self.current_index]
        return max(len(result["original_points_np"]), len(result["optimized_points_np"]))

    def _current_render_policy(self) -> ViewerRenderPolicy:
        if self.navigation_active or self.auto_paused_for_navigation or self.heavy_slider_drag_count > 0:
            return VIEWER_RENDER_POLICY_NAVIGATION
        if self.timer.isActive():
            return VIEWER_RENDER_POLICY_PLAYBACK
        return VIEWER_RENDER_POLICY_IDLE

    def _current_requested_points_per_second(self) -> float:
        return self._points_per_second_from_slider_value(self.speed_slider.value())

    def _restart_playback_clock(self) -> None:
        self.playback_points_per_second = self._current_requested_points_per_second()
        self.playback_start_progress = self.current_progress
        self.playback_elapsed.restart()

    def _progress_from_playback_clock(self) -> float:
        point_count = self._current_point_count()
        if point_count <= 1:
            return 0.0
        if not self.playback_elapsed.isValid():
            return float(self.current_progress)
        elapsed_seconds = self.playback_elapsed.elapsed() / 1000.0
        loop_extent = point_count - 1
        if loop_extent <= 0:
            return 0.0
        return (self.playback_start_progress + elapsed_seconds * self.playback_points_per_second) % loop_extent

    def _sync_current_progress_from_clock(self) -> None:
        self.current_progress = self._progress_from_playback_clock()

    def _start_playback(self) -> None:
        if not self.results:
            return
        self.auto_paused_for_navigation = False
        self.user_paused = False
        self._restart_playback_clock()
        self.timer.start()
        self.play_button.setText("Pause")

    def _stop_playback(self, *, manual: bool) -> None:
        if self.timer.isActive():
            self._sync_current_progress_from_clock()
        self.timer.stop()
        self.playback_elapsed.invalidate()
        self.user_paused = manual
        self.play_button.setText("Play")

    def _pause_for_navigation(self) -> None:
        if self.timer.isActive():
            self._sync_current_progress_from_clock()
            self.timer.stop()
            self.playback_elapsed.invalidate()
            self.auto_paused_for_navigation = True
            self.play_button.setText("Play")

    def _sync_trail_inputs(self) -> None:
        trail_length = self.trail_slider.value()
        gradient_window = self.gradient_slider.value()
        self.trail_input.blockSignals(True)
        self.trail_input.setValue(trail_length)
        self.trail_input.blockSignals(False)
        self.gradient_input.blockSignals(True)
        self.gradient_input.setValue(gradient_window)
        self.gradient_input.blockSignals(False)

    def _current_point_size_mm(self) -> float:
        return viewer_point_size_slider_to_mm(self.point_size_slider.value())

    def _sync_point_size_controls_from_slider(self) -> None:
        self.point_size_mm = self._current_point_size_mm()
        self.point_size_input.blockSignals(True)
        self.point_size_input.setValue(viewer_point_size_mm_to_input_um(self.point_size_mm))
        self.point_size_input.blockSignals(False)

    def _apply_point_size_to_panels(self) -> None:
        self.original_panel.set_marker_size_mm(self.point_size_mm)
        self.optimized_panel.set_marker_size_mm(self.point_size_mm)

    def _update_render_feedback(self) -> None:
        self.trail_label.setText(f"{self.trail_slider.value()} points")
        self.gradient_label.setText(f"{self.gradient_slider.value()} points")

    def _set_result(self, index: int) -> None:
        if not self.results:
            return
        was_playing = self.timer.isActive()
        if was_playing:
            self._sync_current_progress_from_clock()
        self.current_index = max(0, min(index, len(self.results) - 1))
        result = self.results[self.current_index]
        self.current_progress = 0.0
        self.playback_start_progress = 0.0
        point_count = max(len(result["original_points_np"]), len(result["optimized_points_np"]), 1)
        trail_maximum = max(1, point_count)
        trail_default = min(24, trail_maximum)
        gradient_default = min(VIEWER_DEFAULT_GRADIENT_WINDOW, trail_maximum)

        self.trail_slider.blockSignals(True)
        self.trail_input.blockSignals(True)
        self.gradient_slider.blockSignals(True)
        self.gradient_input.blockSignals(True)

        self.trail_slider.setRange(1, trail_maximum)
        self.trail_input.setRange(1, trail_maximum)
        self.gradient_slider.setRange(1, trail_maximum)
        self.gradient_input.setRange(1, trail_maximum)

        trail_value = self.trail_slider.value()
        if trail_value > trail_maximum:
            trail_value = trail_maximum
        elif trail_value < 1 or (trail_value == 1 and trail_maximum > 1):
            trail_value = trail_default

        gradient_value = self.gradient_slider.value()
        if gradient_value > trail_maximum:
            gradient_value = trail_maximum
        elif gradient_value < 1 or (gradient_value == 1 and trail_maximum > 1):
            gradient_value = gradient_default

        self.trail_slider.setValue(trail_value)
        self.gradient_slider.setValue(gradient_value)

        self.trail_slider.blockSignals(False)
        self.trail_input.blockSignals(False)
        self.gradient_slider.blockSignals(False)
        self.gradient_input.blockSignals(False)

        self.original_panel.set_points(result["original_points_np"])
        self.optimized_panel.set_points(result["optimized_points_np"])
        self._apply_point_size_to_panels()
        self._schedule_home_reset()
        self._update_info_label()
        self._sync_trail_inputs()
        self._update_panels()
        self._schedule_opengl_diagnostic()
        if was_playing:
            self._restart_playback_clock()

    def _update_info_label(self) -> None:
        result = self.results[self.current_index]
        archive_member = result.get("archive_member")
        source_hint = f"Archiv-Eintrag: {archive_member}" if archive_member else f"Quelle: {result['source_path']}"
        origin_hint = "0,0 = plate centre" if self.origin_reference == "build_plate_centre" else self.origin_reference
        self.info_label.setText(
            f"Mode: {self.payload.get('mode_label', '')} | {source_hint} | "
            f"Output name: {result['output_name']} | Points: {result['point_count']} | "
            f"Coordinates shown in {self.coordinate_unit} | {origin_hint} | "
            f"Build plate: {self.build_plate_width_mm:.0f} x {self.build_plate_depth_mm:.0f} {self.coordinate_unit} | "
            f"Point spacing: {self.point_spacing_mm:.1f} {self.coordinate_unit} | "
            f"Viewer backend: {self.backend_name}"
        )

    def _points_per_second_from_slider_value(self, slider_value: int) -> float:
        return min(VIEWER_POINTS_PER_SECOND_MAX, VIEWER_POINTS_PER_SECOND_BASE * slider_value)

    def _slider_value_from_points_per_second(self, points_per_second: float) -> int:
        raw_value = int(round(float(points_per_second) / VIEWER_POINTS_PER_SECOND_BASE))
        return max(1, min(ANIMATION_MAX_MULTIPLIER, raw_value))

    def _sync_speed_controls_from_slider(self) -> None:
        points_per_second = self._points_per_second_from_slider_value(self.speed_slider.value())
        self.speed_input.blockSignals(True)
        self.speed_input.setValue(points_per_second)
        self.speed_input.blockSignals(False)

    def _on_speed_slider_changed(self, _value: int) -> None:
        if self.timer.isActive():
            self._sync_current_progress_from_clock()
            self._restart_playback_clock()
        self._sync_speed_controls_from_slider()

    def _on_speed_input_changed(self, value: float) -> None:
        if self.timer.isActive():
            self._sync_current_progress_from_clock()
        slider_value = self._slider_value_from_points_per_second(value)
        if slider_value != self.speed_slider.value():
            self.speed_slider.setValue(slider_value)
        else:
            if self.timer.isActive():
                self._restart_playback_clock()
            self._sync_speed_controls_from_slider()

    def _on_trail_slider_changed(self, _value: int) -> None:
        self._sync_trail_inputs()
        self._update_render_feedback()
        if self.heavy_slider_drag_count > 0:
            self._schedule_throttled_slider_update()
        else:
            self._update_panels()

    def _on_trail_input_changed(self, value: int) -> None:
        if value != self.trail_slider.value():
            self.trail_slider.setValue(value)
        else:
            self._sync_trail_inputs()
            self._update_panels()

    def _on_gradient_slider_changed(self, _value: int) -> None:
        self._sync_trail_inputs()
        self._update_render_feedback()
        if self.heavy_slider_drag_count > 0:
            self._schedule_throttled_slider_update()
        else:
            self._update_panels()

    def _on_gradient_input_changed(self, value: int) -> None:
        if value != self.gradient_slider.value():
            self.gradient_slider.setValue(value)
        else:
            self._sync_trail_inputs()
            self._update_panels()

    def _on_point_size_slider_changed(self, _value: int) -> None:
        self._sync_point_size_controls_from_slider()
        self._apply_point_size_to_panels()
        self._schedule_opengl_diagnostic()

    def _on_point_size_input_changed(self, value: int) -> None:
        point_size_mm = viewer_point_size_input_um_to_mm(value)
        slider_value = viewer_point_size_mm_to_slider(point_size_mm)
        if slider_value != self.point_size_slider.value():
            self.point_size_slider.setValue(slider_value)
        else:
            self.point_size_mm = point_size_mm
            self._sync_point_size_controls_from_slider()
            self._apply_point_size_to_panels()
            self._schedule_opengl_diagnostic()

    def _on_heavy_slider_pressed(self) -> None:
        self.heavy_slider_drag_count += 1
        self._update_panels()

    def _on_heavy_slider_released(self) -> None:
        self.heavy_slider_drag_count = max(0, self.heavy_slider_drag_count - 1)
        self.slider_refresh_timer.stop()
        self._update_panels()

    def _schedule_throttled_slider_update(self) -> None:
        if not self.slider_refresh_timer.isActive():
            self.slider_refresh_timer.start()

    def _apply_throttled_slider_update(self) -> None:
        self._update_panels()

    def _on_manual_range_change(self) -> None:
        if not self.results:
            return
        first_navigation_event = not self.navigation_active
        self.navigation_active = True
        self.navigation_resume_timer.start()
        if self.timer.isActive():
            self._pause_for_navigation()
        if first_navigation_event:
            self._update_panels()

    def _on_navigation_idle(self) -> None:
        had_navigation_state = self.navigation_active or self.auto_paused_for_navigation
        self.navigation_active = False
        if self.auto_paused_for_navigation and not self.user_paused:
            self.auto_paused_for_navigation = False
            self._start_playback()
            return
        self.auto_paused_for_navigation = False
        if had_navigation_state:
            self._update_panels()

    def _toggle_playback(self) -> None:
        if self.timer.isActive():
            self.navigation_resume_timer.stop()
            self.auto_paused_for_navigation = False
            self._stop_playback(manual=True)
        else:
            self.navigation_resume_timer.stop()
            self.auto_paused_for_navigation = False
            self._start_playback()

    def _reset_views(self) -> None:
        self.transform_state.fit_bounds(self._current_result_bounds(), self._current_viewport_size(), "home")

    def _update_panels(self) -> None:
        if not self.results:
            return
        if self.timer.isActive():
            self.current_progress = self._progress_from_playback_clock()
        trail_length = self.trail_slider.value()
        gradient_window = self.gradient_slider.value()
        render_policy = self._current_render_policy()
        effective_gradient_window = gradient_window
        if render_policy.name in (
            VIEWER_RENDER_POLICY_PLAYBACK.name,
            VIEWER_RENDER_POLICY_NAVIGATION.name,
        ):
            effective_gradient_window = min(gradient_window, VIEWER_PLAYBACK_GRADIENT_CAP)
        original_stats = self.original_panel.update_progress(
            self.current_progress,
            trail_length,
            effective_gradient_window,
            render_policy,
        )
        optimized_stats = self.optimized_panel.update_progress(
            self.current_progress,
            trail_length,
            effective_gradient_window,
            render_policy,
        )
        self.last_render_stats = ViewerRenderStats(
            requested_trail_count=max(original_stats.requested_trail_count, optimized_stats.requested_trail_count),
            requested_gradient_count=max(original_stats.requested_gradient_count, optimized_stats.requested_gradient_count),
            displayed_trail_count=max(original_stats.displayed_trail_count, optimized_stats.displayed_trail_count),
            displayed_gradient_count=max(original_stats.displayed_gradient_count, optimized_stats.displayed_gradient_count),
            policy_name=render_policy.name,
        )
        self._update_render_feedback()

    def _tick(self) -> None:
        if not self.results:
            return
        if self._current_point_count() <= 1:
            self._update_panels()
            return
        self._update_panels()

    def _on_result_changed(self, index: int) -> None:
        self._set_result(index)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self._initial_show_handled:
            self._initial_show_handled = True
            self._schedule_home_reset()
        self._schedule_opengl_diagnostic()

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key == QtCore.Qt.Key.Key_Home:
            self._reset_views()
            return
        if key == QtCore.Qt.Key.Key_Left:
            self._on_manual_range_change()
            self.original_panel.pan_by_fraction(-0.08, 0.0)
            return
        if key == QtCore.Qt.Key.Key_Right:
            self._on_manual_range_change()
            self.original_panel.pan_by_fraction(0.08, 0.0)
            return
        if key == QtCore.Qt.Key.Key_Up:
            self._on_manual_range_change()
            self.original_panel.pan_by_fraction(0.0, 0.08)
            return
        if key == QtCore.Qt.Key.Key_Down:
            self._on_manual_range_change()
            self.original_panel.pan_by_fraction(0.0, -0.08)
            return
        super().keyPressEvent(event)

def launch_viewer(payload: Dict[str, Any], backend_kind: str) -> int:
    if backend_kind == "opengl":
        QtGui.QSurfaceFormat.setDefaultFormat(make_surface_format())
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setApplicationName(APP_DISPLAY_NAME)
    if backend_kind == "opengl":
        probe_opengl()
    window = SequenceViewerWindow(payload, backend_kind)
    window.show()
    exit_code = app.exec()
    if backend_kind == "opengl" and getattr(window, "request_backend_fallback", False):
        raise RuntimeError(window.backend_fallback_reason or "OpenGL lieferte kein darstellbares Punktbild.")
    return exit_code






def run_interactive_viewer(payload_path: str, preferred_backend: str = 'raster') -> int:
    """Run the standalone exact Qt viewer for one payload file."""
    try:
        with open(payload_path, 'r', encoding='utf-8-sig') as handle:
            payload = json.load(handle)
    except Exception as exc:
        print(f'Error loading payload: {exc}')
        return 1
    finally:
        try: Path(payload_path).unlink()
        except OSError: pass
    return launch_viewer(payload, preferred_backend)

def interpolate_color(start_rgb: Tuple[int, int, int], end_rgb: Tuple[int, int, int], t: float) -> str:
    """Return a hex color linearly interpolated between two RGB colors."""
    clamped_t = max(0.0, min(1.0, t))
    r = int(round(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * clamped_t))
    g = int(round(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * clamped_t))
    b = int(round(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * clamped_t))
    return f"#{r:02x}{g:02x}{b:02x}"


def build_trail_colors(count: int) -> List[str]:
    """Build a white-to-red color gradient for the visible trail points."""
    if count <= 1:
        return ["#ff1010"]

    start_rgb = (255, 255, 255)
    end_rgb = (255, 16, 16)
    return [
        interpolate_color(start_rgb, end_rgb, index / (count - 1))
        for index in range(count)
    ]


def compute_viewer_trail_ranges(
    point_count: int,
    base_index: int,
    trail_length: int,
    gradient_window: int,
) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """Split the visible trail into a gray history segment and a gradient segment."""
    if point_count <= 0:
        return None, None

    safe_point_count = max(1, int(point_count))
    safe_base_index = min(max(int(base_index), 0), safe_point_count - 1)
    safe_trail_length = max(1, min(int(trail_length), safe_point_count))
    safe_gradient_window = max(1, min(int(gradient_window), safe_point_count))

    trail_start = max(0, safe_base_index - safe_trail_length + 1)
    visible_count = safe_base_index - trail_start + 1
    gradient_count = min(safe_gradient_window, visible_count)
    gradient_start = safe_base_index - gradient_count + 1
    gray_end = gradient_start - 1

    gray_range = (trail_start, gray_end) if gray_end >= trail_start else None
    gradient_range = (gradient_start, safe_base_index)
    return gray_range, gradient_range


def build_gradient_bin_ranges(start_index: int, end_index: int, bin_count: int) -> List[Tuple[int, int]]:
    """Split an inclusive point range into contiguous bins from old to new."""
    if end_index < start_index or bin_count <= 0:
        return []

    point_count = end_index - start_index + 1
    safe_bin_count = max(1, min(bin_count, point_count))
    ranges: List[Tuple[int, int]] = []

    for bin_index in range(safe_bin_count):
        bin_start_offset = (bin_index * point_count) // safe_bin_count
        bin_end_offset = ((bin_index + 1) * point_count) // safe_bin_count - 1
        ranges.append((start_index + bin_start_offset, start_index + bin_end_offset))

    return ranges


def inclusive_range_difference(
    new_range: Optional[Tuple[int, int]],
    old_range: Optional[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Return the inclusive sub-ranges that are present in new_range but not in old_range."""
    if new_range is None:
        return []
    if old_range is None:
        return [new_range]

    new_start, new_end = new_range
    old_start, old_end = old_range

    if old_end < new_start or old_start > new_end:
        return [new_range]

    segments: List[Tuple[int, int]] = []
    if new_start < old_start:
        segments.append((new_start, min(new_end, old_start - 1)))
    if new_end > old_end:
        segments.append((max(new_start, old_end + 1), new_end))
    return segments


def compute_bounds(result: ProcessedFileResult) -> Tuple[float, float, float, float]:
    """Return display bounds based only on the original source point cloud."""
    return compute_point_bounds(result.original_points)


def build_animation_plan(
    point_count: int,
    speed_multiplier: int,
    progress_callback: ProgressCallback = None,
    cancel_event: object = None,
) -> AnimationPlan:
    """Precompute the full animation timeline at a fixed 30 FPS."""
    raise_if_cancelled(cancel_event)
    safe_multiplier = max(ANIMATION_MIN_MULTIPLIER, min(ANIMATION_MAX_MULTIPLIER, int(speed_multiplier)))
    points_per_second = ANIMATION_BASE_POINTS_PER_SECOND * safe_multiplier
    fps = ANIMATION_MAX_FPS

    if point_count <= 1:
        progress_values = array("f", [0.0])
        if progress_callback is not None:
            progress_callback(1.0, "1 / 1 Frames vorbereitet")
        return AnimationPlan(
            progress_values=progress_values,
            frame_count=1,
            fps=fps,
            points_per_second=points_per_second,
            speed_multiplier=safe_multiplier,
        )

    points_per_frame = points_per_second / fps
    total_frames = max(2, int(math.ceil((point_count - 1) / points_per_frame)) + 1)
    report_every = max(1, total_frames // 200)
    progress_values = array("f")

    for frame_index in range(total_frames):
        if frame_index % 64 == 0:
            raise_if_cancelled(cancel_event)
        progress = min(frame_index * points_per_frame, point_count - 1)
        progress_values.append(progress)
        if progress_callback is not None and (
            frame_index == total_frames - 1 or frame_index % report_every == 0
        ):
            progress_callback((frame_index + 1) / total_frames, f"{frame_index + 1} / {total_frames} Frames vorbereitet")

    return AnimationPlan(
        progress_values=progress_values,
        frame_count=total_frames,
        fps=fps,
        points_per_second=points_per_second,
        speed_multiplier=safe_multiplier,
    )


def process_file_in_subprocess(
    file_index: int,
    total_files: int,
    input_source: InputSource,
    w1: float,
    w2: float,
    memory: int,
    mode: str,
    progress_queue: object = None,
    grid_spacing: float = GRID_SPREAD_DEFAULT_SPACING,
    recent_percent: float = GRID_SPREAD_DEFAULT_RECENT_PERCENT,
    age_decay: float = GRID_SPREAD_AGE_DECAY_DEFAULT,
    ghost_delay: int = GHOST_BEAM_DEFAULT_DELAY,
    forward_jump: int = INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
    backward_jump: int = INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
    hilbert_order: int = HILBERT_ORDER_DEFAULT,
    spot_skip: int = SPOT_SKIP_DEFAULT,
    spiral_direction: str = SPIRAL_DIRECTION_DEFAULT,
    hatch_spacing: float = HATCH_SPACING_UM_DEFAULT,
    cancel_event: object = None,
    macro_strategy: str = MACRO_NONE,
    macro_seg_size_mm: float = MACRO_DEFAULT_SEG_SIZE_MM,
    macro_seg_overlap_um: float = MACRO_DEFAULT_SEG_OVERLAP_UM,
    macro_seg_order: str = MACRO_DEFAULT_SEG_ORDER,
    macro_rotation_deg: float = 0.0,
) -> Tuple[int, int, str, ProcessedFileResult]:
    """Process one file in a separate process and forward progress messages."""
    normalized_source = normalize_input_source(input_source)
    file_name = normalized_source.source_label

    def progress_callback(fraction: float, detail: str) -> None:
        if progress_queue is not None:
            progress_queue.put(("file_progress", file_index, total_files, file_name, fraction, detail))

    result = process_file(
        source=normalized_source,
        w1=w1,
        w2=w2,
        memory=memory,
        mode=mode,
        progress_callback=progress_callback if progress_queue is not None else None,
        grid_spacing=grid_spacing,
        recent_percent=recent_percent,
        age_decay=age_decay,
        ghost_delay=ghost_delay,
        forward_jump=forward_jump,
        backward_jump=backward_jump,
        hilbert_order=hilbert_order,
        spot_skip=spot_skip,
        spiral_direction=spiral_direction,
        hatch_spacing=hatch_spacing,
        cancel_event=cancel_event,
        macro_strategy=macro_strategy,
        macro_seg_size_mm=macro_seg_size_mm,
        macro_seg_overlap_um=macro_seg_overlap_um,
        macro_seg_order=macro_seg_order,
        macro_rotation_deg=macro_rotation_deg,
    )
    return (file_index, total_files, file_name, result)


def build_animation_plan_in_subprocess(
    result_index: int,
    file_name: str,
    point_count: int,
    speed_multiplier: int,
    progress_queue: object = None,
    cancel_event: object = None,
) -> Tuple[int, str, int, AnimationPlan]:
    """Prepare the animation timeline in a separate process and forward progress."""

    def progress_callback(fraction: float, detail: str) -> None:
        if progress_queue is not None:
            progress_queue.put(("animation_progress", result_index, file_name, speed_multiplier, fraction, detail))

    plan = build_animation_plan(
        point_count=point_count,
        speed_multiplier=speed_multiplier,
        progress_callback=progress_callback if progress_queue is not None else None,
        cancel_event=cancel_event,
    )
    return (result_index, file_name, speed_multiplier, plan)






# --- Unified Slicer UI ---

class WorkerSignals(QtCore.QObject):
    progress = QtCore.Signal(float, str)
    finished = QtCore.Signal(object)
    error = QtCore.Signal(str)


class ProcessWorker(QtCore.QRunnable):
    def __init__(self, input_files, w1, w2, memory, mode, zip_entry_types=None, zip_support_end_layer=0, preview_only=False, **kwargs):
        super().__init__()
        self.input_files = input_files
        self.w1 = w1
        self.w2 = w2
        self.memory = memory
        self.mode = mode
        self.zip_entry_types = zip_entry_types
        self.zip_support_end_layer = zip_support_end_layer
        self.preview_only = preview_only
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.is_cancelled = False

    @QtCore.Slot()
    def run(self):
        try:
            expanded_sources, expand_errors = build_input_sources(
                self.input_files,
                zip_entry_types=self.zip_entry_types,
                zip_support_end_layer=self.zip_support_end_layer,
            )
            if expand_errors:
                self.signals.error.emit("\n".join(expand_errors))
                return
            if not expanded_sources:
                self.signals.error.emit("Keine verarbeitbaren Eingabedateien gefunden.")
                return
            if self.preview_only:
                expanded_sources = expanded_sources[:1]

            results = []
            total = len(expanded_sources)
            for i, src in enumerate(expanded_sources):
                if self.is_cancelled:
                    break
                res = process_file_in_subprocess(
                    file_index=i,
                    total_files=total,
                    input_source=src,
                    w1=self.w1,
                    w2=self.w2,
                    memory=self.memory,
                    mode=self.mode,
                    grid_spacing=self.kwargs.get("grid_spacing", GRID_SPREAD_DEFAULT_SPACING),
                    recent_percent=self.kwargs.get("recent_percent", GRID_SPREAD_DEFAULT_RECENT_PERCENT),
                    age_decay=self.kwargs.get("age_decay", GRID_SPREAD_AGE_DECAY_DEFAULT),
                    ghost_delay=self.kwargs.get("ghost_delay", GHOST_BEAM_DEFAULT_DELAY),
                    forward_jump=self.kwargs.get("forward_jump", INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP),
                    backward_jump=self.kwargs.get("backward_jump", INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP),
                    hilbert_order=self.kwargs.get("hilbert_order", HILBERT_ORDER_DEFAULT),
                    spot_skip=self.kwargs.get("spot_skip", SPOT_SKIP_DEFAULT),
                    spiral_direction=self.kwargs.get("spiral_direction", SPIRAL_DIRECTION_DEFAULT),
                    hatch_spacing=self.kwargs.get("hatch_spacing", HATCH_SPACING_UM_DEFAULT),
                    macro_strategy=self.kwargs.get("macro_strategy", MACRO_NONE),
                    macro_seg_size_mm=self.kwargs.get("macro_seg_size_mm", MACRO_DEFAULT_SEG_SIZE_MM),
                    macro_seg_overlap_um=self.kwargs.get("macro_seg_overlap_um", MACRO_DEFAULT_SEG_OVERLAP_UM),
                    macro_seg_order=self.kwargs.get("macro_seg_order", MACRO_DEFAULT_SEG_ORDER),
                    macro_rotation_deg=self.kwargs.get("macro_rotation_deg", 0.0),
                )
                if not self.is_cancelled:
                    results.append(res[3])
                    self.signals.progress.emit((i + 1) / total, f"[{i+1}/{total}] {res[2]}")

            if not self.is_cancelled:
                self.signals.finished.emit(results)
        except Exception as e:
            self.signals.error.emit(str(e))

class SlicerMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.showMaximized()
        
        self.threadpool = QtCore.QThreadPool()
        self.current_worker = None
        self.input_files = []
        self.current_results: List[ProcessedFileResult] = []
        
        # Central Widget (Visualizer)
        self.viewer_widget = None
        self.central_container = QtWidgets.QWidget()
        self.central_layout = QtWidgets.QVBoxLayout(self.central_container)
        self.central_layout.setContentsMargins(0,0,0,0)
        self.setCentralWidget(self.central_container)
        
        # Sidebar Dock (Settings)
        self.dock = QtWidgets.QDockWidget("Slicer Einstellungen", self)
        self.dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.dock)
        
        self._build_sidebar()
        self._set_empty_viewer()

    def _build_sidebar(self):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Dateien ---
        group_files = QtWidgets.QGroupBox("Dateien")
        l_files = QtWidgets.QVBoxLayout(group_files)
        self.list_files = QtWidgets.QListWidget()
        self.list_files.setFixedHeight(80)
        self.btn_load = QtWidgets.QPushButton("ZIP-Datei laden")
        self.btn_load.clicked.connect(self._load_files)

        row_start = QtWidgets.QHBoxLayout()
        row_start.addWidget(QtWidgets.QLabel("Support-Schichten überspringen:"))
        self.spin_start_layer = QtWidgets.QSpinBox()
        self.spin_start_layer.setRange(0, 9999)
        self.spin_start_layer.setValue(0)
        self.spin_start_layer.setToolTip(
            "Schichten bis inkl. dieser Nummer werden übersprungen.\n"
            "0 = keine überspringen."
        )
        row_start.addWidget(self.spin_start_layer)

        l_files.addWidget(self.list_files)
        l_files.addWidget(self.btn_load)
        l_files.addLayout(row_start)
        layout.addWidget(group_files)

        # --- Makro-Segmentierung ---
        group_macro = QtWidgets.QGroupBox("Makro-Segmentierung (Stufe 1)")
        l_macro = QtWidgets.QFormLayout(group_macro)

        self.cb_macro = QtWidgets.QComboBox()
        for k, v in MACRO_STRATEGIES.items():
            self.cb_macro.addItem(v, k)
        self.cb_macro.currentIndexChanged.connect(self._on_macro_mode_changed)
        l_macro.addRow("Strategie:", self.cb_macro)

        self._lbl_macro_size = QtWidgets.QLabel("Segmentgröße (mm):")
        self.spin_macro_size = QtWidgets.QDoubleSpinBox()
        self.spin_macro_size.setRange(0.1, 999.0)
        self.spin_macro_size.setValue(MACRO_DEFAULT_SEG_SIZE_MM)
        self.spin_macro_size.setSingleStep(0.5)
        self.spin_macro_size.setDecimals(1)
        l_macro.addRow(self._lbl_macro_size, self.spin_macro_size)

        self._lbl_macro_overlap = QtWidgets.QLabel("Überlappung (µm):")
        self.spin_macro_overlap = QtWidgets.QDoubleSpinBox()
        self.spin_macro_overlap.setRange(0.0, 5000.0)
        self.spin_macro_overlap.setValue(MACRO_DEFAULT_SEG_OVERLAP_UM)
        self.spin_macro_overlap.setSingleStep(10.0)
        self.spin_macro_overlap.setDecimals(0)
        l_macro.addRow(self._lbl_macro_overlap, self.spin_macro_overlap)

        self._lbl_macro_rotation = QtWidgets.QLabel("Rotation (°):")
        self.spin_macro_rotation = QtWidgets.QDoubleSpinBox()
        self.spin_macro_rotation.setRange(0.0, 360.0)
        self.spin_macro_rotation.setValue(MACRO_DEFAULT_ROTATION_DEG)
        self.spin_macro_rotation.setSingleStep(1.0)
        self.spin_macro_rotation.setDecimals(1)
        l_macro.addRow(self._lbl_macro_rotation, self.spin_macro_rotation)

        self._lbl_macro_order = QtWidgets.QLabel("Reihenfolge:")
        self.cb_macro_order = QtWidgets.QComboBox()
        for order in MACRO_SEGMENT_ORDERS:
            self.cb_macro_order.addItem(order, order)
        l_macro.addRow(self._lbl_macro_order, self.cb_macro_order)

        self._macro_param_widgets = [
            self._lbl_macro_size, self.spin_macro_size,
            self._lbl_macro_overlap, self.spin_macro_overlap,
            self._lbl_macro_rotation, self.spin_macro_rotation,
            self._lbl_macro_order, self.cb_macro_order,
        ]

        self.lbl_macro_description = QtWidgets.QLabel()
        self.lbl_macro_description.setWordWrap(True)
        self.lbl_macro_description.setStyleSheet("color: #aaa; font-size: 10px; padding: 2px;")
        l_macro.addRow(self.lbl_macro_description)
        layout.addWidget(group_macro)

        # --- Mikro-Strategie ---
        group_micro = QtWidgets.QGroupBox("Mikro-Strategie (Stufe 2)")
        l_micro = QtWidgets.QFormLayout(group_micro)

        self.cb_micro = QtWidgets.QComboBox()
        for k, v in MODE_SPECS.items():
            self.cb_micro.addItem(v.label, k)
        self.cb_micro.currentIndexChanged.connect(self._on_micro_mode_changed)
        l_micro.addRow("Strategie:", self.cb_micro)

        self._lbl_memory = QtWidgets.QLabel("Gedächtnis (Punkte):")
        self.spin_mem = QtWidgets.QSpinBox()
        self.spin_mem.setRange(0, 500)
        self.spin_mem.setValue(MEMORY_DEFAULT)
        self.spin_mem.setToolTip("Anzahl der zuletzt besuchten Punkte für die Abstoßung.")
        l_micro.addRow(self._lbl_memory, self.spin_mem)

        self._lbl_grid = QtWidgets.QLabel("Gittergröße (mm):")
        self.spin_grid = QtWidgets.QDoubleSpinBox()
        self.spin_grid.setRange(0.01, 50.0)
        self.spin_grid.setValue(GRID_SPREAD_DEFAULT_SPACING)
        self.spin_grid.setDecimals(2)
        self.spin_grid.setSingleStep(0.1)
        self.spin_grid.setToolTip("Zellgröße des virtuellen Gitters in mm.")
        l_micro.addRow(self._lbl_grid, self.spin_grid)

        self._lbl_recent = QtWidgets.QLabel("Aktuelle % (0–100):")
        self.spin_recent = QtWidgets.QDoubleSpinBox()
        self.spin_recent.setRange(1.0, 100.0)
        self.spin_recent.setValue(GRID_SPREAD_DEFAULT_RECENT_PERCENT)
        self.spin_recent.setSingleStep(1.0)
        self.spin_recent.setDecimals(0)
        self.spin_recent.setToolTip("Anteil der zuletzt besuchten Punkte (%) für Gitterdispersion.")
        l_micro.addRow(self._lbl_recent, self.spin_recent)

        self._lbl_age = QtWidgets.QLabel("Alterungsrate (0–1):")
        self.spin_age = QtWidgets.QDoubleSpinBox()
        self.spin_age.setRange(0.01, 1.0)
        self.spin_age.setValue(GRID_SPREAD_AGE_DECAY_DEFAULT)
        self.spin_age.setSingleStep(0.05)
        self.spin_age.setDecimals(2)
        self.spin_age.setToolTip("Exponentielle Alterung (0 = sofort vergessen, 1 = nie vergessen).")
        l_micro.addRow(self._lbl_age, self.spin_age)

        self._lbl_ghost = QtWidgets.QLabel("Ghost-Verzögerung (Punkte):")
        self.spin_ghost = QtWidgets.QSpinBox()
        self.spin_ghost.setRange(1, 9999)
        self.spin_ghost.setValue(GHOST_BEAM_DEFAULT_DELAY)
        self.spin_ghost.setToolTip("Versatz zwischen Primär- und Geiststrahl in Punkten.")
        l_micro.addRow(self._lbl_ghost, self.spin_ghost)

        self._lbl_fwd = QtWidgets.QLabel("Vorwärts-Sprung:")
        self.spin_fwd = QtWidgets.QSpinBox()
        self.spin_fwd.setRange(1, 20)
        self.spin_fwd.setValue(INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP)
        self.spin_fwd.setToolTip("Schrittweite vorwärts beim verschachtelten Streifen-Scan.")
        l_micro.addRow(self._lbl_fwd, self.spin_fwd)

        self._lbl_bwd = QtWidgets.QLabel("Rückwärts-Sprung:")
        self.spin_bwd = QtWidgets.QSpinBox()
        self.spin_bwd.setRange(1, 20)
        self.spin_bwd.setValue(INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP)
        self.spin_bwd.setToolTip("Schrittweite rückwärts beim verschachtelten Streifen-Scan.")
        l_micro.addRow(self._lbl_bwd, self.spin_bwd)

        self._lbl_hilbert = QtWidgets.QLabel("Hilbert-Ordnung (2–7):")
        self.spin_hilbert = QtWidgets.QSpinBox()
        self.spin_hilbert.setRange(2, 7)
        self.spin_hilbert.setValue(HILBERT_ORDER_DEFAULT)
        self.spin_hilbert.setToolTip("Gitterauflösung der Hilbert/Peano-Kurve (2^n × 2^n).")
        l_micro.addRow(self._lbl_hilbert, self.spin_hilbert)

        self._lbl_spot = QtWidgets.QLabel("Spot-Überspringen (1–20):")
        self.spin_spot = QtWidgets.QSpinBox()
        self.spin_spot.setRange(1, 20)
        self.spin_spot.setValue(SPOT_SKIP_DEFAULT)
        self.spin_spot.setToolTip("Raster-Positionen zwischen zwei Passes.")
        l_micro.addRow(self._lbl_spot, self.spin_spot)

        self._lbl_spiral_dir = QtWidgets.QLabel("Spiralrichtung:")
        self.cb_spiral_dir = QtWidgets.QComboBox()
        self.cb_spiral_dir.addItem("Außen → innen", "inward")
        self.cb_spiral_dir.addItem("Innen → außen", "outward")
        l_micro.addRow(self._lbl_spiral_dir, self.cb_spiral_dir)

        self._lbl_hatch = QtWidgets.QLabel("Hatch-Abstand (µm):")
        self.spin_hatch = QtWidgets.QDoubleSpinBox()
        self.spin_hatch.setRange(10.0, 2000.0)
        self.spin_hatch.setValue(HATCH_SPACING_UM_DEFAULT)
        self.spin_hatch.setSingleStep(10.0)
        self.spin_hatch.setDecimals(0)
        self.spin_hatch.setToolTip("Abstand zwischen Scan-Spuren in µm (für Spirale).")
        l_micro.addRow(self._lbl_hatch, self.spin_hatch)

        self._micro_param_pairs: Dict[str, Tuple] = {
            MODE_PARAMETER_MEMORY: (self._lbl_memory, self.spin_mem),
            MODE_PARAMETER_GRID_SPACING: (self._lbl_grid, self.spin_grid),
            MODE_PARAMETER_RECENT_PERCENT: (self._lbl_recent, self.spin_recent),
            MODE_PARAMETER_AGE_DECAY: (self._lbl_age, self.spin_age),
            MODE_PARAMETER_GHOST_DELAY: (self._lbl_ghost, self.spin_ghost),
            MODE_PARAMETER_FORWARD_JUMP: (self._lbl_fwd, self.spin_fwd),
            MODE_PARAMETER_BACKWARD_JUMP: (self._lbl_bwd, self.spin_bwd),
            MODE_PARAMETER_HILBERT_ORDER: (self._lbl_hilbert, self.spin_hilbert),
            MODE_PARAMETER_SPOT_SKIP: (self._lbl_spot, self.spin_spot),
            MODE_PARAMETER_SPIRAL_DIRECTION: (self._lbl_spiral_dir, self.cb_spiral_dir),
            MODE_PARAMETER_HATCH_SPACING: (self._lbl_hatch, self.spin_hatch),
        }

        self.lbl_micro_description = QtWidgets.QLabel()
        self.lbl_micro_description.setWordWrap(True)
        self.lbl_micro_description.setStyleSheet("color: #aaa; font-size: 10px; padding: 2px;")
        l_micro.addRow(self.lbl_micro_description)
        layout.addWidget(group_micro)

        # --- Aktionen ---
        self.btn_calc = QtWidgets.QPushButton("Strategie berechnen")
        self.btn_calc.setFixedHeight(40)
        self.btn_calc.clicked.connect(self._start_calculation)
        layout.addWidget(self.btn_calc)

        self.btn_save = QtWidgets.QPushButton("Ergebnisse als ZIP speichern")
        self.btn_save.setFixedHeight(36)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_results)
        layout.addWidget(self.btn_save)

        # --- Status ---
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)
        self.lbl_status = QtWidgets.QLabel("Bereit.")
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.lbl_status)

        layout.addStretch()
        scroll.setWidget(w)
        self.dock.setWidget(scroll)

        self._on_macro_mode_changed()
        self._on_micro_mode_changed()

    def _on_macro_mode_changed(self):
        macro_id = self.cb_macro.currentData() if self.cb_macro.currentData() else MACRO_NONE
        show_params = macro_id != MACRO_NONE
        for widget in self._macro_param_widgets:
            widget.setVisible(show_params)
        macro_label = MACRO_STRATEGIES.get(macro_id, "")
        self.lbl_macro_description.setText(macro_label if show_params else "")

    def _on_micro_mode_changed(self):
        mode_id = self.cb_micro.currentData()
        spec = MODE_SPECS.get(mode_id)
        visible_params = spec.visible_parameters if spec else ()
        for param_key, (lbl, widget) in self._micro_param_pairs.items():
            show = param_key in visible_params
            lbl.setVisible(show)
            widget.setVisible(show)
        desc = spec.description if spec else ""
        self.lbl_micro_description.setText(desc)

    def _load_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "ZIP-Datei laden", "",
            "ZIP-Dateien (*.zip);;"
            "Unterstützte Dateien (*.txt *.abs *.b99 *.zip *.step *.stp);;"
            "Alle Dateien (*.*)"
        )
        if not paths:
            return
        self.input_files = paths
        self.list_files.clear()
        for p in paths:
            self.list_files.addItem(os.path.basename(p))
        self.btn_save.setEnabled(False)
        self.lbl_status.setText(f"{len(paths)} Datei(en) geladen.")

        # Auto-Vorschau der ersten Schicht
        if self.current_worker is None:
            self._start_preview()

    def _start_preview(self):
        self._set_empty_viewer()
        preview_worker = ProcessWorker(
            self.input_files,
            w1=W1_DEFAULT, w2=W2_DEFAULT, memory=0,
            mode="direct_visualisation",
            zip_support_end_layer=self.spin_start_layer.value(),
            preview_only=True,
        )
        preview_worker.signals.finished.connect(self._on_preview_finished)
        preview_worker.signals.error.connect(lambda e: self.lbl_status.setText(f"Vorschau-Fehler: {e}"))
        self.threadpool.start(preview_worker)
        self.lbl_status.setText("Lade Vorschau…")

    def _set_empty_viewer(self):
        if self.viewer_widget:
            self.viewer_widget.deleteLater()
            self.viewer_widget = None
        empty_lbl = QtWidgets.QLabel("Bitte lade eine ZIP-Datei.")
        empty_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.central_layout.addWidget(empty_lbl)
        self.viewer_widget = empty_lbl

    def _start_calculation(self):
        if not self.input_files:
            QtWidgets.QMessageBox.warning(self, "Fehler", "Bitte zuerst eine Datei laden!")
            return

        self.btn_calc.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_status.setText("Berechne…")

        if self.viewer_widget:
            self.viewer_widget.deleteLater()
            self.viewer_widget = None

        self.current_worker = ProcessWorker(
            self.input_files,
            w1=W1_DEFAULT,
            w2=W2_DEFAULT,
            memory=self.spin_mem.value(),
            mode=self.cb_micro.currentData(),
            zip_support_end_layer=self.spin_start_layer.value(),
            macro_strategy=self.cb_macro.currentData(),
            grid_spacing=self.spin_grid.value(),
            recent_percent=self.spin_recent.value(),
            age_decay=self.spin_age.value(),
            ghost_delay=self.spin_ghost.value(),
            forward_jump=self.spin_fwd.value(),
            backward_jump=self.spin_bwd.value(),
            hilbert_order=self.spin_hilbert.value(),
            spot_skip=self.spin_spot.value(),
            spiral_direction=self.cb_spiral_dir.currentData(),
            hatch_spacing=self.spin_hatch.value(),
            macro_seg_size_mm=self.spin_macro_size.value(),
            macro_seg_overlap_um=self.spin_macro_overlap.value(),
            macro_seg_order=self.cb_macro_order.currentData(),
            macro_rotation_deg=self.spin_macro_rotation.value(),
        )
        self.current_worker.signals.progress.connect(self._on_progress)
        self.current_worker.signals.finished.connect(self._on_finished)
        self.current_worker.signals.error.connect(self._on_error)
        self.threadpool.start(self.current_worker)

    def _on_progress(self, frac, detail):
        self.progress_bar.setValue(int(frac * 100))
        self.lbl_status.setText(detail)

    def _on_error(self, err):
        self.current_worker = None
        self.btn_calc.setEnabled(True)
        self.lbl_status.setText("Fehler!")
        QtWidgets.QMessageBox.critical(self, "Fehler", err)

    def _on_finished(self, results):
        self.current_worker = None
        self.btn_calc.setEnabled(True)
        self.btn_save.setEnabled(bool(results))
        self.current_results = results
        self.lbl_status.setText("Fertig.")
        self.progress_bar.setValue(100)
        self._show_viewer(results)

    def _on_preview_finished(self, results):
        if results:
            total_layers = len(list(build_input_sources(self.input_files, zip_support_end_layer=self.spin_start_layer.value())[0]))
            self.lbl_status.setText(f"Vorschau geladen. {total_layers} Schichten gesamt.")
            self._show_viewer(results)
        else:
            self.lbl_status.setText("Bereit.")

    def _show_viewer(self, results):
        payload = self._build_viewer_payload(results)
        try:
            new_viewer = SequenceViewerWindow(payload, "raster")
            for i in reversed(range(self.central_layout.count())):
                w = self.central_layout.itemAt(i).widget()
                if w is not None:
                    w.setParent(None)
            self.central_layout.addWidget(new_viewer)
            self.viewer_widget = new_viewer
        except Exception as e:
            self.lbl_status.setText(f"Viewer-Fehler: {e}")

    def _save_results(self) -> None:
        if not self.current_results:
            QtWidgets.QMessageBox.warning(self, "Speichern", "Keine Ergebnisse zum Speichern vorhanden.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Ergebnisse als ZIP speichern", "", "ZIP-Dateien (*.zip)"
        )
        if not path:
            return
        try:
            save_results_as_zip(self.current_results, path)
            self.lbl_status.setText(f"Gespeichert: {os.path.basename(path)}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Speicherfehler", str(e))

    def _build_viewer_payload(self, results) -> Dict[str, Any]:
        return {
            "app_name": APP_DISPLAY_NAME,
            "mode_id": self.cb_micro.currentData(),
            "mode_label": self.cb_micro.currentText(),
            "mode_description": "",
            "coordinate_scale_mm": DISPLAY_COORDINATE_SCALE_MM,
            "coordinate_unit": "mm",
            "build_plate_width_mm": BUILD_PLATE_WIDTH_MM,
            "build_plate_depth_mm": BUILD_PLATE_DEPTH_MM,
            "origin_reference": "build_plate_centre",
            "point_spacing_mm": DISPLAY_POINT_SPACING_MM,
            "selected_index": 0,
            "results": [
                {
                    "source_label": result.source_label,
                    "source_path": str(result.source_path),
                    "archive_member": result.archive_member,
                    "output_name": result.output_name,
                    "point_count": len(result.original_points),
                    "original_points": [[point[0], point[1]] for point in result.original_points],
                    "optimized_points": [[point[0], point[1]] for point in result.optimized_points],
                }
                for result in results
            ],
        }

def run_self_tests() -> None:
    """Smoke-Test aller Optimierungsmodi mit einem Minimal-Punktesatz."""
    pts = [(float(i) / 100, float(i % 10) / 100) for i in range(20)]
    for mode in OPTIMIZATION_MODES:
        optimize_path(pts, mode=mode)
    print("run_self_tests: OK")


if __name__ == "__main__":
    mp.freeze_support()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    window = SlicerMainWindow()
    window.show()
    sys.exit(app.exec())
