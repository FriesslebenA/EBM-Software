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

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


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
MODE_PARAMETER_GHOST_DELAY = "ghost_delay"
MODE_PARAMETER_FORWARD_JUMP = "forward_jump"
MODE_PARAMETER_BACKWARD_JUMP = "backward_jump"
MODE_PARAMETER_HILBERT_ORDER = "hilbert_order"
MODE_PARAMETER_SPOT_SKIP = "spot_skip"
HILBERT_ORDER_DEFAULT = 4
SPOT_SKIP_DEFAULT = 2
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
        visible_parameters=(),
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

    with open(file_path, "r", encoding="utf-8") as handle:
        return _parse_points_from_lines(handle.readlines(), file_path)


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
    """Classify the type digit from a B99 filename."""
    if type_digit == 9:
        return "combo"
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
    cancel_event: object = None,
) -> List[Point]:
    """Optimize point order with the selected strategy."""
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
    cancel_event: object = None,
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
        cancel_event=cancel_event,
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


def ask_for_input_files(parent: tk.Misc) -> Tuple[str, ...]:
    """Open the Windows file picker for one or more supported input files."""
    parent.update_idletasks()
    return filedialog.askopenfilenames(
        parent=parent,
        title="Dateien auswaehlen",
        filetypes=(
            ("Unterstuetzte Dateien", "*.txt *.b99 *.zip *.step *.stp"),
            ("Textdateien", "*.txt"),
            ("B99-Dateien", "*.b99"),
            ("ZIP-Dateien", "*.zip"),
            ("STEP-Dateien", "*.step *.stp"),
            ("Alle Dateien", "*.*"),
        ),
    )


def ask_for_zip_path(parent: tk.Misc, initial_name: str) -> str:
    """Open the Windows save dialog for the ZIP archive."""
    return filedialog.asksaveasfilename(
        parent=parent,
        title="ZIP-Datei speichern",
        defaultextension=".zip",
        filetypes=(("ZIP-Dateien", "*.zip"),),
        initialfile=initial_name,
    )


def ask_for_zip_import_settings(
    parent: tk.Misc,
    current_entry_types: Sequence[str],
    current_support_end_layer: int,
) -> Optional[Dict[str, object]]:
    """Ask which B99 types should be imported from ZIP files and where support ends."""
    dialog = tk.Toplevel(parent)
    dialog.title("ZIP-Import")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.configure(bg="#1a1a1a")

    result = {"value": None}
    normalized_types = set(normalize_zip_entry_types(current_entry_types))
    entry_type_vars = {
        entry_type: tk.BooleanVar(value=entry_type in normalized_types)
        for entry_type in ZIP_ENTRY_TYPES
    }
    support_end_var = tk.StringVar(value=str(current_support_end_layer))
    help_var = tk.StringVar(
        value=(
            "ZIP-Dateien werden nach .B99-Dateien durchsucht. "
            "Waehlen Sie die Arten und bis zu welcher Schicht die Stuetstruktur ignoriert werden soll."
        )
    )

    frame = tk.Frame(dialog, bg="#1a1a1a", padx=18, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text="ZIP-Import fuer .B99-Eintraege",
        bg="#1a1a1a",
        fg="#f0f0f0",
        font=("Segoe UI", 10, "bold"),
        justify="left",
    ).pack(anchor="w")

    form = tk.Frame(frame, bg="#1a1a1a")
    form.pack(fill="x", pady=(14, 10))

    tk.Label(form, text="Arten", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=0,
        column=0,
        sticky="nw",
        pady=4,
    )
    type_frame = tk.Frame(form, bg="#1a1a1a")
    type_frame.grid(row=0, column=1, sticky="w", pady=4)
    type_buttons: List[tk.Checkbutton] = []
    for entry_type in ZIP_ENTRY_TYPES:
        button = tk.Checkbutton(
            type_frame,
            text=ZIP_ENTRY_TYPE_LABELS.get(entry_type, entry_type),
            variable=entry_type_vars[entry_type],
            onvalue=True,
            offvalue=False,
            bg="#1a1a1a",
            fg="#f0f0f0",
            activebackground="#1a1a1a",
            activeforeground="#f0f0f0",
            selectcolor="#2a2a2a",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        )
        button.pack(anchor="w")
        type_buttons.append(button)

    tk.Label(form, text="Stuetzstruktur bis Schicht", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=1,
        column=0,
        sticky="w",
        pady=4,
    )
    support_spin = tk.Spinbox(
        form,
        from_=0,
        to=999999,
        textvariable=support_end_var,
        width=12,
        font=("Segoe UI", 10),
    )
    support_spin.grid(row=1, column=1, sticky="w", pady=4)

    tk.Label(
        frame,
        textvariable=help_var,
        bg="#1a1a1a",
        fg="#d0d0d0",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=460,
    ).pack(anchor="w", pady=(0, 14))

    button_row = tk.Frame(frame, bg="#1a1a1a")
    button_row.pack(fill="x")

    def confirm() -> None:
        selected_types = tuple(
            entry_type for entry_type in ZIP_ENTRY_TYPES if bool(entry_type_vars[entry_type].get())
        )
        if not selected_types:
            messagebox.showerror("ZIP-Import", "Bitte mindestens eine gueltige Art auswaehlen.", parent=dialog)
            return

        try:
            support_end_layer = int(support_end_var.get())
        except ValueError:
            messagebox.showerror("ZIP-Import", "Bitte eine ganze Zahl fuer die Schicht eingeben.", parent=dialog)
            return

        if support_end_layer < 0:
            messagebox.showerror("ZIP-Import", "Die Schicht darf nicht negativ sein.", parent=dialog)
            return

        result["value"] = {
            "entry_types": selected_types,
            "support_end_layer": support_end_layer,
        }
        dialog.destroy()

    ttk.Button(button_row, text="Weiter", command=confirm).pack(side="left")
    ttk.Button(button_row, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=(10, 0))

    center_toplevel(parent, dialog)
    if type_buttons:
        type_buttons[0].focus_set()
    dialog.wait_window()
    return result["value"]


def ask_for_step_import_settings(
    parent: tk.Misc,
    current_point_spacing_mm: float,
    current_layer_height_mm: float,
    current_support_layer_count: int,
) -> Optional[Dict[str, Union[float, int]]]:
    """Ask for the geometric STEP import settings."""
    dialog = tk.Toplevel(parent)
    dialog.title("STEP-Import")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.configure(bg="#1a1a1a")

    result = {"value": None}
    point_spacing_var = tk.StringVar(value=f"{float(current_point_spacing_mm):.6g}")
    layer_height_var = tk.StringVar(value=f"{float(current_layer_height_mm):.6g}")
    support_layer_var = tk.StringVar(value=str(max(0, int(current_support_layer_count))))

    frame = tk.Frame(dialog, bg="#1a1a1a", padx=18, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text="STEP-Import zu B99-Layern",
        bg="#1a1a1a",
        fg="#f0f0f0",
        font=("Segoe UI", 10, "bold"),
        justify="left",
    ).pack(anchor="w")

    form = tk.Frame(frame, bg="#1a1a1a")
    form.pack(fill="x", pady=(14, 10))

    tk.Label(form, text="Punktabstand (mm)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=0,
        column=0,
        sticky="w",
        pady=4,
    )
    point_spacing_entry = tk.Entry(form, textvariable=point_spacing_var, width=14, font=("Segoe UI", 10))
    point_spacing_entry.grid(row=0, column=1, sticky="w", pady=4)

    tk.Label(form, text="Ebenendicke (mm)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=1,
        column=0,
        sticky="w",
        pady=4,
    )
    layer_height_entry = tk.Entry(form, textvariable=layer_height_var, width=14, font=("Segoe UI", 10))
    layer_height_entry.grid(row=1, column=1, sticky="w", pady=4)

    tk.Label(form, text="Stuetzschichten", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=2,
        column=0,
        sticky="w",
        pady=4,
    )
    support_layer_spin = tk.Spinbox(
        form,
        from_=0,
        to=9999,
        textvariable=support_layer_var,
        width=12,
        font=("Segoe UI", 10),
    )
    support_layer_spin.grid(row=2, column=1, sticky="w", pady=4)

    tk.Label(
        frame,
        text=(
            "STEP-Einheiten werden als Millimeter interpretiert. "
            "Die Schichten werden entlang der globalen Z-Achse gesliced, "
            "und die XY-Koordinaten bleiben unveraendert. "
            "Stuetzschichten nutzen die Outline der ersten Bauteilschicht."
        ),
        bg="#1a1a1a",
        fg="#d0d0d0",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=460,
    ).pack(anchor="w", pady=(0, 14))

    button_row = tk.Frame(frame, bg="#1a1a1a")
    button_row.pack(fill="x")

    def confirm() -> None:
        try:
            point_spacing_mm = float(point_spacing_var.get())
            layer_height_mm = float(layer_height_var.get())
            support_layer_count = int(support_layer_var.get())
        except ValueError:
            messagebox.showerror(
                "STEP-Import",
                "Bitte gueltige Werte fuer Punktabstand, Ebenendicke und Stuetzschichten eingeben.",
                parent=dialog,
            )
            return

        if point_spacing_mm <= 0.0 or layer_height_mm <= 0.0:
            messagebox.showerror(
                "STEP-Import",
                "Punktabstand und Ebenendicke muessen groesser als 0 sein.",
                parent=dialog,
            )
            return
        if support_layer_count < 0:
            messagebox.showerror(
                "STEP-Import",
                "Die Anzahl der Stuetzschichten muss groesser oder gleich 0 sein.",
                parent=dialog,
            )
            return

        result["value"] = {
            "point_spacing_mm": float(point_spacing_mm),
            "layer_height_mm": float(layer_height_mm),
            "support_layer_count": int(support_layer_count),
        }
        dialog.destroy()

    ttk.Button(button_row, text="Weiter", command=confirm).pack(side="left")
    ttk.Button(button_row, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=(10, 0))

    center_toplevel(parent, dialog)
    point_spacing_entry.focus_set()
    dialog.wait_window()
    return result["value"]


def ask_for_optimization_settings(
    parent: tk.Misc,
    current_mode: str,
    current_memory: int,
    current_grid_spacing: float,
    current_recent_percent: float,
    current_ghost_delay: int,
    current_forward_jump: int,
    current_backward_jump: int,
    current_hilbert_order: int = HILBERT_ORDER_DEFAULT,
    current_spot_skip: int = SPOT_SKIP_DEFAULT,
) -> Optional[Dict[str, object]]:
    """Ask for the processing mode and its mode-specific parameters."""
    dialog = tk.Toplevel(parent)
    dialog.title(f"{APP_DISPLAY_NAME} - Modusauswahl")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.configure(bg="#1a1a1a")

    result = {"value": None}
    mode_var = tk.StringVar(value=get_mode_label(current_mode))
    memory_var = tk.StringVar(value=str(current_memory))
    grid_spacing_var = tk.StringVar(value=f"{current_grid_spacing:.6g}")
    recent_percent_var = tk.StringVar(value=f"{current_recent_percent:.6g}")
    ghost_delay_var = tk.StringVar(value=str(current_ghost_delay))
    forward_jump_var = tk.StringVar(value=str(current_forward_jump))
    backward_jump_var = tk.StringVar(value=str(current_backward_jump))
    hilbert_order_var = tk.StringVar(value=str(current_hilbert_order))
    spot_skip_var = tk.StringVar(value=str(current_spot_skip))
    help_var = tk.StringVar()
    parameter_hint_var = tk.StringVar()
    mode_labels = [MODE_SPECS[mode_id].label for mode_id in OPTIMIZATION_MODES]
    label_to_mode = {spec.label: spec.canonical_id for spec in MODE_SPECS.values()}

    frame = tk.Frame(dialog, bg="#1a1a1a", padx=18, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text="Waehlen Sie den Verarbeitungsmodus und die dazugehoerigen wissenschaftlichen Parameter.",
        bg="#1a1a1a",
        fg="#f0f0f0",
        font=("Segoe UI", 10, "bold"),
        justify="left",
        wraplength=460,
    ).pack(anchor="w")

    form = tk.Frame(frame, bg="#1a1a1a")
    form.pack(fill="x", pady=(14, 10))

    tk.Label(form, text="Modus", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=0,
        column=0,
        sticky="w",
        pady=4,
    )
    mode_box = ttk.Combobox(form, textvariable=mode_var, values=mode_labels, state="readonly", width=34)
    mode_box.grid(row=0, column=1, sticky="we", pady=4)

    memory_label = tk.Label(form, text="Memory", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    memory_spin = tk.Spinbox(form, from_=0, to=9999, textvariable=memory_var, width=12, font=("Segoe UI", 10))

    grid_spacing_label = tk.Label(form, text="Grid spacing", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    grid_spacing_entry = tk.Entry(form, textvariable=grid_spacing_var, width=14, font=("Segoe UI", 10))

    recent_percent_label = tk.Label(form, text="Recent set (%)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    recent_percent_spin = tk.Spinbox(
        form,
        from_=1,
        to=100,
        increment=1,
        textvariable=recent_percent_var,
        width=12,
        font=("Segoe UI", 10),
    )
    ghost_delay_label = tk.Label(form, text="Ghost delay (points)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    ghost_delay_spin = tk.Spinbox(
        form,
        from_=1,
        to=9999,
        textvariable=ghost_delay_var,
        width=12,
        font=("Segoe UI", 10),
    )
    forward_jump_label = tk.Label(form, text="Forward jump", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    forward_jump_spin = tk.Spinbox(
        form,
        from_=1,
        to=9999,
        textvariable=forward_jump_var,
        width=12,
        font=("Segoe UI", 10),
    )
    backward_jump_label = tk.Label(form, text="Backward jump", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    backward_jump_spin = tk.Spinbox(
        form,
        from_=1,
        to=9999,
        textvariable=backward_jump_var,
        width=12,
        font=("Segoe UI", 10),
    )
    hilbert_order_label = tk.Label(form, text="Hilbert order (2–7)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    hilbert_order_spin = tk.Spinbox(
        form,
        from_=2,
        to=7,
        textvariable=hilbert_order_var,
        width=12,
        font=("Segoe UI", 10),
    )
    spot_skip_label = tk.Label(form, text="Spot skip (passes−1)", bg="#1a1a1a", fg="#f0f0f0", font=("Segoe UI", 10))
    spot_skip_spin = tk.Spinbox(
        form,
        from_=1,
        to=20,
        textvariable=spot_skip_var,
        width=12,
        font=("Segoe UI", 10),
    )

    age_decay_label = tk.Label(
        form,
        text=f"Age decay (fixed): {GRID_SPREAD_AGE_DECAY_DEFAULT:.2f}",
        bg="#1a1a1a",
        fg="#cfcfcf",
        font=("Segoe UI", 10),
        justify="left",
    )
    parameter_hint_label = tk.Label(
        form,
        textvariable=parameter_hint_var,
        bg="#1a1a1a",
        fg="#cfcfcf",
        font=("Segoe UI", 9, "italic"),
        justify="left",
        wraplength=460,
    )

    memory_label.grid(
        row=1,
        column=0,
        sticky="w",
        pady=4,
    )
    memory_spin.grid(row=1, column=1, sticky="w", pady=4)

    grid_spacing_label.grid(
        row=2,
        column=0,
        sticky="w",
        pady=4,
    )
    grid_spacing_entry.grid(row=2, column=1, sticky="w", pady=4)

    recent_percent_label.grid(
        row=3,
        column=0,
        sticky="w",
        pady=4,
    )
    recent_percent_spin.grid(row=3, column=1, sticky="w", pady=4)

    ghost_delay_label.grid(
        row=4,
        column=0,
        sticky="w",
        pady=4,
    )
    ghost_delay_spin.grid(row=4, column=1, sticky="w", pady=4)

    forward_jump_label.grid(
        row=5,
        column=0,
        sticky="w",
        pady=4,
    )
    forward_jump_spin.grid(row=5, column=1, sticky="w", pady=4)

    backward_jump_label.grid(
        row=6,
        column=0,
        sticky="w",
        pady=4,
    )
    backward_jump_spin.grid(row=6, column=1, sticky="w", pady=4)

    hilbert_order_label.grid(row=7, column=0, sticky="w", pady=4)
    hilbert_order_spin.grid(row=7, column=1, sticky="w", pady=4)

    spot_skip_label.grid(row=8, column=0, sticky="w", pady=4)
    spot_skip_spin.grid(row=8, column=1, sticky="w", pady=4)

    age_decay_label.grid(
        row=9,
        column=0,
        columnspan=2,
        sticky="w",
        pady=(8, 0),
    )
    parameter_hint_label.grid(
        row=10,
        column=0,
        columnspan=2,
        sticky="w",
        pady=(10, 0),
    )
    form.grid_columnconfigure(1, weight=1)

    tk.Label(
        frame,
        textvariable=help_var,
        bg="#1a1a1a",
        fg="#d0d0d0",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=460,
    ).pack(anchor="w", pady=(0, 14))

    button_row = tk.Frame(frame, bg="#1a1a1a")
    button_row.pack(fill="x")

    def update_mode_fields(*_args: object) -> None:
        selected_mode = label_to_mode.get(mode_var.get(), normalize_mode(current_mode))
        spec = get_mode_spec(selected_mode)
        visible_parameters = set(spec.visible_parameters)

        for widget in (memory_label, memory_spin):
            if MODE_PARAMETER_MEMORY in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (grid_spacing_label, grid_spacing_entry):
            if MODE_PARAMETER_GRID_SPACING in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (recent_percent_label, recent_percent_spin):
            if MODE_PARAMETER_RECENT_PERCENT in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (ghost_delay_label, ghost_delay_spin):
            if MODE_PARAMETER_GHOST_DELAY in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (forward_jump_label, forward_jump_spin):
            if MODE_PARAMETER_FORWARD_JUMP in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (backward_jump_label, backward_jump_spin):
            if MODE_PARAMETER_BACKWARD_JUMP in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (hilbert_order_label, hilbert_order_spin):
            if MODE_PARAMETER_HILBERT_ORDER in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        for widget in (spot_skip_label, spot_skip_spin):
            if MODE_PARAMETER_SPOT_SKIP in visible_parameters:
                widget.grid()
            else:
                widget.grid_remove()

        if MODE_PARAMETER_GRID_SPACING in visible_parameters:
            age_decay_label.grid()
        else:
            age_decay_label.grid_remove()

        if visible_parameters:
            parameter_hint_var.set("")
            parameter_hint_label.grid_remove()
        else:
            parameter_hint_var.set("Dieser Modus benoetigt keine zusaetzlichen Eingabeparameter.")
            parameter_hint_label.grid()

        help_var.set(spec.description)

    def confirm() -> None:
        try:
            selected_mode = label_to_mode[mode_var.get()]
        except KeyError:
            messagebox.showerror("Pfadberechnung", "Bitte einen gueltigen Modus auswaehlen.", parent=dialog)
            return

        spec = get_mode_spec(selected_mode)
        visible_parameters = set(spec.visible_parameters)

        memory_value = current_memory
        grid_spacing = current_grid_spacing
        recent_percent = current_recent_percent
        ghost_delay_value = current_ghost_delay
        forward_jump_value = current_forward_jump
        backward_jump_value = current_backward_jump
        hilbert_order_value = current_hilbert_order
        spot_skip_value = current_spot_skip

        if MODE_PARAMETER_MEMORY in visible_parameters:
            try:
                memory_value = int(memory_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Memory eine ganze Zahl eingeben.", parent=dialog)
                return

        if MODE_PARAMETER_MEMORY in visible_parameters and memory_value < 0:
            messagebox.showerror("Pfadberechnung", "Memory darf nicht negativ sein.", parent=dialog)
            return

        if MODE_PARAMETER_GRID_SPACING in visible_parameters:
            try:
                grid_spacing = float(grid_spacing_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte einen gueltigen Gitterabstand eingeben.", parent=dialog)
                return

            try:
                recent_percent = float(recent_percent_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte einen gueltigen Prozentwert eingeben.", parent=dialog)
                return

            if grid_spacing <= 0.0:
                messagebox.showerror("Pfadberechnung", "Der Gitterabstand muss groesser als 0 sein.", parent=dialog)
                return

            if not 0.0 < recent_percent <= 100.0:
                messagebox.showerror("Pfadberechnung", "Der Prozentwert muss zwischen 0 und 100 liegen.", parent=dialog)
                return

        if MODE_PARAMETER_GHOST_DELAY in visible_parameters:
            try:
                ghost_delay_value = int(ghost_delay_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Ghost delay eine ganze Zahl eingeben.", parent=dialog)
                return
            if ghost_delay_value < 1:
                messagebox.showerror("Pfadberechnung", "Ghost delay muss mindestens 1 sein.", parent=dialog)
                return

        if MODE_PARAMETER_FORWARD_JUMP in visible_parameters:
            try:
                forward_jump_value = int(forward_jump_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Forward jump eine ganze Zahl eingeben.", parent=dialog)
                return
            if forward_jump_value < 1:
                messagebox.showerror("Pfadberechnung", "Forward jump muss mindestens 1 sein.", parent=dialog)
                return

        if MODE_PARAMETER_BACKWARD_JUMP in visible_parameters:
            try:
                backward_jump_value = int(backward_jump_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Backward jump eine ganze Zahl eingeben.", parent=dialog)
                return
            if backward_jump_value < 1:
                messagebox.showerror("Pfadberechnung", "Backward jump muss mindestens 1 sein.", parent=dialog)
                return

        if MODE_PARAMETER_HILBERT_ORDER in visible_parameters:
            try:
                hilbert_order_value = int(hilbert_order_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Hilbert order eine ganze Zahl eingeben.", parent=dialog)
                return
            if not 2 <= hilbert_order_value <= 7:
                messagebox.showerror("Pfadberechnung", "Hilbert order muss zwischen 2 und 7 liegen.", parent=dialog)
                return

        if MODE_PARAMETER_SPOT_SKIP in visible_parameters:
            try:
                spot_skip_value = int(spot_skip_var.get())
            except ValueError:
                messagebox.showerror("Pfadberechnung", "Bitte fuer Spot skip eine ganze Zahl eingeben.", parent=dialog)
                return
            if not 1 <= spot_skip_value <= 20:
                messagebox.showerror("Pfadberechnung", "Spot skip muss zwischen 1 und 20 liegen.", parent=dialog)
                return

        result["value"] = {
            "mode": spec.canonical_id,
            "memory": memory_value,
            "grid_spacing": grid_spacing,
            "recent_percent": recent_percent,
            "age_decay": GRID_SPREAD_AGE_DECAY_DEFAULT,
            "ghost_delay": ghost_delay_value,
            "forward_jump": forward_jump_value,
            "backward_jump": backward_jump_value,
            "hilbert_order": hilbert_order_value,
            "spot_skip": spot_skip_value,
        }
        dialog.destroy()

    ttk.Button(button_row, text="Weiter", command=confirm).pack(side="left")
    ttk.Button(button_row, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=(10, 0))

    mode_box.bind("<<ComboboxSelected>>", lambda _event: update_mode_fields())
    update_mode_fields()
    center_toplevel(parent, dialog)
    mode_box.focus_set()
    dialog.wait_window()
    return result["value"]


def ask_for_grid_spread_preview(
    parent: tk.Misc,
    preview_items: Sequence[GridPreviewData],
    current_grid_spacing: float,
) -> Optional[float]:
    """Show a modal grid preview dialog before starting a grid-dispersion mode."""
    if not preview_items:
        messagebox.showerror(
            "Grid-Vorschau",
            "Es konnten keine gueltigen ABS-Punkte fuer die Gittervorschau geladen werden.",
            parent=parent,
        )
        return None

    dialog = tk.Toplevel(parent)
    dialog.title("Grid-Dispersion Vorschau")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(True, True)
    dialog.configure(bg="#161616")
    dialog.minsize(820, 760)

    result = {"value": None}
    selected_name_var = tk.StringVar(value=preview_items[0].source_label)
    spacing_var = tk.StringVar(value=f"{current_grid_spacing:.6g}")
    spacing_slider_var = tk.DoubleVar(value=500.0)
    slider_hint_var = tk.StringVar()
    info_var = tk.StringVar()
    redraw_job: Dict[str, Optional[str]] = {"id": None}
    slider_state = {"min": 0.0, "max": 1.0, "syncing": False}

    frame = tk.Frame(dialog, bg="#161616", padx=16, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=(
            "Pruefen Sie vor dem Rechnen die Gittergroesse auf der groben Form. "
            "Sie koennen den Gitterabstand hier noch anpassen."
        ),
        bg="#161616",
        fg="#f0f0f0",
        font=("Segoe UI", 10, "bold"),
        justify="left",
        wraplength=760,
    ).pack(anchor="w")

    controls = tk.Frame(frame, bg="#161616")
    controls.pack(fill="x", pady=(12, 10))

    tk.Label(controls, text="Datei", bg="#161616", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=0,
        column=0,
        sticky="w",
        pady=4,
    )
    file_box = ttk.Combobox(
        controls,
        textvariable=selected_name_var,
        values=[item.source_label for item in preview_items],
        state="readonly",
        width=42,
    )
    file_box.grid(row=0, column=1, sticky="we", padx=(10, 14), pady=4)

    tk.Label(controls, text="Gitterabstand", bg="#161616", fg="#f0f0f0", font=("Segoe UI", 10)).grid(
        row=0,
        column=2,
        sticky="w",
        pady=4,
    )
    spacing_entry = tk.Entry(controls, textvariable=spacing_var, width=14, font=("Segoe UI", 10))
    spacing_entry.grid(row=0, column=3, sticky="w", padx=(10, 10), pady=4)

    preview_button = ttk.Button(controls, text="Vorschau aktualisieren")
    preview_button.grid(row=0, column=4, sticky="w", pady=4)
    controls.grid_columnconfigure(1, weight=1)

    slider_row = tk.Frame(frame, bg="#161616")
    slider_row.pack(fill="x", pady=(0, 10))

    tk.Label(
        slider_row,
        text="Gittergroesse",
        bg="#161616",
        fg="#f0f0f0",
        font=("Segoe UI", 10),
    ).pack(side="left")

    spacing_slider = tk.Scale(
        slider_row,
        from_=0,
        to=1000,
        orient="horizontal",
        resolution=1,
        showvalue=False,
        variable=spacing_slider_var,
        bg="#161616",
        fg="#f0f0f0",
        troughcolor="#2b2b2b",
        highlightthickness=0,
        length=360,
    )
    spacing_slider.pack(side="left", padx=(12, 12), fill="x", expand=True)

    tk.Label(
        slider_row,
        textvariable=slider_hint_var,
        bg="#161616",
        fg="#d0d0d0",
        font=("Segoe UI", 9),
    ).pack(side="left")

    canvas = tk.Canvas(
        frame,
        width=760,
        height=560,
        bg="#000000",
        highlightthickness=1,
        highlightbackground="#2a2a2a",
    )
    canvas.pack(fill="both", expand=True)

    tk.Label(
        frame,
        textvariable=info_var,
        bg="#161616",
        fg="#d7d7d7",
        font=("Segoe UI", 10),
        justify="left",
        anchor="w",
    ).pack(fill="x", pady=(10, 0))

    button_row = tk.Frame(frame, bg="#161616")
    button_row.pack(fill="x", pady=(14, 0))

    def get_selected_item() -> GridPreviewData:
        selected_name = selected_name_var.get()
        for item in preview_items:
            if item.source_label == selected_name:
                return item
        return preview_items[0]

    def compute_spacing_limits(preview_item: GridPreviewData) -> Tuple[float, float]:
        min_x, max_x, min_y, max_y = preview_item.bounds
        span_x = max(max_x - min_x, 1e-12)
        span_y = max(max_y - min_y, 1e-12)
        span_max = max(span_x, span_y, 1e-9)
        non_zero_spans = [span for span in (span_x, span_y) if span > 1e-9]
        reference_span = min(non_zero_spans) if non_zero_spans else span_max
        min_spacing = max(reference_span / 200.0, span_max / 1000.0, 1e-6)
        max_spacing = max(span_max, min_spacing * 10.0)
        return (min_spacing, max_spacing)

    def slider_to_spacing(slider_value: float) -> float:
        min_spacing = slider_state["min"]
        max_spacing = slider_state["max"]
        if max_spacing <= min_spacing * 1.0000001:
            return min_spacing
        ratio = max(0.0, min(1.0, slider_value / 1000.0))
        return min_spacing * ((max_spacing / min_spacing) ** ratio)

    def spacing_to_slider(spacing_value: float) -> float:
        min_spacing = slider_state["min"]
        max_spacing = slider_state["max"]
        clamped_spacing = min(max(spacing_value, min_spacing), max_spacing)
        if max_spacing <= min_spacing * 1.0000001:
            return 0.0
        return 1000.0 * math.log(clamped_spacing / min_spacing) / math.log(max_spacing / min_spacing)

    def sync_slider_range_from_file(keep_spacing: Optional[float] = None) -> None:
        preview_item = get_selected_item()
        min_spacing, max_spacing = compute_spacing_limits(preview_item)
        slider_state["min"] = min_spacing
        slider_state["max"] = max_spacing
        target_spacing = keep_spacing
        if target_spacing is None:
            try:
                target_spacing = float(spacing_var.get())
            except ValueError:
                target_spacing = current_grid_spacing
        target_spacing = min(max(float(target_spacing), min_spacing), max_spacing)
        slider_state["syncing"] = True
        spacing_slider_var.set(spacing_to_slider(target_spacing))
        spacing_var.set(f"{target_spacing:.6g}")
        slider_hint_var.set(f"ca. {min_spacing:.6g} bis {max_spacing:.6g}")
        slider_state["syncing"] = False

    def count_axis_positions(min_value: float, max_value: float, spacing_value: float) -> int:
        if max_value <= min_value:
            return 1
        return max(1, int(math.floor((max_value - min_value) / spacing_value + 1e-9)) + 1)

    def schedule_redraw(delay_ms: int = 100) -> None:
        if redraw_job["id"] is not None:
            dialog.after_cancel(redraw_job["id"])
        redraw_job["id"] = dialog.after(delay_ms, redraw_preview)

    def on_slider_changed(_value: str) -> None:
        if slider_state["syncing"]:
            return
        spacing_value = slider_to_spacing(spacing_slider_var.get())
        spacing_var.set(f"{spacing_value:.6g}")
        schedule_redraw(90)

    def sync_slider_from_entry() -> Optional[float]:
        try:
            spacing_value = float(spacing_var.get())
        except ValueError:
            return None

        min_spacing = slider_state["min"]
        max_spacing = slider_state["max"]
        spacing_value = min(max(spacing_value, min_spacing), max_spacing)
        slider_state["syncing"] = True
        spacing_slider_var.set(spacing_to_slider(spacing_value))
        spacing_var.set(f"{spacing_value:.6g}")
        slider_state["syncing"] = False
        return spacing_value

    def redraw_preview(*_args: object) -> bool:
        redraw_job["id"] = None
        try:
            spacing_value = float(spacing_var.get())
        except ValueError:
            info_var.set("Bitte einen gueltigen Gitterabstand eingeben.")
            return False

        if spacing_value <= 0.0:
            info_var.set("Der Gitterabstand muss groesser als 0 sein.")
            return False

        preview_item = get_selected_item()
        min_spacing, max_spacing = compute_spacing_limits(preview_item)
        if spacing_value < min_spacing or spacing_value > max_spacing:
            spacing_value = min(max(spacing_value, min_spacing), max_spacing)
            spacing_var.set(f"{spacing_value:.6g}")
        sync_slider_range_from_file(keep_spacing=spacing_value)
        width = max(canvas.winfo_width(), int(canvas["width"]))
        height = max(canvas.winfo_height(), int(canvas["height"]))
        bounds = preview_item.bounds

        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#000000", outline="")

        for point in preview_item.sampled_points:
            x_pos, y_pos = map_point_to_bounds(point, bounds, width, height)
            canvas.create_rectangle(x_pos - 1, y_pos - 1, x_pos + 1, y_pos + 1, fill="#404040", outline="")

        min_x, max_x, min_y, max_y = bounds
        total_grid_x = count_axis_positions(min_x, max_x, spacing_value)
        total_grid_y = count_axis_positions(min_y, max_y, spacing_value)

        max_draw_lines = 120
        x_stride = max(1, int(math.ceil(total_grid_x / max_draw_lines)))
        y_stride = max(1, int(math.ceil(total_grid_y / max_draw_lines)))

        for x_index in range(0, total_grid_x, x_stride):
            x_value = min_x + x_index * spacing_value
            line_x, _ = map_point_to_bounds((x_value, min_y), bounds, width, height)
            canvas.create_line(line_x, 0, line_x, height, fill="#14465a")

        for y_index in range(0, total_grid_y, y_stride):
            y_value = min_y + y_index * spacing_value
            _, line_y = map_point_to_bounds((min_x, y_value), bounds, width, height)
            canvas.create_line(0, line_y, width, line_y, fill="#14465a")

        canvas.create_rectangle(1, 1, width - 1, height - 1, outline="#2a2a2a")

        stride_text = ""
        if x_stride > 1 or y_stride > 1:
            stride_text = f" | Vorschau vereinfacht: jede {x_stride}. vertikale / jede {y_stride}. horizontale Linie"

        info_var.set(
            f"{preview_item.source_label} | Punkte: {preview_item.point_count} | "
            f"Vorschaupunkte: {len(preview_item.sampled_points)} | "
            f"Gitter: {total_grid_x} x {total_grid_y} Linien{stride_text}"
        )
        return True

    def confirm() -> None:
        if not redraw_preview():
            messagebox.showerror(
                "Grid-Vorschau",
                "Bitte korrigieren Sie zuerst den Gitterabstand.",
                parent=dialog,
            )
            return

        result["value"] = float(spacing_var.get())
        dialog.destroy()

    ttk.Button(button_row, text="Weiter", command=confirm).pack(side="left")
    ttk.Button(button_row, text="Abbrechen", command=dialog.destroy).pack(side="left", padx=(10, 0))

    preview_button.config(command=redraw_preview)
    spacing_slider.config(command=on_slider_changed)
    file_box.bind("<<ComboboxSelected>>", lambda _event: (sync_slider_range_from_file(), redraw_preview()))
    spacing_entry.bind(
        "<Return>",
        lambda _event: (sync_slider_from_entry() is not None and redraw_preview()),
    )
    spacing_entry.bind(
        "<FocusOut>",
        lambda _event: (sync_slider_from_entry() is not None and redraw_preview()),
    )
    canvas.bind("<Configure>", redraw_preview)

    sync_slider_range_from_file()
    center_toplevel(parent, dialog)
    dialog.after(20, redraw_preview)
    spacing_entry.focus_set()
    dialog.wait_window()
    return result["value"]


def show_fatal_error(message: str) -> None:
    """Show a last-resort error message even if Tk cannot start correctly."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, APP_DISPLAY_NAME, 0x10)
            return
        except Exception:
            pass

    print(message, file=sys.stderr)


def center_toplevel(parent: tk.Misc, window: tk.Toplevel) -> None:
    """Center a dialog window over the main application window."""
    parent.update_idletasks()
    window.update_idletasks()

    parent_x = parent.winfo_rootx()
    parent_y = parent.winfo_rooty()
    parent_width = max(parent.winfo_width(), 1)
    parent_height = max(parent.winfo_height(), 1)

    window_width = max(window.winfo_width(), 1)
    window_height = max(window.winfo_height(), 1)

    x_pos = parent_x + (parent_width - window_width) // 2
    y_pos = parent_y + (parent_height - window_height) // 2
    window.geometry(f"+{max(x_pos, 0)}+{max(y_pos, 0)}")


def run_modal_background_task(
    parent: tk.Misc,
    title: str,
    message: str,
    task: Callable[[], Any],
) -> Any:
    """Run one blocking task in a worker thread while keeping the Tk UI responsive."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    dialog.configure(bg="#1a1a1a")
    dialog.protocol("WM_DELETE_WINDOW", lambda: None)

    frame = tk.Frame(dialog, bg="#1a1a1a", padx=18, pady=16)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=title,
        bg="#1a1a1a",
        fg="#f0f0f0",
        font=("Segoe UI", 10, "bold"),
        justify="left",
    ).pack(anchor="w")

    tk.Label(
        frame,
        text=message,
        bg="#1a1a1a",
        fg="#d0d0d0",
        font=("Segoe UI", 9),
        justify="left",
        wraplength=460,
    ).pack(anchor="w", pady=(10, 8))

    elapsed_var = tk.StringVar(value="Bitte warten...")
    ttk.Progressbar(frame, mode="indeterminate", length=320).pack(fill="x", pady=(0, 8))
    progress = frame.winfo_children()[-1]
    assert isinstance(progress, ttk.Progressbar)
    progress.start(12)

    tk.Label(
        frame,
        textvariable=elapsed_var,
        bg="#1a1a1a",
        fg="#b8b8b8",
        font=("Segoe UI", 9),
        justify="left",
    ).pack(anchor="w")

    result: Dict[str, Any] = {"value": None, "error": None}
    started_at = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(task)
    cursor_supported = True
    previous_cursor = ""
    try:
        previous_cursor = str(parent.cget("cursor"))
        parent.configure(cursor="watch")
    except Exception:
        cursor_supported = False

    def finish() -> None:
        try:
            progress.stop()
        except Exception:
            pass
        try:
            dialog.grab_release()
        except Exception:
            pass
        if dialog.winfo_exists():
            dialog.destroy()

    def poll() -> None:
        if future.done():
            try:
                result["value"] = future.result()
            except BaseException as exc:
                result["error"] = exc
            finish()
            return

        elapsed_seconds = time.perf_counter() - started_at
        elapsed_var.set(f"Bitte warten... {format_duration(elapsed_seconds, include_tenths=True)}")
        dialog.after(80, poll)

    center_toplevel(parent, dialog)
    dialog.after(60, poll)
    dialog.wait_window()
    executor.shutdown(wait=False)
    if cursor_supported:
        try:
            parent.configure(cursor=previous_cursor)
        except Exception:
            pass

    if result["error"] is not None:
        raise result["error"]
    return result["value"]


def run_interactive_viewer(payload_path: str, preferred_backend: str = "raster") -> int:
    """Run the standalone exact Qt viewer for one payload file."""
    try:
        import numpy as np
        from PySide6 import QtCore, QtGui, QtOpenGL, QtOpenGLWidgets, QtWidgets
    except Exception as exc:
        show_fatal_error(
            "Der interaktive Viewer konnte nicht gestartet werden.\n\n"
            "Bitte installieren Sie PySide6 und numpy.\n\n"
            f"Technischer Hinweis:\n{exc}"
        )
        return 1

    try:
        with open(payload_path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    finally:
        try:
            Path(payload_path).unlink()
        except OSError:
            pass

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

    class SequenceViewerWindow(QtWidgets.QMainWindow):
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

            self.setWindowTitle(f"{payload_data.get('app_name', APP_DISPLAY_NAME)} - Interactive viewer")
            self.resize(1560, 920)

            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            outer_layout = QtWidgets.QVBoxLayout(central)
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

    def launch_viewer(backend_kind: str) -> int:
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

    normalized_backend = str(preferred_backend or "raster").strip().lower()
    if normalized_backend not in {"raster", "opengl", "auto"}:
        normalized_backend = "raster"
    if normalized_backend == "opengl":
        backend_order = ["opengl", "raster"]
    else:
        backend_order = ["raster", "opengl"]

    first_backend = backend_order[0]
    second_backend = backend_order[1]
    try:
        return launch_viewer(first_backend)
    except Exception as first_exc:
        try:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                for widget in list(app.topLevelWidgets()):
                    widget.close()
                app.processEvents()
        except Exception:
            pass
        try:
            return launch_viewer(second_backend)
        except Exception as second_exc:
            show_fatal_error(
                "Der interaktive Viewer konnte nicht gestartet werden.\n\n"
                f"Der Start mit dem bevorzugten Backend '{first_backend}' ist fehlgeschlagen, "
                f"und auch das alternative Backend '{second_backend}' konnte nicht geladen werden.\n\n"
                f"{first_backend}-Hinweis:\n{first_exc}\n\n"
                f"{second_backend}-Hinweis:\n{second_exc}"
            )
            return 1


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
    cancel_event: object = None,
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
        cancel_event=cancel_event,
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


class ComparisonApp(tk.Tk):
    """Desktop UI with file selection, visible progress and animation preview."""

    def __init__(
        self,
        initial_files: Sequence[str],
        w1: float,
        w2: float,
        memory: int,
        mode: str,
        grid_spacing: float,
        recent_percent: float,
        age_decay: float,
        ghost_delay: int = GHOST_BEAM_DEFAULT_DELAY,
        forward_jump: int = INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
        backward_jump: int = INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
        hilbert_order: int = HILBERT_ORDER_DEFAULT,
        spot_skip: int = SPOT_SKIP_DEFAULT,
    ):
        super().__init__()
        self.w1 = w1
        self.w2 = w2
        self.memory = memory
        self.mode = normalize_mode(mode)
        self.grid_spacing = grid_spacing
        self.recent_percent = recent_percent
        self.age_decay = age_decay
        self.ghost_delay = int(ghost_delay)
        self.forward_jump = int(forward_jump)
        self.backward_jump = int(backward_jump)
        self.hilbert_order = int(hilbert_order)
        self.spot_skip = int(spot_skip)
        self.zip_entry_types = ("infill",)
        self.zip_support_end_layer = 0
        self.step_point_spacing_mm = STEP_POINT_SPACING_MM_DEFAULT
        self.step_layer_height_mm = STEP_LAYER_HEIGHT_MM_DEFAULT
        self.step_support_layer_count = STEP_SUPPORT_LAYER_COUNT_DEFAULT
        self.initial_files = tuple(initial_files)
        self.selected_input_files = tuple(initial_files)
        self.selected_sources: Tuple[InputSource, ...] = tuple()

        self.max_process_workers = max(1, os.cpu_count() or 1)
        self.mp_context = mp.get_context("spawn")
        self.mp_manager = self.mp_context.Manager()
        self.process_progress_queue = self.mp_manager.Queue()
        self.process_cancel_event = self.mp_manager.Event()
        self.animation_cancel_event = self.mp_manager.Event()
        self.process_pool = ProcessPoolExecutor(
            max_workers=self.max_process_workers,
            mp_context=self.mp_context,
        )

        self.results: List[ProcessedFileResult] = []
        self.errors: List[str] = []
        self.cancelled_files: List[str] = []
        self.total_files = 0
        self.completed_files = 0
        self.file_progress_map: Dict[int, float] = {}
        self.result_map: Dict[int, ProcessedFileResult] = {}
        self.error_map: Dict[int, str] = {}
        self.cancelled_map: Dict[int, str] = {}
        self.worker_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.current_result: Optional[ProcessedFileResult] = None
        self.current_bounds: Optional[Tuple[float, float, float, float]] = None
        self.animation_plan: Optional[AnimationPlan] = None
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_job: Optional[str] = None
        self.queue_poll_job: Optional[str] = None
        self.loading_job: Optional[str] = None
        self.resize_refresh_job: Optional[str] = None
        self.prepare_animation_job: Optional[str] = None
        self.animation_prepare_active = False
        self.animation_future: Optional[Future] = None
        self.animation_progress_fraction = 0.0
        self.animation_paused = False
        self.pending_animation_prepare: Optional[Tuple[int, int]] = None
        self.processing_active = False
        self.processing_cancel_requested = False
        self.animation_cancel_requested = False
        self.zip_saved = False
        self.current_progress_fraction = 0.0
        self.loading_message = ""
        self.loading_tick = 0
        self.view_zoom = 1.0
        self.processing_started_at: Optional[float] = None
        self.processing_total_seconds = 0.0
        self.current_operation_started_at: Optional[float] = None
        self.last_animation_prepare_seconds = 0.0
        self.viewer_processes: List[subprocess.Popen[Any]] = []
        self.step_generated_artifacts: List[StepGeneratedArtifact] = []
        self.step_layer_bundle_saved = False

        self.file_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Bitte Dateien auswaehlen.")
        self.progress_var = tk.StringVar(value="Noch keine Verarbeitung gestartet.")
        self.timer_var = tk.StringVar(value="Laufzeit: 00:00:00.0")
        self.original_label_var = tk.StringVar(value="Punkt 0 / 0")
        self.optimized_label_var = tk.StringVar(value="Punkt 0 / 0")
        self.animation_speed_var = tk.IntVar(value=ANIMATION_MIN_MULTIPLIER)
        self.animation_speed_label_var = tk.StringVar()
        self.trail_count_var = tk.IntVar(value=TRAIL_DEFAULT_POINTS)
        self.trail_count_label_var = tk.StringVar()
        self.pause_button_var = tk.StringVar(value="Play")
        self.cancel_button_var = tk.StringVar(value="Abbrechen")
        self.animation_ready = False
        self.last_confirmed_speed = int(self.animation_speed_var.get())
        self.last_confirmed_trail = int(self.trail_count_var.get())
        self.trail_color_cache: Dict[int, List[str]] = {}

        self.title(APP_DISPLAY_NAME)
        self.geometry("1240x860")
        self.minsize(1140, 800)
        self.configure(bg="#111111")
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

        self._build_ui()
        self._update_animation_speed_label()
        self._update_trail_count_label()
        self._reset_preview("Bitte zuerst Dateien auswaehlen und verarbeiten.")

        if self.initial_files:
            self.after(200, lambda: self._start_processing_flow(self.initial_files))
        else:
            self.after(250, self._prompt_for_files)

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg="#181818", padx=14, pady=12)
        header.pack(fill="x")

        ttk.Button(header, text="Dateien auswaehlen...", command=self._prompt_for_files).pack(side="left")
        self.recalculate_button = ttk.Button(
            header,
            text="Neu berechnen...",
            command=self._recalculate_current_files,
            state="normal" if self.selected_input_files else "disabled",
        )
        self.recalculate_button.pack(side="left", padx=(10, 0))
        self.cancel_button = ttk.Button(
            header,
            textvariable=self.cancel_button_var,
            command=self._cancel_active_work,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(10, 0))
        self.zip_button = ttk.Button(header, text="ZIP speichern...", command=self._save_zip, state="disabled")
        self.zip_button.pack(side="left", padx=(10, 0))
        self.step_zip_button = ttk.Button(
            header,
            text="STEP-Layer speichern...",
            command=self._save_step_layer_zip,
            state="disabled",
        )
        self.step_zip_button.pack(side="left", padx=(10, 0))
        ttk.Button(header, text="Schliessen", command=self._handle_close).pack(side="left", padx=(10, 0))

        tk.Label(
            header,
            textvariable=self.status_var,
            bg="#181818",
            fg="#d0d0d0",
            font=("Segoe UI", 10),
        ).pack(side="right")

        body_container = tk.Frame(self, bg="#111111")
        body_container.pack(fill="both", expand=True)

        self.main_scroll_canvas = tk.Canvas(
            body_container,
            bg="#111111",
            highlightthickness=0,
            bd=0,
        )
        self.main_scroll_canvas.pack(side="left", fill="both", expand=True)

        self.main_scrollbar = ttk.Scrollbar(
            body_container,
            orient="vertical",
            command=self.main_scroll_canvas.yview,
        )
        self.main_scrollbar.pack(side="right", fill="y")
        self.main_scroll_canvas.configure(yscrollcommand=self.main_scrollbar.set)

        self.main_content = tk.Frame(self.main_scroll_canvas, bg="#111111")
        self.main_scroll_window = self.main_scroll_canvas.create_window(
            (0, 0),
            window=self.main_content,
            anchor="nw",
        )
        self.main_content.bind("<Configure>", self._on_main_content_configure)
        self.main_scroll_canvas.bind("<Configure>", self._on_main_canvas_configure)

        progress_frame = tk.Frame(self.main_content, bg="#111111", padx=14, pady=12)
        progress_frame.pack(fill="x")

        tk.Label(
            progress_frame,
            text="Verarbeitung",
            bg="#111111",
            fg="#f0f0f0",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
        self.progress_bar.pack(fill="x", pady=(8, 4))

        tk.Label(
            progress_frame,
            textvariable=self.progress_var,
            bg="#111111",
            fg="#d7d7d7",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        ).pack(fill="x")

        tk.Label(
            progress_frame,
            textvariable=self.timer_var,
            bg="#111111",
            fg="#bcbcbc",
            font=("Consolas", 10),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(4, 0))

        selection_frame = tk.Frame(self.main_content, bg="#111111", padx=14, pady=8)
        selection_frame.pack(fill="x")

        tk.Label(
            selection_frame,
            text="Ergebnis anzeigen:",
            bg="#111111",
            fg="#f0f0f0",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")

        self.file_selector = ttk.Combobox(
            selection_frame,
            values=[],
            textvariable=self.file_var,
            state="disabled",
            width=60,
        )
        self.file_selector.pack(side="left", padx=(10, 10))
        self.file_selector.bind("<<ComboboxSelected>>", self._on_result_selected)

        action_frame = tk.Frame(self.main_content, bg="#111111", padx=14, pady=8)
        action_frame.pack(fill="x")

        self.viewer_button = ttk.Button(
            action_frame,
            text="Interactive viewer oeffnen",
            command=self._open_interactive_viewer,
            state="disabled",
        )
        self.viewer_button.pack(side="left")
        self.preview_button = self.viewer_button

        self.pause_button = ttk.Button(
            action_frame,
            textvariable=self.pause_button_var,
            command=self._toggle_pause,
            state="disabled",
        )

        tk.Label(
            action_frame,
            text=(
                "Der Viewer laeuft in einem separaten Qt-Fenster mit exaktem Raster-Rendering "
                "als Standard sowie optionalem OpenGL, inklusive Zoom, Panning, Dateiauswahl, "
                "Animation und synchronisierten Achsen."
            ),
            bg="#111111",
            fg="#d0d0d0",
            font=("Segoe UI", 10),
            justify="left",
        ).pack(side="left", padx=(14, 0))

        info_frame = tk.Frame(self.main_content, bg="#111111", padx=14, pady=10)
        info_frame.pack(fill="x")

        self.file_info_label = tk.Label(
            info_frame,
            bg="#111111",
            fg="#f5f5f5",
            anchor="w",
            justify="left",
            font=("Consolas", 10),
        )
        self.file_info_label.pack(fill="x")

        stats_frame = tk.Frame(self.main_content, bg="#111111", padx=14)
        stats_frame.pack(fill="x")

        self.original_stats_label = tk.Label(
            stats_frame,
            bg="#111111",
            fg="#f1f1f1",
            anchor="nw",
            justify="left",
            font=("Consolas", 10),
        )
        self.original_stats_label.pack(side="left", fill="both", expand=True)

        self.optimized_stats_label = tk.Label(
            stats_frame,
            bg="#111111",
            fg="#f1f1f1",
            anchor="nw",
            justify="left",
            font=("Consolas", 10),
        )
        self.optimized_stats_label.pack(side="left", fill="both", expand=True, padx=(20, 0))

        viewer_notes = tk.Frame(self.main_content, bg="#0d0d0d", padx=14, pady=14, bd=1, relief="solid")
        viewer_notes.pack(fill="x", padx=14, pady=(8, 14))

        tk.Label(
            viewer_notes,
            text="Interaktive Visualisierung",
            bg="#0d0d0d",
            fg="#f4f4f4",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x")

        tk.Label(
            viewer_notes,
            text=(
                "Oeffnen Sie den separaten Viewer fuer eine fluessigere Darstellung. "
                "Dort stehen Mausrad-Zoom, linkes Maus-Panning, Pfeiltasten-Navigation, "
                "Home-Reset, Play/Pause, Speed und Trail Length zur Verfuegung."
            ),
            bg="#0d0d0d",
            fg="#d8d8d8",
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
            wraplength=1080,
        ).pack(fill="x", pady=(8, 0))

        # Hidden compatibility widgets: the legacy in-window animation code is no longer surfaced,
        # but a few helper methods still expect these objects to exist.
        self.speed_slider = tk.Scale(
            self.main_content,
            from_=ANIMATION_MIN_MULTIPLIER,
            to=ANIMATION_MAX_MULTIPLIER,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.animation_speed_var,
            command=self._on_speed_changed,
        )
        self.trail_slider = tk.Scale(
            self.main_content,
            from_=TRAIL_MIN_POINTS,
            to=TRAIL_MAX_POINTS,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.trail_count_var,
            command=self._on_trail_count_changed,
        )

    def _on_main_content_configure(self, _event: tk.Event) -> None:
        """Keep the scroll region in sync with the full UI content height."""
        self.main_scroll_canvas.configure(scrollregion=self.main_scroll_canvas.bbox("all"))

    def _on_main_canvas_configure(self, event: tk.Event) -> None:
        """Stretch the embedded content frame to the visible canvas width."""
        self.main_scroll_canvas.itemconfigure(self.main_scroll_window, width=event.width)

    def _build_panel(self, parent: tk.Misc, title: str, label_var: tk.StringVar) -> tk.Frame:
        panel = tk.Frame(parent, bg="#070707", bd=1, relief="solid", padx=12, pady=12)

        tk.Label(
            panel,
            text=title,
            bg="#070707",
            fg="#f3f3f3",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")

        canvas = tk.Canvas(
            panel,
            width=560,
            height=560,
            bg="#000000",
            highlightthickness=1,
            highlightbackground="#202020",
        )
        canvas.pack(fill="both", expand=True, pady=(10, 8))
        canvas.bind("<Configure>", self._on_canvas_resized)
        canvas.bind("<MouseWheel>", self._on_canvas_mousewheel)
        canvas.bind("<Button-4>", self._on_canvas_mousewheel)
        canvas.bind("<Button-5>", self._on_canvas_mousewheel)
        panel.canvas = canvas

        tk.Label(
            panel,
            textvariable=label_var,
            bg="#070707",
            fg="#dddddd",
            font=("Segoe UI", 11),
        ).pack(anchor="w")

        return panel

    def _enqueue_file_future_result(
        self,
        future: Future,
        file_index: int,
        total_files: int,
        input_source: InputSource,
    ) -> None:
        """Forward a completed file-processing future into the UI queue."""
        self.worker_queue.put(("file_future_done", file_index, total_files, input_source, future))

    def _enqueue_animation_future_result(self, future: Future, result_index: int, speed_multiplier: int) -> None:
        """Forward a completed animation-preparation future into the UI queue."""
        self.worker_queue.put(("animation_future_done", result_index, speed_multiplier, future))

    def _show_animation_compute_button(self) -> None:
        """Show the button that opens the external interactive viewer."""
        if not self.preview_button.winfo_ismapped():
            self.preview_button.pack(side="left")
        self.preview_button.config(state="normal" if self.results else "disabled")
        if self.pause_button.winfo_ismapped():
            self.pause_button.pack_forget()

    def _show_pause_button(self) -> None:
        """Legacy no-op retained for compatibility after moving animation into the Qt viewer."""
        self.pause_button.config(state="disabled")

    def _set_timer_text(self, prefix: str, seconds: float, include_tenths: bool = True) -> None:
        """Update the visible timer label."""
        self.timer_var.set(f"{prefix}: {format_duration(seconds, include_tenths=include_tenths)}")

    def _set_cancel_button_state(self, enabled: bool, running_text: str = "Abbrechen") -> None:
        """Update cancel button state and label consistently."""
        self.cancel_button_var.set(running_text if enabled else "Abbrechen")
        self.cancel_button.config(state="normal" if enabled else "disabled")

    def _cancel_active_work(self) -> None:
        """Request cooperative cancellation for the current processing step."""
        if self.processing_active:
            if self.processing_cancel_requested:
                return
            answer = messagebox.askyesno(
                "Berechnung abbrechen",
                "Die Dateiverarbeitung laeuft noch.\n\nSoll die Berechnung abgebrochen werden?",
                parent=self,
            )
            if not answer:
                return
            self.processing_cancel_requested = True
            self.process_cancel_event.set()
            self.loading_message = "Abbruch der Dateiverarbeitung wird angefordert"
            self.status_var.set("Abbruch der Dateiverarbeitung wurde angefordert.")
            self._set_cancel_button_state(False)
            return

        if self.animation_prepare_active:
            if self.animation_cancel_requested:
                return
            answer = messagebox.askyesno(
                "Animation abbrechen",
                "Die Animationsvorbereitung laeuft noch.\n\nSoll sie abgebrochen werden?",
                parent=self,
            )
            if not answer:
                return
            self.animation_cancel_requested = True
            self.animation_cancel_event.set()
            self.loading_message = "Abbruch der Animationsvorbereitung wird angefordert"
            self.status_var.set("Abbruch der Animationsvorbereitung wurde angefordert.")
            self._set_cancel_button_state(False)
            return

        messagebox.showinfo("Abbrechen", "Es laeuft gerade keine Berechnung.", parent=self)

    def _clear_prepared_animation(self, keep_current_result: bool = True) -> None:
        """Delete the prepared animation and return to the calculation state."""
        if self.animation_job is not None:
            self.after_cancel(self.animation_job)
            self.animation_job = None

        self.animation_plan = None
        self.animation_ready = False
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_paused = False
        self.pause_button_var.set("Play")
        self.pending_animation_prepare = None
        self.animation_future = None
        self.animation_prepare_active = False
        self.animation_cancel_requested = False
        self.animation_cancel_event.clear()
        self._show_animation_compute_button()
        self.pause_button.config(state="disabled")
        self.speed_slider.config(state="normal")
        self.trail_slider.config(state="normal")
        self.file_selector.config(state="readonly" if self.results else "disabled")
        self.current_progress_fraction = 0.0
        self.animation_progress_fraction = 0.0
        self._set_cancel_button_state(False)

        if keep_current_result and self.current_result is not None:
            self._load_result(self.results.index(self.current_result), start_playback=False)
            self.status_var.set("Animation geloescht. Jetzt 'Animation berechnen' klicken.")

    def _confirm_delete_animation_for_slider_change(self, slider_name: str, new_value: int) -> bool:
        """Ask whether a prepared animation should be discarded after a slider change."""
        if not self.animation_ready:
            return True

        answer = messagebox.askyesno(
            "Animation loeschen?",
            f"Die vorhandene Animation wurde schon berechnet.\n\n"
            f"Soll sie wegen der Aenderung an '{slider_name}' geloescht werden?",
            parent=self,
        )
        if answer:
            self._clear_prepared_animation(keep_current_result=True)
        return answer

    def _drain_process_progress_queue(self) -> None:
        """Drain progress messages emitted by subprocesses."""
        while True:
            try:
                message = self.process_progress_queue.get_nowait()
            except queue.Empty:
                break

            kind = message[0]
            if kind == "file_progress":
                _, file_index, total_files, file_name, fraction, detail = message
                if self.processing_active:
                    self.file_progress_map[file_index] = max(0.0, min(float(fraction), 1.0))
                    if self.processing_cancel_requested:
                        self.loading_message = "Abbruch der Dateiverarbeitung wird angefordert"
                    else:
                        self.loading_message = f"Datei {file_index} / {total_files}: {file_name} - {detail}"
                    self.current_progress_fraction = self.file_progress_map[file_index]
                    self._update_progress_widgets()
            elif kind == "animation_progress":
                _, result_index, file_name, speed_multiplier, fraction, detail = message
                if (
                    self.animation_prepare_active
                    and self.current_result is not None
                    and result_index < len(self.results)
                    and self.results[result_index].source_label == file_name
                    and int(self.animation_speed_var.get()) == speed_multiplier
                ):
                    self.animation_progress_fraction = max(0.0, min(float(fraction), 1.0))
                    if self.animation_cancel_requested:
                        self.loading_message = "Abbruch der Animationsvorbereitung wird angefordert"
                        self.progress_var.set("Abbruch angefordert - warte auf Rueckmeldung der Worker")
                    else:
                        self.loading_message = f"Berechne Animation: {file_name} - {detail}"
                        self.progress_var.set(detail)
                    self.progress_bar.config(maximum=1.0, value=self.animation_progress_fraction)

    def _get_animation_timing(self) -> Tuple[float, float, float, int]:
        """Return points-per-second, FPS, points-per-frame and timer interval."""
        multiplier = max(
            ANIMATION_MIN_MULTIPLIER,
            min(ANIMATION_MAX_MULTIPLIER, int(self.animation_speed_var.get())),
        )
        points_per_second = ANIMATION_BASE_POINTS_PER_SECOND * multiplier
        frames_per_second = ANIMATION_MAX_FPS
        points_per_frame = points_per_second / frames_per_second
        interval_ms = max(1, int(round(1000.0 / frames_per_second)))
        return (points_per_second, frames_per_second, points_per_frame, interval_ms)

    def _update_animation_speed_label(self) -> None:
        """Refresh the slider label for speed and FPS information."""
        points_per_second, frames_per_second, _, _ = self._get_animation_timing()
        self.animation_speed_label_var.set(
            f"{int(self.animation_speed_var.get())}x | {points_per_second:.1f} Punkte/s | {frames_per_second:.1f} FPS"
        )

    def _update_trail_count_label(self) -> None:
        """Refresh the label for the number of visible gradient points."""
        self.trail_count_label_var.set(f"{int(self.trail_count_var.get())} Punkte")

    def _update_trail_slider_range(self, point_count: int) -> None:
        """Adapt the trail slider to the number of points in the active file."""
        max_points = max(TRAIL_MIN_POINTS, int(point_count))
        self.trail_slider.config(to=max_points)
        if self.trail_count_var.get() > max_points:
            self.trail_count_var.set(max_points)
        self._update_trail_count_label()

    def _on_speed_changed(self, _value: str) -> None:
        """Apply a new animation speed from the slider."""
        self._update_animation_speed_label()
        new_speed = int(self.animation_speed_var.get())
        if self.current_result is None:
            self.last_confirmed_speed = new_speed
            return

        if new_speed == self.last_confirmed_speed:
            return

        if self.animation_prepare_active:
            self.animation_speed_var.set(self.last_confirmed_speed)
            self._update_animation_speed_label()
            messagebox.showinfo(
                "Animation",
                "Waehrend die Animation berechnet wird, kann die Geschwindigkeit nicht geaendert werden.",
                parent=self,
            )
            return

        if self.animation_ready and not self._confirm_delete_animation_for_slider_change("Geschwindigkeit", new_speed):
            self.animation_speed_var.set(self.last_confirmed_speed)
            self._update_animation_speed_label()
            return

        self.last_confirmed_speed = new_speed

    def _on_trail_count_changed(self, _value: str) -> None:
        """Apply a new count for visible gradient trail points."""
        self._update_trail_count_label()
        new_trail = int(self.trail_count_var.get())
        had_prepared_animation = self.animation_ready
        if self.current_result is None:
            self.last_confirmed_trail = new_trail
            return

        if new_trail == self.last_confirmed_trail:
            return

        if self.animation_prepare_active:
            self.trail_count_var.set(self.last_confirmed_trail)
            self._update_trail_count_label()
            messagebox.showinfo(
                "Animation",
                "Waehrend die Animation berechnet wird, kann der Gradient nicht geaendert werden.",
                parent=self,
            )
            return

        if self.animation_ready and not self._confirm_delete_animation_for_slider_change("Gradient-Punkte", new_trail):
            self.trail_count_var.set(self.last_confirmed_trail)
            self._update_trail_count_label()
            return

        self.last_confirmed_trail = new_trail
        if not had_prepared_animation:
            self.status_var.set(
                "Gradient-Wert aktualisiert. Er wird bei der naechsten Animationsberechnung verwendet."
            )

    def _on_canvas_resized(self, _event: tk.Event) -> None:
        """Rebuild cached canvas geometry after the preview size changes."""
        if self.current_result is None:
            return
        self._schedule_view_refresh(120)

    def _schedule_view_refresh(self, delay_ms: int = 120) -> None:
        """Throttle expensive preview rebuilds after resize or zoom changes."""
        if self.current_result is None:
            return
        if self.resize_refresh_job is not None:
            self.after_cancel(self.resize_refresh_job)
        self.resize_refresh_job = self.after(delay_ms, self._refresh_current_view)

    def _on_canvas_mousewheel(self, event: tk.Event) -> str:
        """Zoom both preview panels with the mouse wheel."""
        if self.current_result is None:
            return "break"

        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", 0)
        if delta > 0 or num == 4:
            zoom_factor = 1.15
        elif delta < 0 or num == 5:
            zoom_factor = 1.0 / 1.15
        else:
            return "break"

        new_zoom = min(25.0, max(0.2, self.view_zoom * zoom_factor))
        if abs(new_zoom - self.view_zoom) < 1e-9:
            return "break"

        self.view_zoom = new_zoom
        self.status_var.set(f"Zoom: {self.view_zoom * 100:.0f}%")
        self._schedule_view_refresh(40)
        return "break"

    def _refresh_current_view(self) -> None:
        """Recompute the cached preview points so the zoom fits the current canvas size."""
        self.resize_refresh_job = None
        if self.current_result is None:
            return
        self._prepare_canvas_view(self.original_panel.canvas, self.current_result.original_points)
        self._prepare_canvas_view(self.optimized_panel.canvas, self.current_result.optimized_points)
        self._draw_current_frame()

    def _recalculate_current_files(self) -> None:
        """Restart processing for the already selected files with new parameters."""
        if self.processing_active or self.animation_prepare_active:
            messagebox.showinfo(
                "Neu berechnen",
                "Bitte warten Sie, bis die aktuelle Verarbeitung abgeschlossen ist.",
                parent=self,
            )
            return

        if not self.selected_input_files:
            messagebox.showinfo(
                "Neu berechnen",
                "Es wurden noch keine Eingabedateien ausgewaehlt.",
                parent=self,
            )
            return

        self._start_processing_flow(self.selected_input_files)

    def _cleanup_step_generated_artifacts(self) -> None:
        for artifact in self.step_generated_artifacts:
            try:
                shutil.rmtree(artifact.generated_zip_path.parent, ignore_errors=True)
            except Exception:
                pass
        self.step_generated_artifacts = []
        self.step_layer_bundle_saved = False
        self.step_zip_button.config(state="disabled")

    def _build_step_bounds_warning(self, artifact: StepGeneratedArtifact) -> Optional[str]:
        bounds = artifact.manifest_data.get("xy_bounds_mm")
        if not isinstance(bounds, dict):
            return None

        try:
            min_x = float(bounds["min_x"])
            max_x = float(bounds["max_x"])
            min_y = float(bounds["min_y"])
            max_y = float(bounds["max_y"])
        except (KeyError, TypeError, ValueError):
            return None

        warnings: List[str] = []
        span_x = max_x - min_x
        span_y = max_y - min_y
        if span_x > BUILD_PLATE_WIDTH_MM + 1e-9 or span_y > BUILD_PLATE_DEPTH_MM + 1e-9:
            warnings.append(
                f"Bounding Box {span_x:.3f} x {span_y:.3f} mm ist groesser als die Bauplatte "
                f"{BUILD_PLATE_WIDTH_MM:.0f} x {BUILD_PLATE_DEPTH_MM:.0f} mm."
            )
        if (
            min_x < -(BUILD_PLATE_WIDTH_MM * 0.5) - 1e-9
            or max_x > (BUILD_PLATE_WIDTH_MM * 0.5) + 1e-9
            or min_y < -(BUILD_PLATE_DEPTH_MM * 0.5) - 1e-9
            or max_y > (BUILD_PLATE_DEPTH_MM * 0.5) + 1e-9
        ):
            warnings.append(
                "Die Original-XY-Koordinaten liegen teilweise ausserhalb der um 0,0 zentrierten Bauplatte."
            )

        if not warnings:
            return None
        return f"{artifact.source_step_path.name}: " + " ".join(warnings)

    def _save_step_layer_zip(self) -> bool:
        if not self.step_generated_artifacts:
            messagebox.showinfo("STEP-Layer speichern", "Es gibt keine generierten STEP-Layer in dieser Sitzung.", parent=self)
            return False

        if len(self.step_generated_artifacts) == 1:
            artifact = self.step_generated_artifacts[0]
            target_path = ask_for_zip_path(self, artifact.generated_zip_path.name)
            if not target_path:
                return False
            try:
                shutil.copyfile(artifact.generated_zip_path, target_path)
            except Exception as exc:
                messagebox.showerror(
                    "STEP-Layer speichern",
                    f"Das STEP-Layer-ZIP konnte nicht gespeichert werden:\n{exc}",
                    parent=self,
                )
                return False

            self.step_layer_bundle_saved = True
            self.status_var.set(f"STEP-Layer-ZIP gespeichert: {target_path}")
            messagebox.showinfo("Fertig", f"STEP-Layer-ZIP gespeichert:\n{target_path}", parent=self)
            return True

        bundle_path = ask_for_zip_path(self, "step_layer_archives.zip")
        if not bundle_path:
            return False

        try:
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for artifact in self.step_generated_artifacts:
                    folder_name = re.sub(r"[^A-Za-z0-9._-]+", "_", artifact.source_step_path.stem).strip("._-") or "step_model"
                    archive.write(artifact.generated_zip_path, arcname=f"{folder_name}/{artifact.generated_zip_path.name}")
                    archive.write(artifact.manifest_json_path, arcname=f"{folder_name}/{artifact.manifest_json_path.name}")
        except Exception as exc:
            messagebox.showerror(
                "STEP-Layer speichern",
                f"Das STEP-Layer-Buendel konnte nicht gespeichert werden:\n{exc}",
                parent=self,
            )
            return False

        self.step_layer_bundle_saved = True
        self.status_var.set(f"STEP-Layer-Buendel gespeichert: {bundle_path}")
        messagebox.showinfo("Fertig", f"STEP-Layer-Buendel gespeichert:\n{bundle_path}", parent=self)
        return True

    def _prompt_for_files(self) -> None:
        if self.processing_active:
            return

        selected_files = ask_for_input_files(self)
        if not selected_files:
            self.status_var.set("Keine Dateien ausgewaehlt. Das Fenster bleibt offen.")
            self.progress_var.set("Bitte ueber 'Dateien auswaehlen...' erneut starten.")
            return

        self.selected_input_files = tuple(selected_files)
        self.recalculate_button.config(state="normal")
        self._start_processing_flow(selected_files)

    def _start_processing_flow(self, selected_files: Sequence[str]) -> None:
        """Ask for the optimization settings and then start file processing."""
        settings = ask_for_optimization_settings(
            self,
            current_mode=self.mode,
            current_memory=self.memory,
            current_grid_spacing=self.grid_spacing,
            current_recent_percent=self.recent_percent,
            current_ghost_delay=self.ghost_delay,
            current_forward_jump=self.forward_jump,
            current_backward_jump=self.backward_jump,
            current_hilbert_order=self.hilbert_order,
            current_spot_skip=self.spot_skip,
        )
        if settings is None:
            self.status_var.set("Dateiauswahl abgebrochen. Keine Verarbeitung gestartet.")
            self.progress_var.set("Bitte ueber 'Dateien auswaehlen...' erneut starten.")
            return

        self.mode = str(settings["mode"])
        self.memory = int(settings["memory"])
        self.grid_spacing = float(settings["grid_spacing"])
        self.recent_percent = float(settings["recent_percent"])
        self.age_decay = float(settings["age_decay"])
        self.ghost_delay = int(settings["ghost_delay"])
        self.forward_jump = int(settings["forward_jump"])
        self.backward_jump = int(settings["backward_jump"])
        self.hilbert_order = int(settings.get("hilbert_order", HILBERT_ORDER_DEFAULT))
        self.spot_skip = int(settings.get("spot_skip", SPOT_SKIP_DEFAULT))

        plain_files = [file_path for file_path in selected_files if Path(file_path).suffix.lower() not in {".zip", ".step", ".stp"}]
        zip_files = [file_path for file_path in selected_files if Path(file_path).suffix.lower() == ".zip"]
        step_files = [file_path for file_path in selected_files if is_step_file_path(file_path)]
        input_sources: List[InputSource] = [normalize_input_source(file_path) for file_path in plain_files]
        pending_step_settings: Optional[Dict[str, float]] = None
        pending_zip_settings: Optional[Dict[str, object]] = None

        if step_files:
            pending_step_settings = ask_for_step_import_settings(
                self,
                current_point_spacing_mm=self.step_point_spacing_mm,
                current_layer_height_mm=self.step_layer_height_mm,
                current_support_layer_count=self.step_support_layer_count,
            )
            if pending_step_settings is None:
                self.status_var.set("STEP-Import abgebrochen. Keine Verarbeitung gestartet.")
                self.progress_var.set("Bitte ueber 'Dateien auswaehlen...' erneut starten.")
                return

        if zip_files:
            zip_settings = ask_for_zip_import_settings(
                self,
                current_entry_types=self.zip_entry_types,
                current_support_end_layer=self.zip_support_end_layer,
            )
            if zip_settings is None:
                self.status_var.set("ZIP-Import abgebrochen. Keine Verarbeitung gestartet.")
                self.progress_var.set("Bitte ueber 'Dateien auswaehlen...' erneut starten.")
                return
            pending_zip_settings = zip_settings

        self._cleanup_step_generated_artifacts()

        if pending_step_settings is not None:
            self.step_point_spacing_mm = float(pending_step_settings["point_spacing_mm"])
            self.step_layer_height_mm = float(pending_step_settings["layer_height_mm"])
            self.step_support_layer_count = max(0, int(pending_step_settings["support_layer_count"]))
            step_generation_errors: List[str] = []
            step_generation_warnings: List[str] = []

            for current_index, step_file_path in enumerate(step_files, start=1):
                step_name = Path(step_file_path).name
                self.status_var.set(f"STEP wird gesliced: {step_name}")
                self.progress_var.set(
                    f"{current_index} / {len(step_files)} STEP-Datei(en) werden zu B99-Layern umgesetzt."
                )
                self.update_idletasks()
                try:
                    artifact = run_modal_background_task(
                        self,
                        "STEP wird geladen",
                        (
                            f"{step_name} wird zu B99-Layern umgesetzt.\n\n"
                            f"Punktabstand: {self.step_point_spacing_mm:.6g} mm | "
                            f"Ebenendicke: {self.step_layer_height_mm:.6g} mm | "
                            f"Stuetzschichten: {self.step_support_layer_count}"
                        ),
                        lambda step_file_path=step_file_path: generate_step_layer_artifact(
                            step_file_path=step_file_path,
                            point_spacing_mm=self.step_point_spacing_mm,
                            layer_height_mm=self.step_layer_height_mm,
                            support_layer_count=self.step_support_layer_count,
                        ),
                    )
                except Exception as exc:
                    step_generation_errors.append(f"{step_name}: {exc}")
                    continue

                self.step_generated_artifacts.append(artifact)
                warning_message = self._build_step_bounds_warning(artifact)
                if warning_message:
                    step_generation_warnings.append(warning_message)

            self.step_zip_button.config(state="normal" if self.step_generated_artifacts else "disabled")

            if step_generation_warnings:
                messagebox.showwarning(
                    "STEP-Bounds",
                    "Einige STEP-Modelle liegen ausserhalb der aktuellen Bauplatten-Annahme:\n\n"
                    + "\n\n".join(step_generation_warnings),
                    parent=self,
                )

            if step_generation_errors:
                messagebox.showwarning(
                    "STEP-Import",
                    "Einige STEP-Dateien konnten nicht umgesetzt werden:\n\n" + "\n\n".join(step_generation_errors),
                    parent=self,
                )

            if self.step_generated_artifacts:
                step_input_sources, step_source_errors = build_input_sources(
                    [str(artifact.generated_zip_path) for artifact in self.step_generated_artifacts],
                    zip_entry_types=STEP_GENERATED_ZIP_ENTRY_TYPES,
                    zip_support_end_layer=0,
                )
                if step_source_errors:
                    messagebox.showwarning(
                        "STEP-Import",
                        "Einige generierte STEP-Layer konnten nicht geladen werden:\n\n"
                        + "\n\n".join(step_source_errors),
                        parent=self,
                    )
                input_sources.extend(step_input_sources)

        if pending_zip_settings is not None:
            self.zip_entry_types = normalize_zip_entry_types(pending_zip_settings["entry_types"])
            self.zip_support_end_layer = int(pending_zip_settings["support_end_layer"])

            zip_input_sources, source_errors = build_input_sources(
                zip_files,
                zip_entry_types=self.zip_entry_types,
                zip_support_end_layer=self.zip_support_end_layer,
            )
            if source_errors:
                messagebox.showwarning(
                    "ZIP-Import",
                    "Einige Eingaben konnten nicht aufgeloest werden:\n\n" + "\n\n".join(source_errors),
                    parent=self,
                )
            input_sources.extend(zip_input_sources)

        if not input_sources:
            self.status_var.set("Keine gueltigen Eingaben gefunden.")
            self.progress_var.set("Bitte andere Dateien oder Import-Parameter waehlen.")
            return

        if self.mode in {"deterministic_grid_dispersion", "stochastic_grid_dispersion"}:
            preview_items, preview_errors = build_grid_preview_data(input_sources)
            if preview_errors:
                messagebox.showwarning(
                    "Grid-Vorschau",
                    "Einige Dateien konnten fuer die Gittervorschau nicht geladen werden:\n\n"
                    + "\n\n".join(preview_errors),
                    parent=self,
                )

            preview_spacing = ask_for_grid_spread_preview(
                self,
                preview_items=preview_items,
                current_grid_spacing=self.grid_spacing,
            )
            if preview_spacing is None:
                self.status_var.set("Grid-Vorschau abgebrochen. Keine Verarbeitung gestartet.")
                self.progress_var.set("Bitte ueber 'Dateien auswaehlen...' erneut starten.")
                return

            self.grid_spacing = preview_spacing

        self.start_processing(input_sources)

    def _on_result_selected(self, _event: tk.Event) -> None:
        """Update the visible file information when the selected result changes."""
        if not self.results:
            return

        selected_name = self.file_var.get()
        try:
            index = [result.source_label for result in self.results].index(selected_name)
        except ValueError:
            return

        self._load_result(index, start_playback=False)
        self.status_var.set("Ergebnis ausgewaehlt. Interaktiven Viewer bei Bedarf oeffnen.")

    def start_processing(self, input_sources: Sequence[Union[str, InputSource]]) -> None:
        normalized_sources = [normalize_input_source(source) for source in input_sources]
        self.selected_sources = tuple(normalized_sources)
        self.process_cancel_event.clear()
        self.animation_cancel_event.clear()
        if self.queue_poll_job is not None:
            self.after_cancel(self.queue_poll_job)
            self.queue_poll_job = None
        if self.loading_job is not None:
            self.after_cancel(self.loading_job)
            self.loading_job = None
        if self.prepare_animation_job is not None:
            self.after_cancel(self.prepare_animation_job)
            self.prepare_animation_job = None
        if self.animation_job is not None:
            self.after_cancel(self.animation_job)
            self.animation_job = None

        self.total_files = len(normalized_sources)
        self.completed_files = 0
        self.results = []
        self.errors = []
        self.cancelled_files = []
        self.result_map = {}
        self.error_map = {}
        self.cancelled_map = {}
        self.file_progress_map = {index: 0.0 for index in range(1, self.total_files + 1)}
        self.current_result = None
        self.current_bounds = None
        self.animation_plan = None
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_paused = False
        self.pause_button_var.set("Play")
        self.pending_animation_prepare = None
        self.processing_active = True
        self.processing_cancel_requested = False
        self.animation_cancel_requested = False
        self.zip_saved = False
        self.current_progress_fraction = 0.0
        self.loading_tick = 0
        self.processing_started_at = time.perf_counter()
        self.processing_total_seconds = 0.0
        self.current_operation_started_at = self.processing_started_at
        self.last_animation_prepare_seconds = 0.0
        self.loading_message = (
            f"Verarbeite {self.total_files} Datei(en)/Eintraege im Modus {get_mode_label(self.mode)} "
            f"parallel auf bis zu {self.max_process_workers} Kernen"
        )
        self.worker_queue = queue.Queue()
        self.view_zoom = 1.0

        self.file_var.set("")
        self.file_selector.config(values=[], state="disabled")
        self.recalculate_button.config(state="disabled")
        self.preview_button.config(state="disabled")
        self.pause_button.config(state="disabled")
        self.zip_button.config(state="disabled")
        self._set_cancel_button_state(True)
        self.progress_bar.config(mode="determinate", maximum=max(1, self.total_files), value=0.0)
        self.progress_var.set(f"0 / {self.total_files} Dateien verarbeitet")
        self.status_var.set("Verarbeitung gestartet.")
        self._set_timer_text("Laufzeit", 0.0)
        self._reset_preview("Dateien werden verarbeitet...")

        for file_index, input_source in enumerate(normalized_sources, start=1):
            file_name = input_source.source_label
            self.worker_queue.put(("file_start", file_index, self.total_files, file_name))
            future = self.process_pool.submit(
                process_file_in_subprocess,
                file_index,
                self.total_files,
                input_source,
                self.w1,
                self.w2,
                self.memory,
                self.mode,
                self.process_progress_queue,
                self.grid_spacing,
                self.recent_percent,
                self.age_decay,
                self.ghost_delay,
                self.forward_jump,
                self.backward_jump,
                self.hilbert_order,
                self.spot_skip,
                self.process_cancel_event,
            )
            future.add_done_callback(
                lambda done_future, current_index=file_index, total=self.total_files, current_source=input_source: self._enqueue_file_future_result(
                    done_future,
                    current_index,
                    total,
                    current_source,
                )
            )
        self.queue_poll_job = self.after(80, self._poll_worker_queue)
        self.loading_job = self.after(120, self._animate_loading)

    def _poll_worker_queue(self) -> None:
        self._drain_process_progress_queue()

        while True:
            try:
                message = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            kind = message[0]

            if kind == "file_start":
                _, file_index, total_files, file_name = message
                self.loading_message = f"Datei {file_index} / {total_files}: {file_name}"
                self.current_progress_fraction = self.file_progress_map.get(file_index, 0.0)
                self._update_progress_widgets()
            elif kind == "file_future_done":
                _, file_index, total_files, input_source, future = message
                file_name = normalize_input_source(input_source).source_label
                self.file_progress_map[file_index] = 1.0
                try:
                    _, _, _, result = future.result()
                except Exception as exc:
                    if is_cancelled_exception(exc):
                        self.cancelled_map[file_index] = file_name
                        self.loading_message = f"Datei {file_index} / {total_files}: {file_name} - abgebrochen"
                    else:
                        self.error_map[file_index] = f"{file_name}: {exc}"
                        self.loading_message = f"Datei {file_index} / {total_files}: {file_name} - Fehler"
                else:
                    self.result_map[file_index] = result
                    print_analysis_for_file(result)
                    self.loading_message = f"Datei {file_index} / {total_files}: {file_name} abgeschlossen"

                self.completed_files = len(self.result_map) + len(self.error_map) + len(self.cancelled_map)
                self.current_progress_fraction = 0.0
                self._update_progress_widgets()

                if self.completed_files == self.total_files:
                    self.results = [self.result_map[index] for index in sorted(self.result_map)]
                    self.errors = [self.error_map[index] for index in sorted(self.error_map)]
                    self.cancelled_files = [self.cancelled_map[index] for index in sorted(self.cancelled_map)]
                    self.file_progress_map = {}
                    self.processing_active = False
                    self.processing_total_seconds = (
                        0.0
                        if self.processing_started_at is None
                        else max(0.0, time.perf_counter() - self.processing_started_at)
                    )
                    self.processing_started_at = None
                    self.current_operation_started_at = None
                    self.process_cancel_event.clear()
                    self.processing_cancel_requested = False
                    self._set_cancel_button_state(False)
                    self._finish_processing()
            elif kind == "animation_future_done":
                _, result_index, speed_multiplier, future = message
                self.animation_future = None
                self.animation_prepare_active = False
                self.last_animation_prepare_seconds = (
                    0.0
                    if self.current_operation_started_at is None
                    else max(0.0, time.perf_counter() - self.current_operation_started_at)
                )
                self.current_operation_started_at = None
                self._set_cancel_button_state(False)

                try:
                    _, file_name, result_speed, plan = future.result()
                except Exception as exc:
                    self._show_animation_compute_button()
                    self.file_selector.config(state="readonly")
                    self.speed_slider.config(state="normal")
                    self.trail_slider.config(state="normal")
                    self.pause_button.config(state="disabled")
                    if self.loading_job is not None:
                        self.after_cancel(self.loading_job)
                        self.loading_job = None
                    if is_cancelled_exception(exc):
                        self.status_var.set("Animationsvorbereitung abgebrochen.")
                        self.progress_var.set("Animation nicht vorbereitet. Bitte erneut 'Animation berechnen' klicken.")
                        self._set_timer_text("Vorbereitung", self.last_animation_prepare_seconds)
                    else:
                        messagebox.showerror("Animation", f"Animation konnte nicht vorbereitet werden:\n{exc}", parent=self)
                        self.status_var.set("Fehler bei der Animationsvorbereitung.")
                        self.progress_var.set(str(exc))
                else:
                    if (
                        self.current_result is None
                        or result_index >= len(self.results)
                        or self.results[result_index].source_label != file_name
                        or int(self.animation_speed_var.get()) != result_speed
                    ):
                        self.pending_animation_prepare = (result_index, int(self.animation_speed_var.get()))
                    else:
                        self.animation_plan = plan
                        self.animation_ready = True
                        self.animation_frame_index = 0
                        self.animation_index = 0
                        self.animation_progress_fraction = 0.0
                        self._show_pause_button()
                        self.pause_button_var.set("Pause")
                        self.file_selector.config(state="readonly")
                        self.speed_slider.config(state="normal")
                        self.trail_slider.config(state="normal")
                        if self.loading_job is not None:
                            self.after_cancel(self.loading_job)
                            self.loading_job = None
                        self.progress_bar.config(maximum=1.0, value=1.0)
                        self.progress_var.set(
                            f"{plan.frame_count} Frames vorbereitet - {plan.points_per_second:.1f} Punkte/s bei {plan.fps:.0f} FPS"
                        )
                        self.status_var.set(f"Animation bereit fuer: {file_name}")
                        self._set_timer_text("Vorbereitung", self.last_animation_prepare_seconds)
                        self._load_result(result_index, start_playback=True)
                self.animation_cancel_requested = False
                self.animation_cancel_event.clear()

            if (
                not self.processing_active
                and not self.animation_prepare_active
                and self.pending_animation_prepare is not None
                and self.animation_future is None
            ):
                next_index, _ = self.pending_animation_prepare
                self.pending_animation_prepare = None
                self._start_animation_prepare(next_index)
                return

        if self.processing_active or self.animation_prepare_active or self.animation_future is not None:
            self.queue_poll_job = self.after(80, self._poll_worker_queue)
        else:
            self.queue_poll_job = None

    def _animate_loading(self) -> None:
        if not self.processing_active and not self.animation_prepare_active:
            self.loading_job = None
            return

        frames = ["   ", ".  ", ".. ", "..."]
        suffix = frames[self.loading_tick % len(frames)]
        elapsed_seconds = 0.0
        if self.current_operation_started_at is not None:
            elapsed_seconds = time.perf_counter() - self.current_operation_started_at
        self._set_timer_text("Laufzeit", elapsed_seconds)
        status_suffix = " | Abbruch laeuft" if (self.processing_cancel_requested or self.animation_cancel_requested) else ""
        self.status_var.set(f"{self.loading_message}{suffix}{status_suffix}")
        self.loading_tick += 1
        self.loading_job = self.after(160, self._animate_loading)

    def _update_progress_widgets(self) -> None:
        if self.processing_active:
            total_value = sum(self.file_progress_map.values()) if self.file_progress_map else 0.0
            self.progress_bar.config(maximum=max(1, self.total_files), value=min(total_value, self.total_files))
            current_display = min(self.total_files, max(1, self.completed_files + 1))
            percent = int(self.current_progress_fraction * 100)
            cancelled_count = len(self.cancelled_map)
            if self.processing_cancel_requested:
                self.progress_var.set(
                    f"Abbruch angefordert - {self.completed_files} / {self.total_files} abgeschlossen "
                    f"({cancelled_count} abgebrochen)"
                )
            else:
                self.progress_var.set(
                    f"{self.completed_files} / {self.total_files} Dateien fertig - "
                    f"aktive Datei {current_display} bei {percent}%"
                )

    def _finish_processing(self) -> None:
        if self.loading_job is not None:
            self.after_cancel(self.loading_job)
            self.loading_job = None
        self.progress_bar.config(value=len(self.results) + len(self.errors) + len(self.cancelled_files))
        self._set_cancel_button_state(False)
        self.recalculate_button.config(state="normal" if self.selected_input_files else "disabled")
        self.status_var.set("Verarbeitung abgeschlossen.")
        self._set_timer_text("Gesamtzeit", self.processing_total_seconds)

        if self.errors:
            messagebox.showwarning(
                "Einige Dateien konnten nicht verarbeitet werden",
                "\n\n".join(self.errors),
                parent=self,
            )

        if not self.results:
            if self.cancelled_files and not self.errors:
                self.status_var.set("Verarbeitung abgebrochen.")
                self.progress_var.set(
                    f"Keine Datei abgeschlossen. Gesamtzeit: {format_duration(self.processing_total_seconds, include_tenths=True)}"
                )
            else:
                self.status_var.set("Keine Datei konnte verarbeitet werden.")
                self.progress_var.set("Bitte waehlen Sie andere Dateien aus.")
            self._reset_preview("Keine gueltigen Ergebnisse vorhanden.")
            return

        file_names = [result.source_label for result in self.results]
        self.file_selector.config(values=file_names, state="readonly")
        self.zip_button.config(state="normal")
        self.file_var.set(file_names[0])
        self._show_animation_compute_button()
        self.pause_button.config(state="disabled")
        self.last_confirmed_speed = int(self.animation_speed_var.get())
        self.last_confirmed_trail = int(self.trail_count_var.get())

        summary_lines = [
            f"Gesamtzeit: {format_duration(self.processing_total_seconds, include_tenths=True)}",
            f"Erfolgreich: {len(self.results)} | Fehler: {len(self.errors)} | Abgebrochen: {len(self.cancelled_files)}",
        ]
        summary_lines.extend(
            f"{result.source_label}: {format_duration(result.processing_seconds, include_tenths=True)}"
            for result in self.results
        )
        self.status_var.set("Verarbeitung abgeschlossen. Bitte Ergebnis pruefen oder Viewer oeffnen.")
        self.progress_var.set("\n".join(summary_lines))
        self._load_result(0, start_playback=False)
        self.status_var.set("Ergebnis geladen. Interaktiven Viewer bei Bedarf oeffnen.")

    def _build_viewer_payload(self, selected_index: int) -> Dict[str, Any]:
        """Serialize the current results into a payload consumed by the standalone viewer."""
        return {
            "app_name": APP_DISPLAY_NAME,
            "mode_id": normalize_mode(self.mode),
            "mode_label": get_mode_label(self.mode),
            "mode_description": get_mode_spec(self.mode).description,
            "coordinate_scale_mm": DISPLAY_COORDINATE_SCALE_MM,
            "coordinate_unit": "mm",
            "build_plate_width_mm": BUILD_PLATE_WIDTH_MM,
            "build_plate_depth_mm": BUILD_PLATE_DEPTH_MM,
            "origin_reference": "build_plate_centre",
            "point_spacing_mm": DISPLAY_POINT_SPACING_MM,
            "selected_index": selected_index,
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
                for result in self.results
            ],
        }

    def _create_viewer_payload_file(self, selected_index: int) -> str:
        """Write one temporary viewer payload file and return its path."""
        payload = self._build_viewer_payload(selected_index)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".json",
            prefix=VIEWER_PAYLOAD_PREFIX,
            delete=False,
        ) as handle:
            json.dump(payload, handle)
            return handle.name

    def _open_interactive_viewer(self) -> None:
        """Launch the separate Qt viewer for the currently processed results."""
        if not self.results:
            messagebox.showinfo(APP_DISPLAY_NAME, "Es gibt noch keine Ergebnisse fuer den Viewer.", parent=self)
            return

        selected_name = self.file_var.get() or self.results[0].source_label
        try:
            selected_index = [result.source_label for result in self.results].index(selected_name)
        except ValueError:
            selected_index = 0

        try:
            self.status_var.set("Viewer wird vorbereitet...")
            self.progress_var.set("Animationsdaten und Punktlisten werden fuer den externen Viewer gepackt.")
            self.update_idletasks()
            payload_path = str(
                run_modal_background_task(
                    self,
                    "Viewer vorbereiten",
                    "Die Daten fuer den interaktiven Viewer werden vorbereitet. Das Hauptfenster bleibt dabei bedienbar.",
                    lambda: self._create_viewer_payload_file(selected_index),
                )
            )
        except Exception as exc:
            messagebox.showerror(APP_DISPLAY_NAME, f"Viewer-Payload konnte nicht erstellt werden:\n{exc}", parent=self)
            return

        if getattr(sys, "frozen", False):
            command = [sys.executable, "--viewer-payload", payload_path]
        else:
            command = [sys.executable, str(Path(__file__).resolve()), "--viewer-payload", payload_path]

        try:
            viewer_process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent))
        except Exception as exc:
            messagebox.showerror(
                APP_DISPLAY_NAME,
                (
                    "Der interaktive Viewer konnte nicht gestartet werden.\n\n"
                    "Bitte pruefen Sie, ob PySide6 und numpy installiert sind.\n\n"
                    f"Technischer Hinweis:\n{exc}"
                ),
                parent=self,
            )
            try:
                Path(payload_path).unlink()
            except OSError:
                pass
            return

        self.viewer_processes.append(viewer_process)
        self.status_var.set(f"Interaktiver Viewer gestartet fuer: {self.results[selected_index].source_label}")
        self.progress_var.set("Viewer erfolgreich gestartet.")

    def _show_selected_animation(self) -> None:
        self._open_interactive_viewer()

    def _toggle_pause(self) -> None:
        """Pause or resume the already prepared animation."""
        if self.animation_plan is None:
            return

        self.animation_paused = not self.animation_paused
        if self.animation_paused:
            if self.animation_job is not None:
                self.after_cancel(self.animation_job)
                self.animation_job = None
            self.pause_button_var.set("Play")
            self.status_var.set("Animation pausiert.")
        else:
            self.pause_button_var.set("Pause")
            if self.current_result is not None:
                if self.animation_plan is not None:
                    self.status_var.set(
                        f"Animation aktiv fuer: {self.current_result.source_label} - {self.animation_plan.fps:.0f} FPS"
                    )
                else:
                    self.status_var.set(f"Animation aktiv fuer: {self.current_result.source_label}")
            self._restart_animation()

    def _start_animation_prepare(self, index: int) -> None:
        """Prepare the full animation timeline in a separate process before playback."""
        if self.processing_active:
            return

        if self.prepare_animation_job is not None:
            self.after_cancel(self.prepare_animation_job)
            self.prepare_animation_job = None

        if self.animation_job is not None:
            self.after_cancel(self.animation_job)
            self.animation_job = None

        self.current_result = self.results[index]
        self.current_bounds = compute_bounds(self.current_result)
        self.animation_plan = None
        self.animation_ready = False
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_paused = False
        self.pause_button_var.set("Play")
        self.file_var.set(self.current_result.source_label)
        self.animation_prepare_active = True
        self.animation_cancel_requested = False
        self.animation_cancel_event.clear()
        self.pending_animation_prepare = None
        self.current_progress_fraction = 0.0
        self.animation_progress_fraction = 0.0
        self.loading_tick = 0
        self.current_operation_started_at = time.perf_counter()
        self.last_animation_prepare_seconds = 0.0
        self.loading_message = f"Berechne Animation: {self.current_result.source_label}"

        self.preview_button.config(state="disabled")
        self.pause_button.config(state="disabled")
        self.file_selector.config(state="disabled")
        self.speed_slider.config(state="disabled")
        self.trail_slider.config(state="disabled")
        self._set_cancel_button_state(True)
        self.progress_bar.config(mode="determinate", maximum=1.0, value=0.0)
        self.progress_var.set("Animationsdaten werden vorbereitet")
        self.status_var.set("Animation wird vorbereitet")
        self._set_timer_text("Laufzeit", 0.0)
        self._prepare_canvas_view(self.original_panel.canvas, self.current_result.original_points)
        self._prepare_canvas_view(self.optimized_panel.canvas, self.current_result.optimized_points)
        self._draw_current_frame()

        if self.loading_job is not None:
            self.after_cancel(self.loading_job)
        self.loading_job = self.after(120, self._animate_loading)

        selected_index = index
        selected_speed = int(self.animation_speed_var.get())
        self.animation_future = self.process_pool.submit(
            build_animation_plan_in_subprocess,
            selected_index,
            self.current_result.source_label,
            len(self.current_result.original_points),
            selected_speed,
            self.process_progress_queue,
            self.animation_cancel_event,
        )
        self.animation_future.add_done_callback(
            lambda done_future, result_idx=selected_index, speed_value=selected_speed: self._enqueue_animation_future_result(
                done_future,
                result_idx,
                speed_value,
            )
        )
        if self.queue_poll_job is None:
            self.queue_poll_job = self.after(80, self._poll_worker_queue)

    def _load_result(self, index: int, start_playback: bool = False) -> None:
        self.current_result = self.results[index]
        self.current_bounds = compute_bounds(self.current_result)
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_paused = False
        self.pause_button_var.set("Play")

        self.file_var.set(self.current_result.source_label)
        source_lines = [f"Quelle: {self.current_result.source_path}"]
        if self.current_result.archive_member is not None:
            source_lines = [
                f"Archiv: {self.current_result.source_path}",
                f"Eintrag: {self.current_result.archive_member}",
            ]
        mode_spec = get_mode_spec(self.mode)
        parameter_lines: List[str] = []
        if MODE_PARAMETER_MEMORY in mode_spec.visible_parameters:
            parameter_lines.append(f"Memory: {self.memory}")
        if MODE_PARAMETER_GRID_SPACING in mode_spec.visible_parameters:
            parameter_lines.append(
                f"Grid spacing: {self.grid_spacing:.6g} rel (~ {scale_distance_for_display(self.grid_spacing):.6g} mm)"
            )
        if MODE_PARAMETER_RECENT_PERCENT in mode_spec.visible_parameters:
            parameter_lines.append(f"Recent set: {self.recent_percent:.1f}%")
            parameter_lines.append(f"Age decay (fixed): {self.age_decay:.2f}")
        if MODE_PARAMETER_GHOST_DELAY in mode_spec.visible_parameters:
            parameter_lines.append(f"Ghost delay: {self.ghost_delay} points")
        if MODE_PARAMETER_FORWARD_JUMP in mode_spec.visible_parameters:
            parameter_lines.append(f"Forward jump: {self.forward_jump}")
        if MODE_PARAMETER_BACKWARD_JUMP in mode_spec.visible_parameters:
            parameter_lines.append(f"Backward jump: {self.backward_jump}")
        if not parameter_lines:
            parameter_lines.append("Zusaetzliche Parameter: keine")

        current_zip_name = build_default_zip_name([self.current_result], self.mode)
        viewer_hint = "Interaktiver Viewer: separates Qt-Fenster mit exaktem Raster-Backend als Standard und optionalem OpenGL."
        coordinate_hint = (
            f"Anzeige in mm: x/y = ABS * {DISPLAY_COORDINATE_SCALE_MM:.0f} | "
            f"0,0 = Mittelpunkt | Bauplatte: {BUILD_PLATE_WIDTH_MM:.0f} x {BUILD_PLATE_DEPTH_MM:.0f} mm | "
            f"Punktabstand: {DISPLAY_POINT_SPACING_MM:.1f} mm"
        )
        self.file_info_label.config(
            text=(
                "\n".join(source_lines)
                + "\n"
                + f"Anzeige: {self.current_result.source_label}\n"
                f"Exportname im Ergebnis-ZIP: {self.current_result.output_name}\n"
                f"Vorgeschlagener ZIP-Name: {current_zip_name}\n"
                f"Punkte: {len(self.current_result.original_points)}\n"
                f"Berechnungszeit: {format_duration(self.current_result.processing_seconds, include_tenths=True)}\n"
                f"Modus: {mode_spec.label}\n"
                + "\n".join(parameter_lines)
                + "\n"
                + f"Beschreibung: {mode_spec.description}\n"
                + coordinate_hint
                + "\n"
                + viewer_hint
            )
        )
        self.original_stats_label.config(
            text=(
                "Original\n--------\n"
                + format_stats(
                    self.current_result.original_stats,
                    distance_scale=DISPLAY_COORDINATE_SCALE_MM,
                    distance_unit=" mm",
                )
            )
        )
        self.optimized_stats_label.config(
            text=(
                "Optimised\n---------\n"
                + format_stats(
                    self.current_result.optimized_stats,
                    distance_scale=DISPLAY_COORDINATE_SCALE_MM,
                    distance_unit=" mm",
                )
            )
        )

        self.status_var.set(f"Ergebnis aktiv: {self.current_result.source_label}")
        self.preview_button.config(state="normal")
        self.pause_button.config(state="disabled")

    def _restart_animation(self) -> None:
        if self.animation_job is not None:
            self.after_cancel(self.animation_job)
            self.animation_job = None
        if self.animation_plan is None or self.animation_paused:
            return
        _, _, _, interval_ms = self._get_animation_timing()
        self.animation_job = self.after(interval_ms, self._animation_step)

    def _animation_step(self) -> None:
        if self.current_result is None or self.animation_plan is None:
            return

        point_count = len(self.current_result.original_points)
        if point_count == 0:
            return

        _, _, _, interval_ms = self._get_animation_timing()
        self.animation_frame_index = (self.animation_frame_index + 1) % self.animation_plan.frame_count
        current_progress = self.animation_plan.progress_values[self.animation_frame_index]
        self.animation_index = min(int(current_progress), point_count - 1)
        self._draw_current_frame()
        if not self.animation_paused:
            self.animation_job = self.after(interval_ms, self._animation_step)

    def _draw_current_frame(self) -> None:
        if self.current_result is None:
            return

        current_progress = 0.0
        if self.animation_plan is not None and self.animation_plan.frame_count > 0:
            current_progress = self.animation_plan.progress_values[self.animation_frame_index]

        self.original_label_var.set(
            f"Punkt {min(int(current_progress) + 1, max(1, len(self.current_result.original_points)))} / "
            f"{len(self.current_result.original_points)}"
        )
        self.optimized_label_var.set(
            f"Punkt {min(int(current_progress) + 1, max(1, len(self.current_result.optimized_points)))} / "
            f"{len(self.current_result.optimized_points)}"
        )

    def _map_point(self, point: Point, width: int, height: int) -> Tuple[float, float]:
        assert self.current_bounds is not None
        min_x, max_x, min_y, max_y = self.current_bounds
        span_x = max(max_x - min_x, 1e-12)
        span_y = max(max_y - min_y, 1e-12)
        margin = max(8.0, min(width, height) * 0.02)

        scale = min((width - margin * 2) / span_x, (height - margin * 2) / span_y) * self.view_zoom
        content_width = span_x * scale
        content_height = span_y * scale

        offset_x = (width - content_width) / 2
        offset_y = (height - content_height) / 2

        x = offset_x + (point[0] - min_x) * scale
        y = height - (offset_y + (point[1] - min_y) * scale)
        return (x, y)

    def _reset_preview(self, message: str) -> None:
        self.current_result = None
        self.current_bounds = None
        self.view_zoom = 1.0
        self.animation_plan = None
        self.animation_ready = False
        self.animation_frame_index = 0
        self.animation_index = 0
        self.animation_paused = False
        self.pause_button_var.set("Play")
        self.original_label_var.set("Punkt 0 / 0")
        self.optimized_label_var.set("Punkt 0 / 0")
        self.pause_button.config(state="disabled")
        self.preview_button.config(state="normal" if self.results else "disabled")
        self.trail_slider.config(to=TRAIL_MAX_POINTS)
        self.file_info_label.config(text=message)
        self.original_stats_label.config(text="Original\n--------\nNoch keine Datei aktiv.")
        self.optimized_stats_label.config(text="Optimised\n---------\nNoch keine Datei aktiv.")

    def _draw_placeholder(self, canvas: tk.Canvas, message: str) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), int(canvas["width"]))
        height = max(canvas.winfo_height(), int(canvas["height"]))
        canvas.create_rectangle(0, 0, width, height, fill="#000000", outline="")
        canvas.create_text(
            width / 2,
            height / 2,
            text=message,
            fill="#8a8a8a",
            font=("Segoe UI", 13),
            width=max(320, width - 80),
            justify="center",
        )
    def _get_trail_colors(self, count: int) -> List[str]:
        """Return a cached list of gradient colors for the requested bin count."""
        safe_count = max(1, int(count))
        colors = self.trail_color_cache.get(safe_count)
        if colors is None:
            colors = build_trail_colors(safe_count)
            self.trail_color_cache[safe_count] = colors
        return colors

    def _put_photo_square(
        self,
        photo: tk.PhotoImage,
        x_pos: int,
        y_pos: int,
        color: str,
        half_size: int,
        width: int,
        height: int,
    ) -> None:
        """Paint one small filled square into a PhotoImage."""
        left = max(0, x_pos - half_size)
        top = max(0, y_pos - half_size)
        right = min(width, x_pos + half_size + 1)
        bottom = min(height, y_pos + half_size + 1)
        if left >= right or top >= bottom:
            return
        photo.put(color, to=(left, top, right, bottom))

    def _paint_point_range(
        self,
        canvas: tk.Canvas,
        start_index: int,
        end_index: int,
        color: str,
        half_size: int,
    ) -> None:
        """Paint a contiguous point range into the canvas raster layer."""
        if end_index < start_index:
            return

        photo: Optional[tk.PhotoImage] = getattr(canvas, "render_photo", None)
        cached_points: List[Tuple[int, int]] = getattr(canvas, "cached_points", [])
        width = getattr(canvas, "cached_width", 0)
        height = getattr(canvas, "cached_height", 0)

        if photo is None or not cached_points or width <= 0 or height <= 0:
            return

        max_index = min(end_index, len(cached_points) - 1)
        for point_index in range(max(start_index, 0), max_index + 1):
            x_pos, y_pos = cached_points[point_index]
            self._put_photo_square(photo, x_pos, y_pos, color, half_size, width, height)

    def _reset_canvas_raster(self, canvas: tk.Canvas) -> None:
        """Reset one canvas to the base image with all points as dark background dots."""
        import numpy as np
        import base64
        import io
        from PIL import Image

        cached_points: List[Tuple[int, int]] = getattr(canvas, "cached_points", [])
        width = getattr(canvas, "cached_width", 0)
        height = getattr(canvas, "cached_height", 0)
        image_item = getattr(canvas, "image_item", None)

        if image_item is None or width <= 0 or height <= 0:
            return

        arr = np.zeros((height, width, 3), dtype=np.uint8)
        half = BACKGROUND_POINT_HALF_SIZE
        dim = 0x1b
        for x_pos, y_pos in cached_points:
            x0 = max(0, x_pos - half)
            x1 = min(width, x_pos + half + 1)
            y0 = max(0, y_pos - half)
            y1 = min(height, y_pos + half + 1)
            arr[y0:y1, x0:x1] = dim

        buf = io.BytesIO()
        Image.fromarray(arr, "RGB").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue())
        new_photo = tk.PhotoImage(data=b64)
        canvas.render_photo = new_photo
        canvas.itemconfigure(image_item, image=new_photo)

        canvas.last_base_index = -1
        canvas.last_gray_limit = -1
        canvas.last_gradient_ranges = []
        canvas.last_gradient_bin_count = 0
        canvas.last_trail_count = int(self.trail_count_var.get())
        head_item = getattr(canvas, "head_item", None)
        if head_item is not None:
            canvas.itemconfigure(head_item, state="hidden")

    def _draw_canvas_state_from_scratch(self, canvas: tk.Canvas, base_index: int, trail_count: int) -> None:
        """Rebuild the visible canvas state for the current animation position."""
        self._reset_canvas_raster(canvas)

        if base_index < 0:
            return

        gray_limit = max(-1, base_index - trail_count)
        if gray_limit >= 0:
            self._paint_point_range(canvas, 0, gray_limit, "#666666", VISITED_POINT_HALF_SIZE)

        gradient_start = max(0, base_index - trail_count + 1)
        gradient_count = base_index - gradient_start + 1
        gradient_bin_count = min(MAX_RENDER_GRADIENT_BINS, trail_count, gradient_count)
        gradient_ranges = build_gradient_bin_ranges(gradient_start, base_index, gradient_bin_count)
        gradient_colors = self._get_trail_colors(gradient_bin_count)

        for range_index, point_range in enumerate(gradient_ranges):
            self._paint_point_range(
                canvas,
                point_range[0],
                point_range[1],
                gradient_colors[range_index],
                VISITED_POINT_HALF_SIZE,
            )

        canvas.last_base_index = base_index
        canvas.last_gray_limit = gray_limit
        canvas.last_gradient_ranges = gradient_ranges
        canvas.last_gradient_bin_count = gradient_bin_count
        canvas.last_trail_count = trail_count

    def _update_canvas_raster_state(self, canvas: tk.Canvas, base_index: int, trail_count: int) -> None:
        """Update only the changed raster regions for the current animation step."""
        cached_points: List[Tuple[int, int]] = getattr(canvas, "cached_points", [])
        if not cached_points:
            return

        point_count = len(cached_points)
        safe_base_index = min(max(base_index, 0), point_count - 1)
        safe_trail_count = max(TRAIL_MIN_POINTS, min(int(trail_count), point_count))

        last_base_index = getattr(canvas, "last_base_index", -1)
        last_trail_count = getattr(canvas, "last_trail_count", safe_trail_count)
        last_gradient_bin_count = getattr(canvas, "last_gradient_bin_count", 0)

        gradient_start = max(0, safe_base_index - safe_trail_count + 1)
        gradient_count = safe_base_index - gradient_start + 1
        gradient_bin_count = min(MAX_RENDER_GRADIENT_BINS, safe_trail_count, gradient_count)

        if (
            safe_base_index < last_base_index
            or safe_trail_count != last_trail_count
            or gradient_bin_count != last_gradient_bin_count
        ):
            self._draw_canvas_state_from_scratch(canvas, safe_base_index, safe_trail_count)
            return

        if safe_base_index == last_base_index:
            return

        new_gray_limit = max(-1, safe_base_index - safe_trail_count)
        last_gray_limit = getattr(canvas, "last_gray_limit", -1)
        if new_gray_limit > last_gray_limit:
            self._paint_point_range(
                canvas,
                last_gray_limit + 1,
                new_gray_limit,
                "#666666",
                VISITED_POINT_HALF_SIZE,
            )

        new_gradient_ranges = build_gradient_bin_ranges(gradient_start, safe_base_index, gradient_bin_count)
        old_gradient_ranges: List[Tuple[int, int]] = getattr(canvas, "last_gradient_ranges", [])
        gradient_colors = self._get_trail_colors(gradient_bin_count)

        for range_index, new_range in enumerate(new_gradient_ranges):
            old_range = old_gradient_ranges[range_index] if range_index < len(old_gradient_ranges) else None
            for update_range in inclusive_range_difference(new_range, old_range):
                self._paint_point_range(
                    canvas,
                    update_range[0],
                    update_range[1],
                    gradient_colors[range_index],
                    VISITED_POINT_HALF_SIZE,
                )

        canvas.last_base_index = safe_base_index
        canvas.last_gray_limit = new_gray_limit
        canvas.last_gradient_ranges = new_gradient_ranges
        canvas.last_gradient_bin_count = gradient_bin_count
        canvas.last_trail_count = safe_trail_count

    def _prepare_canvas_view(self, canvas: tk.Canvas, points: List[Point]) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), int(canvas["width"]))
        height = max(canvas.winfo_height(), int(canvas["height"]))

        if not points or self.current_bounds is None:
            self._draw_placeholder(canvas, "Keine Punkte vorhanden.")
            canvas.cached_points = []
            canvas.render_photo = None
            canvas.head_item = None
            canvas.last_base_index = -1
            canvas.last_gray_limit = -1
            canvas.last_gradient_ranges = []
            canvas.last_gradient_bin_count = 0
            canvas.last_trail_count = int(self.trail_count_var.get())
            return

        cached_points = [
            (int(round(mapped_x)), int(round(mapped_y)))
            for mapped_x, mapped_y in (self._map_point(point, width, height) for point in points)
        ]
        canvas.cached_points = cached_points
        canvas.cached_width = width
        canvas.cached_height = height
        canvas.render_photo = tk.PhotoImage(width=width, height=height)
        canvas.image_item = canvas.create_image(0, 0, anchor="nw", image=canvas.render_photo)
        canvas.head_item = canvas.create_oval(0, 0, 0, 0, fill="#ff1010", outline="", state="hidden")
        canvas.last_base_index = -1
        canvas.last_gray_limit = -1
        canvas.last_gradient_ranges = []
        canvas.last_gradient_bin_count = 0
        canvas.last_trail_count = int(self.trail_count_var.get())
        self._reset_canvas_raster(canvas)

    def _update_canvas_animation(
        self,
        canvas: tk.Canvas,
        current_progress: float,
        label_var: tk.StringVar,
    ) -> None:
        cached_points: List[Tuple[float, float]] = getattr(canvas, "cached_points", [])
        head_item = getattr(canvas, "head_item", None)

        if not cached_points or head_item is None:
            label_var.set("Punkt 0 / 0")
            return

        point_count = len(cached_points)
        base_index = min(max(int(current_progress), 0), point_count - 1)
        next_index = min(base_index + 1, point_count - 1)
        segment_fraction = max(0.0, min(1.0, current_progress - base_index))
        start_x, start_y = cached_points[base_index]
        end_x, end_y = cached_points[next_index]
        head_x = start_x + (end_x - start_x) * segment_fraction
        head_y = start_y + (end_y - start_y) * segment_fraction

        trail_count = max(TRAIL_MIN_POINTS, min(int(self.trail_count_var.get()), point_count))
        self._update_canvas_raster_state(canvas, base_index, trail_count)
        canvas.coords(
            head_item,
            head_x - HEAD_POINT_RADIUS,
            head_y - HEAD_POINT_RADIUS,
            head_x + HEAD_POINT_RADIUS,
            head_y + HEAD_POINT_RADIUS,
        )
        canvas.itemconfigure(head_item, state="normal")

        label_var.set(f"Punkt {base_index + 1} / {len(cached_points)}")


    def _save_zip(self) -> bool:
        if not self.results:
            messagebox.showinfo("ZIP speichern", "Es gibt noch keine verarbeiteten Dateien.", parent=self)
            return False

        zip_path = ask_for_zip_path(self, build_default_zip_name(self.results, self.mode))
        if not zip_path:
            return False

        try:
            save_results_as_zip(self.results, zip_path)
        except Exception as exc:
            messagebox.showerror("Speicherfehler", f"ZIP-Datei konnte nicht gespeichert werden:\n{exc}", parent=self)
            return False

        self.zip_saved = True
        self.status_var.set(f"ZIP gespeichert: {zip_path}")
        messagebox.showinfo("Fertig", f"ZIP-Datei gespeichert:\n{zip_path}", parent=self)
        return True

    def _handle_close(self) -> None:
        if self.processing_active or self.animation_prepare_active:
            answer = messagebox.askyesno(
                "Vorgang abbrechen",
                "Es laeuft noch eine Verarbeitung oder Animationsvorbereitung. Moechten Sie das Programm wirklich schliessen?",
                parent=self,
            )
            if not answer:
                return
            self.process_cancel_event.set()
            self.animation_cancel_event.set()

        if self.results and not self.zip_saved:
            answer = messagebox.askyesnocancel(
                "ZIP speichern",
                "Moechten Sie vor dem Schliessen alle optimierten Dateien als ZIP speichern?",
                parent=self,
            )
            if answer is None:
                return
            if answer and not self._save_zip():
                return

        if self.queue_poll_job is not None:
            self.after_cancel(self.queue_poll_job)
            self.queue_poll_job = None
        if self.loading_job is not None:
            self.after_cancel(self.loading_job)
            self.loading_job = None
        if self.prepare_animation_job is not None:
            self.after_cancel(self.prepare_animation_job)
            self.prepare_animation_job = None
        if self.resize_refresh_job is not None:
            self.after_cancel(self.resize_refresh_job)
            self.resize_refresh_job = None
        if self.animation_job is not None:
            self.after_cancel(self.animation_job)
            self.animation_job = None
        try:
            self.process_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.mp_manager.shutdown()
        except Exception:
            pass
        self._cleanup_step_generated_artifacts()
        self.destroy()


def run_self_tests() -> None:
    """Run small assert-based tests before the real program starts."""
    assert _parse_abs_line("ABS 0.14821933333333334 -0.23783766666666667") == (
        0.14821933333333334,
        -0.23783766666666667,
    )
    assert _parse_abs_line("# Kommentar") is None
    assert _parse_abs_line("HEADER irgendwas") is None
    assert normalize_mode("basic") == "local_greedy"
    assert normalize_mode("greedy") == "local_greedy"
    assert normalize_mode("random_noise") == "density_adaptive_sampling"
    assert normalize_mode("direct_visualisation") == "direct_visualisation"
    assert normalize_mode("ghost_beam_scanning") == "ghost_beam_scanning"
    assert normalize_mode("interlaced_stripe_scanning") == "interlaced_stripe_scanning"
    assert get_mode_label("direct_visualisation") == "Direct Visualisation (Unchanged)"
    assert get_mode_label("ghost_beam_scanning") == "Ghost Beam Scanning"
    assert get_mode_label("spread") == "Dispersion Maximisation"
    assert get_mode_label("interlaced_stripe_scanning") == "Interlaced Stripe Scanning"
    assert build_output_name_for_source(
        InputSource("file", "sample_abs_input.txt", "sample_abs_input.txt", "sample_abs_input.txt"),
        "local_greedy",
    ) == "sample_abs_input_optimised_local_greedy.txt"
    assert build_output_name_for_source(
        InputSource("file", "sample_abs_input.txt", "sample_abs_input.txt", "sample_abs_input.txt"),
        "direct_visualisation",
    ) == "sample_abs_input_optimised_direct_visualisation.txt"
    assert build_output_name_for_source(
        InputSource("file", "sample_abs_input.txt", "sample_abs_input.txt", "sample_abs_input.txt"),
        "ghost_beam_scanning",
    ) == "sample_abs_input_optimised_ghost_beam_scanning.txt"
    assert build_output_name_for_source(
        InputSource("file", "sample_abs_input.txt", "sample_abs_input.txt", "sample_abs_input.txt"),
        "interlaced_stripe_scanning",
    ) == "sample_abs_input_optimised_interlaced_stripe_scanning.txt"
    assert _archive_member_keeps_original_name("figure files/37091.B99") is True
    assert _archive_member_keeps_original_name("Figure Files/37091.B99") is True
    assert _archive_member_keeps_original_name("inner/37091.B99") is False
    assert build_output_name_for_source(
        InputSource(
            "zip_entry",
            "sample.zip",
            "sample.zip :: figure files/37091.B99",
            "figure files/37091.B99",
            "figure files/37091.B99",
        ),
        "density_adaptive_sampling",
    ) == "figure files/37091.B99"
    assert build_output_name_for_source(
        InputSource(
            "zip_entry",
            "sample.zip",
            "sample.zip :: inner/01021.B99",
            "inner/01021.B99",
            "inner/01021.B99",
        ),
        "density_adaptive_sampling",
    ) == "inner/01021_optimised_density_adaptive_sampling.B99"
    assert parse_b99_archive_entry_name("12021.B99") == (12, "infill")
    assert parse_b99_archive_entry_name("12031.B99") == (12, "boundary")
    assert parse_b99_archive_entry_name("12091.B99") == (12, "combo")
    assert normalize_zip_entry_types(None) == ("infill",)
    assert normalize_zip_entry_types(("boundary", "infill", "boundary")) == ("boundary", "infill")
    assert set(select_bucket_candidates(list(range(10)), candidate_limit=4, randomized=False)) == {0, 1, 2, 3}
    seeded_candidates = select_bucket_candidates(
        list(range(10)),
        candidate_limit=4,
        randomized=True,
        rng=random.Random(123),
    )
    assert len(seeded_candidates) == 4
    assert len(set(seeded_candidates)) == 4
    assert set(seeded_candidates).issubset(set(range(10)))
    assert len(sample_points_for_preview([(float(index), 0.0) for index in range(100)], max_points=10)) <= 11
    assert compute_point_bounds([(0.0, 1.0), (2.0, 3.0), (-1.0, 4.0)]) == (-1.0, 2.0, 1.0, 4.0)
    assert math.isclose(scale_distance_for_display(1.0), 60.0, rel_tol=0.0, abs_tol=1e-12)
    assert scale_point_for_display((-1.0, -1.0)) == (-60.0, -60.0)
    assert scale_point_for_display((0.0, 0.0)) == (0.0, 0.0)
    assert scale_point_for_display((1.0, 1.0)) == (60.0, 60.0)
    assert scale_points_for_display([(0.0, 0.0), (1.0, 1.0)]) == [(0.0, 0.0), (60.0, 60.0)]
    assert math.isclose(clamp_viewer_point_size_mm(0.01), VIEWER_POINT_SIZE_MM_MIN, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(clamp_viewer_point_size_mm(10.0), VIEWER_POINT_SIZE_MM_MAX, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(VIEWER_POINT_SIZE_MM_DEFAULT, DISPLAY_POINT_SPACING_MM, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(viewer_point_size_slider_to_mm(125), 0.125, rel_tol=0.0, abs_tol=1e-12)
    assert viewer_point_size_mm_to_slider(1.25) == 1250
    assert viewer_point_size_mm_to_input_um(0.1) == 100
    assert math.isclose(viewer_point_size_input_um_to_mm(100), 0.1, rel_tol=0.0, abs_tol=1e-12)

    stats = analyze_path([(0.0, 0.0), (3.0, 4.0), (6.0, 8.0)])
    assert math.isclose(stats["mean_jump"], 5.0, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(stats["max_jump"], 5.0, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(stats["min_jump"], 5.0, rel_tol=0.0, abs_tol=1e-12)
    assert int(stats["count_jumps"]) == 2
    formatted_stats_mm = format_stats(stats, distance_scale=DISPLAY_COORDINATE_SCALE_MM, distance_unit=" mm")
    assert "mean_jump : 300.000000 mm" in formatted_stats_mm
    assert "std_jump  : 0.000000 mm" in formatted_stats_mm
    assert "count_jumps: 2" in formatted_stats_mm
    assert format_duration(65.4, include_tenths=True) == "00:01:05.4"

    plan = build_animation_plan(point_count=10, speed_multiplier=1)
    assert plan.frame_count > 1
    assert math.isclose(plan.fps, 30.0, rel_tol=0.0, abs_tol=1e-12)
    assert plan.progress_values[0] == 0.0
    assert plan.progress_values[-1] >= 9.0

    _, _, _, worker_plan = build_animation_plan_in_subprocess(0, "sample.txt", 10, 1, None)
    assert worker_plan.frame_count == plan.frame_count

    colors = build_trail_colors(4)
    assert len(colors) == 4
    assert colors[0] == "#ffffff"
    assert colors[-1] == "#ff1010"
    assert compute_viewer_trail_ranges(10, 8, 6, 3) == ((3, 5), (6, 8))
    assert compute_viewer_trail_ranges(10, 2, 6, 5) == (None, (0, 2))
    assert compute_viewer_trail_ranges(10, 9, 20, 20) == (None, (0, 9))
    assert build_gradient_bin_ranges(0, 9, 4) == [(0, 1), (2, 4), (5, 6), (7, 9)]
    assert inclusive_range_difference((3, 7), (1, 4)) == [(5, 7)]
    assert inclusive_range_difference((3, 7), None) == [(3, 7)]
    assert build_interlaced_block_order(5, 3) == [0, 3, 1, 4, 2]
    assert build_interlaced_block_order(6, 4) == [0, 4, 2, 1, 5, 3]
    assert reorder_ghost_beam_stripe_indices([0, 1, 2, 3], 2) == [2, 0, 3, 1]
    assert reorder_ghost_beam_stripe_indices([0, 1, 2, 3, 4, 5], 3) == [3, 0, 4, 1, 5, 2]
    assert reorder_interlaced_stripe_indices([0, 1, 2, 3, 4], 3, 2) == [0, 3, 1, 4, 2]
    assert reorder_interlaced_stripe_indices([0, 1, 2, 3], 3, 2) == [0, 3, 1, 2]
    direct_points = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.5)]
    direct_result = optimize_path(direct_points, mode="direct_visualisation")
    assert direct_result == direct_points
    assert direct_result is not direct_points
    interlaced_detection_points = [
        (-1.20, 0.0),
        (-1.10, 0.0),
        (-1.00, 0.0),
        (-2.00, 1.0),
        (-1.90, 1.0),
        (-1.80, 1.0),
    ]
    assert detect_source_stripe_ranges(interlaced_detection_points) == [(0, 2), (3, 5)]
    assert detect_interlaced_stripe_ranges(interlaced_detection_points) == [(0, 2), (3, 5)]
    ghost_points = [
        (0.0, 0.0),
        (1.0, 0.0),
        (2.0, 0.0),
        (3.0, 0.0),
        (-10.0, 1.0),
        (-9.0, 1.0),
        (-8.0, 1.0),
        (-7.0, 1.0),
    ]
    ghost_result = optimize_path(
        ghost_points,
        mode="ghost_beam_scanning",
        ghost_delay=2,
    )
    assert ghost_result == [
        ghost_points[2],
        ghost_points[0],
        ghost_points[3],
        ghost_points[1],
        ghost_points[6],
        ghost_points[4],
        ghost_points[7],
        ghost_points[5],
    ]
    assert sorted(ghost_result) == sorted(ghost_points)
    interlaced_points = [
        (0.0, 0.0),
        (1.0, 0.0),
        (2.0, 0.0),
        (3.0, 0.0),
        (4.0, 0.0),
        (-10.0, 1.0),
        (-9.0, 1.0),
        (-8.0, 1.0),
        (-7.0, 1.0),
        (-6.0, 1.0),
    ]
    interlaced_result = optimize_path(
        interlaced_points,
        mode="interlaced_stripe_scanning",
        forward_jump=3,
        backward_jump=2,
    )
    assert interlaced_result == [
        interlaced_points[0],
        interlaced_points[3],
        interlaced_points[1],
        interlaced_points[4],
        interlaced_points[2],
        interlaced_points[5],
        interlaced_points[8],
        interlaced_points[6],
        interlaced_points[9],
        interlaced_points[7],
    ]
    assert sorted(interlaced_result) == sorted(interlaced_points)
    try:
        optimize_path(interlaced_points, mode="interlaced_stripe_scanning", forward_jump=0, backward_jump=2)
    except ValueError as exc:
        assert "forward_jump" in str(exc)
    else:
        raise AssertionError("forward_jump=0 sollte fehlschlagen.")
    try:
        optimize_path(interlaced_points, mode="interlaced_stripe_scanning", forward_jump=3, backward_jump=0)
    except ValueError as exc:
        assert "backward_jump" in str(exc)
    else:
        raise AssertionError("backward_jump=0 sollte fehlschlagen.")
    try:
        optimize_path(ghost_points, mode="ghost_beam_scanning", ghost_delay=0)
    except ValueError as exc:
        assert "ghost_delay" in str(exc)
    else:
        raise AssertionError("ghost_delay=0 sollte fehlschlagen.")

    payload_test_result = ProcessedFileResult(
        source_path=Path("payload_test.txt"),
        source_label="payload_test.txt",
        archive_member=None,
        output_name="payload_test_optimised_local_greedy.txt",
        original_lines=["ABS 0.0 0.0", "ABS 1.0 1.0"],
        original_points=[(0.0, 0.0), (1.0, 1.0)],
        optimized_points=[(1.0, 1.0), (0.0, 0.0)],
        original_stats=stats,
        optimized_stats=stats,
        output_text="ABS 1.0 1.0\nABS 0.0 0.0\n",
        processing_seconds=0.1,
    )
    payload_app = ComparisonApp.__new__(ComparisonApp)
    payload_app.mode = "local_greedy"
    payload_app.results = [payload_test_result]
    viewer_payload = ComparisonApp._build_viewer_payload(payload_app, 0)
    assert math.isclose(viewer_payload["coordinate_scale_mm"], DISPLAY_COORDINATE_SCALE_MM, rel_tol=0.0, abs_tol=1e-12)
    assert viewer_payload["coordinate_unit"] == "mm"
    assert viewer_payload["origin_reference"] == "build_plate_centre"
    assert math.isclose(viewer_payload["point_spacing_mm"], DISPLAY_POINT_SPACING_MM, rel_tol=0.0, abs_tol=1e-12)
    assert viewer_payload["results"][0]["original_points"] == [[0.0, 0.0], [1.0, 1.0]]

    grid_spread_points = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.1, 0.1)]
    grid_spread_result = optimize_path(
        grid_spread_points,
        mode="grid_spread",
        grid_spacing=1.0,
        recent_percent=50.0,
        age_decay=0.9,
    )
    assert set(grid_spread_result[:4]) == {(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)}
    assert grid_spread_result[-1] == (0.1, 0.1)
    assert sorted(grid_spread_result) == sorted(grid_spread_points)
    assert grid_spread_points == [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.1, 0.1)]
    random_grid_result = optimize_path(
        grid_spread_points,
        mode="random_grid",
        grid_spacing=1.0,
        recent_percent=50.0,
        age_decay=0.9,
    )
    assert sorted(random_grid_result) == sorted(grid_spread_points)
    random_noise_result = optimize_path(
        grid_spread_points,
        mode="random_noise",
    )
    assert len(random_noise_result) == len(grid_spread_points)
    assert sorted(random_noise_result) == sorted(grid_spread_points)

    raster_points = [
        (0.0, 0.0), (1.0, 0.0), (2.0, 0.0),
        (0.0, 1.0), (1.0, 1.0), (2.0, 1.0),
        (0.0, 2.0), (1.0, 2.0), (2.0, 2.0),
    ]
    zigzag_result = optimize_path(raster_points, mode="raster_zigzag")
    assert sorted(zigzag_result) == sorted(raster_points)
    assert len(zigzag_result) == len(raster_points)
    assert zigzag_result[0][1] == zigzag_result[1][1]

    hilbert_result = optimize_path(raster_points, mode="hilbert_curve", hilbert_order=3)
    assert sorted(hilbert_result) == sorted(raster_points)
    assert len(hilbert_result) == len(raster_points)

    spot_result = optimize_path(raster_points, mode="spot_ordered", spot_skip=2)
    assert sorted(spot_result) == sorted(raster_points)
    assert len(spot_result) == len(raster_points)

    island_result = optimize_path(raster_points, mode="island_raster", grid_spacing=1.5)
    assert sorted(island_result) == sorted(raster_points)
    assert len(island_result) == len(raster_points)

    test_root = Path(__file__).resolve().parent
    input_path = test_root / "_selftest_sample_abs.txt"
    archive_input_path = test_root / "_selftest_archive.zip"
    output_path = test_root / "_selftest_output.txt"
    zip_path = test_root / "_selftest_output.zip"

    try:
        with open(input_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Header bleibt erhalten\n")
            handle.write("ABS 0.0 0.0\n")
            handle.write("Kommentarzeile\n")
            handle.write("ABS 1.0 0.0\n")
            handle.write("ABS 1.0 1.0\n")

        lines, points = load_points_from_file(str(input_path))
        assert len(lines) == 5
        assert points == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]

        preview_items, preview_errors = build_grid_preview_data([str(input_path)], max_points_per_file=2)
        assert not preview_errors
        assert len(preview_items) == 1
        assert preview_items[0].point_count == 3
        assert len(preview_items[0].sampled_points) <= 3

        with zipfile.ZipFile(archive_input_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("inner/01021.B99", "ABS 0.0 0.0\nABS 1.0 1.0\n")
            archive.writestr("inner/02031.B99", "ABS 2.0 2.0\nABS 3.0 3.0\n")
            archive.writestr("inner/00021.B99", "ABS 9.0 9.0\n")
            archive.writestr("figure files/37091.B99", "ABS 0.0 0.0\nABS 2.0 2.0\n")
            archive.writestr("inner/readme.txt", "ignored\n")

        input_sources, source_errors = build_input_sources(
            [str(archive_input_path)],
            zip_entry_types=("infill",),
            zip_support_end_layer=0,
        )
        assert not source_errors
        assert len(input_sources) == 1
        assert input_sources[0].archive_member == "inner/01021.B99"
        assert input_sources[0].output_name == "inner/01021.B99"
        dual_sources, dual_errors = build_input_sources(
            [str(archive_input_path)],
            zip_entry_types=("infill", "boundary"),
            zip_support_end_layer=0,
        )
        assert not dual_errors
        assert [source.archive_member for source in dual_sources] == ["inner/01021.B99", "inner/02031.B99"]
        assert build_output_name_for_source(input_sources[0], "stochastic_grid_dispersion") == (
            "inner/01021_optimised_stochastic_grid_dispersion.B99"
        )
        zip_lines, zip_points = load_points_from_input_source(input_sources[0])
        assert len(zip_lines) == 2
        assert zip_points == [(0.0, 0.0), (1.0, 1.0)]

        _, _, _, worker_result = process_file_in_subprocess(
            1,
            1,
            str(input_path),
            W1_DEFAULT,
            W2_DEFAULT,
            MEMORY_DEFAULT,
            "basic",
            None,
        )
        assert worker_result.output_name == "_selftest_sample_abs_optimised_local_greedy.txt"

        save_points(lines, points, str(output_path))
        assert output_path.is_file()

        result = ProcessedFileResult(
            source_path=input_path,
            source_label=input_path.name,
            archive_member=None,
            output_name=input_path.name,
            original_lines=lines,
            original_points=points,
            optimized_points=points,
            original_stats=analyze_path(points),
            optimized_stats=analyze_path(points),
            output_text=build_output_text(lines, points),
            processing_seconds=0.123,
        )
        save_results_as_zip([result], str(zip_path))
        assert zip_path.is_file()

        with zipfile.ZipFile(zip_path, "r") as archive:
            assert input_path.name in archive.namelist()

        processed_zip_result = process_file(
            input_sources[0],
            w1=W1_DEFAULT,
            w2=W2_DEFAULT,
            memory=MEMORY_DEFAULT,
            mode="basic",
        )
        save_results_as_zip([processed_zip_result], str(zip_path))

        with zipfile.ZipFile(zip_path, "r") as archive:
            assert set(archive.namelist()) == {
                "inner/01021_optimised_local_greedy.B99",
                "inner/02031.B99",
                "inner/00021.B99",
                "figure files/37091.B99",
                "inner/readme.txt",
            }
            optimized_member = archive.read("inner/01021_optimised_local_greedy.B99").decode("utf-8")
            untouched_member = archive.read("inner/02031.B99").decode("utf-8")
            assert optimized_member == processed_zip_result.output_text
            assert untouched_member == "ABS 2.0 2.0\nABS 3.0 3.0\n"
        combo_input_sources, combo_source_errors = build_input_sources(
            [str(archive_input_path)],
            zip_entry_types=("combo",),
            zip_support_end_layer=0,
        )
        assert not combo_source_errors
        assert len(combo_input_sources) == 1
        assert combo_input_sources[0].archive_member == "figure files/37091.B99"
        processed_combo_result = process_file(
            combo_input_sources[0],
            w1=W1_DEFAULT,
            w2=W2_DEFAULT,
            memory=MEMORY_DEFAULT,
            mode="random_noise",
        )
        assert processed_combo_result.output_name == "figure files/37091.B99"
        save_results_as_zip([processed_combo_result], str(zip_path))

        with zipfile.ZipFile(zip_path, "r") as archive:
            assert "figure files/37091.B99" in archive.namelist()
            assert not any(name.startswith("figure files/37091_optimised_") for name in archive.namelist())
        assert build_default_zip_name([worker_result], "local_greedy") == "_selftest_sample_abs_optimised_local_greedy.zip"
    finally:
        for path in (input_path, archive_input_path, output_path, zip_path):
            if path.exists():
                path.unlink()


def main() -> None:
    mp.freeze_support()
    allowed_modes = OPTIMIZATION_MODES + tuple(MODE_ALIASES)
    canonical_mode_help = ", ".join(
        f"{mode_id} ({MODE_SPECS[mode_id].label})"
        for mode_id in OPTIMIZATION_MODES
    )

    parser = argparse.ArgumentParser(
        description=(
            f"{APP_DISPLAY_NAME}: ABS-Dateien auswaehlen, Reihenfolge optimieren, "
            "wissenschaftlich beschreiben und im interaktiven Viewer untersuchen."
        )
    )
    parser.add_argument("--viewer-payload", help=argparse.SUPPRESS)
    parser.add_argument(
        "--viewer-backend",
        choices=("auto", "raster", "opengl"),
        default=os.environ.get("SEQUENCE_VIEWER_BACKEND", "raster"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("input_files", nargs="*", help="Optionale Eingabedateien. Ohne Angabe erscheint der Explorer.")
    parser.add_argument("--w1", type=float, default=W1_DEFAULT, help="Gewicht fuer die Distanz zum aktuellen Punkt.")
    parser.add_argument("--w2", type=float, default=W2_DEFAULT, help="Gewicht fuer die Distanz zu den letzten Punkten.")
    parser.add_argument("--memory", type=int, default=MEMORY_DEFAULT, help="Anzahl der gemerkten letzten Punkte.")
    parser.add_argument(
        "--mode",
        choices=allowed_modes,
        default="local_greedy",
        help=(
            "Optimierungsmodus. Kanonische Modi: "
            f"{canonical_mode_help}. Legacy-Aliase bleiben unterstuetzt."
        ),
    )
    parser.add_argument(
        "--grid-spacing",
        type=float,
        default=GRID_SPREAD_DEFAULT_SPACING,
        help="Virtueller Gitterabstand fuer die Grid-Dispersion-Modi.",
    )
    parser.add_argument(
        "--recent-percent",
        type=float,
        default=GRID_SPREAD_DEFAULT_RECENT_PERCENT,
        help="Groesse des beruecksichtigten Recent Sets fuer die Grid-Dispersion-Modi.",
    )
    parser.add_argument(
        "--age-decay",
        type=float,
        default=GRID_SPREAD_AGE_DECAY_DEFAULT,
        help="Altersabwaegung fuer die Grid-Dispersion-Modi. 1.0 = kein Abfall.",
    )
    parser.add_argument(
        "--ghost-delay",
        type=int,
        default=GHOST_BEAM_DEFAULT_DELAY,
        help="Punktbasierte Verzoegerung fuer den adaptierten Ghost-Beam-Modus innerhalb eines Streifens.",
    )
    parser.add_argument(
        "--forward-jump",
        type=int,
        default=INTERLACED_STRIPE_DEFAULT_FORWARD_JUMP,
        help="Vorwaertssprung innerhalb eines Streifenblocks fuer Interlaced Stripe Scanning.",
    )
    parser.add_argument(
        "--backward-jump",
        type=int,
        default=INTERLACED_STRIPE_DEFAULT_BACKWARD_JUMP,
        help="Rueckwaertssprung-Anteil fuer die Blockgroesse bei Interlaced Stripe Scanning.",
    )
    parser.add_argument(
        "--hilbert-order",
        type=int,
        default=HILBERT_ORDER_DEFAULT,
        help="Ordnung der Hilbert-Kurve (2–7). Nur fuer hilbert_curve-Modus.",
    )
    parser.add_argument(
        "--spot-skip",
        type=int,
        default=SPOT_SKIP_DEFAULT,
        help="Anzahl uebersprungener Punkte zwischen Paessen fuer spot_ordered-Modus (1–20).",
    )
    args = parser.parse_args()

    if args.viewer_payload:
        sys.exit(run_interactive_viewer(args.viewer_payload, preferred_backend=args.viewer_backend))

    try:
        run_self_tests()
    except AssertionError as exc:
        print(f"Self-Test fehlgeschlagen: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        app = ComparisonApp(
            args.input_files,
            w1=args.w1,
            w2=args.w2,
            memory=args.memory,
            mode=normalize_mode(args.mode),
            grid_spacing=args.grid_spacing,
            recent_percent=args.recent_percent,
            age_decay=args.age_decay,
            ghost_delay=args.ghost_delay,
            forward_jump=args.forward_jump,
            backward_jump=args.backward_jump,
            hilbert_order=args.hilbert_order,
            spot_skip=args.spot_skip,
        )
        app.mainloop()
    except tk.TclError as exc:
        show_fatal_error(
            "Die grafische Oberfläche konnte nicht gestartet werden.\n\n"
            "Bitte verwenden Sie eine Python-Installation mit funktionierendem Tkinter/Tcl-Tk.\n\n"
            f"Technischer Hinweis:\n{exc}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
