import argparse
import json
import math
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cadquery as cq
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN, TopAbs_ON


MM_TO_ABS_SCALE = 1.0 / 60.0
INFILL_TYPE_DIGIT = 2
BOUNDARY_TYPE_DIGIT = 3
DEFAULT_POINT_SPACING_MM = 0.1
DEFAULT_LAYER_HEIGHT_MM = 0.1
DEFAULT_SUPPORT_LAYER_COUNT = 0
MIN_CURVE_SEGMENT_MM = 0.02
MAX_CURVE_SEGMENT_MM = 0.25

Point2D = Tuple[float, float]


@dataclass(frozen=True)
class SliceLayerResult:
    layer_index: int
    z_height_mm: float
    boundary_points_mm: List[Point2D]
    infill_points_mm: List[Point2D]


@dataclass(frozen=True)
class SupportFootprint:
    boundary_points_mm: List[Point2D]
    rings_mm: List[List[Point2D]]


def _round_key(point: Point2D, digits: int = 9) -> Tuple[float, float]:
    return (round(float(point[0]), digits), round(float(point[1]), digits))


def _points_are_close(a: Point2D, b: Point2D, tolerance: float = 1e-9) -> bool:
    return math.isclose(a[0], b[0], abs_tol=tolerance) and math.isclose(a[1], b[1], abs_tol=tolerance)


def mm_point_to_abs(point_mm: Point2D) -> Point2D:
    return (point_mm[0] * MM_TO_ABS_SCALE, point_mm[1] * MM_TO_ABS_SCALE)


def format_abs_lines(points_mm: Sequence[Point2D]) -> List[str]:
    return [f"ABS {x_abs:.17g} {y_abs:.17g}" for x_abs, y_abs in (mm_point_to_abs(point) for point in points_mm)]


def _edge_points_xy(edge: object, target_step_mm: float) -> List[Point2D]:
    length_mm = max(float(edge.Length()), 0.0)
    sample_count = max(2, int(math.ceil(length_mm / max(target_step_mm, 1e-9))) + 1)
    sampled_points, _ = edge.sample(sample_count)
    edge_points: List[Point2D] = []
    for vector in sampled_points:
        x_pos, y_pos, _ = vector.toTuple()
        point = (float(x_pos), float(y_pos))
        if edge_points and _points_are_close(edge_points[-1], point):
            continue
        edge_points.append(point)
    return edge_points


def sample_wire_points_xy(wire: object, target_step_mm: float, closed: bool) -> List[Point2D]:
    sampled: List[Point2D] = []
    for edge in wire.Edges():
        edge_points = _edge_points_xy(edge, target_step_mm)
        if sampled and edge_points and _points_are_close(sampled[-1], edge_points[0]):
            edge_points = edge_points[1:]
        sampled.extend(edge_points)

    deduped: List[Point2D] = []
    for point in sampled:
        if deduped and _points_are_close(deduped[-1], point):
            continue
        deduped.append(point)

    if not deduped:
        return []

    if closed:
        if not _points_are_close(deduped[0], deduped[-1]):
            deduped.append(deduped[0])
    elif len(deduped) > 1 and _points_are_close(deduped[0], deduped[-1]):
        deduped.pop()

    return deduped


def _wire_polygon_step_mm(point_spacing_mm: float) -> float:
    return min(MAX_CURVE_SEGMENT_MM, max(MIN_CURVE_SEGMENT_MM, point_spacing_mm * 0.5))


def _swap_xy_points(points: Sequence[Point2D]) -> List[Point2D]:
    return [(float(y_value), float(x_value)) for x_value, y_value in points]


def _scan_line_intersections(ring: Sequence[Point2D], y_value: float) -> List[float]:
    intersections: List[float] = []
    if len(ring) < 2:
        return intersections

    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        if math.isclose(y1, y2, abs_tol=1e-12):
            continue
        if (y1 <= y_value < y2) or (y2 <= y_value < y1):
            ratio = (y_value - y1) / (y2 - y1)
            intersections.append(x1 + ratio * (x2 - x1))
    intersections.sort()

    normalized: List[float] = []
    for value in intersections:
        if normalized and math.isclose(normalized[-1], value, abs_tol=1e-9):
            continue
        normalized.append(value)
    return normalized


def _normalize_sorted_values(values: Iterable[float], tolerance: float = 1e-9) -> List[float]:
    normalized: List[float] = []
    for value in sorted(float(entry) for entry in values):
        if normalized and math.isclose(normalized[-1], value, abs_tol=tolerance):
            continue
        normalized.append(value)
    return normalized


def _point_on_segment(point: Point2D, segment_start: Point2D, segment_end: Point2D, tolerance: float = 1e-9) -> bool:
    px, py = point
    x1, y1 = segment_start
    x2, y2 = segment_end
    if (
        px < min(x1, x2) - tolerance
        or px > max(x1, x2) + tolerance
        or py < min(y1, y2) - tolerance
        or py > max(y1, y2) + tolerance
    ):
        return False
    cross_product = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if not math.isclose(cross_product, 0.0, abs_tol=tolerance):
        return False
    return True


def _point_in_ring(point: Point2D, ring: Sequence[Point2D]) -> bool:
    if len(ring) < 4:
        return False

    px, py = point
    inside = False
    for index in range(len(ring) - 1):
        start = ring[index]
        end = ring[index + 1]
        if _point_on_segment(point, start, end):
            return True

        x1, y1 = start
        x2, y2 = end
        if (y1 > py) != (y2 > py):
            x_at_py = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if math.isclose(x_at_py, px, abs_tol=1e-9):
                return True
            if x_at_py > px:
                inside = not inside
    return inside


def _point_in_rings_odd_even(point: Point2D, rings: Sequence[Sequence[Point2D]]) -> bool:
    inside_hits = 0
    for ring in rings:
        if _point_in_ring(point, ring):
            inside_hits += 1
    return bool(inside_hits % 2)


def _interval_points(interval_start: float, interval_end: float, point_spacing_mm: float) -> List[float]:
    if interval_end <= interval_start + 1e-9:
        return []

    width = interval_end - interval_start
    if width <= point_spacing_mm:
        return [interval_start + width * 0.5]

    points: List[float] = []
    current = interval_start + point_spacing_mm * 0.5
    while current < interval_end - point_spacing_mm * 0.25:
        points.append(current)
        current += point_spacing_mm

    if not points:
        points.append(interval_start + width * 0.5)
    return points


def _axis_scan_values(axis_min: float, axis_max: float, point_spacing_mm: float) -> List[float]:
    axis_min = float(axis_min)
    axis_max = float(axis_max)
    width = axis_max - axis_min
    if width <= 1e-9:
        return [axis_min]
    if width <= point_spacing_mm:
        return [axis_min + width * 0.5]

    values: List[float] = []
    current = axis_min + point_spacing_mm * 0.5
    while current < axis_max - point_spacing_mm * 0.25 + 1e-9:
        values.append(current)
        current += point_spacing_mm
    if not values:
        values.append(axis_min + width * 0.5)
    return values


def collect_face_rings(face: object, point_spacing_mm: float) -> List[List[Point2D]]:
    polygon_step_mm = _wire_polygon_step_mm(point_spacing_mm)
    rings = [sample_wire_points_xy(face.outerWire(), polygon_step_mm, closed=True)]
    rings.extend(sample_wire_points_xy(inner_wire, polygon_step_mm, closed=True) for inner_wire in face.innerWires())
    return [ring for ring in rings if len(ring) >= 4]


def generate_infill_points_for_rings(
    rings: Sequence[Sequence[Point2D]],
    point_spacing_mm: float,
    orientation: str = "horizontal",
) -> List[Point2D]:
    safe_rings = [list(ring) for ring in rings if len(ring) >= 4]
    if not safe_rings:
        return []

    if orientation not in {"horizontal", "vertical"}:
        raise ValueError(f"Unbekannte Hedge-Orientierung: {orientation}")

    working_rings = (
        [_swap_xy_points(ring) for ring in safe_rings]
        if orientation == "vertical"
        else [list(ring) for ring in safe_rings]
    )
    all_points = [point for ring in working_rings for point in ring]
    min_y = min(point[1] for point in all_points)
    max_y = max(point[1] for point in all_points)
    y_value = min_y + point_spacing_mm * 0.5
    row_index = 0
    infill_points: List[Point2D] = []

    while y_value < max_y - point_spacing_mm * 0.25 + 1e-9:
        intersections = _normalize_sorted_values(
            intersection
            for ring in working_rings
            for intersection in _scan_line_intersections(ring, y_value)
        )

        row_points: List[Point2D] = []
        for start_x, end_x in zip(intersections, intersections[1:]):
            if end_x <= start_x + 1e-9:
                continue
            midpoint = (start_x + end_x) * 0.5
            if not _point_in_rings_odd_even((midpoint, y_value), working_rings):
                continue
            row_points.extend((x_value, y_value) for x_value in _interval_points(start_x, end_x, point_spacing_mm))

        deduped_row: List[Point2D] = []
        for point in row_points:
            if deduped_row and _points_are_close(deduped_row[-1], point):
                continue
            deduped_row.append(point)

        if deduped_row:
            if row_index % 2 == 1:
                deduped_row.reverse()
            infill_points.extend(deduped_row)
            row_index += 1

        y_value += point_spacing_mm

    if orientation == "vertical":
        return _swap_xy_points(infill_points)
    return infill_points


def generate_infill_points_for_face(face: object, point_spacing_mm: float) -> List[Point2D]:
    return generate_infill_points_for_rings(collect_face_rings(face, point_spacing_mm), point_spacing_mm)


def _face_bounds_record(face: object) -> Tuple[object, float, float, float, float]:
    bounds = face.BoundingBox()
    return (face, float(bounds.xmin), float(bounds.xmax), float(bounds.ymin), float(bounds.ymax))


def _point_is_inside_any_face(
    point: Point2D,
    z_height_mm: float,
    face_records: Sequence[Tuple[object, float, float, float, float]],
    classifier: BRepClass_FaceClassifier,
) -> bool:
    x_pos, y_pos = point
    for face, min_x, max_x, min_y, max_y in face_records:
        if x_pos < min_x - 1e-9 or x_pos > max_x + 1e-9 or y_pos < min_y - 1e-9 or y_pos > max_y + 1e-9:
            continue
        classifier.Perform(face.wrapped, gp_Pnt(float(x_pos), float(y_pos), float(z_height_mm)), 1e-7, True)
        if classifier.State() in (TopAbs_IN, TopAbs_ON):
            return True
    return False


def generate_infill_points_for_faces(
    faces: Sequence[object],
    point_spacing_mm: float,
    z_height_mm: float,
    orientation: str = "horizontal",
    rings: Optional[Sequence[Sequence[Point2D]]] = None,
) -> List[Point2D]:
    safe_faces = list(faces)
    if not safe_faces:
        return []
    if orientation not in {"horizontal", "vertical"}:
        raise ValueError(f"Unbekannte Hedge-Orientierung: {orientation}")

    face_records = [_face_bounds_record(face) for face in safe_faces]
    classifier = BRepClass_FaceClassifier()

    def to_xy(working_point: Point2D) -> Point2D:
        u_value, v_value = working_point
        return (
            (u_value, v_value)
            if orientation == "horizontal"
            else (v_value, u_value)
        )

    safe_rings = [list(ring) for ring in rings or () if len(ring) >= 4]
    if safe_rings:
        working_rings = (
            [_swap_xy_points(ring) for ring in safe_rings]
            if orientation == "vertical"
            else [list(ring) for ring in safe_rings]
        )
        all_ring_points = [point for ring in working_rings for point in ring]
        min_v = min(point[1] for point in all_ring_points)
        max_v = max(point[1] for point in all_ring_points)
        infill_points: List[Point2D] = []
        filled_row_index = 0

        for v_value in _axis_scan_values(min_v, max_v, point_spacing_mm):
            intersections = _normalize_sorted_values(
                intersection
                for ring in working_rings
                for intersection in _scan_line_intersections(ring, v_value)
            )
            row_segments: List[List[Point2D]] = []
            for start_u, end_u in zip(intersections, intersections[1:]):
                if end_u <= start_u + 1e-9:
                    continue
                midpoint = to_xy(((start_u + end_u) * 0.5, v_value))
                if not _point_is_inside_any_face(midpoint, z_height_mm, face_records, classifier):
                    continue
                segment = [to_xy((u_value, v_value)) for u_value in _interval_points(start_u, end_u, point_spacing_mm)]
                if segment:
                    row_segments.append(segment)

            if not row_segments:
                continue

            if filled_row_index % 2 == 1:
                row_segments = [list(reversed(segment)) for segment in reversed(row_segments)]
            for segment in row_segments:
                infill_points.extend(segment)
            filled_row_index += 1

        if infill_points:
            return infill_points

    min_x = min(record[1] for record in face_records)
    max_x = max(record[2] for record in face_records)
    min_y = min(record[3] for record in face_records)
    max_y = max(record[4] for record in face_records)
    min_u, max_u, min_v, max_v = (
        (min_x, max_x, min_y, max_y)
        if orientation == "horizontal"
        else (min_y, max_y, min_x, max_x)
    )
    u_values = _axis_scan_values(min_u, max_u, point_spacing_mm)
    v_values = _axis_scan_values(min_v, max_v, point_spacing_mm)
    infill_points: List[Point2D] = []
    filled_row_index = 0

    for v_value in v_values:
        row_segments: List[List[Point2D]] = []
        current_segment: List[Point2D] = []
        for u_value in u_values:
            candidate = to_xy((u_value, v_value))
            if _point_is_inside_any_face(candidate, z_height_mm, face_records, classifier):
                current_segment.append(candidate)
            elif current_segment:
                row_segments.append(current_segment)
                current_segment = []
        if current_segment:
            row_segments.append(current_segment)

        if not row_segments:
            continue

        if filled_row_index % 2 == 1:
            row_segments = [list(reversed(segment)) for segment in reversed(row_segments)]
        for segment in row_segments:
            infill_points.extend(segment)
        filled_row_index += 1

    if not infill_points:
        for face, *_ in face_records:
            center = face.Center()
            center_x, center_y, center_z = center.toTuple()
            center_point = (float(center_x), float(center_y))
            if _point_is_inside_any_face(center_point, float(center_z), face_records, classifier):
                infill_points.append(center_point)

    return infill_points


def generate_boundary_points_for_rings(rings: Sequence[Sequence[Point2D]]) -> List[Point2D]:
    boundary_points: List[Point2D] = []
    for ring in rings:
        ring_points = list(ring[:-1] if len(ring) > 1 and _points_are_close(ring[0], ring[-1]) else ring)
        if not ring_points:
            continue
        if boundary_points and _points_are_close(boundary_points[-1], ring_points[0]):
            ring_points = ring_points[1:]
        boundary_points.extend(ring_points)
    return boundary_points


def build_support_layers(
    support_footprint: SupportFootprint,
    support_layer_count: int,
    point_spacing_mm: float,
    layer_height_mm: float,
    z_min_mm: float,
) -> List[SliceLayerResult]:
    support_layers: List[SliceLayerResult] = []
    if support_layer_count <= 0:
        return support_layers

    for support_index in range(support_layer_count):
        orientation = "horizontal" if support_index % 2 == 0 else "vertical"
        infill_points_mm = generate_infill_points_for_rings(
            support_footprint.rings_mm,
            point_spacing_mm,
            orientation=orientation,
        )
        support_layers.append(
            SliceLayerResult(
                layer_index=support_index + 1,
                z_height_mm=z_min_mm + layer_height_mm * 0.5 + support_index * layer_height_mm,
                boundary_points_mm=list(support_footprint.boundary_points_mm),
                infill_points_mm=infill_points_mm,
            )
        )

    return support_layers


def slice_model(
    step_path: Path,
    point_spacing_mm: float,
    layer_height_mm: float,
) -> Tuple[List[SliceLayerResult], Dict[str, float], Optional[SupportFootprint]]:
    imported = cq.importers.importStep(str(step_path))
    solid = imported.val()
    bounds = solid.BoundingBox()
    min_z = float(bounds.zmin)
    max_z = float(bounds.zmax)
    slice_z = min_z + layer_height_mm * 0.5
    layer_index = 1
    layers: List[SliceLayerResult] = []
    support_footprint: Optional[SupportFootprint] = None

    while slice_z < max_z + 1e-9:
        section_compound = imported.section(height=slice_z).val()
        faces = list(section_compound.Faces())
        slice_rings: List[List[Point2D]] = []

        for face in faces:
            slice_rings.extend(collect_face_rings(face, point_spacing_mm))

        boundary_points = generate_boundary_points_for_rings(slice_rings)
        infill_points = generate_infill_points_for_faces(faces, point_spacing_mm, slice_z, rings=slice_rings)

        if boundary_points or infill_points:
            if support_footprint is None and slice_rings:
                support_footprint = SupportFootprint(
                    boundary_points_mm=list(boundary_points),
                    rings_mm=[list(ring) for ring in slice_rings],
                )
            layers.append(
                SliceLayerResult(
                    layer_index=layer_index,
                    z_height_mm=slice_z,
                    boundary_points_mm=boundary_points,
                    infill_points_mm=infill_points,
                )
            )

        layer_index += 1
        slice_z = min_z + layer_height_mm * 0.5 + (layer_index - 1) * layer_height_mm

    metadata = {
        "z_min_mm": min_z,
        "z_max_mm": max_z,
        "x_min_mm": float(bounds.xmin),
        "x_max_mm": float(bounds.xmax),
        "y_min_mm": float(bounds.ymin),
        "y_max_mm": float(bounds.ymax),
    }
    return layers, metadata, support_footprint


def apply_support_layers(
    sliced_layers: Sequence[SliceLayerResult],
    support_footprint: Optional[SupportFootprint],
    support_layer_count: int,
    point_spacing_mm: float,
    layer_height_mm: float,
    z_min_mm: float,
) -> Tuple[List[SliceLayerResult], Optional[int]]:
    if not sliced_layers:
        return [], None

    support_layers = build_support_layers(
        support_footprint=support_footprint or SupportFootprint(boundary_points_mm=[], rings_mm=[]),
        support_layer_count=support_layer_count,
        point_spacing_mm=point_spacing_mm,
        layer_height_mm=layer_height_mm,
        z_min_mm=z_min_mm,
    )
    layer_offset = max(0, int(support_layer_count))
    shifted_model_layers = [
        SliceLayerResult(
            layer_index=layer.layer_index + layer_offset,
            z_height_mm=layer.z_height_mm + layer_offset * layer_height_mm,
            boundary_points_mm=list(layer.boundary_points_mm),
            infill_points_mm=list(layer.infill_points_mm),
        )
        for layer in sliced_layers
    ]
    model_start_layer_index = shifted_model_layers[0].layer_index if shifted_model_layers else None
    return support_layers + shifted_model_layers, model_start_layer_index


def _member_name(layer_index: int, type_digit: int) -> str:
    return f"{layer_index:03d}0{type_digit}1.B99"


def write_layer_archive(
    layers: Sequence[SliceLayerResult],
    output_zip_path: Path,
    manifest_json_path: Path,
    source_step_path: Path,
    point_spacing_mm: float,
    layer_height_mm: float,
    support_layer_count: int,
    model_start_layer_index: Optional[int],
    bounds_metadata: Dict[str, float],
) -> Dict[str, object]:
    member_names: List[str] = []
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_json_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for layer in layers:
            if layer.infill_points_mm:
                infill_name = _member_name(layer.layer_index, INFILL_TYPE_DIGIT)
                archive.writestr(infill_name, "\n".join(format_abs_lines(layer.infill_points_mm)) + "\n")
                member_names.append(infill_name)
            if layer.boundary_points_mm:
                boundary_name = _member_name(layer.layer_index, BOUNDARY_TYPE_DIGIT)
                archive.writestr(boundary_name, "\n".join(format_abs_lines(layer.boundary_points_mm)) + "\n")
                member_names.append(boundary_name)

    manifest_data: Dict[str, object] = {
        "source_step_path": str(source_step_path),
        "generated_zip_path": str(output_zip_path),
        "member_names": member_names,
        "layer_count": len(layers),
        "z_min_mm": bounds_metadata["z_min_mm"],
        "z_max_mm": bounds_metadata["z_max_mm"],
        "xy_bounds_mm": {
            "min_x": bounds_metadata["x_min_mm"],
            "max_x": bounds_metadata["x_max_mm"],
            "min_y": bounds_metadata["y_min_mm"],
            "max_y": bounds_metadata["y_max_mm"],
        },
        "point_spacing_mm": point_spacing_mm,
        "layer_height_mm": layer_height_mm,
        "support_layer_count": int(support_layer_count),
        "model_start_layer_index": model_start_layer_index,
        "total_output_layer_count": len(layers),
        "layer_z_values_mm": [layer.z_height_mm for layer in layers],
    }
    manifest_json_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    return manifest_data


def generate_layer_archive(
    step_path: Path,
    output_zip_path: Path,
    manifest_json_path: Path,
    point_spacing_mm: float,
    layer_height_mm: float,
    support_layer_count: int = DEFAULT_SUPPORT_LAYER_COUNT,
) -> Dict[str, object]:
    if point_spacing_mm <= 0.0:
        raise ValueError("point_spacing_mm muss groesser als 0 sein.")
    if layer_height_mm <= 0.0:
        raise ValueError("layer_height_mm muss groesser als 0 sein.")
    if support_layer_count < 0:
        raise ValueError("support_layer_count muss groesser oder gleich 0 sein.")
    if step_path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError(f"Nicht unterstuetzte STEP-Datei: {step_path}")
    if not step_path.is_file():
        raise FileNotFoundError(f"STEP-Datei nicht gefunden: {step_path}")

    sliced_layers, bounds_metadata, support_footprint = slice_model(step_path, point_spacing_mm, layer_height_mm)
    if not sliced_layers:
        raise ValueError("Die STEP-Datei erzeugt bei den angegebenen Parametern keine gueltigen Layer.")
    if support_layer_count > 0 and support_footprint is None:
        raise ValueError("Es konnte kein gueltiger Footprint fuer die STEP-Stuetzschichten bestimmt werden.")

    output_layers, model_start_layer_index = apply_support_layers(
        sliced_layers=sliced_layers,
        support_footprint=support_footprint,
        support_layer_count=support_layer_count,
        point_spacing_mm=point_spacing_mm,
        layer_height_mm=layer_height_mm,
        z_min_mm=bounds_metadata["z_min_mm"],
    )

    return write_layer_archive(
        layers=output_layers,
        output_zip_path=output_zip_path,
        manifest_json_path=manifest_json_path,
        source_step_path=step_path,
        point_spacing_mm=point_spacing_mm,
        layer_height_mm=layer_height_mm,
        support_layer_count=support_layer_count,
        model_start_layer_index=model_start_layer_index,
        bounds_metadata=bounds_metadata,
    )


def run_selftest() -> None:
    def _read_abs_points_mm(archive: zipfile.ZipFile, member_name: str) -> List[Point2D]:
        points_mm: List[Point2D] = []
        for raw_line in archive.read(member_name).decode("utf-8").strip().splitlines():
            parts = raw_line.strip().split()
            if len(parts) != 3 or parts[0].upper() != "ABS":
                continue
            points_mm.append((float(parts[1]) / MM_TO_ABS_SCALE, float(parts[2]) / MM_TO_ABS_SCALE))
        return points_mm

    with tempfile.TemporaryDirectory(prefix="step_layer_selftest_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        step_path = temp_dir / "selftest_box.step"
        output_zip_path = temp_dir / "selftest_layers.zip"
        manifest_json_path = temp_dir / "selftest_manifest.json"

        box = cq.Workplane("XY").box(10.0, 10.0, 3.0, centered=(False, False, False))
        box.val().exportStep(str(step_path))

        manifest = generate_layer_archive(
            step_path=step_path,
            output_zip_path=output_zip_path,
            manifest_json_path=manifest_json_path,
            point_spacing_mm=1.0,
            layer_height_mm=1.0,
        )

        assert output_zip_path.is_file()
        assert manifest_json_path.is_file()
        assert manifest["layer_count"] == 3
        assert manifest["member_names"] == [
            "001021.B99",
            "001031.B99",
            "002021.B99",
            "002031.B99",
            "003021.B99",
            "003031.B99",
        ]
        assert manifest["layer_z_values_mm"] == [0.5, 1.5, 2.5]

        with zipfile.ZipFile(output_zip_path, "r") as archive:
            names = archive.namelist()
            assert "001021.B99" in names
            assert "001031.B99" in names
            assert all(not name.endswith("091.B99") for name in names)
            first_boundary = archive.read("001031.B99").decode("utf-8").strip().splitlines()[0]
            first_infill = archive.read("001021.B99").decode("utf-8").strip().splitlines()[0]
            assert first_boundary.startswith("ABS ")
            assert first_infill.startswith("ABS ")

        supported_zip_path = temp_dir / "selftest_layers_with_support.zip"
        supported_manifest_json_path = temp_dir / "selftest_manifest_with_support.json"
        supported_manifest = generate_layer_archive(
            step_path=step_path,
            output_zip_path=supported_zip_path,
            manifest_json_path=supported_manifest_json_path,
            point_spacing_mm=1.0,
            layer_height_mm=1.0,
            support_layer_count=2,
        )

        assert supported_manifest["support_layer_count"] == 2
        assert supported_manifest["model_start_layer_index"] == 3
        assert supported_manifest["total_output_layer_count"] == 5
        assert supported_manifest["layer_count"] == 5
        assert supported_manifest["layer_z_values_mm"] == [0.5, 1.5, 2.5, 3.5, 4.5]
        assert supported_manifest["member_names"] == [
            "001021.B99",
            "001031.B99",
            "002021.B99",
            "002031.B99",
            "003021.B99",
            "003031.B99",
            "004021.B99",
            "004031.B99",
            "005021.B99",
            "005031.B99",
        ]

        with zipfile.ZipFile(supported_zip_path, "r") as archive:
            supported_names = archive.namelist()
            assert "001021.B99" in supported_names
            assert "005031.B99" in supported_names
            support_boundary_lines = archive.read("001031.B99").decode("utf-8").strip().splitlines()
            first_model_boundary_lines = archive.read("003031.B99").decode("utf-8").strip().splitlines()
            assert support_boundary_lines == first_model_boundary_lines
            support_infill_layer_1 = archive.read("001021.B99").decode("utf-8").strip().splitlines()
            support_infill_layer_2 = archive.read("002021.B99").decode("utf-8").strip().splitlines()
            assert support_infill_layer_1 != support_infill_layer_2
            assert len(support_infill_layer_1) > 0
            assert len(support_infill_layer_2) > 0

        composite_rings = [
            [(0.0, 0.0), (12.0, 0.0), (12.0, 12.0), (0.0, 12.0), (0.0, 0.0)],
            [(4.0, 2.0), (8.0, 2.0), (8.0, 10.0), (4.0, 10.0), (4.0, 2.0)],
        ]
        composite_infill = generate_infill_points_for_rings(composite_rings, point_spacing_mm=1.0)
        assert composite_infill
        assert any(point[0] < 4.0 for point in composite_infill)
        assert any(point[0] > 8.0 for point in composite_infill)
        assert all(not (4.0 < point[0] < 8.0 and 2.0 < point[1] < 10.0) for point in composite_infill)

        holed_step_path = temp_dir / "selftest_holed_plate.step"
        holed_output_zip_path = temp_dir / "selftest_holed_layers.zip"
        holed_manifest_json_path = temp_dir / "selftest_holed_manifest.json"
        holed_plate = (
            cq.Workplane("XY")
            .rect(12.0, 12.0)
            .extrude(1.0)
            .faces(">Z")
            .workplane()
            .rect(4.0, 8.0)
            .cutThruAll()
        )
        holed_plate.val().exportStep(str(holed_step_path))

        holed_manifest = generate_layer_archive(
            step_path=holed_step_path,
            output_zip_path=holed_output_zip_path,
            manifest_json_path=holed_manifest_json_path,
            point_spacing_mm=1.0,
            layer_height_mm=1.0,
        )
        assert holed_manifest["layer_count"] == 1

        with zipfile.ZipFile(holed_output_zip_path, "r") as archive:
            holed_infill_points_mm = _read_abs_points_mm(archive, "001021.B99")
            assert holed_infill_points_mm
            assert all(not (-2.0 < x_pos < 2.0 and -4.0 < y_pos < 4.0) for x_pos, y_pos in holed_infill_points_mm)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate B99 layer ZIP archives from STEP files.")
    parser.add_argument("--step-file", type=Path, help="Input STEP/STP file.")
    parser.add_argument("--output-zip", type=Path, help="Target ZIP path for generated layer files.")
    parser.add_argument("--manifest-json", type=Path, help="Target JSON path for the generation manifest.")
    parser.add_argument(
        "--point-spacing-mm",
        type=float,
        default=DEFAULT_POINT_SPACING_MM,
        help="Spacing between generated points in millimetres.",
    )
    parser.add_argument(
        "--layer-height-mm",
        type=float,
        default=DEFAULT_LAYER_HEIGHT_MM,
        help="Distance between slice planes in millimetres.",
    )
    parser.add_argument(
        "--support-layer-count",
        type=int,
        default=DEFAULT_SUPPORT_LAYER_COUNT,
        help="Number of synthetic support layers generated before the model layers.",
    )
    parser.add_argument("--selftest", action="store_true", help="Run the built-in helper self-test and exit.")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
        return

    if args.step_file is None or args.output_zip is None or args.manifest_json is None:
        parser.error("--step-file, --output-zip und --manifest-json sind erforderlich.")

    manifest = generate_layer_archive(
        step_path=args.step_file,
        output_zip_path=args.output_zip,
        manifest_json_path=args.manifest_json,
        point_spacing_mm=float(args.point_spacing_mm),
        layer_height_mm=float(args.layer_height_mm),
        support_layer_count=int(args.support_layer_count),
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
