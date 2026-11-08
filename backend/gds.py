"""Small, bounded GDSII directory/filter helper for ViennaPS GDSGeometry.

ViennaPS 4.6.2 exposes layers and structure counts but no structure-selection
setter. This module retains the selected structure and its SREF/AREF dependency
closure, preserving all original records for ViennaPS to parse natively.
"""

from __future__ import annotations

import math
import struct
from bisect import bisect_left
from dataclasses import dataclass
from typing import Iterable


MAX_GDS_BYTES = 64 * 1024 * 1024
MAX_GDS_RECORDS = 2_000_000
# The installed native blur keeps one refined exposure grid per sigma and
# allocates all of them at once. This is the product limit on
# fineX * fineY * sigmaCount that validation and the worker enforce before the
# native rasterizer runs.
MAX_GDS_BLUR_CELLS = 8_000_000


def blur_exposure_cell_count(bounds_um: tuple[float, float, float, float], *,
                             grid_delta: float, exposure_delta: float,
                             grid_refine: int, sigma_count: int) -> int:
    """Return the native fine-grid cell count for one padded GDS layout.

    ``bounds_um`` is ``(min_x, min_y, max_x, max_y)`` *after* the native
    boundary padding the worker applies. The installed blur creates a refined
    exposure grid whose per-axis size is
    ``(ceil(width / exposure_delta) + 1) * deltaRatio`` with
    ``deltaRatio = ceil(gridRefine * exposure_delta / gridDelta)``, and keeps
    one such grid per sigma.
    """
    try:
        grid_delta = float(grid_delta)
        exposure_delta = float(exposure_delta)
        ratio = max(1, math.ceil(int(grid_refine) * exposure_delta / grid_delta))
    except (OverflowError, ValueError, ZeroDivisionError):
        return MAX_GDS_BLUR_CELLS + 1
    if (not math.isfinite(grid_delta) or grid_delta <= 0
            or not math.isfinite(exposure_delta) or exposure_delta <= 0):
        return MAX_GDS_BLUR_CELLS + 1
    fine = []
    for minimum, maximum in ((bounds_um[0], bounds_um[2]),
                             (bounds_um[1], bounds_um[3])):
        width = float(maximum) - float(minimum)
        if not math.isfinite(width) or width <= 0:
            return MAX_GDS_BLUR_CELLS + 1
        coarse_exact = width / exposure_delta
        if not math.isfinite(coarse_exact) or coarse_exact > MAX_GDS_BLUR_CELLS:
            return MAX_GDS_BLUR_CELLS + 1
        fine.append((math.ceil(coarse_exact) + 1) * ratio)
    return fine[0] * fine[1] * max(1, int(sigma_count))


class GdsFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    start: int
    end: int
    kind: int
    data_type: int
    payload_start: int
    payload_end: int


@dataclass(frozen=True)
class Structure:
    name: str
    start: int
    end: int
    references: frozenset[str]
    layers: frozenset[int]
    layer_datatypes: frozenset[tuple[int, int]] = frozenset()
    mask_layer_datatypes: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True)
class Library:
    data: bytes
    records: tuple[Record, ...]
    structures: tuple[Structure, ...]
    prefix_end: int
    suffix_start: int
    user_units_per_database_unit: float
    meters_per_database_unit: float

    def structure_names(self) -> list[str]:
        return [structure.name for structure in self.structures]

    def find_structure(self, name: str) -> Structure:
        for structure in self.structures:
            if structure.name == name:
                return structure
        raise GdsFormatError(f"GDS structure {name!r} was not found")

    def closure(self, name: str) -> set[str]:
        by_name = {structure.name: structure for structure in self.structures}
        result: set[str] = set()
        pending = [name]
        while pending:
            current = pending.pop()
            if current in result:
                continue
            structure = by_name.get(current)
            if structure is None:
                raise GdsFormatError(f"GDS structure reference {current!r} is missing")
            result.add(current)
            pending.extend(sorted(structure.references - result))
        return result

    def effective_layers(self, name: str) -> list[int]:
        closure = self.closure(name)
        return sorted({layer for structure in self.structures if structure.name in closure for layer in structure.layers})

    def effective_layer_datatypes(self, name: str) -> list[tuple[int, int]]:
        closure = self.closure(name)
        return sorted({pair for structure in self.structures if structure.name in closure
                       for pair in structure.layer_datatypes})

    def effective_mask_layer_datatypes(self, name: str) -> list[tuple[int, int]]:
        """Return layer/datatype pairs containing closed GDS BOUNDARY polygons."""
        closure = self.closure(name)
        return sorted({pair for structure in self.structures if structure.name in closure
                       for pair in structure.mask_layer_datatypes})

    def structure_xy_bounds(self, name: str) -> tuple[float, float, float, float] | None:
        """Return the direct-element XY bounds of one structure in database units.

        References are deliberately unsupported: SREF/AREF placement is applied
        by ViennaPS and re-implementing rotations, magnifications and arrays here
        would risk a wrong rejection. ``None`` means "not provable in Python", so
        callers must keep their native run-time check instead of guessing.
        """
        structure = self.find_structure(name)
        if structure.references:
            return None
        element_starts = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15, 0x2D}
        xy_kinds = {0x08, 0x09, 0x2D}  # BOUNDARY, PATH, BOX
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        current_kind: int | None = None
        for record in self.records:
            if record.start < structure.start or record.start >= structure.end:
                continue
            if record.kind in element_starts:
                current_kind = record.kind
                continue
            if record.kind == 0x11:
                current_kind = None
                continue
            if record.kind != 0x10 or current_kind not in xy_kinds:
                continue
            payload = self.data[record.payload_start:record.payload_end]
            if len(payload) < 8 or len(payload) % 8:
                return None
            coordinates = struct.unpack(">" + "i" * (len(payload) // 4), payload)
            for index in range(0, len(coordinates), 2):
                min_x = min(min_x, coordinates[index])
                max_x = max(max_x, coordinates[index])
                min_y = min(min_y, coordinates[index + 1])
                max_y = max(max_y, coordinates[index + 1])
        if min_x == float("inf") or max_x < min_x:
            return None
        return (min_x, min_y, max_x, max_y)

    def selected_layout_bounds_um(self, name: str, *, unit_scale: float = 1.0,
                                  position_um: Iterable[float] = (0.0, 0.0)
                                  ) -> tuple[float, float, float, float] | None:
        """Return the XY bounds ViennaPS will read for the selected structure.

        The bounds use the same scale and placement that ``select_and_transform``
        applies to the selected structure's coordinates. Unsupported references
        return ``None`` so validation never rejects a layout it cannot prove.
        """
        bounds = self.structure_xy_bounds(name)
        if bounds is None:
            return None
        position = tuple(float(value) for value in position_um)
        if len(position) != 2 or any(not math.isfinite(value) for value in position):
            return None
        scale = self.meters_per_database_unit * 1_000_000 * float(unit_scale)
        if not math.isfinite(scale) or scale <= 0:
            return None
        min_x, min_y, max_x, max_y = (value * scale for value in bounds)
        # select_and_transform snaps a nonzero requested position to a database
        # unit multiple. Keep the Python bounds conservative in that case, but
        # do not inflate an unshifted layout that exactly fills the domain.
        margin = 0.5 * scale if any(abs(value) > 0.0 for value in position) else 0.0
        return (min_x + position[0] - margin, min_y + position[1] - margin,
                max_x + position[0] + margin, max_y + position[1] + margin)

    def select_and_transform(self, name: str, *, unit_scale: float = 1.0,
                              position_um: Iterable[float] = (0.0, 0.0),
                              selected_layers: Iterable[int] | None = None,
                              selected_layer_datatypes: Iterable[tuple[int, int]] | None = None) -> tuple[bytes, dict[str, object]]:
        if not math.isfinite(unit_scale) or unit_scale <= 0 or unit_scale > 1_000_000:
            raise GdsFormatError("GDS unit scale must be finite, positive and at most 1,000,000")
        position = tuple(float(value) for value in position_um)
        if len(position) != 2 or any(not math.isfinite(value) or abs(value) > 1_000_000 for value in position):
            raise GdsFormatError("GDS offset must contain two finite coordinates within ±1,000,000 µm")
        closure = self.closure(name)
        needed = [structure for structure in self.structures if structure.name in closure]
        if not needed:
            raise GdsFormatError("GDS structure selection produced no structures")
        available_layers = set(self.effective_layers(name))
        layers = None if selected_layers is None else {int(value) for value in selected_layers}
        if layers is not None and (not layers or layers - available_layers):
            raise GdsFormatError("selected GDS layers must be a nonempty subset of the selected structure's layers")
        pairs = None if selected_layer_datatypes is None else {(int(layer), int(datatype)) for layer, datatype in selected_layer_datatypes}
        if pairs is not None and (not pairs or pairs - set(self.effective_layer_datatypes(name))):
            raise GdsFormatError("selected GDS layer/datatype pairs must exist in the selected structure")
        all_layers = {layer for structure in self.structures for layer in structure.layers}
        support_layer = next((value for value in range(32767, -1, -1) if value not in all_layers), None)
        if layers is not None and support_layer is None:
            raise GdsFormatError("no unused native GDS layer is available for isolated 3D structure selection")
        first_factor = self.user_units_per_database_unit * unit_scale
        if not math.isfinite(first_factor) or first_factor <= 0:
            raise GdsFormatError("GDS user-unit scale is invalid")
        database_offsets = [int(round(value / first_factor)) for value in position]
        actual_position = [offset * first_factor for offset in database_offsets]
        starts = [record.start for record in self.records]
        shifted_records: list[bytes] = []
        element_starts = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15, 0x2D}
        for item in self.structures:
            translate_xy = item.name == name
            keep_selected_geometry = item.name in closure
            index = bisect_left(starts, item.start)
            end_index = bisect_left(starts, item.end)
            element: list[Record] = []
            element_layer: int | None = None
            element_datatype = 0

            def emit(record: Record, *, support_geometry: bool = False) -> None:
                if record.kind == 0x10:
                    payload = self.data[record.payload_start:record.payload_end]
                    if len(payload) < 8 or len(payload) % 8:
                        raise GdsFormatError("GDS XY record must contain signed 32-bit coordinate pairs")
                    coords = list(struct.unpack(">" + "i" * (len(payload) // 4), payload))
                    for coordinate in range(0, len(coords), 2):
                        x = coords[coordinate] + (database_offsets[0] if translate_xy else 0)
                        y = coords[coordinate + 1] + (database_offsets[1] if translate_xy else 0)
                        if not (-2**31 <= x < 2**31 and -2**31 <= y < 2**31):
                            raise GdsFormatError("translated GDS XY coordinates exceed signed 32-bit range")
                        coords[coordinate] = x
                        coords[coordinate + 1] = y
                    shifted_records.append(_encode_record(record.kind, record.data_type,
                                                          struct.pack(">" + "i" * len(coords), *coords)))
                elif support_geometry and record.kind == 0x0D and support_layer is not None:
                    shifted_records.append(_encode_record(record.kind, record.data_type,
                                                          struct.pack(">h", support_layer)))
                elif record.kind == 0x0E:
                    # ViennaPS uses geometry by layer and ignores DATATYPE; zero it
                    # in the filtered copy after pair selection to avoid a warning per polygon.
                    shifted_records.append(_encode_record(record.kind, record.data_type, struct.pack(">h", 0)))
                else:
                    shifted_records.append(self.data[record.start:record.end])

            def flush_element() -> None:
                if not element:
                    return
                is_reference = element[0].kind in (0x0A, 0x0B)
                keep_requested_layer = (layers is None or is_reference or element_layer is None
                                        or (keep_selected_geometry and element_layer in layers))
                if pairs is not None and not is_reference and element_layer is not None:
                    keep_requested_layer = (keep_selected_geometry and (element_layer, element_datatype) in pairs)
                if keep_requested_layer:
                    for item_record in element:
                        emit(item_record)
                elif element_layer is not None and support_layer is not None:
                    # ViennaPS 4.6.2's GDSGeometry needs library-wide bounds to
                    # construct a level set. Preserve non-selected polygons on
                    # one unselected support layer; they size the native reader
                    # but can never enter a selected layer's material level set.
                    for item_record in element:
                        emit(item_record, support_geometry=True)
                element.clear()

            while index < end_index:
                record = self.records[index]
                index += 1
                if record.kind in element_starts:
                    flush_element()
                    element = [record]
                    element_layer = None
                    element_datatype = 0
                    continue
                if element:
                    element.append(record)
                    if record.kind == 0x0D and record.payload_end - record.payload_start >= 2:
                        element_layer = struct.unpack_from(">h", self.data, record.payload_start)[0]
                    elif record.kind == 0x0E and record.payload_end - record.payload_start >= 2:
                        element_datatype = struct.unpack_from(">h", self.data, record.payload_start)[0]
                    if record.kind == 0x11:
                        flush_element()
                else:
                    emit(record)
            flush_element()

        prefix = bytearray(self.data[:self.prefix_end])
        units = next((record for record in self.records if record.kind == 0x03), None)
        if units is None or units.end > self.prefix_end or units.payload_end - units.payload_start != 16:
            raise GdsFormatError("GDS library is missing its two-value UNITS record")
        new_units = _encode_record(0x03, units.data_type,
                                   _encode_real8(self.user_units_per_database_unit * unit_scale)
                                   + _encode_real8(self.meters_per_database_unit * unit_scale))
        prefix[units.start:units.end] = new_units
        selected_bytes = bytes(prefix) + b"".join(shifted_records) + self.data[self.suffix_start:]
        metadata = {
            "selectedStructure": name,
            "includedStructures": [item.name for item in needed],
            "layers": self.effective_layers(name),
            "selectedLayers": sorted(layers) if layers is not None else self.effective_layers(name),
            "selectedLayerDatatypes": sorted([list(pair) for pair in pairs]) if pairs is not None else self.effective_layer_datatypes(name),
            "supportLayer": support_layer if layers is not None else None,
            "supportStructures": [item.name for item in self.structures if item.name not in closure],
            "requestedPositionUm": list(position),
            "actualPositionUm": actual_position,
            "unitScale": unit_scale,
            "nativeUserUnitsPerDatabaseUnit": self.user_units_per_database_unit,
            "nativeMetersPerDatabaseUnit": self.meters_per_database_unit,
            "effectiveUserUnitsPerDatabaseUnit": self.user_units_per_database_unit * unit_scale,
        }
        return selected_bytes, metadata


def add_periodic_extent_support(data: bytes, structure_name: str, *, dimension: int,
                               x_extent_um: float, y_extent_um: float | None = None,
                               cross_section_y_bounds_um: tuple[float, float] | None = None,
                               grid_delta_um: float) -> tuple[bytes, dict[str, object]]:
    """Add an unmapped boundary polygon so ViennaPS reads the requested period.

    ViennaPS 4.6.2's GDSGeometry uses layout bounds as the periodic extent.
    This synthetic polygon occupies an unused GDS layer, so it fixes those
    bounds without changing any selected material level set.
    """
    values = [x_extent_um, grid_delta_um]
    if dimension == 3:
        if y_extent_um is None:
            raise GdsFormatError("3D periodic GDS support requires a y extent")
        values.append(y_extent_um)
    if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        raise GdsFormatError("periodic GDS extents and grid spacing must be finite and positive")
    if dimension not in (2, 3):
        raise GdsFormatError("periodic GDS support only accepts two or three dimensions")

    library = parse_gds(data)
    structure = library.find_structure(structure_name)
    used_layers = {layer for item in library.structures for layer in item.layers}
    support_layer = next((value for value in range(32767, -1, -1) if value not in used_layers), None)
    if support_layer is None:
        raise GdsFormatError("no unused GDS layer is available for periodic boundary support")

    meters_per_um = 1e-6
    um_per_database_unit = library.meters_per_database_unit / meters_per_um

    def to_dbu(value: float) -> int:
        coordinate = int(round(float(value) / um_per_database_unit))
        if not -2**31 <= coordinate < 2**31:
            raise GdsFormatError("periodic GDS boundary support exceeds signed 32-bit coordinate range")
        return coordinate

    x_min, x_max = -float(x_extent_um) / 2.0, float(x_extent_um) / 2.0
    if dimension == 3:
        assert y_extent_um is not None
        y_min, y_max = -float(y_extent_um) / 2.0, float(y_extent_um) / 2.0
    else:
        if cross_section_y_bounds_um is None:
            raise GdsFormatError("2D periodic GDS support requires the native cross-section bounds")
        cross_min, cross_max = (float(value) for value in cross_section_y_bounds_um)
        if not math.isfinite(cross_min) or not math.isfinite(cross_max) or cross_max <= cross_min:
            raise GdsFormatError("2D periodic GDS cross-section bounds are invalid")
        available = cross_max - cross_min
        inset = min(float(grid_delta_um) * 0.25, available * 0.25)
        y_min, y_max = cross_min + inset, cross_max - inset
        if y_max <= y_min:
            raise GdsFormatError("2D periodic GDS cross-section is too narrow for boundary support")

    xy = [(to_dbu(x_min), to_dbu(y_min)), (to_dbu(x_max), to_dbu(y_min)),
          (to_dbu(x_max), to_dbu(y_max)), (to_dbu(x_min), to_dbu(y_max)),
          (to_dbu(x_min), to_dbu(y_min))]
    if xy[0] == xy[1] or xy[1] == xy[2]:
        raise GdsFormatError("periodic GDS support polygon collapsed at native database-unit precision")
    xy_payload = struct.pack(">" + "i" * (2 * len(xy)), *(coord for point in xy for coord in point))

    def record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
        return _encode_record(kind, data_type, payload)

    support = (record(0x08, 0) + record(0x0D, 2, struct.pack(">h", support_layer))
               + record(0x0E, 2, struct.pack(">h", 0)) + record(0x10, 3, xy_payload)
               + record(0x11, 0))
    end_record = next((item for item in library.records if item.start == structure.end - 4), None)
    if end_record is None or end_record.kind != 0x07:
        raise GdsFormatError("selected GDS structure has no valid ENDSTR insertion point")
    supported = data[:end_record.start] + support + data[end_record.start:]
    reported = {"supportLayer": support_layer,
                "supportBoundsUm": [coordinate * um_per_database_unit
                                    for coordinate in (xy[0][0], xy[1][0], xy[0][1], xy[2][1])],
                "coordinateToleranceUm": um_per_database_unit,
                "supportPurpose": "align-native-periodic-gds-bounds-with-project-domain"}
    return supported, reported


def _decode_real8(value: bytes) -> float:
    if len(value) != 8:
        raise GdsFormatError("GDS REAL8 values must contain eight bytes")
    if value == b"\0" * 8:
        return 0.0
    sign = -1.0 if value[0] & 0x80 else 1.0
    exponent = (value[0] & 0x7F) - 64
    mantissa = int.from_bytes(value[1:], "big") / float(1 << 56)
    result = sign * mantissa * (16.0 ** exponent)
    if not math.isfinite(result):
        raise GdsFormatError("GDS REAL8 contains a non-finite value")
    return result


def _encode_real8(value: float) -> bytes:
    if not math.isfinite(value):
        raise GdsFormatError("GDS unit conversion produced a non-finite value")
    if value == 0:
        return b"\0" * 8
    sign = 0x80 if value < 0 else 0
    normalized = abs(value)
    exponent = 0
    while normalized < 1.0 / 16.0:
        normalized *= 16.0
        exponent -= 1
    while normalized >= 1.0:
        normalized /= 16.0
        exponent += 1
    biased = exponent + 64
    if not 0 <= biased <= 127:
        raise GdsFormatError("GDS unit conversion exceeds REAL8 exponent range")
    mantissa = int(normalized * (1 << 56))
    if mantissa >= 1 << 56:
        mantissa >>= 4
        biased += 1
    return bytes([sign | biased]) + mantissa.to_bytes(7, "big")


def _encode_record(kind: int, data_type: int, payload: bytes) -> bytes:
    length = len(payload) + 4
    if length > 0xFFFF:
        raise GdsFormatError("rewritten GDS record exceeds the 65,535-byte format limit")
    return struct.pack(">HBB", length, kind, data_type) + payload


def parse_gds(data: bytes) -> Library:
    if not isinstance(data, bytes) or len(data) < 64 or len(data) > MAX_GDS_BYTES:
        raise GdsFormatError(f"GDS file size must be between 64 bytes and {MAX_GDS_BYTES} bytes")
    records: list[Record] = []
    cursor = 0
    while cursor < len(data):
        if len(records) >= MAX_GDS_RECORDS or cursor + 4 > len(data):
            raise GdsFormatError("GDS record table is truncated or exceeds the record limit")
        length = struct.unpack_from(">H", data, cursor)[0]
        if length < 4 or length % 2 or cursor + length > len(data):
            raise GdsFormatError(f"invalid GDS record length at byte {cursor}")
        records.append(Record(cursor, cursor + length, data[cursor + 2], data[cursor + 3], cursor + 4, cursor + length))
        cursor += length
    if cursor != len(data) or not records or records[0].kind != 0x00 or records[-1].kind != 0x04:
        raise GdsFormatError("GDS file must have a HEADER first and ENDLIB last")
    units_record = next((item for item in records if item.kind == 0x03), None)
    if units_record is None or units_record.payload_end - units_record.payload_start != 16:
        raise GdsFormatError("GDS file must include a valid two-value UNITS record")
    units = data[units_record.payload_start:units_record.payload_end]
    user_units = _decode_real8(units[:8])
    meters = _decode_real8(units[8:])
    if user_units <= 0 or meters <= 0:
        raise GdsFormatError("GDS UNITS values must be positive")

    structures: list[Structure] = []
    active_start: int | None = None
    active_name: str | None = None
    active_refs: set[str] = set()
    active_layers: set[int] = set()
    active_layer_datatypes: set[tuple[int, int]] = set()
    active_mask_layer_datatypes: set[tuple[int, int]] = set()
    current_layer: int | None = None
    current_datatype = 0
    active_reference = False
    current_element_kind: int | None = None
    current_boundary_closed = False
    prefix_end: int | None = None
    suffix_start: int | None = None
    for record in records:
        if record.kind == 0x05:
            if active_start is not None:
                raise GdsFormatError("nested GDS structure definitions are invalid")
            if prefix_end is None:
                prefix_end = record.start
            active_start = record.start
            active_name = None
            active_refs = set()
            active_layers = set()
            active_layer_datatypes = set()
            active_mask_layer_datatypes = set()
            current_layer = None
            current_datatype = 0
            active_reference = False
            current_element_kind = None
            current_boundary_closed = False
        elif active_start is not None:
            if record.kind == 0x06:
                if active_name is not None:
                    raise GdsFormatError("GDS structure has duplicate STRNAME records")
                active_name = data[record.payload_start:record.payload_end].rstrip(b"\0").decode("ascii", errors="strict")
                if not active_name or len(active_name) > 512:
                    raise GdsFormatError("GDS structure name is empty or too long")
            elif record.kind == 0x0A or record.kind == 0x0B:
                active_reference = True
            elif record.kind == 0x12 and active_reference:
                reference = data[record.payload_start:record.payload_end].rstrip(b"\0").decode("ascii", errors="strict")
                if reference:
                    active_refs.add(reference)
            elif record.kind in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15, 0x2D):
                current_element_kind = record.kind
                current_layer = None
                current_datatype = 0
                current_boundary_closed = False
            elif record.kind == 0x0D and record.payload_end - record.payload_start >= 2:
                current_layer = struct.unpack_from(">h", data, record.payload_start)[0]
                active_layers.add(current_layer)
            elif record.kind == 0x0E and record.payload_end - record.payload_start >= 2:
                current_datatype = struct.unpack_from(">h", data, record.payload_start)[0]
            elif record.kind == 0x10 and current_element_kind == 0x08:
                payload = data[record.payload_start:record.payload_end]
                if len(payload) >= 32 and len(payload) % 8 == 0:
                    coordinates = struct.unpack(">" + "i" * (len(payload) // 4), payload)
                    current_boundary_closed = coordinates[:2] == coordinates[-2:]
            elif record.kind == 0x11:
                if current_layer is not None:
                    active_layer_datatypes.add((current_layer, current_datatype))
                    if current_element_kind == 0x08 and current_boundary_closed:
                        active_mask_layer_datatypes.add((current_layer, current_datatype))
                current_layer = None
                current_datatype = 0
                active_reference = False
                current_element_kind = None
                current_boundary_closed = False
            elif record.kind == 0x07:
                if active_name is None:
                    raise GdsFormatError("GDS structure is missing STRNAME")
                structures.append(Structure(active_name, active_start, record.end,
                                            frozenset(active_refs), frozenset(active_layers),
                                            frozenset(active_layer_datatypes),
                                            frozenset(active_mask_layer_datatypes)))
                active_start = None
                active_name = None
                active_reference = False
                suffix_start = record.end
    if active_start is not None or prefix_end is None or suffix_start is None or not structures:
        raise GdsFormatError("GDS file has an unterminated or missing structure")
    names = [item.name for item in structures]
    if len(names) != len(set(names)):
        raise GdsFormatError("GDS structure names must be unique")
    # Validate the entire dependency graph now, before any native reader sees it.
    by_name = {item.name: item for item in structures}
    for item in structures:
        missing = item.references - by_name.keys()
        if missing:
            raise GdsFormatError(f"GDS structure {item.name!r} references missing cell {sorted(missing)[0]!r}")
    return Library(data, tuple(records), tuple(structures), prefix_end, suffix_start, user_units, meters)


def inspect_gds(data: bytes) -> dict[str, object]:
    library = parse_gds(data)
    return {
        "structures": [{"name": item.name, "layers": sorted(item.layers),
                        "layerDatatypes": [list(pair) for pair in sorted(item.layer_datatypes)],
                        "maskLayerDatatypes": [list(pair) for pair in sorted(item.mask_layer_datatypes)],
                        "references": sorted(item.references)}
                       for item in library.structures],
        "units": {"userUnitsPerDatabaseUnit": library.user_units_per_database_unit,
                  "metersPerDatabaseUnit": library.meters_per_database_unit,
                  "micrometersPerNativeUserUnit": library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000,
                  "nativeOutputAssumption": "ViennaPS GDSGeometry coordinates are reported in the Workbench micrometer unit"},
        "fileSizeBytes": len(data),
    }
