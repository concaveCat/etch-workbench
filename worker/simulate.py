"""One-run ViennaPS adapter. Run only as a bounded child of the API supervisor."""

from __future__ import annotations

import argparse
import copy
import ctypes
import ctypes.util
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from array import array
from importlib.metadata import version
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.rates import has_motion, is_geometry_step, uses_flux, signed_rates, signed_directional_rates, signed_directional_rate_sets
from backend.models import (normalized_geometry_v3, parse_project,
                            project_structure_hash, strict_boolean,
                            strict_finite_number, strict_integer)
from backend.runtime_limits import MAX_CELL_SET_CELLS, artifact_limit_bytes, artifact_limit_label
from backend.preflight import CELL_SET_JSON_BYTES, CELL_SET_VTU_BYTES_PER_CELL
from backend.validation import (GPU_MULTIPARTICLE_FLUX_STRATEGY,
                               GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY,
                               GPU_MULTIPARTICLE_NATIVE_STRATEGY,
                               GPU_MULTIPARTICLE_SPLIT_STRATEGY,
                               capability_verified, fin_construction_decision,
                               gpu_project_signature, slice_axis_bounds)
from backend.engine import multi_ion_gpu_error
from backend.gpu_devices import cuda_devices, resolve_gpu_device
from backend.gds import MAX_GDS_BLUR_CELLS, blur_exposure_cell_count
from process_models.v2.engines.viennaps import (
    configure_advection,
    configure_rays,
    material_handle,
    material_map,
    protected_material_names,
)
from worker.gds_mask import CONSTRUCTION as GDS_MASK_CONSTRUCTION
from worker.gds_mask import (NATIVE_CONSTRUCTION, NATIVE_REGRID_CONSTRUCTION,
                             apply_gds_blur, build_aligned_gds_layer,
                             build_aligned_gds_mask, native_layer_body,
                             planar_point_count, resolve_blur)
from worker.specialized import build_specialized_model
from worker.csv_process import build_csv_process
from worker.artifacts import (artifact_bytes_used, atomic_json, check_artifact_budget,
                              file_sha256, output_artifact)
from worker.engine_policy import (multi_ion_gpu_step, native_slice_position,
                                  requested_engine_for_step, require_multi_ion_flux_fields,
                                  require_multi_ion_gpu_policy, verify_worker_engine_evidence)
from worker.steps.cellset import run_cell_set_step
from worker.steps.context import RunContext
from worker.steps.geometry import run_geometry_step
from worker.steps.noop import run_no_op_step
from worker.steps.process import ProcessStepResult, run_process_step
MATERIAL_CATALOG = json.loads((ROOT / "docs" / "materials-catalog.json").read_text(encoding="utf-8"))
MATERIAL_ROWS = MATERIAL_CATALOG["materials"]
MATERIAL_NAMES = {int(row["id"]): row["name"] for row in MATERIAL_ROWS}
MATERIAL_IDS = {row["name"]: int(row["id"]) for row in MATERIAL_ROWS}
MATERIAL_COLORS = {int(row["id"]): row["color"] for row in MATERIAL_ROWS}
GPU_MULTIPARTICLE_SPLIT_SWEEPS = 4


def configure_native_materials(ps: Any, custom_rows: list[dict[str, Any]]) -> None:
    """Recreate the persisted 4.6.2 registry prefix in every isolated worker."""
    global MATERIAL_ROWS, MATERIAL_NAMES, MATERIAL_IDS, MATERIAL_COLORS
    rows = list(MATERIAL_CATALOG["materials"])
    builtin_names = {row["name"] for row in rows}
    builtin_ids = {int(row["id"]) for row in rows}
    registry = ps.MaterialRegistry.instance()
    for index, row in enumerate(custom_rows):
        name = row.get("name")
        if (row.get("customId") != index or row.get("id") != 177 + index or not isinstance(name, str)
                or name in builtin_names or row.get("id") in builtin_ids):
            raise RuntimeError("persisted custom material registry is not a stable, collision-free native ID prefix")
        material = registry.registerMaterial(name)
        if not material.isCustom() or int(material.customId()) != index or int(material.legacyId()) != int(row["id"]):
            raise RuntimeError(f"ViennaPS assigned unexpected native identity to custom material {name!r}")
        info = registry.getInfo(material)
        info.color_hex = int(str(row["color"]).lstrip("#"), 16)
        category_name = str(row.get("category", "Generic"))
        try:
            category = ps.MaterialCategory[category_name]
        except KeyError as exc:
            raise RuntimeError(f"custom material {name!r} has an unknown MaterialCategory {category_name!r}") from exc
        info.category = category
        if row.get("densityGcm3") is not None:
            info.density_gcm3 = float(row["densityGcm3"])
        info.conductive = bool(row.get("conductive", False))
        registry.setInfo(material, info)
        readback = registry.getInfo(material)
        if (int(readback.color_hex) != int(str(row["color"]).lstrip("#"), 16)
                or int(readback.category) != int(category)
                or bool(readback.conductive) != bool(row.get("conductive", False))
                or (row.get("densityGcm3") is not None and not math.isclose(float(readback.density_gcm3), float(row["densityGcm3"]), rel_tol=0, abs_tol=1e-12))):
            raise RuntimeError(f"ViennaPS did not retain custom MaterialInfo for {name!r}")
        rows.append({"name": name, "id": int(row["id"]), "color": row["color"], "category": category_name})
    MATERIAL_ROWS = rows
    MATERIAL_NAMES = {int(row["id"]): row["name"] for row in rows}
    MATERIAL_IDS = {row["name"]: int(row["id"]) for row in rows}
    MATERIAL_COLORS = {int(row["id"]): row["color"] for row in rows}


def _surface_vertical_bounds(points: list, *, is3d: bool) -> tuple[float, float]:
    axis = 2 if is3d else 1
    values = [float(point[axis]) for point in points]
    if not values:
        raise RuntimeError("DenseCellSet output requires the extracted native surface")
    if any(not math.isfinite(value) for value in values):
        raise RuntimeError("DenseCellSet surface bounds contain a non-finite coordinate")
    return max(values), min(values)


def _level_set_band_half_cells(domain: Any) -> int:
    """Return the native band half-width in grid cells, with a safety margin.

    ``DenseCellSet.fromLevelSets`` builds its grid from the stored level-set
    bounds, not from the extracted surface. A level set widened by
    ``viennals.Expand`` can therefore reach many cells beyond the surface
    (probe: width 20 at gridDelta 0.1 gives a 10-cell band). The stored width
    is the only bound API the Python bindings expose, so use it instead of a
    fixed surface margin. The artificial cell-set position plane adds a small
    default-width band, covered by the extra cells.
    """
    widest = 2
    for level_set in domain.getLevelSets():
        try:
            width = int(level_set.getLevelSetWidth())
        except (AttributeError, TypeError, ValueError):
            width = 2
        widest = max(widest, max(1, width))
    return math.ceil(widest / 2) + 2


def _cell_set_upper_bound(requested: dict[str, Any], *, height: float, top: float, bottom: float,
                          delta: float, domain: Any) -> int:
    """Upper-bound the native cell count before allocating the dense grid.

    The installed generator walks the actual stored level-set band (which can
    be much wider than the surface) up to the requested position. Domain cells
    are fixed laterally; the vertical band width comes from every stored level
    set, so an expanded band cannot under-bound the dense allocation.
    """
    x_extent = float(requested["domain"]["xExtent"])
    nx = max(1, math.ceil(x_extent / delta))
    if int(requested["dimension"]) == 3:
        ny = max(1, math.ceil(float(requested["domain"]["yExtent"]) / delta))
        lateral = (nx + 2) * (ny + 2)
    else:
        lateral = nx + 2
    band = _level_set_band_half_cells(domain)
    # ``Domain.generateCellSet(position, ...)`` treats ``position`` as the
    # absolute grid top, while the generic output path passes ``top + height``
    # to ``fromLevelSets``. Keep both interpretations inside the bound.
    top_bound = max(float(height), top + height, top + band * delta)
    bottom_bound = bottom - band * delta
    rows = math.ceil((top_bound - bottom_bound) / delta) + 2
    return lateral * max(rows, 2)


def _cell_set_field_row(label: str, values: list[float],
                        material_names: dict[int, str] | None = None) -> dict[str, Any]:
    if not values:
        raise RuntimeError(f"DenseCellSet scalar {label!r} has no values")
    if any(not math.isfinite(value) for value in values):
        raise RuntimeError(f"DenseCellSet scalar {label!r} contains a non-finite value")
    row: dict[str, Any] = {
        "count": len(values),
        "min": min(values),
        "max": max(values),
    }
    if label == "Material":
        counts: dict[int, int] = {}
        for value in values:
            counts[int(value)] = counts.get(int(value), 0) + 1
        row["distinctCount"] = len(counts)
        row["materialCounts"] = {str(key): counts[key] for key in sorted(counts)}
        names = material_names or MATERIAL_NAMES
        row["materialNames"] = {str(key): names.get(key, "unknown") for key in sorted(counts)}
    else:
        row["mean"] = sum(values) / len(values)
        row["nonZeroCount"] = sum(1 for value in values if value != 0.0)
        row["distinctCount"] = len(set(values))
        if label == "FillingFraction":
            row["partialCount"] = sum(1 for value in values if 1e-6 < value < 1.0 - 1e-6)
    return row


def write_cell_set_outputs(ps: Any, domain: Any, run_dir: Path, run_id: str,
                           step_id: str, requested: dict[str, Any], settings: dict[str, Any],
                           *, surface_top: float, surface_bottom: float,
                           is3d: bool) -> list[dict[str, Any]]:
    """Write the optional native DenseCellSet VTU and compact JSON summary.

    A fresh ``DenseCellSet`` is built directly from the domain's level sets.
    ``Domain.generateCellSet`` would store the grid on the Domain and a second
    call on the same Domain appends duplicate scalar arrays, so the generic
    output must not mutate the simulation Domain that is persisted as .vpsd.
    """
    dimension = int(requested["dimension"])
    axis = 2 if is3d else 1
    top = float(surface_top)
    bottom = float(surface_bottom)
    delta = float(domain.getGridDelta())
    height = float(settings["height"])
    if height + 1e-12 < delta:
        raise RuntimeError(
            f"cellSet.height {height:g} um is below one grid interval ({delta:g} um); "
            "the installed DenseCellSet generator would crash")
    cover_name = str(settings.get("coverMaterial") or "GAS")
    if cover_name == "GAS":
        cover = ps.Material.GAS
    else:
        cover = material_handle(ps, cover_name)
    format_ = str(settings.get("format") or "both")
    if format_ not in {"vtu", "json", "both"}:
        raise RuntimeError(f"unsupported cellSet.format {format_!r}")
    position = top + height
    cells_upper = _cell_set_upper_bound(requested, height=height, top=top, bottom=bottom,
                                        delta=delta, domain=domain)
    if cells_upper > MAX_CELL_SET_CELLS:
        raise RuntimeError(
            f"cell-set output needs at least {cells_upper:,} cells, above the "
            f"{MAX_CELL_SET_CELLS:,}-cell limit; reduce cellSet.height or coarsen gridDelta")
    estimate = 0
    if format_ in {"vtu", "both"}:
        estimate += cells_upper * CELL_SET_VTU_BYTES_PER_CELL + 8192
    if format_ in {"json", "both"}:
        estimate += CELL_SET_JSON_BYTES
    artifact_dir = run_dir / "artifacts"
    target_paths = []
    if format_ in {"vtu", "both"}:
        target_paths.append(artifact_dir / f"{step_id}-cell-set.vtu")
    if format_ in {"json", "both"}:
        target_paths.append(artifact_dir / f"{step_id}-cell-set.json")
    # A deferred snapshot is materialised from its .vpsd state; the artifact it
    # rewrites already counts in the run directory. Replace, do not double count.
    replaced = sum(path.stat().st_size for path in target_paths if path.is_file())
    cap = artifact_limit_bytes()
    used = artifact_bytes_used(run_dir) - replaced
    if used + estimate > cap:
        raise RuntimeError(
            f"cell-set output would need about {estimate / (1024 * 1024):.1f} MiB but only "
            f"{(cap - used) / (1024 * 1024):.1f} MiB remain under the "
            f"{artifact_limit_label()} cap; reduce cellSet.height, disable VTU, or raise the cap")
    periodic = bool(settings.get("periodic", False))
    periodic_boundary = ([periodic, periodic, False] if is3d else [periodic, False])
    cell_set = ps.DenseCellSet()
    cell_set.setCellSetPosition(True)
    cell_set.setCoverMaterial(int(cover))
    cell_set.fromLevelSets(domain.getLevelSets(), domain.getMaterialMap().getMaterialMap(), position)
    cell_set.setPeriodicBoundary(periodic_boundary)
    cells = int(cell_set.getNumberOfCells())
    if cells <= 0:
        raise RuntimeError("ViennaPS DenseCellSet generation produced no cells")
    if cells > cells_upper:
        raise RuntimeError(f"DenseCellSet produced {cells} cells, above the {cells_upper}-cell pre-write bound")
    labels = [str(label) for label in cell_set.getScalarDataLabels()]
    selected = list(settings.get("scalarLabels") or labels)
    missing = sorted(set(selected) - set(labels))
    if missing:
        raise RuntimeError("DenseCellSet is missing requested scalar label(s): " + ", ".join(missing))
    material_values = [float(value) for value in cell_set.getScalarData("Material")]
    if len(material_values) != cells:
        raise RuntimeError("DenseCellSet Material scalar length does not match its cell count")
    if "FillingFraction" in labels:
        filling = [float(value) for value in cell_set.getFillingFractions()]
        if len(filling) != cells:
            raise RuntimeError("DenseCellSet filling fraction length does not match its cell count")
    else:
        filling = []
    bounds = json.loads(json.dumps(cell_set.getBoundingBox(), default=float))
    material_names = {**MATERIAL_NAMES, int(ps.Material.GAS): "GAS"}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    association = "native DenseCellSet cell grid; separate from the returned surface mesh"
    if format_ in {"vtu", "both"}:
        path = artifact_dir / f"{step_id}-cell-set.vtu"
        cell_set.writeVTU(str(path))
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("ViennaPS DenseCellSet.writeVTU did not produce a nonempty VTU file")
        rows.append(output_artifact(
            path, run_dir, run_id, step_id, "cellSet", "VTU",
            association=association, dimension=dimension, gridDelta=delta,
            numberOfCells=cells, coverMaterial=cover_name, heightAboveSurface=height,
            periodic=periodic))
    if format_ in {"json", "both"}:
        payload = {
            "schemaVersion": 1,
            "source": "viennaps.DenseCellSet.fromLevelSets after setCellSetPosition(true) and setCoverMaterial",
            "nativeType": "DenseCellSet",
            "dimension": dimension,
            "association": association,
            "setup": {
                "heightAboveSurface": height,
                "surfaceTop": top,
                "surfaceBottom": bottom,
                "position": position,
                "verticalAxis": axis,
                "coverMaterial": cover_name,
                "coverMaterialId": int(cover),
                "isAboveSurface": True,
                "periodic": periodic,
                "periodicBoundary": periodic_boundary,
                "scalarLabels": selected,
                "nativeScalarLabels": labels,
            },
            "numberOfCells": cells,
            "numberOfNodes": len(cell_set.getNodes()),
            "grid": {
                "gridDelta": delta,
                "nodes": len(cell_set.getNodes()),
                "elements": len(cell_set.getElements()),
                "elementNodes": len(cell_set.getElement(0)),
                "nativeElementType": "tetra" if dimension == 2 else "hexahedron",
                "logicalCellShape": "quad" if dimension == 2 else "hexahedron",
                "bounds": bounds,
            },
            "fields": {
                label: _cell_set_field_row(
                    label,
                    material_values if label == "Material" else filling if label == "FillingFraction" else
                    [float(value) for value in cell_set.getScalarData(label)],
                    material_names,
                )
                for label in selected
            },
            "nativeUnit": None,
        }
        path = artifact_dir / f"{step_id}-cell-set.json"
        atomic_json(path, payload)
        rows.append(output_artifact(
            path, run_dir, run_id, step_id, "cellSetSummary", "JSON",
            association=association, dimension=dimension, gridDelta=delta,
            numberOfCells=cells, coverMaterial=cover_name, heightAboveSurface=height,
            periodic=periodic))
    return rows


def cuda_runtime_readback(expected_device: dict[str, Any]) -> dict[str, Any]:
    library = ctypes.util.find_library("cudart")
    if not library:
        raise RuntimeError("GPU_DISK was selected but the CUDA Runtime library cannot be loaded")
    runtime = ctypes.CDLL(library)

    def call(name: str, *args: Any) -> int:
        function = getattr(runtime, name)
        function.restype = ctypes.c_int
        return int(function(*args))

    count = ctypes.c_int()
    count_status = call("cudaGetDeviceCount", ctypes.byref(count))
    if count_status != 0 or count.value != 1:
        raise RuntimeError(f"selected CUDA UUID must be the only runtime-visible device; count status={count_status}, count={count.value}")
    set_status = call("cudaSetDevice", 0)
    current = ctypes.c_int(-1)
    current_status = call("cudaGetDevice", ctypes.byref(current))
    pci_bus = ctypes.create_string_buffer(32)
    pci_status = call("cudaDeviceGetPCIBusId", pci_bus, len(pci_bus), 0)
    observed_bus = pci_bus.value.decode("ascii", errors="replace") if pci_status == 0 else None
    synchronize_status = call("cudaDeviceSynchronize")

    def normalize_bus(value: object) -> str:
        parts = str(value or "").lower().split(":")
        if len(parts) != 3:
            return ""
        return f"{parts[0][-4:]}:{parts[1]}:{parts[2]}"

    if set_status != 0 or current_status != 0 or pci_status != 0 or synchronize_status != 0:
        raise RuntimeError(f"CUDA Runtime device readback failed: set={set_status}, current={current_status}, pci={pci_status}, synchronize={synchronize_status}")
    if normalize_bus(observed_bus) != normalize_bus(expected_device.get("pciBusId")):
        raise RuntimeError(f"CUDA Runtime selected PCI device {observed_bus}, expected {expected_device.get('pciBusId')} ({expected_device.get('uuid')})")
    return {"runtimeLibrary": library, "visibleDeviceCount": count.value,
            "logicalDevice": current.value, "pciBusId": observed_bus,
            "uuid": expected_device["uuid"], "name": expected_device["name"],
            "setDeviceStatus": set_status, "synchronizeStatus": synchronize_status,
            "readbackStatus": "verified"}


def reset_native_gpu_context(ps: Any, expected_device: dict[str, Any]) -> dict[str, Any]:
    """Release ViennaCore's process-global CUDA/OptiX context before another GPU step."""
    if not hasattr(ps, "gpu") or not hasattr(ps.gpu, "Context"):
        raise RuntimeError("GPU recipe needs ViennaCore's exposed Context lifecycle API")
    context = ps.gpu.Context.getContextFromRegistry(0)
    if context is None:
        return {"status": "already-absent", "logicalDeviceId": 0,
                "gpuUuid": expected_device["uuid"]}
    logical_device_id = int(context.deviceID)
    context.destroy()
    del context
    import gc
    gc.collect()
    remaining = [int(value) for value in ps.gpu.Context.getRegisteredDeviceIDs()]
    if logical_device_id in remaining:
        raise RuntimeError("ViennaCore retained its CUDA/OptiX context after destroy()")
    return {"status": "destroyed", "logicalDeviceId": logical_device_id,
            "gpuUuid": expected_device["uuid"], "registeredDevicesAfter": remaining}


def nvidia_process_readback(expected_uuid: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"processId": os.getpid(), "gpuUuid": expected_uuid, "processListed": False,
                "processListStatus": "unavailable", "queryError": f"{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"processId": os.getpid(), "gpuUuid": expected_uuid, "processListed": False,
                "processListStatus": "unavailable", "queryReturnCode": result.returncode,
                "queryError": result.stderr.strip()[:500]}
    rows = []
    for values in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
        if len(values) == 3:
            pid_text, uuid, memory = [value.strip() for value in values]
            if pid_text.isdigit():
                rows.append({"pid": int(pid_text), "gpuUuid": uuid,
                             "usedMemoryMiB": int(memory) if memory.isdigit() else None})
    own = [row for row in rows if row["pid"] == os.getpid()]
    matching = [row for row in own if row["gpuUuid"] == expected_uuid]
    if own and not matching:
        raise RuntimeError(f"nvidia-smi confirmed worker PID {os.getpid()} on an unexpected GPU; selected={expected_uuid}, observed={own}")
    if len(own) != len(matching):
        raise RuntimeError(f"nvidia-smi confirmed worker PID {os.getpid()} on more than the selected GPU; selected={expected_uuid}, observed={own}")
    if not matching:
        return {"processId": os.getpid(), "gpuUuid": None, "expectedGpuUuid": expected_uuid,
                "processListed": False,
                "processListStatus": "not-observed", "observedProcessCount": len(rows),
                "visibilityNote": "nvidia-smi did not enumerate this CUDA worker; CUDA Runtime PCI/UUID readback and the pinned ViennaPS GPU engine are recorded separately"}
    return {"processId": os.getpid(), "gpuUuid": expected_uuid,
            "usedMemoryMiB": matching[0]["usedMemoryMiB"], "processListed": True,
            "processListStatus": "verified", "observedProcessCount": len(rows)}


def nvidia_gpu_sample() -> dict[str, Any]:
    """One-shot GPU utilisation/clocks sample; never fails a run.

    The process list from `nvidia-smi --query-compute-apps` does not always
    enumerate the CUDA worker (WSL2), so utilisation is sampled separately to
    answer "was the GPU actually busy" without guessing.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,clocks.sm,pstate",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    devices = []
    for values in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
        if len(values) >= 4:
            utilization, memory, clock, pstate = (value.strip() for value in values[:4])
            devices.append({"utilizationPercent": int(utilization) if utilization.isdigit() else None,
                            "memoryUsedMiB": int(memory) if memory.isdigit() else None,
                            "clockSmMHz": int(clock) if clock.isdigit() else None,
                            "pstate": pstate})
    if result.returncode != 0 or not devices:
        return {"status": "unavailable", "returnCode": result.returncode,
                "error": (result.stderr or "").strip()[:200]}
    return {"status": "sampled", "devices": devices}


def event(run_dir: Path, data: dict[str, Any]) -> None:
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **data}
    with (run_dir / "events.ndjson").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


# Phase timings: the worker otherwise records only one aggregate
# process.apply() duration, which hides setup, snapshot and teardown cost.
# Recorded by default so every run is auditable; set
# VIENNAPS_WORKBENCH_PROFILE=0 to disable (adds ~10 perf_counter calls).
PROFILE_ENABLED = os.environ.get("VIENNAPS_WORKBENCH_PROFILE") != "0"
# Reuse the CUDA/OptiX context across consecutive GPU steps unless explicitly
# disabled. An in-process CUDA Runtime call (cuda_runtime_readback) between two
# GPU steps is what breaks the second OptiX acceleration-structure build
# (optixAccelBuild 7001 Invalid value); steps that are followed by another GPU
# step therefore skip that call and use nvidia-smi evidence instead.
GPU_CONTEXT_REUSE = os.environ.get("VIENNAPS_WORKBENCH_GPU_CONTEXT_REUSE") != "0"
# Step snapshots are materialised on first read (see backend/snapshots.py).
# The initial snapshot stays eager: the viewer needs it immediately.
LAZY_SNAPSHOTS = os.environ.get("VIENNAPS_WORKBENCH_LAZY_SNAPSHOTS") != "0"
PROFILE_PHASES: list[dict[str, Any]] = []


class phase:
    """Time a worker phase; records nothing unless profiling is enabled."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.started = time.perf_counter()

    def stop(self) -> None:
        if PROFILE_ENABLED:
            PROFILE_PHASES.append({"phase": self.name,
                                   "seconds": round(time.perf_counter() - self.started, 6)})

    def __enter__(self) -> "phase":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.stop()
        return False


def _data_scalars(data: Any) -> dict[str, list[float]]:
    result = {}
    for index in range(data.getScalarDataSize()):
        label = str(data.getScalarDataLabel(index))
        result[label] = [float(value) for value in data.getScalarData(index)]
    return result


def _mesh_data_arrays(data: Any) -> dict[str, dict[str, list[Any]]]:
    """Copy every native scalar/vector array without changing its association."""
    scalars = {}
    vectors = {}
    for index in range(data.getScalarDataSize()):
        label = str(data.getScalarDataLabel(index))
        scalars[label] = [float(value) for value in data.getScalarData(index)]
    for index in range(data.getVectorDataSize()):
        label = str(data.getVectorDataLabel(index))
        vectors[label] = [[float(value) for value in row] for row in data.getVectorData(index)]
    return {"scalars": scalars, "vectors": vectors}


def native_mesh_report(mesh: Any) -> dict[str, Any]:
    points = [[float(axis) for axis in point] for point in mesh.getNodes()]
    cells = {
        "lines": [[int(value) for value in row] for row in mesh.getLines()],
        "triangles": [[int(value) for value in row] for row in mesh.getTriangles()],
        "tetras": [[int(value) for value in row] for row in mesh.getTetras()],
        "hexas": [[int(value) for value in row] for row in mesh.getHexas()],
        "vertices": [[int(value) for value in row] for row in mesh.getVerticies()],
    }
    topology = {name: value for name, value in cells.items() if value}
    point_data = _mesh_data_arrays(mesh.getPointData())
    cell_data = _mesh_data_arrays(mesh.getCellData())
    fields = []
    for association, data in (("pointData", point_data), ("cellData", cell_data)):
        for kind in ("scalars", "vectors"):
            for name, values in data[kind].items():
                fields.append({"name": name, "association": association, "components": 1 if kind == "scalars" else 3,
                               "tuples": len(values), "nativeUnit": None,
                               "unitStatus": "not-provided-by-native-mesh-interface"})
    mesh_identity_payload = {"points": points, "topology": topology}
    mesh_identity = hashlib.sha256(json.dumps(mesh_identity_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"points": points, **topology, "pointData": point_data, "cellData": cell_data,
            "meshIdentity": mesh_identity, "fields": fields}


def mesh_report(mesh: Any) -> dict[str, Any]:
    nodes = [[float(axis) for axis in node] for node in mesh.getNodes()]
    lines = [[int(index) for index in line] for line in mesh.getLines()]
    if any(len(point) != 3 or not all(math.isfinite(value) for value in point) for point in nodes):
        raise RuntimeError("ViennaPS returned a non-finite or non-3D point in the 2D mesh")
    if any(len(line) != 2 or line[0] == line[1] or any(index < 0 or index >= len(nodes) for index in line) for line in lines):
        raise RuntimeError("ViennaPS returned invalid line connectivity")
    cell_data = _data_scalars(mesh.getCellData())
    material_ids = cell_data.get("MaterialIds")
    if material_ids is None or len(material_ids) != len(lines):
        raise RuntimeError("ViennaPS surface mesh has no aligned MaterialIds cell array")
    if any(int(value) not in MATERIAL_NAMES for value in material_ids):
        raise RuntimeError("ViennaPS surface mesh contains an unknown catalog material ID")
    point_arrays = _mesh_data_arrays(mesh.getPointData())
    cell_arrays = _mesh_data_arrays(mesh.getCellData())
    return {
        "points": nodes,
        "lines": lines,
        "cellData": cell_data,
        "pointData": point_arrays["scalars"],
        "cellVectorData": cell_arrays["vectors"],
        "pointVectorData": point_arrays["vectors"],
    }


def trench_has_two_walls(mesh: dict[str, Any], delta: float, substrate_id: int = 10) -> bool:
    nodes = mesh["points"]
    ids = mesh["cellData"]["MaterialIds"]
    sides = set()
    for (first, second), material_id in zip(mesh["lines"], ids):
        if int(material_id) != substrate_id:
            continue
        a, b = nodes[first], nodes[second]
        if abs(a[1] - b[1]) <= 0.5 * delta:
            continue
        middle_x = 0.5 * (a[0] + b[0])
        if middle_x < -1e-9:
            sides.add("negative")
        elif middle_x > 1e-9:
            sides.add("positive")
    return sides == {"negative", "positive"}


def measure_initial_geometry(mesh: dict[str, Any], geometry: dict[str, Any], delta: float) -> dict[str, float]:
    points = mesh["points"]
    if geometry["kind"] == "plane":
        return {"heightMedian": float(sorted(point[1] for point in points)[len(points) // 2])}
    ids = mesh["cellData"]["MaterialIds"]
    top_segments = []
    for (first, second), _material_id in zip(mesh["lines"], ids):
        a, b = points[first], points[second]
        if abs(a[1]) <= delta * 0.05 and abs(b[1]) <= delta * 0.05:
            top_segments.append((a[0], b[0]))
    left_points = [x for pair in top_segments for x in pair if x < -1e-9]
    right_points = [x for pair in top_segments for x in pair if x > 1e-9]
    # The initial trench has only the substrate at negative h; mask points are
    # at or above the zero datum. The returned raster contour is authoritative.
    # A half trench retains only x>=0, so its opening is measured from the
    # symmetry plane at x=0 to the retained Si shoulder.
    half_trench = bool(geometry.get("halfTrench"))
    if half_trench:
        if not right_points:
            raise RuntimeError("cannot measure the initial half-trench opening from its retained top interface")
        measured_width = min(right_points)
        requested_width = float(geometry["width"]) / 2
    else:
        if not left_points or not right_points:
            raise RuntimeError("cannot measure the initial trench opening from its top interface")
        measured_width = min(right_points) - max(left_points)
        requested_width = float(geometry["width"])
    measured_depth = -min(point[1] for point in points)
    requested_depth = float(geometry["depth"])
    tolerance = 2 * delta + 1e-9
    if abs(measured_width - requested_width) > tolerance:
        raise RuntimeError(f"rasterized trench opening {measured_width:.6g} um differs from requested width by more than 2 grid intervals")
    if abs(measured_depth - requested_depth) > tolerance:
        raise RuntimeError(f"rasterized trench depth {measured_depth:.6g} um differs from requested depth by more than 2 grid intervals")
    return {"openingWidth": measured_width, "depth": measured_depth}


# An exposed surface cell counts as "up-facing" when its normal is within 60
# degrees of the vertical axis. One threshold is shared by 2D line cells
# ([x, h, 0]) and 3D triangle cells ([x, y, z]) so both dimensions use the same
# definition of a material's top surface.
TOP_FACING_COSINE = 0.5
MEASUREMENT_METHOD = "surface-mesh up-facing cells; no volume mesh"
# CMP stop height: area-weighted 90th percentile of the stop material's
# up-facing heights. A bimodal stop surface (for example STI at 0.036 and ILD
# at 0.095 in the 22 nm FinFET flow, where the lower plateau is even slightly
# larger) must select the upper plateau, while a narrow tall sliver must not
# move the cut. The median cannot do the first job; the 90th percentile cannot
# do it only when the upper plateau covers 10% of the area or less.
STOP_HEIGHT_PERCENTILE = 0.9
STOP_HEIGHT_METHOD = "area-weighted-p90"
THICKNESS_BASIS_MEDIAN = "area-weighted-median"
THICKNESS_BASIS_HIGHEST = "highest-top"


def _area_percentile(rows: list[tuple[float, float]], fraction: float) -> float:
    """Return the height where the accumulated up-facing area reaches fraction.

    ``rows`` is sorted by height. This is the area-weighted percentile: each
    surface cell contributes its own area, so a large plateau dominates while a
    narrow sliver cannot.
    """
    total = sum(area for _height, area in rows)
    running = 0.0
    for height, area in rows:
        running += area
        if running >= fraction * total:
            return float(height)
    return float(rows[-1][0])


def measure_material_tops(report: dict[str, Any], is3d: bool) -> dict[str, dict[str, float]]:
    """Return the exposed top height of every material in a surface mesh.

    The caller has already extracted the surface mesh for the snapshot, so this
    adds no native work: it only classifies the returned cells. Per material it
    reports

    * ``topHeight``: the highest up-facing point, i.e. the top of that material
      (a blanket layer's surface, or the highest rim of a partially removed
      one). Reported for context only; a tall feature plus a low field makes it
      unsuitable as a thickness, see ``medianTopHeight``.
    * ``medianTopHeight``: the area-weighted median of the up-facing heights,
      i.e. the level that dominates the exposed surface. Used for the measured
      add/remove thickness.
    * ``p90TopHeight``: the area-weighted 90th percentile. Used to derive a CMP
      stop height so that a large upper plateau wins over a larger lower one
      while a narrow tall sliver is ignored.
    * ``upFacingArea``/``upFacingCellCount``: the evidence behind all values.

    A material with no up-facing cell (fully buried, fully removed, or only
    vertical sidewalls left) is absent from the result.
    """
    points = report["points"]
    cells = report["triangles"] if is3d else report["lines"]
    material_ids = report["cellData"]["MaterialIds"]
    heights: dict[int, list[tuple[float, float]]] = {}
    if is3d:
        for (first, second, third), material_id in zip(cells, material_ids):
            ax, ay, az = points[first]
            bx, by, bz = points[second]
            cx, cy, cz = points[third]
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            if nz <= 0.0:
                continue
            norm_squared = nx * nx + ny * ny + nz * nz
            if norm_squared <= 0.0 or nz * nz < TOP_FACING_COSINE ** 2 * norm_squared:
                continue
            heights.setdefault(int(material_id), []).append(
                ((az + bz + cz) / 3.0, 0.5 * math.sqrt(norm_squared)))
    else:
        for (first, second), material_id in zip(cells, material_ids):
            ax, ah = points[first][0], points[first][1]
            bx, bh = points[second][0], points[second][1]
            dh = bh - ah
            length = math.hypot(bx - ax, dh)
            if length <= 0.0 or abs(dh) > TOP_FACING_COSINE * length:
                continue
            heights.setdefault(int(material_id), []).append((0.5 * (ah + bh), length))
    result: dict[str, dict[str, float]] = {}
    for material_id, rows in heights.items():
        name = MATERIAL_NAMES.get(material_id)
        if name is None:
            raise RuntimeError(f"surface mesh contains unknown material ID {material_id}")
        rows.sort()
        total_area = sum(area for _height, area in rows)
        result[name] = {"topHeight": float(rows[-1][0]),
                        "medianTopHeight": _area_percentile(rows, 0.5),
                        "p90TopHeight": _area_percentile(rows, STOP_HEIGHT_PERCENTILE),
                        "upFacingArea": float(total_area),
                        "upFacingCellCount": len(rows)}
    return result


def _changed_material_rows(previous_tops: dict[str, Any], current_tops: dict[str, Any],
                           tolerance: float) -> list[dict[str, Any]]:
    """Per-material rows describing the exposed-surface change of one step.

    Thickness uses the area-weighted median of the up-facing heights, capped by
    the highest up-facing change: a median that jumped to a different patch
    (for example TiN keeping a low liner after the blanket was polished away)
    must not report more removal than the material's topmost surface dropped.
    """
    rows = []
    for name in sorted(set(previous_tops) | set(current_tops)):
        before = previous_tops.get(name) or {}
        after = current_tops.get(name) or {}
        top_before = before.get("topHeight")
        top_after = after.get("topHeight")
        median_before = before.get("medianTopHeight")
        median_after = after.get("medianTopHeight")
        top_delta = None if top_before is None or top_after is None else float(top_after) - float(top_before)
        median_delta = (None if median_before is None or median_after is None
                        else float(median_after) - float(median_before))
        appeared = before.get("upFacingArea") is None and after.get("upFacingArea") is not None
        vanished = before.get("upFacingArea") is not None and after.get("upFacingArea") is None
        if top_delta is None:
            changed = appeared or vanished
        else:
            changed = abs(top_delta) > tolerance or (median_delta is not None and abs(median_delta) > tolerance)
        if not changed:
            continue
        # Median-based thickness, never larger than the highest-point change
        # (both are negative for removal and positive for deposition, so the
        # smaller magnitude is the larger value on the negative side).
        delta_used = None
        basis = None
        if median_delta is not None and top_delta is not None:
            if median_delta < 0.0 and top_delta < 0.0:
                delta_used = max(median_delta, top_delta)
                basis = (THICKNESS_BASIS_MEDIAN if median_delta >= top_delta
                         else f"{THICKNESS_BASIS_HIGHEST} (median surface changed)")
            elif median_delta > 0.0 and top_delta > 0.0:
                delta_used = min(median_delta, top_delta)
                basis = (THICKNESS_BASIS_MEDIAN if median_delta <= top_delta
                         else f"{THICKNESS_BASIS_HIGHEST} (median surface changed)")
            else:
                delta_used = median_delta
                basis = THICKNESS_BASIS_MEDIAN
        elif top_delta is not None:
            delta_used = top_delta
            basis = THICKNESS_BASIS_HIGHEST
        rows.append({
            "material": name,
            "topHeightBefore": top_before, "topHeightAfter": top_after,
            "medianHeightBefore": median_before, "medianHeightAfter": median_after,
            "topHeightDelta": top_delta, "medianHeightDelta": median_delta,
            "thicknessBasis": basis,
            "removedThickness": -delta_used if delta_used is not None and delta_used < 0.0 else None,
            "addedThickness": delta_used if delta_used is not None and delta_used > 0.0 else None,
            "removedCompletely": vanished,
            "upFacingAreaBefore": before.get("upFacingArea"), "upFacingAreaAfter": after.get("upFacingArea"),
        })
    rows.sort(key=lambda row: (-abs(row["medianHeightDelta"]) if row["medianHeightDelta"] is not None
                               else (-abs(row["topHeightDelta"]) if row["topHeightDelta"] is not None else 0.0),
                               row["material"]))
    return rows


def measure_step_change(step: dict[str, Any], previous_tops: dict[str, Any] | None,
                        current_tops: dict[str, Any], delta: float) -> dict[str, Any] | None:
    """Record what a step really did to each material's exposed top.

    Both operands come from the surface mesh each snapshot already extracts, so
    this costs one classification pass and no extra native work. The nominal
    rate x duration of a particle or ion-beam step is a calibrated flux
    quantity, not a line speed; this record is what actually happened.

    The reported thickness is the area-weighted median change, not the change of
    the highest point: a blanket film on a tall feature loses its top first, so
    the highest-point delta describes where the film used to be, not how much
    came off (the 22 nm FinFET spacer etch read 49.7 nm that way while the
    exposed surface only dropped about 12 nm).
    """
    if not previous_tops:
        return None
    rows = _changed_material_rows(previous_tops, current_tops, max(0.25 * float(delta), 1e-9))
    deposition = step.get("operation") == "deposit"
    target = step.get("depositMaterial") if deposition else None
    if target is None and step.get("model") == "removeMaterial":
        target = step.get("material")
    primary = next((row for row in rows if row["material"] == target), None) if target else None
    if deposition and isinstance(target, str) and (primary is None or primary.get("topHeightDelta") is None):
        # ``Domain.duplicateTopLevelSet`` starts the new level set on the
        # previous top surface, so a material that did not exist before the
        # step grows from the highest point of the surface that existed then.
        # The highest point is the only well-defined start for a conformal
        # film that begins on the previous top level set.
        after = (current_tops.get(target) or {}).get("topHeight")
        previous_heights = [(row or {}).get("topHeight") for row in previous_tops.values()]
        before = max((value for value in previous_heights if value is not None), default=None)
        if after is not None and before is not None:
            appeared = {"material": target, "topHeightBefore": before, "topHeightAfter": after,
                        "medianHeightBefore": None,
                        "medianHeightAfter": (current_tops.get(target) or {}).get("medianTopHeight"),
                        "topHeightDelta": after - before, "medianHeightDelta": None,
                        "thicknessBasis": f"{THICKNESS_BASIS_HIGHEST} (new material)",
                        "removedThickness": None,
                        "addedThickness": after - before if after > before else None,
                        "removedCompletely": False, "materialAppeared": True,
                        "upFacingAreaBefore": None,
                        "upFacingAreaAfter": (current_tops.get(target) or {}).get("upFacingArea")}
            rows = [row for row in rows if row["material"] != target]
            rows.insert(0, appeared)
            primary = appeared
    if primary is None:
        # A material that vanished from the surface has no measurable delta;
        # it is still a removal, so prefer real drops and then the topmost
        # completely removed layer.
        def removal_rank(row: dict[str, Any]) -> tuple[int, float]:
            magnitude = row["removedThickness"]
            if magnitude is not None and magnitude > 0.0:
                return (0, -magnitude)
            return (1, -(row["topHeightBefore"] if row["topHeightBefore"] is not None else 0.0))
        removals = [row for row in rows if row["removedThickness"] is not None or row["removedCompletely"]]
        if removals and not deposition:
            primary = min(removals, key=removal_rank)
    if primary is None:
        return None
    key = "measuredAdded" if deposition else "measuredRemoval"
    thickness_key = "addedThickness" if deposition else "removedThickness"
    remaining_top = max((row["topHeight"] for row in current_tops.values()
                         if isinstance(row, dict) and row.get("topHeight") is not None), default=None)
    record = {
        "material": primary["material"],
        "topHeightBefore": primary["topHeightBefore"],
        "topHeightAfter": primary["topHeightAfter"],
        "medianHeightBefore": primary["medianHeightBefore"],
        "medianHeightAfter": primary["medianHeightAfter"],
        "topHeightDelta": primary["topHeightDelta"],
        "medianHeightDelta": primary["medianHeightDelta"],
        thickness_key: primary[thickness_key],
        "thicknessBasis": primary["thicknessBasis"],
        "removedCompletely": primary["removedCompletely"],
        # A material that vanished has no remaining top to difference against.
        # The highest remaining surface bounds where it went, so record it
        # instead of inventing a thickness.
        "remainingTopHeight": remaining_top,
        "method": MEASUREMENT_METHOD,
        "materials": rows[:16],
    }
    return {key: record}


def _levelset_index(path: Path) -> int:
    match = re.search(r"_layer(\d+)\.(?:lvst|vtp)$", path.name)
    if not match:
        raise RuntimeError(f"native level-set output filename is malformed: {path.name}")
    return int(match.group(1))


def save_native_state(ps: Any, domain: Any, run_dir: Path, *, project: dict[str, Any], run_id: str,
                      step_id: str, viennaps_version: str, viennals_version: str) -> dict[str, Any]:
    path = run_dir / f"state-{step_id}.vpsd"
    ps.Writer(domain, str(path)).apply()
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("ViennaPS Writer did not create a nonempty native Domain state")
    mapping = material_map(domain)
    parsed = parse_project(project)
    geometry = normalized_geometry_v3(parsed)
    ref = {
        "formatVersion": 1,
        "stateId": file_sha256(path),
        "structureHash": project_structure_hash(project["dimension"], parsed.units, parsed.domain, geometry),
        "dimension": int(project["dimension"]),
        "gridDelta": float(domain.getGridDelta()),
        "xExtent": float(project["domain"]["xExtent"]),
        **({"yExtent": float(project["domain"]["yExtent"])} if project["dimension"] == 3 else {}),
        "boundary": project["domain"].get("xBoundary", project["domain"].get("boundaryXY")),
        "boundaryConditions": ["reflective" if str(value) == "0" else "periodic" if str(value) == "2" else "infinite" if str(value) == "1" else f"unknown:{value}" for value in domain.getBoundaryConditions()],
        "materialIdsByLevelSetIndex": mapping,
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "viennapsVersion": viennaps_version,
        "viennalsVersion": viennals_version,
        "sourceRunId": run_id,
        "sourceStepId": step_id,
    }
    check_artifact_budget(run_dir)
    return {"fileName": path.name, **ref}


RATE_SET_OVERLOAD = "DirectionalProcess(rateSets: Sequence[RateSet])"


def directional_rate_set_records(project: dict[str, Any], step: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the request into the exact native ``ps.RateSet`` payloads.

    ``maskMaterials`` omitted means the geometry-derived masks shared with the
    scalar directional path; an explicit list (including ``[]``) is used
    verbatim. Every name must exist in the material catalog, and the record
    keeps both names and native IDs so a run is auditable without the project.
    """
    rows = signed_directional_rate_sets(step)
    if rows is None:
        raise RuntimeError("directional step does not use the native rate-set overload")
    default_masks = protected_material_names(project, step)
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        requested_masks = row["requestedMaskMaterials"]
        masks = list(default_masks) if requested_masks is None else list(requested_masks)
        for name in masks:
            if name not in MATERIAL_IDS:
                raise RuntimeError(f"rateSets[{index}] mask material {name!r} is absent from the material catalog")
        active = row["directionalRateMagnitude"] > 0.0
        records.append({
            "index": index,
            "requestedDirection": row["requestedDirection"],
            "nativeUnitDirection": row["nativeUnitDirection"],
            "directionalRateMagnitude": row["directionalRateMagnitude"],
            "isotropicRateMagnitude": row["isotropicRateMagnitude"],
            "nativeDirectionalVelocity": row["nativeDirectionalVelocity"],
            "nativeIsotropicVelocity": row["nativeIsotropicVelocity"],
            "requestedMaskMaterials": requested_masks,
            "maskMaterials": masks,
            "maskMaterialIds": [MATERIAL_IDS[name] for name in masks],
            "maskSource": "geometry" if requested_masks is None else "request",
            "requestedCalculateVisibility": row["requestedCalculateVisibility"],
            "calculateVisibility": (row["requestedCalculateVisibility"] if active
                                    else "not-applicable: directional velocity is zero"),
        })
    return records


def native_rate_sets(ps: Any, records: list[dict[str, Any]]) -> list[Any]:
    """Build real ``ps.RateSet`` objects for the native sequence overload."""
    return [
        ps.RateSet(list(record["nativeUnitDirection"]),
                   float(record["nativeDirectionalVelocity"]),
                   float(record["nativeIsotropicVelocity"]),
                   [material_handle(ps, name) for name in record["maskMaterials"]],
                   bool(record["requestedCalculateVisibility"]) and record["directionalRateMagnitude"] > 0.0)
        for record in records
    ]


DIRECTIONAL_MATERIAL_OVERLOAD = (
    "DirectionalProcess(direction, materialRates: Mapping[Material, (directional, isotropic)], "
    "defaultDirectionalRate, defaultIsotropicRate)")


def directional_material_mode(step: dict[str, Any]) -> bool:
    """True when the step uses the native directional material rate map."""
    return step.get("materialRates") is not None or step.get("defaultMaterialRate") is not None


def _directional_rate_pair(raw: Any, label: str) -> tuple[float, float]:
    if raw is None:
        return 0.0, 0.0
    if not isinstance(raw, dict):
        raise RuntimeError(f"{label} must be an object with directional and isotropic rates")
    pair = []
    for key in ("directional", "isotropic"):
        value = float(raw.get(key, 0.0))
        if not math.isfinite(value) or value < 0.0 or value > 2.0:
            raise RuntimeError(f"{label}.{key} {value} is outside the finite 0..2 um/s schema range")
        pair.append(value)
    return pair[0], pair[1]


def directional_material_rate_records(step: dict[str, Any]) -> dict[str, Any]:
    """Signed native rate map plus an auditable record for the material overload.

    The request carries nonnegative operation magnitudes only.  The native
    ``DirectionalVelocityFieldSimple`` uses two different sign conventions:
    a positive directional rate moves *along* ``direction`` (an etch when the
    direction points into the surface), while a positive isotropic rate grows
    the surface (a deposition).  Therefore the operation mapping is::

        etch:    directional +magnitude, isotropic -magnitude
        deposit: directional -magnitude, isotropic +magnitude

    Applying one uniform sign to both components (as the scalar overload does)
    makes a material-mode "etch" grow the surface.  Both native signs are
    recorded so the run is auditable.
    """
    if not directional_material_mode(step):
        raise RuntimeError("directional step does not use the material rate map overload")
    etch = step["operation"] == "etch"
    if step.get("rateSigns") == "native":
        directional_sign = 1.0
        isotropic_sign = 1.0
    else:
        directional_sign = 1.0 if etch else -1.0
        isotropic_sign = -1.0 if etch else 1.0
    rows: list[dict[str, Any]] = []
    native: dict[str, tuple[float, float]] = {}
    for name, raw in (step.get("materialRates") or {}).items():
        if name not in MATERIAL_IDS:
            raise RuntimeError(f"materialRates material {name!r} is absent from the material catalog")
        directional, isotropic = _directional_rate_pair(raw, f"materialRates.{name}")
        rows.append({
            "name": name,
            "id": MATERIAL_IDS[name],
            "directionalRateMagnitude": directional,
            "isotropicRateMagnitude": isotropic,
            "nativeDirectionalRate": directional * directional_sign,
            "nativeIsotropicRate": isotropic * isotropic_sign,
        })
        native[name] = (directional * directional_sign, isotropic * isotropic_sign)
    default_directional, default_isotropic = _directional_rate_pair(
        step.get("defaultMaterialRate"), "defaultMaterialRate")
    return {
        "rows": rows,
        "native": native,
        "requestedDefault": {"directional": default_directional, "isotropic": default_isotropic},
        "nativeDefault": (default_directional * directional_sign,
                          default_isotropic * isotropic_sign),
        "defaultOmitted": step.get("defaultMaterialRate") is None,
        "directionalSign": directional_sign,
        "isotropicSign": isotropic_sign,
        "rateSigns": step.get("rateSigns") or "operation",
    }


def isotropic_thickness(step: dict[str, Any]) -> float:
    """Uniform displacement ``rateMagnitude * duration`` in um."""
    return abs(float(step.get("rateMagnitude", 0.0))) * float(step["duration"])


def _isotropic_time_step_ratio(project: dict[str, Any], step: dict[str, Any]) -> tuple[float, str]:
    requested = step.get("isotropicTimeStepRatio")
    if requested is None:
        return float(project["solver"]["timeStepRatio"]), "solver.timeStepRatio"
    ratio = float(requested)
    if not math.isfinite(ratio) or not 0.05 <= ratio <= 0.5:
        raise RuntimeError(
            f"isotropicTimeStepRatio {ratio} is outside the finite 0.05..0.5 schema range")
    return ratio, "step.isotropicTimeStepRatio"


def resolve_isotropic_method(project: dict[str, Any], step: dict[str, Any]) -> tuple[str, str]:
    """Resolve ``auto``/``geometric``/``levelSet`` into the actual native path.

    ``auto`` only selects the geometric distribution where it is measured both
    safe and faster: 2D, uniform rate, one full grid interval of displacement,
    and an unmasked deposition. 3D auto always stays on the level-set PDE
    because measured 3D ``SphereDistribution`` is 3-30x slower than
    ``IsotropicProcess`` for the 22 nm cell / 5 nm grid, plane included.
    """
    requested = step.get("isotropicMethod") or "levelSet"
    if requested not in {"auto", "levelSet", "geometric"}:
        raise RuntimeError(f"unknown isotropicMethod {requested!r}")
    if step.get("isotropicTimeStepRatio") is not None:
        # Validate the range even when the geometric path will ignore it.
        _isotropic_time_step_ratio(project, step)
    if requested == "levelSet":
        reason = ("explicit level-set request" if step.get("isotropicMethod") == "levelSet"
                  else "isotropicMethod absent: legacy level-set PDE path")
        return "levelSet", reason
    dimension = int(project["dimension"])
    grid_delta = float(project["domain"]["gridDelta"])
    thickness = isotropic_thickness(step)
    blockers: list[str] = []
    if str(step.get("rateMode", "uniform")) != "uniform":
        blockers.append(f"rateMode={step.get('rateMode')} requires the native material rate table")
    elif step.get("materialRates"):
        blockers.append("materialRates requires the native material rate table")
    if thickness + 1e-12 < grid_delta:
        blockers.append(
            f"thickness {thickness} um is below one grid interval {grid_delta} um")
    if step["operation"] == "deposit":
        masks = protected_material_names(project, step)
        if masks:
            blockers.append(
                f"positive masked geometric deposition is unsafe (geometry masks {masks})")
    if requested == "geometric":
        if blockers:
            raise RuntimeError("isotropicMethod geometric is unsafe: " + "; ".join(blockers))
        reason = "explicit geometric SphereDistribution request"
        if dimension == 3:
            reason += ("; explicit 3D override: measured 3-30x slower than IsotropicProcess")
        return "geometric", reason
    if dimension != 2:
        blockers.append(
            "auto uses geometric only in 2D; measured 3D SphereDistribution is 3-30x slower "
            "than IsotropicProcess")
    if blockers:
        return "levelSet", "auto selected level-set: " + "; ".join(blockers)
    return "geometric", (
        f"auto selected geometric: 2D uniform isotropic step with thickness {thickness} um "
        f">= gridDelta {grid_delta} um")


def isotropic_level_set_record(project: dict[str, Any], step: dict[str, Any],
                               reason: str) -> dict[str, Any]:
    grid_delta = float(project["domain"]["gridDelta"])
    thickness = isotropic_thickness(step)
    material_mode = (project.get("schemaVersion") in (4, 5, 6, 7)
                     and step.get("rateMode") == "material")
    masks = [] if material_mode else protected_material_names(project, step)
    ratio, ratio_source = _isotropic_time_step_ratio(project, step)
    return {
        "requestedIsotropicMethod": step.get("isotropicMethod") or "levelSet",
        "actualIsotropicMethod": "levelSet",
        "nativeClass": "IsotropicProcess",
        "nativeRadiusUm": None,
        "thicknessUm": thickness,
        "cells": thickness / grid_delta,
        "gridDeltaUm": grid_delta,
        "maskMaterials": masks,
        "reason": reason,
        "nativeDurationIgnored": False,
        "timeStepRatio": ratio,
        "timeStepRatioSource": ratio_source,
    }


def build_isotropic_geometric(ps: Any, domain: Any, project: dict[str, Any],
                              step: dict[str, Any], *, reason: str,
                              ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Construct the native ``SphereDistribution`` for an isotropic offset.

    The geometric strategy consumes only a signed radius; the requested
    duration is ignored by construction.  Etches may carry geometry mask
    materials through ``addMaskMaterial`` (the supported direction), while a
    deposition is always constructed without masks.
    """
    dimension = int(project["dimension"])
    grid_delta = float(project["domain"]["gridDelta"])
    thickness = isotropic_thickness(step)
    if thickness + 1e-12 < grid_delta:
        raise RuntimeError(
            f"geometric isotropic thickness {thickness} um is below one grid interval "
            f"{grid_delta} um; native geometric advection is unsafe below that")
    sign = -1.0 if step["operation"] == "etch" else 1.0
    signed_radius = sign * thickness
    model = ps.SphereDistribution(signed_radius)
    applied: list[str] = []
    omitted: list[str] = []
    if sign < 0.0:
        for name in protected_material_names(project, step):
            handle = material_handle(ps, name)
            material_level_set = domain.getMaterialLevelSet(handle)
            if material_level_set is None or int(material_level_set.getNumberOfPoints()) == 0:
                # The geometry declared the material but the domain does not
                # carry it; passing it would make the native strategy log
                # "not found" and silently stay maskless.
                omitted.append(name)
                continue
            model.addMaskMaterial(handle)
            applied.append(name)
    before = surface_state(domain, dimension)
    record = {
        "requestedIsotropicMethod": step.get("isotropicMethod") or "levelSet",
        "actualIsotropicMethod": "geometric",
        "nativeClass": "SphereDistribution",
        "nativeConstructor": f"viennaps.d{dimension}.SphereDistribution(radius)",
        "nativeRadiusUm": signed_radius,
        "thicknessUm": thickness,
        "cells": thickness / grid_delta,
        "gridDeltaUm": grid_delta,
        "maskMaterials": applied,
        "omittedMaskMaterials": omitted,
        "reason": reason,
        "nativeDurationIgnored": float(step["duration"]),
        "timeStepRatio": "ignored: geometric advection integrates no PDE",
        "dimension": dimension,
        "nativeProcessBackend": "CPU_DISK pinned; geometric distribution has no stochastic flux engine",
    }
    return model, record, before


def build_multi_particle_model(ps: Any, step: dict[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Build the installed 4.6.2 model and retain a bounded callback audit."""
    model = ps.MultiParticleProcess()
    labels: list[str] = []
    native_species: list[dict[str, Any]] = []
    for species in step["species"]:
        label = str(species["label"])
        labels.append(label)
        if species["kind"] == "ion":
            parameters = {key: species[key] for key in (
                "sourcePower", "thetaRMin", "thetaRMax", "minAngle", "B_sp", "meanEnergy",
                "sigmaEnergy", "thresholdEnergy", "inflectAngle", "n")}
            model.addIonParticle(
                sourcePower=float(parameters["sourcePower"]),
                thetaRMin=float(parameters["thetaRMin"]), thetaRMax=float(parameters["thetaRMax"]),
                minAngle=float(parameters["minAngle"]), B_sp=float(parameters["B_sp"]),
                meanEnergy=float(parameters["meanEnergy"]), sigmaEnergy=float(parameters["sigmaEnergy"]),
                thresholdEnergy=float(parameters["thresholdEnergy"]), inflectAngle=float(parameters["inflectAngle"]),
                n=int(parameters["n"]), label=label,
            )
            native_species.append({"kind": "ion", "label": label, **parameters})
        elif species["kind"] == "neutral":
            sticking_by_material = species.get("stickingProbabilityByMaterial", {})
            native_sticking = {material_handle(ps, name): float(probability)
                               for name, probability in sticking_by_material.items()}
            model.addNeutralParticle(
                native_sticking,
                defaultStickingProbability=float(species["defaultStickingProbability"]), label=label,
            )
            native_species.append({
                "kind": "neutral", "label": label,
                "defaultStickingProbability": float(species["defaultStickingProbability"]),
                "stickingProbabilityByMaterial": {name: float(value) for name, value in sticking_by_material.items()},
                "stickingMaterialIds": {name: MATERIAL_IDS[name] for name in sticking_by_material},
            })
        else:
            raise RuntimeError(f"unsupported multi-particle species kind {species['kind']!r}")

    relation = step["rateCoefficients"]
    defaults = {label: float(relation["defaultBySpecies"][label]) for label in labels}
    overrides = {material: {label: float(value) for label, value in row.items()}
                 for material, row in relation.get("byMaterial", {}).items()}
    native_material_names = {
        str(material_handle(ps, name)): name for name in MATERIAL_IDS
    }
    callback_state: dict[str, Any] = {
        "count": 0, "materials": {}, "samples": [],
        "fluxSums": [0.0] * len(labels), "fluxMaxima": [0.0] * len(labels),
    }
    operation_sign = -1.0 if step["operation"] == "etch" else 1.0

    def rate_function(fluxes: Any, material: Any) -> float:
        material_repr = str(material)
        material_name = native_material_names.get(material_repr)
        if material_name is None:
            raise RuntimeError(f"ViennaPS rate callback returned an unregistered material {material_repr}")
        values = [float(value) for value in fluxes]
        if len(values) != len(labels):
            raise RuntimeError(
                f"ViennaPS rate callback flux vector length {len(values)} does not match "
                f"the {len(labels)} configured species"
            )
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError("ViennaPS rate callback returned a non-finite species flux")
        for index, value in enumerate(values):
            callback_state["fluxSums"][index] += value
            callback_state["fluxMaxima"][index] = max(callback_state["fluxMaxima"][index], value)
        coefficients = overrides.get(material_name, {})
        magnitude = sum(values[index] * coefficients.get(label, defaults[label])
                        for index, label in enumerate(labels))
        signed = operation_sign * magnitude
        if not math.isfinite(signed):
            raise RuntimeError("multi-particle rate relation returned a non-finite velocity")
        callback_state["count"] += 1
        callback_state["materials"][material_name] = callback_state["materials"].get(material_name, 0) + 1
        if len(callback_state["samples"]) < 256:
            sample = {"material": material_name, "fluxes": values,
                      "coefficients": {label: coefficients.get(label, defaults[label]) for label in labels},
                      "returnedVelocity": signed}
            if os.environ.get("VIENNAPS_DEBUG_NATIVE_CALLBACKS") == "1":
                sample["nativeMaterial"] = material_repr
            callback_state["samples"].append(sample)
        return signed

    model.setRateFunction(rate_function)
    actual = {
        "nativeProcess": "viennaps.MultiParticleProcess",
        "speciesOrder": labels,
        "species": native_species,
        "rateFunction": {
            "kind": "weighted-sum-table", "formula": "operationSign * sum(flux[i] * coefficient[material][species[i]])",
            "operationSign": operation_sign, "defaultBySpecies": defaults, "byMaterial": overrides,
            "nativeFluxUnits": "not-declared-by-installed-binding",
            "returnedVelocityUnit": "um/s workbench convention; coefficients perform the conversion",
        },
        "nativeSpeciesFieldNames": [f"{label}{index}" for index, label in enumerate(labels)],
    }
    return model, actual, callback_state


def multi_particle_species_step(step: dict[str, Any], species: dict[str, Any]) -> dict[str, Any]:
    """Project one species and its matching material-rate columns for a native sub-process."""
    label = str(species["label"])
    relation = step["rateCoefficients"]
    return {**step, "species": [species], "rateCoefficients": {
        "defaultBySpecies": {label: float(relation["defaultBySpecies"][label])},
        "byMaterial": {
            material: {label: float(values[label])}
            for material, values in relation.get("byMaterial", {}).items() if label in values
        },
    }}


def multi_particle_step_summary(step: dict[str, Any]) -> dict[str, Any]:
    labels = [str(row["label"]) for row in step["species"]]
    relation = step["rateCoefficients"]
    return {
        "nativeProcess": "viennaps.MultiParticleProcess",
        "speciesOrder": labels,
        "species": step["species"],
        "rateFunction": {
            "kind": "weighted-sum-table",
            "formula": "operationSign * sum(flux[i] * coefficient[material][species[i]])",
            "operationSign": -1.0 if step["operation"] == "etch" else 1.0,
            "defaultBySpecies": relation["defaultBySpecies"],
            "byMaterial": relation.get("byMaterial", {}),
            "nativeFluxUnits": "not-declared-by-installed-binding",
            "returnedVelocityUnit": "um/s workbench convention; coefficients perform the conversion",
        },
    }


def _vtk_appended_payload(root: ET.Element) -> str:
    appended = root.find("AppendedData")
    if appended is None:
        return ""
    text = "".join((appended.text or "").split())
    if not text.startswith("_"):
        raise RuntimeError("native VTP appended-data block has no VTK sentinel")
    return text[1:]


def _vtk_appended_chunks(root: ET.Element) -> dict[ET.Element, str]:
    arrays = [array for array in root.findall(".//DataArray") if array.attrib.get("format") == "appended"]
    payload = _vtk_appended_payload(root)
    offsets: dict[ET.Element, int] = {}
    for array in arrays:
        try:
            offset = int(array.attrib["offset"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError("native VTP appended DataArray has an invalid offset") from exc
        if offset < 0 or offset >= len(payload):
            raise RuntimeError("native VTP appended DataArray offset is outside its payload")
        offsets[array] = offset
    sorted_offsets = sorted(set(offsets.values()))
    chunks: dict[ET.Element, str] = {}
    for array, start in offsets.items():
        end = next((offset for offset in sorted_offsets if offset > start), len(payload))
        if end <= start:
            raise RuntimeError("native VTP appended DataArray has an empty payload")
        chunks[array] = payload[start:end]
    if arrays and not payload:
        raise RuntimeError("native VTP declares appended DataArrays without an appended payload")
    return chunks


def _vtk_data_array_signature(array: ET.Element, chunks: dict[ET.Element, str]) -> tuple[Any, ...]:
    attributes = tuple(sorted((key, value) for key, value in array.attrib.items() if key != "offset"))
    value: Any = chunks.get(array) if array.attrib.get("format") == "appended" else (array.text or "").strip()
    return attributes, value


def merge_native_flux_vtp(species_paths: list[Path], output_path: Path) -> None:
    """Merge real per-species VTP fields while preserving their native association.

    The method requires every native calculation to return an identical mesh.
    It retains the first VTK geometry and adds the other calls' named point/cell
    arrays. It never resamples fields or associates them with another mesh.
    """
    if not species_paths:
        raise RuntimeError("per-species ViennaPS flux merge received no native VTP files")
    roots = [ET.parse(path).getroot() for path in species_paths]
    root_chunks = [_vtk_appended_chunks(root) for root in roots]
    first = roots[0]
    appended_payload = _vtk_appended_payload(first)
    if first.attrib.get("type") != "PolyData":
        raise RuntimeError("per-species ViennaPS flux output is not VTK PolyData")
    first_piece = first.find(".//Piece")
    if first_piece is None:
        raise RuntimeError("per-species ViennaPS flux output has no PolyData Piece")

    def geometry_signature(root: ET.Element, chunks: dict[ET.Element, str]) -> tuple[Any, ...]:
        piece = root.find(".//Piece")
        if root.attrib.get("type") != "PolyData" or piece is None:
            raise RuntimeError("per-species ViennaPS flux output is not valid PolyData")
        sections = []
        for name in ("Points", "Verts", "Lines", "Strips", "Polys"):
            node = piece.find(name)
            if node is None:
                sections.append((name, None))
            else:
                arrays = tuple((array.tag, _vtk_data_array_signature(array, chunks))
                               for array in node.findall("DataArray"))
                sections.append((name, (tuple(sorted(node.attrib.items())), arrays)))
        return (root.attrib.get("type"), tuple(sorted(piece.attrib.items())), tuple(sections))

    expected_geometry = geometry_signature(first, root_chunks[0])
    for root, chunks in zip(roots[1:], root_chunks[1:]):
        if geometry_signature(root, chunks) != expected_geometry:
            raise RuntimeError("per-species ViennaPS calculateFlux calls returned different mesh geometry or topology")

    for association in ("FieldData", "PointData", "CellData"):
        target = first_piece.find(association)
        if target is None:
            target = ET.SubElement(first_piece, association)
        names: dict[str, ET.Element] = {}
        for array in target.findall("DataArray"):
            name = array.attrib.get("Name")
            if not name or name in names:
                raise RuntimeError(f"native {association} contains a missing or duplicate DataArray name")
            names[name] = array
        for root, chunks in zip(roots[1:], root_chunks[1:]):
            piece = root.find(".//Piece")
            source = piece.find(association) if piece is not None else None
            if source is None:
                continue
            for array in source.findall("DataArray"):
                name = array.attrib.get("Name")
                if not name:
                    raise RuntimeError(f"native {association} contains a DataArray without a name")
                prior = names.get(name)
                if prior is not None:
                    if _vtk_data_array_signature(prior, root_chunks[0]) != _vtk_data_array_signature(array, chunks):
                        raise RuntimeError(f"per-species native flux arrays collide with different values: {association}.{name}")
                    continue
                copied = copy.deepcopy(array)
                if copied.attrib.get("format") == "appended":
                    segment = chunks.get(array)
                    if not segment:
                        raise RuntimeError(f"native {association}.{name} has no appended VTK data block")
                    copied.attrib["offset"] = str(len(appended_payload))
                    appended_payload += segment
                names[name] = copied
                target.append(copied)
    appended = first.find("AppendedData")
    if appended is not None:
        appended.text = "\n   _" + appended_payload + "\n  "
    elif appended_payload:
        raise RuntimeError("native VTP has appended DataArrays but no AppendedData element")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(first).write(output_path, encoding="utf-8", xml_declaration=True)


def merge_native_flux_reports(species_reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not species_reports:
        raise RuntimeError("per-species ViennaPS flux merge received no native mesh reports")
    base = species_reports[0]
    for report in species_reports[1:]:
        if report["meshIdentity"] != base["meshIdentity"]:
            raise RuntimeError("per-species ViennaPS calculateFlux calls returned different mesh identity")
    merged = {key: value for key, value in base.items()}
    for association in ("pointData", "cellData"):
        merged[association] = {kind: {} for kind in ("scalars", "vectors")}
        for report in species_reports:
            for kind in ("scalars", "vectors"):
                for name, values in report[association][kind].items():
                    prior = merged[association][kind].get(name)
                    if prior is not None and prior != values:
                        raise RuntimeError(f"per-species native flux arrays collide with different values: {association}.{name}")
                    merged[association][kind][name] = values
    merged["fields"] = []
    for association, arrays in (("pointData", merged["pointData"]), ("cellData", merged["cellData"])):
        for kind in ("scalars", "vectors"):
            for name, values in arrays[kind].items():
                merged["fields"].append({"name": name, "association": association,
                                         "components": 1 if kind == "scalars" else 3,
                                         "tuples": len(values), "nativeUnit": None,
                                         "unitStatus": "not-provided-by-native-mesh-interface"})
    return merged


def calculate_multi_particle_flux_by_species(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any],
                                             engine: str, seed: int, run_dir: Path) -> dict[str, Any]:
    """Run native calculateFlux once per particle, then merge only identical meshes."""
    if engine != "GPU_DISK":
        raise RuntimeError("per-species MultiParticle flux strategy is currently verified only for GPU_DISK")
    species_dir = run_dir / "flux-species"
    species_dir.mkdir(parents=True, exist_ok=False)
    reports: list[dict[str, Any]] = []
    species_records: list[dict[str, Any]] = []
    for index, species in enumerate(step["species"]):
        label = str(species["label"])
        species_step = multi_particle_species_step(step, species)
        model, multi_actual, callback_state = build_multi_particle_model(ps, species_step)
        process = ps.Process(domain, model, float(step.get("duration") or 0.0))
        process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
        rays, ray_actual = configure_rays(ps, project, int(seed))
        process.setParameters(rays)
        native_path = species_dir / f"species-{index:02d}.vtp"
        started = time.perf_counter()
        mesh = process.calculateFlux()
        elapsed = time.perf_counter() - started
        report = native_mesh_report(mesh)
        if not report["points"]:
            raise RuntimeError(f"ViennaPS calculateFlux returned an empty mesh for species {label!r}")
        for association in ("pointData", "cellData"):
            for kind in ("scalars", "vectors"):
                for field_name, values in report[association][kind].items():
                    if any(not math.isfinite(value) for row in values for value in (row if isinstance(row, list) else [row])):
                        raise RuntimeError(f"ViennaPS flux field {field_name!r} for {label!r} contains non-finite values")
        writer = ps.ls.VTKWriter(mesh, ps.ls.FileFormatEnum.VTP, str(native_path))
        writer.apply()
        if not native_path.is_file() or native_path.stat().st_size <= 0:
            raise RuntimeError(f"ViennaLS VTKWriter did not create native flux mesh for {label!r}")
        reports.append(report)
        species_records.append({"label": label, "kind": species["kind"], "actualSeed": int(seed),
                                "actualEngine": engine, "rayTracing": ray_actual,
                                "elapsedSeconds": elapsed, "meshIdentity": report["meshIdentity"],
                                "nativeVtk": native_path.relative_to(run_dir).as_posix(),
                                "fields": report["fields"],
                                "nativeModel": multi_actual,
                                "rateCallbackCount": int(callback_state["count"])})
    merged_report = merge_native_flux_reports(reports)
    return {"report": merged_report, "species": species_records,
            "speciesPaths": [species_dir / f"species-{index:02d}.vtp" for index in range(len(species_records))],
            "execution": {"strategy": GPU_MULTIPARTICLE_FLUX_STRATEGY,
                          "speciesOrder": [row["label"] for row in species_records],
                          "speciesCount": len(species_records),
                          "allNativeMeshesIdentical": True}}


def apply_gpu_multi_particle_split(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any],
                                   *, seed: int, run_dir: Path, intermediate_dir: Path,
                                   selected_gpu_device: dict[str, Any]) -> dict[str, Any]:
    """Apply a measured symmetric sequence of single-species GPU_DISK processes.

    ViennaPS 4.6.2's GPU binding drops ion species after the first. Each native
    sub-process here therefore receives exactly one species and its own rate
    column. This is a deterministic numerical approximation to the combined
    MultiParticleProcess and is gated by a versioned, exact-signature evidence
    row in backend.validation.
    """
    species = list(step["species"])
    if len(species) < 2 or sum(row["kind"] == "ion" for row in species) < 2:
        raise RuntimeError("GPU MultiParticle splitting requires at least two ion species")
    count = len(species)
    order = ([(index, 0.5) for index in range(count - 1)]
             + [(count - 1, 1.0)]
             + [(index, 0.5) for index in reversed(range(count - 1))])
    duration = float(step["duration"])
    base_seed = int(seed)
    material_callback_counts: dict[str, int] = {}
    callback_samples: list[dict[str, Any]] = []
    callbacks_by_species = {str(row["label"]): 0 for row in species}
    flux_sums_by_species = {str(row["label"]): 0.0 for row in species}
    flux_maxima_by_species = {str(row["label"]): 0.0 for row in species}
    stage_rows: list[dict[str, Any]] = []
    gpu_stage_rows: list[dict[str, Any]] = []
    total_apply_seconds = 0.0
    previous_cwd = Path.cwd()
    try:
        for sweep in range(GPU_MULTIPARTICLE_SPLIT_SWEEPS):
            for species_index, fraction in order:
                particle = species[species_index]
                label = str(particle["label"])
                stage_index = len(stage_rows)
                stage_seed = (base_seed + stage_index) & 0xFFFFFFFF
                stage_duration = duration * fraction / GPU_MULTIPARTICLE_SPLIT_SWEEPS
                native_step = multi_particle_species_step(step, particle)
                model, model_actual, callback_state = build_multi_particle_model(ps, native_step)
                process = ps.Process(domain, model, stage_duration)
                process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
                advection, advection_actual = configure_advection(ps, project)
                process.setParameters(advection)
                rays, ray_actual = configure_rays(ps, project, stage_seed)
                process.setParameters(rays)
                stage_dir = intermediate_dir / f"_gpu-split-stage-{stage_index:04d}"
                stage_dir.mkdir(parents=True, exist_ok=False)
                try:
                    os.chdir(stage_dir)
                    process.setIntermediateOutputPath(str(stage_dir))
                    started = time.perf_counter()
                    process.apply()
                    elapsed = time.perf_counter() - started
                finally:
                    os.chdir(previous_cwd)
                if callback_state["count"] <= 0:
                    raise RuntimeError(f"ViennaPS did not invoke the {label!r} split rate relation")
                expected_sign = -1 if step["operation"] == "etch" else 1
                for sample in callback_state["samples"]:
                    value = float(sample["returnedVelocity"])
                    if (expected_sign < 0 and value > 0) or (expected_sign > 0 and value < 0):
                        raise RuntimeError(f"{step['operation']} split callback returned the wrong native velocity sign")
                callbacks_by_species[label] += int(callback_state["count"])
                if len(callback_state["fluxSums"]) != 1 or len(callback_state["fluxMaxima"]) != 1:
                    raise RuntimeError("GPU MultiParticle split callback did not return one flux channel")
                flux_sums_by_species[label] += float(callback_state["fluxSums"][0])
                flux_maxima_by_species[label] = max(
                    flux_maxima_by_species[label], float(callback_state["fluxMaxima"][0]))
                for material_name, number in callback_state["materials"].items():
                    material_callback_counts[material_name] = material_callback_counts.get(material_name, 0) + int(number)
                callback_samples.extend({**row, "splitSpecies": label, "splitStage": stage_index}
                                        for row in callback_state["samples"][:max(0, 256 - len(callback_samples))])
                flattened_outputs = []
                for output_path in sorted(stage_dir.iterdir()):
                    if not output_path.is_file():
                        continue
                    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", output_path.name)
                    flattened = intermediate_dir / f"split-{stage_index:04d}-{label}-{safe_name}"
                    shutil.copyfile(output_path, flattened)
                    flattened_outputs.append(flattened.name)
                shutil.rmtree(stage_dir)
                total_apply_seconds += elapsed
                gpu_stage_rows.append({"stage": stage_index, "species": label,
                                       "engine": "GPU_DISK",
                                       "selectedDeviceUuid": selected_gpu_device["uuid"]})
                stage_rows.append({
                    "stage": stage_index, "sweep": sweep, "speciesIndex": species_index,
                    "species": label, "kind": particle["kind"], "durationFraction": fraction,
                    "durationSeconds": stage_duration, "seed": stage_seed, "engine": "GPU_DISK",
                    "gpuDeviceUuid": selected_gpu_device["uuid"], "processApplySeconds": elapsed,
                    "callbackCount": int(callback_state["count"]),
                    "callbackMaterials": callback_state["materials"],
                    "nativeModel": model_actual, "advection": advection_actual, "rays": ray_actual,
                    "intermediateFiles": flattened_outputs,
                })
        accumulated = {label: sum(row["durationSeconds"] for row in stage_rows if row["species"] == label)
                       for label in callbacks_by_species}
        if any(not math.isclose(value, duration, rel_tol=0.0, abs_tol=max(1e-12, duration * 1e-10))
               for value in accumulated.values()):
            raise RuntimeError("GPU MultiParticle split did not preserve the requested integrated duration per species")
    finally:
        os.chdir(previous_cwd)
    return {
        "elapsedSeconds": total_apply_seconds,
        "callbackState": {"count": sum(callbacks_by_species.values()),
                          "materials": material_callback_counts, "samples": callback_samples,
                          "fluxSums": [flux_sums_by_species[str(row["label"])] for row in species],
                          "fluxMaxima": [flux_maxima_by_species[str(row["label"])] for row in species],
                          "callbackCountBySpecies": callbacks_by_species},
        "gpuStageSelection": gpu_stage_rows,
        "execution": {"strategy": GPU_MULTIPARTICLE_SPLIT_STRATEGY,
                      "sweeps": GPU_MULTIPARTICLE_SPLIT_SWEEPS,
                      "composition": "symmetric Strang sequence: first n-1 species half-step, last species full-step, reverse first n-1 half-step",
                      "durationPolicy": "each species receives the requested step duration per sweep; total integrated species duration equals the requested duration",
                      "speciesOrder": [str(row["label"]) for row in species],
                      "nativeProcessCount": len(stage_rows),
                      "stageSeeds": [row["seed"] for row in stage_rows],
                      "stageDeviceUuids": sorted({row["gpuDeviceUuid"] for row in stage_rows}),
                      "stages": stage_rows,
                      "callbacksBySpecies": callbacks_by_species},
    }


def _per_layer_stack_geometry(ps: Any, domain: Any,
                              project: dict[str, Any]) -> dict[str, Any]:
    """Build a stack through the installed native ``MaterialLayer[]`` overload.

    ``MakeTrench`` clears the domain and lays the listed layers from the bottom
    up. The first non-mask layer is created as the substrate half-space, every
    later layer as a finite slab, and ``isMask`` cuts an opening through its
    own layer immediately after insertion. The Workbench substrate is one
    level set below ``baseHeight``, so it is represented by a native substrate
    layer of ``baseHeight + 1e-4`` (the constructor's own cutout epsilon).
    """
    g = project["geometry"]
    pattern = g["pattern"]
    stack = g["layers"]
    if g.get("gdsMask") is not None:
        raise RuntimeError("per-layer trench cannot be combined with a GDS mask overlay")
    if pattern.get("kind") != "trench":
        raise RuntimeError(
            "per-layer cutouts require a trench pattern; installed MakeHole has "
            "no MaterialLayer[] overload")
    entries = pattern.get("layers")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("per-layer trench requires a non-empty layers array")
    if len(entries) != len(stack):
        raise RuntimeError(
            "per-layer trench must reference every stack layer exactly once "
            "from bottom to top; omit no layer, including the bottom film")
    domain_config = project.get("domain") or {}
    grid_delta = strict_finite_number(domain_config.get("gridDelta"),
                                      "domain.gridDelta")
    x_extent = strict_finite_number(domain_config.get("xExtent"), "domain.xExtent")
    if grid_delta <= 0 or x_extent <= 0:
        raise RuntimeError("per-layer trench requires positive gridDelta and xExtent")
    base_height = strict_finite_number(g.get("baseHeight", 0.0),
                                       "geometry.baseHeight")
    if base_height < 0:
        raise RuntimeError("per-layer trench requires a non-negative baseHeight")
    if base_height / grid_delta > 1_000_000:
        raise RuntimeError("per-layer baseHeight exceeds the coordinate budget")

    by_id = {layer.get("id"): index for index, layer in enumerate(stack)}
    cutouts: dict[int, dict[str, Any]] = {}
    thicknesses: list[float] = []
    for position, (entry, layer) in enumerate(zip(entries, stack)):
        if not isinstance(entry, dict):
            raise RuntimeError(f"per-layer cutout {position} must be a JSON object")
        index = entry.get("layerIndex")
        layer_id = entry.get("layerId")
        if (index is None) == (layer_id is None):
            raise RuntimeError(
                f"per-layer cutout {position} must reference exactly one stack layer")
        if index is not None:
            index = strict_integer(index, f"pattern.layers[{position}].layerIndex")
        else:
            if not isinstance(layer_id, str) or layer_id not in by_id:
                raise RuntimeError(
                    f"per-layer cutout {position} references unknown stack layer "
                    f"{layer_id!r}")
            index = by_id[layer_id]
        if index != position:
            raise RuntimeError(
                "per-layer cutouts must be ordered from bottom to top and "
                "reference every stack layer exactly once")
        path = f"pattern.layers[{position}]"
        width = strict_finite_number(entry.get("width"), f"{path}.width")
        taper_angle = strict_finite_number(entry.get("taperAngle"),
                                           f"{path}.taperAngle")
        is_mask = strict_boolean(entry.get("isMask"), f"{path}.isMask")
        thickness = strict_finite_number(layer.get("thickness"),
                                         f"geometry.layers[{position}].thickness")
        if width <= 0:
            raise RuntimeError(f"{path}.width must be positive")
        if not -45.0 <= taper_angle <= 45.0:
            raise RuntimeError(f"{path}.taperAngle must be between -45 and 45 degrees")
        if thickness <= 0:
            raise RuntimeError(
                f"geometry.layers[{position}].thickness must be positive")
        change = 2.0 * thickness * math.tan(math.radians(taper_angle))
        narrow = width + min(0.0, change)
        wide = width + max(0.0, change)
        if narrow < 2.0 * grid_delta - 1e-12:
            raise RuntimeError(
                f"{path}.taperAngle closes the opening below two grid intervals")
        if wide >= x_extent:
            raise RuntimeError(
                f"{path} cutout and tapered opening must fit inside the lateral domain")
        if layer.get("source") == "gds-mask":
            raise RuntimeError("a GDS mask layer cannot carry a per-layer native cutout")
        thicknesses.append(thickness)
        cutouts[position] = {"width": width, "taperAngle": taper_angle,
                             "isMask": is_mask}
    if not cutouts[len(stack) - 1]["isMask"]:
        raise RuntimeError(
            "per-layer trench requires isMask=true on the top stack layer")

    epsilon = 1e-4
    records: list[dict[str, Any]] = []
    native_layers: list[Any] = []

    def append_layer(index: int | None, layer_id: str | None, name: str,
                     material: str, height: float, width: float,
                     taper_angle: float, is_mask: bool) -> None:
        native_layers.append(ps.MakeTrench.MaterialLayer(
            height=float(height), width=float(width),
            taperAngle=float(taper_angle),
            material=material_handle(ps, material), isMask=bool(is_mask)))
        records.append({"stackLayerIndex": index, "stackLayerId": layer_id,
                        "name": name, "material": material,
                        "height": float(height), "width": float(width),
                        "taperAngle": float(taper_angle), "isMask": bool(is_mask)})

    append_layer(None, None, "substrate", g["substrateMaterial"],
                 base_height + epsilon, 0.0, 0.0, False)
    for index, layer in enumerate(stack):
        entry = cutouts[index]
        append_layer(index, layer.get("id"), str(layer.get("name", layer.get("id", ""))),
                     layer["material"], thicknesses[index],
                     entry["width"], entry["taperAngle"], entry["isMask"])

    ps.MakeTrench(domain=domain, materialLayers=native_layers,
                  halfTrench=False).apply()
    return {
        "stack": True,
        "perLayerPattern": {
            "nativeConstructor":
                "viennaps.MakeTrench(domain, materialLayers=MaterialLayer[], halfTrench=False)",
            "nativeLayerType": "viennaps.MakeTrench.MaterialLayer",
            "nativeOrder": "bottom-to-top; first non-mask native layer is the substrate",
            "baseHeight": base_height,
            "substrateEpsilon": epsilon,
            "layers": records,
        },
    }


def _stack_geometry(ps: Any, domain: Any, project: dict[str, Any]) -> None:
    g=project["geometry"]; dim=project["dimension"]
    height=float(g.get("baseHeight",0.0))
    for index,layer in enumerate(g["layers"]):
        height += float(layer["thickness"])
        # A source-backed GDS mask is listed as a real top stack layer for
        # editing and material bookkeeping, but its 3D shape is inserted from
        # the selected GDS polygons below. Do not also create a full-area slab.
        if layer.get("source") == "gds-mask":
            continue
        ps.MakePlane(domain,height=height,material=material_handle(ps,layer["material"]),addToExisting=True).apply()
    pattern=g["pattern"]
    if pattern["kind"] in {"none", "gds"}: return
    top=g["layers"][-1]; top_thickness=float(top["thickness"])
    eps=float(project["domain"]["gridDelta"])*1e-3
    factory=ps.GeometryFactory(domain.getSetup(),"WorkbenchStackPattern")
    pos=[0.0]*dim
    pos[-1]=height-top_thickness-eps
    angle=-float(pattern.get("taperAngle",0.0))
    if pattern["kind"]=="trench":
        stencil=factory.makeBoxStencil(pos,float(pattern["width"]),top_thickness+2*eps,angle)
    else:
        stencil=factory.makeCylinderStencil(pos,float(pattern["radius"]),top_thickness+2*eps,angle)
    domain.applyBooleanOperation(stencil,ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,False)


def _load_gds_geometry(ps: Any, dimension: int, path: Path, delta: float,
                       lateral: Any, *, padding: float = 0.0,
                       blur: dict[str, Any] | None = None,
                       second_boundary: Any | None = None) -> Any:
    """Read one filtered GDS file with the requested native frame padding.

    Padding is applied through the installed `GDSGeometry.setBoundaryPadding`
    before the reader runs; it changes the native level-set frame, not the
    polygons, so a polygon that exactly fills the layout bounds gets a real
    inside/outside grid pair instead of an empty level set.
    """
    if dimension == 3:
        geometry = ps.d3.GDSGeometry(float(delta))
        geometry.setBoundaryConditions(
            [lateral, lateral, ps.BoundaryType.INFINITE_BOUNDARY])
    else:
        geometry = ps.d2.GDSGeometry(float(delta))
        geometry.setBoundaryConditions(
            [lateral, second_boundary if second_boundary is not None
             else ps.BoundaryType.INFINITE_BOUNDARY])
    if padding:
        geometry.setBoundaryPadding(float(padding), float(padding))
    apply_gds_blur(geometry, blur)
    if dimension == 3:
        ps.d3.GDSReader(geometry, str(path)).apply()
    else:
        ps.d2.GDSReader(geometry, str(path)).apply()
    return geometry


def _gds_planar_layers(ps: Any, path: Path, delta: float, lateral: Any,
                       layers: list[int], *, padding: float,
                       blur: dict[str, Any] | None,
                       three_d_source: bool = False) -> tuple[Any, dict[int, Any]]:
    """Read the native 2D polygon interiors used by both GDS body paths.

    A 3D body source uses the same reflective second boundary as the installed
    3D `layerToLevelSet` internal 2D read, so a non-empty planar result proves
    that the native 3D call cannot hit its empty-interior crash path.
    """
    geometry = _load_gds_geometry(
        ps, 2, path, delta, lateral, padding=padding, blur=blur,
        # The installed blur path cannot build its 2D contour grid on an
        # INFINITE second boundary (std::bad_alloc at delta=0.1 on a 4x4 layout).
        # The native blurred mask itself always uses the GDS boundary pair, so a
        # reflective second boundary is the compatible choice whenever blur is on.
        second_boundary=(lateral if (three_d_source or blur is not None) else None))
    if blur is not None:
        # Validation already enforces this from the asset; repeat it here so a
        # hand-written worker config cannot reach the native allocator.
        bounds = [float(value) for value in geometry.getBounds()]
        cells = blur_exposure_cell_count(
            (bounds[0], bounds[2], bounds[1], bounds[3]),
            grid_delta=float(delta), exposure_delta=float(blur["delta"]),
            grid_refine=int(blur["gridRefine"]), sigma_count=len(blur["sigmas"]))
        if cells > MAX_GDS_BLUR_CELLS:
            raise RuntimeError(
                f"GDS blur exposure grid would allocate {cells} cells; the limit is "
                f"{MAX_GDS_BLUR_CELLS}. Increase delta, reduce gridRefine/sigmas, "
                "or shrink the layout")
    planars = {int(layer): geometry.layerToLevelSet(int(layer), True) for layer in layers}
    return geometry, planars


def _regrid_planar_to_domain(ps: Any, domain: Any, planar: Any) -> Any:
    """Move a blurred 2D contour onto the simulation grid.

    The installed blur always builds its contour on the GDS boundary pair, so
    inserting it directly would replace the 2D domain's infinite second
    boundary with a reflective one. Regridding keeps the contour and the
    product's 2D `[lateral, INFINITE]` domain semantics.
    """
    mesh = ps.ls.Mesh()
    ps.ls.d2.ToSurfaceMesh(planar, mesh).apply()
    target = ps.ls.d2.Domain(domain.getGrid())
    reconstruction = ps.ls.d2.FromSurfaceMesh(target, mesh)
    reconstruction.setRemoveBoundaryTriangles(False)
    reconstruction.apply()
    return target


def _gds_missing_interior_error(layers: list[int], padding: float) -> RuntimeError:
    return RuntimeError(
        "GDS 图层 " + ", ".join(str(layer) for layer in layers)
        + " 在当前 GDS 边界内没有可用的多边形内部（多边形退化或小于网格间距），"
        "无法构造实体。请检查这些图层的多边形，或减小 gridDelta 后重试。"
        + (f"（已使用原生边界 padding={padding:g} um）" if padding else ""))


def _require_gds_bounds_inside_domain(dimension: int, geometry: Any,
                                      project: dict[str, Any], *,
                                      padding: float) -> list[float]:
    """Reject a layout that cannot be inserted into the simulation domain.

    ViennaPS 4.6.2 does not clip an inserted level set: a GDS body wider than
    the 2D/3D domain makes `insertNextLevelSetAsMaterial` crash the worker with
    SIGSEGV. Keep the native call, but fail with a diagnostic before it.
    """
    delta = float(project["domain"]["gridDelta"])
    tolerance = max(1e-7, delta * 1e-6)
    bounds = [float(value) for value in geometry.getBounds()]
    x_extent = float(project["domain"]["xExtent"])
    if dimension == 3:
        half_x = x_extent / 2.0
        half_y = float(project["domain"]["yExtent"]) / 2.0
        outside = (bounds[0] < -half_x - tolerance or bounds[1] > half_x + tolerance
                   or bounds[2] < -half_y - tolerance or bounds[3] > half_y + tolerance)
    else:
        # ViennaPS builds the 2D Domain centered, while Stage 5 GDS layouts are
        # authored in the absolute [0, xExtent] frame. A layout is acceptable
        # when it fits either frame entirely; anything outside both is still
        # rejected before the native insert can crash. The native bounds already
        # include any boundary padding, so no extra margin is subtracted here.
        absolute = (bounds[0] >= -tolerance and bounds[1] <= x_extent + tolerance)
        centered = (bounds[0] >= -x_extent / 2.0 - tolerance
                    and bounds[1] <= x_extent / 2.0 + tolerance)
        outside = not (absolute or centered)
    if outside:
        domain_text = (f"xExtent={x_extent:g}"
                       + ("；2D 允许绝对坐标 [0, xExtent] 或原生居中 [-xExtent/2, xExtent/2]"
                          if dimension == 2 else "")
                       + (f", yExtent={float(project['domain']['yExtent']):g}"
                          if dimension == 3 else ""))
        layout_text = (f"[{bounds[0]:g}, {bounds[1]:g}]"
                       + (f" x [{bounds[2]:g}, {bounds[3]:g}]" if dimension == 3 else ""))
        padding_text = (f"；全边界多边形需要 {padding:g} um 原生 padding，"
                        "请扩大计算域" if padding else "")
        raise RuntimeError(
            f"GDS 版图边界 {layout_text} 超出当前计算域（{domain_text}）。"
            "ViennaPS 4.6.2 插入超出计算域的 GDS level set 会直接崩溃，"
            f"请扩大计算域或移动版图后重试{padding_text}。")
    return bounds


def _apply_gds_stack_mask(ps: Any, domain: Any, project: dict[str, Any], *,
                          config: dict[str, Any] | None, run_dir: Path | None) -> dict[str, Any]:
    """Insert one selected GDS layer/datatype as a mask over a 3D film stack."""
    from backend.gds import parse_gds
    mask = project["geometry"]["gdsMask"]
    if config is None or run_dir is None:
        raise RuntimeError("GDS mask transfer requires an isolated run input and artifact directory")
    source = Path(config.get("gdsAssetPath", "")).resolve()
    try:
        source.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("GDS mask asset copy must remain inside the current run directory") from exc
    if not source.is_file() or file_sha256(source) != mask["assetId"]:
        raise RuntimeError("GDS mask asset is missing or does not match its SHA-256 reference")
    library = parse_gds(source.read_bytes())
    physical_scale = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
    effective_scale = physical_scale * float(mask["unitScale"])
    selected_bytes, transform = library.select_and_transform(
        mask["structure"], unit_scale=effective_scale, position_um=mask["position"][:2],
        selected_layers=[int(mask["layer"])],
        selected_layer_datatypes=[(int(mask["layer"]), int(mask["datatype"]))])
    filtered = run_dir / "gds-mask-selected.gds"
    filtered.write_bytes(selected_bytes)
    # Build the selected layout as a 2D material region first, then extrude it
    # through the requested mask thickness along z.  GDSGeometry's native 3D
    # layerToLevelSet is bounded by the current Domain top surface, so a mask
    # positioned above an existing film stack silently disappears.  The 2D
    # conversion plus the shared finite-body construction preserves XY polygons
    # and allows a real finite mask volume above the stack.
    delta = float(project["domain"]["gridDelta"])
    lateral = ps.BoundaryType.REFLECTIVE_BOUNDARY
    geometry, planars = _gds_planar_layers(
        ps, filtered, delta, lateral, [int(mask["layer"])], padding=0.0, blur=None,
        three_d_source=True)
    layers = sorted(int(value) for value in geometry.getAllLayers())
    if int(mask["layer"]) not in layers:
        raise RuntimeError("selected GDS layer was not read by ViennaPS")
    base_height = float(mask["baseHeight"]) + float(mask["position"][2])
    planar_inside = planars[int(mask["layer"])]
    planar_padding = 0.0
    if planar_point_count(planar_inside) == 0:
        # A polygon that exactly fills the filtered layout bounds has no
        # inside/outside pair; one cell of native boundary padding recovers its
        # real interior instead of inventing geometry.
        planar_padding = delta
        geometry, planars = _gds_planar_layers(
            ps, filtered, delta, lateral, [int(mask["layer"])],
            padding=planar_padding, blur=None, three_d_source=True)
        planar_inside = planars[int(mask["layer"])]
        if planar_point_count(planar_inside) == 0:
            raise _gds_missing_interior_error([int(mask["layer"])], planar_padding)
    gds3 = _load_gds_geometry(ps, 3, filtered, delta, lateral,
                              padding=planar_padding, blur=None)
    native_body = native_layer_body(
        gds3, int(mask["layer"]), base_height=base_height,
        height=float(mask["height"]), mask_tone=False, blur_layer=True)
    aligned_mask, diagnostics = build_aligned_gds_mask(
        ps, domain, planar_inside, base_height=base_height,
        height=float(mask["height"]), retained_region=mask["retainedRegion"],
        layer=int(mask["layer"]), native_body=native_body)
    diagnostics["planarGridPadding"] = planar_padding
    diagnostics["nativeBodyUsed"] = native_body is not None
    # The regridded mask is a complete finite body on the simulation grid, so it
    # is unioned into the existing top level set (Si ∪ SiO₂ ∪ Mask) instead of
    # replacing it.
    domain.insertNextLevelSetAsMaterial(aligned_mask, material_handle(ps, mask["material"]), True)
    transform.update({"selectedLayers": [int(mask["layer"])],
                      "selectedLayerDatatypes": [[int(mask["layer"]), int(mask["datatype"])]],
                      "selectedNativeLayers": [int(mask["layer"])],
                      "dimension": 3, "layer": int(mask["layer"]), "datatype": int(mask["datatype"]),
                      "retainedRegion": mask["retainedRegion"], "protectsEtch": bool(mask["protectsEtch"]),
                      "baseHeight": base_height, "height": float(mask["height"]),
                      "topHeight": base_height + float(mask["height"]),
                      "extrusionAxis": 2,
                      "material": mask["material"], "filteredFile": filtered.name,
                      **diagnostics})
    atomic_json(run_dir / "gds-mask-transform.json", transform)
    native_geometry = ("viennaps.GDSReader + GDSGeometry.layerToLevelSet(3D inside body when available)"
                       " + viennals.Extrude fallback for empty native bodies"
                       " + viennals.BooleanOperation(RELATIVE_COMPLEMENT for outside)"
                       " + viennals.ToSurfaceMesh + viennals.FromSurfaceMesh"
                       " + Domain.insertNextLevelSetAsMaterial(UNION)"
                       f" [{NATIVE_REGRID_CONSTRUCTION} / {GDS_MASK_CONSTRUCTION}]")
    return {"nativeGeometry": native_geometry, **transform}


def _halve_x_axis(domain: Any) -> None:
    """Apply the native half-geometry setup before a Boolean construction.

    Native MakeFin/MakeTrench call ``getSetup().halveXAxis()`` before
    ``apply()`` rebuilds the domain. An already-allocated Domain needs the
    halved setup re-applied, otherwise the grid keeps its full x bounds and the
    flag is silently lost.
    """
    setup = domain.getSetup()
    setup.halveXAxis()
    domain.setup(setup)


def _boolean_fin_2d(ps: Any, domain: Any, geometry: dict[str, Any], material: Any,
                    mask_material: Any) -> None:
    # Use the explicit 2D binding: when a 3D project is set up for extrusion,
    # the top-level ps.MakePlane alias resolves to d3 and rejects a d2 domain.
    ps.d2.MakePlane(domain, height=0.0, material=material, addToExisting=False).apply()
    factory = ps.d2.GeometryFactory(domain.getSetup(), "WorkbenchBooleanFin")
    stencil = factory.makeBoxStencil(
        [0.0, 0.0], float(geometry["width"]), float(geometry["height"]),
        -float(geometry["taperAngle"]),
    )
    domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.UNION, False)
    mask_height = float(geometry.get("maskHeight", 0.0))
    if mask_height > 0:
        mask = factory.makeBoxStencil(
            [0.0, float(geometry["height"])],
            float(geometry["width"]), mask_height, -float(geometry["maskTaperAngle"]),
        )
        domain.insertNextLevelSetAsMaterial(mask, mask_material)


def _boolean_fin_3d(ps: Any, domain: Any, geometry: dict[str, Any], material: Any,
                    mask_material: Any, y_center: float, y_extent: float) -> None:
    ps.d3.MakePlane(domain, height=0.0, material=material, addToExisting=False).apply()
    factory = ps.d3.GeometryFactory(domain.getSetup(), "WorkbenchBooleanFin")
    fin_stencil = factory.makeBoxStencil(
        [0.0, y_center, 0.0], 2.0 * float(geometry["width"]),
        float(geometry["height"]), -float(geometry["taperAngle"]), y_extent,
    )
    domain.applyBooleanOperation(fin_stencil, ps.ls.BooleanOperationEnum.UNION, False)
    mask_height = float(geometry.get("maskHeight", 0.0))
    if mask_height > 0:
        mask = factory.makeBoxStencil(
            [0.0, y_center, float(geometry["height"])],
            2.0 * float(geometry["width"]), mask_height,
            -float(geometry["maskTaperAngle"]), y_extent,
        )
        domain.insertNextLevelSetAsMaterial(mask, mask_material)


def make_v3_geometry(ps: Any, domain: Any, project: dict[str,Any], *, config: dict[str,Any] | None = None,
                     run_dir: Path | None = None) -> dict[str,Any] | None:
    g=project["geometry"]
    if g["kind"]=="stack":
        if g["pattern"].get("layers") is not None:
            # The native multi-layer constructor rebuilds the whole domain;
            # adding the substrate plane first would be discarded.
            return _per_layer_stack_geometry(ps, domain, project)
        # The bulk substrate is the bottom half-space; the first plane marks
        # its top interface, and subsequent planes add requested films.
        ps.MakePlane(domain,height=float(g.get("baseHeight",0.0)),material=material_handle(ps,g["substrateMaterial"]),addToExisting=False).apply()
        _stack_geometry(ps,domain,project)
        mask_readback = _apply_gds_stack_mask(ps, domain, project, config=config, run_dir=run_dir) if g.get("gdsMask") else None
        return {"stack": True, "gdsMask": mask_readback} if mask_readback else None
    if g["kind"]=="fin":
        construction_decision = fin_construction_decision(project)
        construction = construction_decision["effective"]
        requested_construction = construction_decision["requested"]
        fallback_models = construction_decision["fallbackModels"]
        fallback = requested_construction != construction
        extrusion_route = construction_decision["extrusionRoute"]
        extrusion_fallback_models = construction_decision["extrusionFallbackModels"]
        half_fin = bool(g.get("halfFin"))
        args = {"finWidth": float(g["width"]), "finHeight": float(g["height"]),
                "finTaperAngle": float(g["taperAngle"]), "maskHeight": float(g["maskHeight"]),
                "maskTaperAngle": float(g["maskTaperAngle"]), "halfFin": half_fin,
                "material": material_handle(ps,g["substrateMaterial"]),
                "maskMaterial": material_handle(ps,g["maskMaterial"])}
        if project["dimension"] == 2:
            if construction == "boolean":
                # The native constructor halves inside MakeFin; the
                # independently verified Boolean route must apply the same
                # setup change explicitly so halfFin is never silently lost.
                if half_fin:
                    _halve_x_axis(domain)
                _boolean_fin_2d(ps, domain, g, args["material"], args["maskMaterial"])
            else:
                ps.d2.MakeFin(domain, **args).apply()
            return {"nativeGeometry": ("viennaps.MakePlane + viennaps.d2.GeometryFactory + Domain.applyBooleanOperation"
                                       if construction == "boolean" else "viennaps.d2.MakeFin"),
                    "construction": construction,
                    "requestedConstruction": requested_construction,
                    "automaticFallback": fallback,
                    "fallbackModels": fallback_models,
                    "constructionFallbackModels": construction_decision["constructionFallbackModels"],
                    "constructionFallbackReason": construction_decision["constructionFallbackReason"],
                    "extrusionRoute": extrusion_route,
                    "automaticExtrusionFallback": False,
                    "extrusionFallbackModels": extrusion_fallback_models,
                    "arguments": {key:value for key,value in args.items() if key not in ("material", "maskMaterial")},
                    "substrateMaterial":g["substrateMaterial"],"maskMaterial":g["maskMaterial"]}
        if g.get("extrusion") is not None and extrusion_route == "direct-stencil":
            extent = [float(value) for value in g["extrusion"]["extent"]]
            y_extent = extent[1] - extent[0]
            if y_extent <= 0:
                raise RuntimeError("Fin extrusion y extent must have positive length")
            if half_fin:
                _halve_x_axis(domain)
            _boolean_fin_3d(ps, domain, g, args["material"], args["maskMaterial"],
                            0.5 * (extent[0] + extent[1]), y_extent)
            return {"nativeGeometry": "viennaps.MakePlane + viennaps.d3.GeometryFactory + Domain.applyBooleanOperation (extrusion extent stencil)",
                    "construction": "boolean", "requestedConstruction": requested_construction,
                    "automaticFallback": fallback, "fallbackModels": fallback_models,
                    "constructionFallbackModels": construction_decision["constructionFallbackModels"],
                    "constructionFallbackReason": construction_decision["constructionFallbackReason"],
                    "extrusionRoute": extrusion_route, "automaticExtrusionFallback": True,
                    "extrusionFallbackModels": extrusion_fallback_models,
                    "finArguments": {key:value for key,value in args.items() if key not in ("material", "maskMaterial")},
                    "extrusionExtent": g["extrusion"]["extent"], "extrusionAxis": 1,
                    "substrateMaterial": g["substrateMaterial"], "maskMaterial": g["maskMaterial"],
                    "boundaryReadback": [str(value) for value in domain.getBoundaryConditions()]}
        if g.get("extrusion") is not None:
            bc = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"]["boundaryXY"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
            input_domain = ps.d2.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]), bc)
            if construction == "boolean":
                if half_fin:
                    _halve_x_axis(input_domain)
                _boolean_fin_2d(ps, input_domain, g, args["material"], args["maskMaterial"])
            else:
                ps.d2.MakeFin(input_domain, **args).apply()
            extrude = ps.Extrude()
            extrude.setInputDomain(input_domain)
            extrude.setOutputDomain(domain)
            extrude.setExtent([float(value) for value in g["extrusion"]["extent"]])
            extrude.setExtrusionAxis(1)
            extrude.setBoundaryConditions([bc, bc, ps.BoundaryType.INFINITE_BOUNDARY])
            extrude.apply()
            return {"nativeGeometry":("viennaps.MakePlane + viennaps.d2.GeometryFactory + Domain.applyBooleanOperation + viennaps.Extrude"
                                       if construction == "boolean" else "viennaps.d2.MakeFin + viennaps.Extrude"),
                    "construction":construction,
                    "requestedConstruction": requested_construction,
                    "automaticFallback": fallback,
                    "fallbackModels": fallback_models,
                    "constructionFallbackModels": construction_decision["constructionFallbackModels"],
                    "constructionFallbackReason": construction_decision["constructionFallbackReason"],
                    "extrusionRoute": extrusion_route,
                    "automaticExtrusionFallback": False,
                    "extrusionFallbackModels": extrusion_fallback_models,
                    "finArguments":{key:value for key,value in args.items() if key not in ("material", "maskMaterial")},
                    "extrusionExtent":g["extrusion"]["extent"],"extrusionAxis":1,
                    "substrateMaterial":g["substrateMaterial"],"maskMaterial":g["maskMaterial"],
                    "boundaryReadback":[str(value) for value in domain.getBoundaryConditions()]}
        if construction == "boolean":
            if half_fin:
                _halve_x_axis(domain)
            _boolean_fin_3d(ps, domain, g, args["material"], args["maskMaterial"],
                            0.0, float(project["domain"]["yExtent"]))
        else:
            ps.d3.MakeFin(domain, **args).apply()
        return {"nativeGeometry":("viennaps.MakePlane + viennaps.d3.GeometryFactory + Domain.applyBooleanOperation"
                                   if construction == "boolean" else "viennaps.d3.MakeFin"),
                "construction":construction,
                "requestedConstruction": requested_construction,
                "automaticFallback": fallback,
                "fallbackModels": fallback_models,
                "constructionFallbackModels": construction_decision["constructionFallbackModels"],
                "constructionFallbackReason": construction_decision["constructionFallbackReason"],
                "extrusionRoute": extrusion_route,
                "automaticExtrusionFallback": False,
                "extrusionFallbackModels": extrusion_fallback_models,
                "arguments":{key:value for key,value in args.items() if key not in ("material", "maskMaterial")},
                "substrateMaterial":g["substrateMaterial"],"maskMaterial":g["maskMaterial"]}
    if g["kind"]=="gds":
        if config is None or run_dir is None:
            raise RuntimeError("GDS geometry requires an isolated run input and artifact directory")
        from backend.gds import add_periodic_extent_support, parse_gds
        source = Path(config.get("gdsAssetPath", "")).resolve()
        try:
            source.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise RuntimeError("GDS asset copy must remain inside the current run directory") from exc
        if not source.is_file() or file_sha256(source) != g["assetId"]:
            raise RuntimeError("GDS source asset is missing or does not match the project's SHA-256")
        library = parse_gds(source.read_bytes())
        selected_layers = [int(row["layer"]) for row in g["layers"]]
        physical_scale = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
        effective_scale = physical_scale * float(g["unitScale"])
        selected_bytes, transform = library.select_and_transform(
            g["structure"], unit_scale=effective_scale, position_um=g["position"][:2],
            selected_layers=selected_layers)
        support_metadata: dict[str, Any] = {}
        boundary_name = project["domain"]["xBoundary"] if project["dimension"] == 2 else project["domain"]["boundaryXY"]
        if boundary_name == "periodic":
            delta = float(project["domain"]["gridDelta"])
            x_extent = float(project["domain"]["xExtent"])
            edge = ps.BoundaryType.PERIODIC_BOUNDARY
            if project["dimension"] == 2:
                probe_geometry = ps.d2.GDSGeometry(delta)
                probe_geometry.setBoundaryConditions([edge, ps.BoundaryType.INFINITE_BOUNDARY])
                probe_path = run_dir / "gds-periodic-bounds-probe.gds"
                probe_path.write_bytes(selected_bytes)
                ps.d2.GDSReader(probe_geometry, str(probe_path)).apply()
                source_bounds = [float(value) for value in probe_geometry.getBounds()]
                tolerance = max(1e-8, delta * 1e-6)
                if source_bounds[0] < -x_extent / 2.0 - tolerance or source_bounds[1] > x_extent / 2.0 + tolerance:
                    raise RuntimeError("periodic GDS geometry extends outside the requested x domain; enlarge the domain or adjust GDS placement")
                del probe_geometry
                selected_bytes, support_metadata = add_periodic_extent_support(
                    selected_bytes, g["structure"], dimension=2, x_extent_um=x_extent,
                    cross_section_y_bounds_um=(source_bounds[2], source_bounds[3]), grid_delta_um=delta)
                probe_path.unlink(missing_ok=True)
            else:
                y_extent = float(project["domain"]["yExtent"])
                probe_geometry = ps.d3.GDSGeometry(delta)
                probe_geometry.setBoundaryConditions([edge, edge, ps.BoundaryType.INFINITE_BOUNDARY])
                probe_path = run_dir / "gds-periodic-bounds-probe.gds"
                probe_path.write_bytes(selected_bytes)
                ps.d3.GDSReader(probe_geometry, str(probe_path)).apply()
                source_bounds = [float(value) for value in probe_geometry.getBounds()]
                tolerance = max(1e-8, delta * 1e-6)
                if (source_bounds[0] < -x_extent / 2.0 - tolerance
                        or source_bounds[1] > x_extent / 2.0 + tolerance
                        or source_bounds[2] < -y_extent / 2.0 - tolerance
                        or source_bounds[3] > y_extent / 2.0 + tolerance):
                    raise RuntimeError("periodic GDS geometry extends outside the requested XY domain; enlarge the domain or adjust GDS placement")
                del probe_geometry
                selected_bytes, support_metadata = add_periodic_extent_support(
                    selected_bytes, g["structure"], dimension=3, x_extent_um=x_extent,
                    y_extent_um=y_extent, grid_delta_um=delta)
                probe_path.unlink(missing_ok=True)
        selected_path = run_dir / "gds-selected.gds"
        selected_path.write_bytes(selected_bytes)
        if project["dimension"] == 2:
            delta = float(project["domain"]["gridDelta"])
            lateral = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"]["xBoundary"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
            blur_spec = resolve_blur(g.get("blur"), delta)
            if blur_spec is not None and lateral == ps.BoundaryType.PERIODIC_BOUNDARY:
                raise RuntimeError(
                    "GDS blur requires a reflective lateral boundary; periodic GDS layouts "
                    "are sized to the full domain and cannot carry native blur padding")
            # Native blur needs a real outside frame; without padding its
            # exposure-grid sampling reads past the layout frame.
            planar_padding = 2.0 * delta if blur_spec is not None else 0.0
            geometry, planars = _gds_planar_layers(
                ps, selected_path, delta, lateral, [int(row["layer"]) for row in g["layers"]],
                padding=planar_padding, blur=blur_spec)
            actual_layers = sorted(int(value) for value in geometry.getAllLayers())
            selected_native_layers = sorted(set(actual_layers) & set(selected_layers))
            if selected_native_layers != sorted(selected_layers):
                raise RuntimeError(f"native GDS layer readback {selected_native_layers} differs from selected layers {sorted(selected_layers)}")
            empty_layers = sorted(layer for layer, planar in planars.items()
                                  if planar_point_count(planar) == 0)
            if empty_layers and planar_padding == 0.0:
                # A polygon that fills the GDS bounds has no zero crossing, so
                # its interior reads back empty. One cell of native frame
                # padding makes the native rasterizer report the real interior.
                planar_padding = delta
                geometry, planars = _gds_planar_layers(
                    ps, selected_path, delta, lateral,
                    [int(row["layer"]) for row in g["layers"]],
                    padding=planar_padding, blur=blur_spec)
                empty_layers = sorted(layer for layer, planar in planars.items()
                                      if planar_point_count(planar) == 0)
            if empty_layers:
                raise _gds_missing_interior_error(empty_layers, planar_padding)
            native_bounds = _require_gds_bounds_inside_domain(
                2, geometry, project, padding=planar_padding)
            bounds_readback = [float(value) for value in geometry.getBounds()]
            if blur_spec is not None:
                # Native blur contour -> simulation grid. Inserting the raw
                # reflective-second-boundary contour would rewrite the 2D
                # domain's infinite h boundary.
                planars = {layer: _regrid_planar_to_domain(ps, domain, planar)
                           for layer, planar in planars.items()}
                empty_layers = sorted(layer for layer, planar in planars.items()
                                      if planar_point_count(planar) == 0)
                if empty_layers:
                    raise _gds_missing_interior_error(empty_layers, planar_padding)
            tolerance = max(1e-7, float(support_metadata.get("coordinateToleranceUm", 0.0)) * 1.1)
            if support_metadata and planar_padding == 0.0 and (
                    abs(native_bounds[0] + float(project["domain"]["xExtent"]) / 2.0) > tolerance
                    or abs(native_bounds[1] - float(project["domain"]["xExtent"]) / 2.0) > tolerance):
                raise RuntimeError("native periodic 2D GDS bounds do not match the requested x period")
            for mapping in g["layers"]:
                level_set = planars[int(mapping["layer"])]
                # All GDS layers share this native grid; cumulative wrapping keeps
                # LS_n = LS_(n-1) union layer_n instead of replacing lower layers.
                domain.insertNextLevelSetAsMaterial(level_set, material_handle(ps, mapping["material"]), True)
            blur_readback = None
            if blur_spec is not None:
                blur_readback = {**blur_spec, "nativeMethod": "GDSGeometry.addBlur",
                                 "blurLayer": True, "blurred": True,
                                 "planarGridPadding": planar_padding,
                                 "regriddedToDomain": True,
                                 "semantics": "GDS proximity blur + threshold contour; not a resist/OPC model"}
            layer_constructions = None
        else:
            delta = float(project["domain"]["gridDelta"])
            lateral = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"]["boundaryXY"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
            blur_spec = resolve_blur(g.get("blur"), delta)
            if blur_spec is not None and lateral == ps.BoundaryType.PERIODIC_BOUNDARY:
                raise RuntimeError(
                    "GDS blur requires a reflective lateral boundary; periodic GDS layouts "
                    "are sized to the full domain and cannot carry native blur padding")
            planar_padding = 2.0 * delta if blur_spec is not None else 0.0
            layers = [int(row["layer"]) for row in g["layers"]]
            geometry, planars = _gds_planar_layers(
                ps, selected_path, delta, lateral, layers,
                padding=planar_padding, blur=blur_spec, three_d_source=True)
            native_geometry = _load_gds_geometry(
                ps, 3, selected_path, delta, lateral,
                padding=planar_padding, blur=blur_spec)
            actual_layers = sorted(int(value) for value in native_geometry.getAllLayers())
            selected_native_layers = sorted(set(actual_layers) & set(selected_layers))
            if selected_native_layers != sorted(selected_layers):
                raise RuntimeError(f"native GDS layer readback {selected_native_layers} differs from selected layers {sorted(selected_layers)}")
            empty_layers = sorted(layer for layer, planar in planars.items()
                                  if planar_point_count(planar) == 0)
            if empty_layers and planar_padding == 0.0:
                # A polygon that fills the GDS bounds is clipped to the library
                # frame, so the native rasterizer finds no inside/outside pair
                # and returns an empty level set; extruding that field crashes
                # ViennaLS.  Native boundary padding makes the rasterizer report
                # the polygon's real interior, so no geometry is invented.
                planar_padding = delta
                geometry, planars = _gds_planar_layers(
                    ps, selected_path, delta, lateral, layers,
                    padding=planar_padding, blur=blur_spec, three_d_source=True)
                native_geometry = _load_gds_geometry(
                    ps, 3, selected_path, delta, lateral,
                    padding=planar_padding, blur=blur_spec)
                empty_layers = sorted(layer for layer, planar in planars.items()
                                      if planar_point_count(planar) == 0)
            if empty_layers:
                raise _gds_missing_interior_error(empty_layers, planar_padding)
            native_bounds = _require_gds_bounds_inside_domain(
                3, native_geometry, project, padding=planar_padding)
            bounds_readback = [float(value) for value in native_geometry.getBounds()]
            tolerance = max(1e-7, float(support_metadata.get("coordinateToleranceUm", 0.0)) * 1.1)
            if support_metadata and planar_padding == 0.0 and (
                    abs(native_bounds[0] + float(project["domain"]["xExtent"]) / 2.0) > tolerance
                    or abs(native_bounds[1] - float(project["domain"]["xExtent"]) / 2.0) > tolerance
                    or abs(native_bounds[2] + float(project["domain"]["yExtent"]) / 2.0) > tolerance
                    or abs(native_bounds[3] - float(project["domain"]["yExtent"]) / 2.0) > tolerance):
                raise RuntimeError("native periodic 3D GDS bounds do not match the requested XY periods")
            layer_constructions = []
            for mapping in g["layers"]:
                layer = int(mapping["layer"])
                base_height = float(mapping["baseHeight"]) + float(g["position"][2])
                mask_tone = bool(mapping.get("mask", False))
                # Prefer the installed native 3D body. ViennaPS 4.6.2 returns a
                # deterministic empty level set for some valid baseHeight/height
                # pairs, so a measured empty result falls back to the native 2D
                # read plus ViennaLS extrusion/caps on the same GDS grid.
                native_body = native_layer_body(
                    native_geometry, layer, base_height=base_height,
                    height=float(mapping["height"]), mask_tone=mask_tone,
                    blur_layer=True)
                aligned, diagnostics = build_aligned_gds_layer(
                    ps, domain, planars[layer], base_height=base_height,
                    height=float(mapping["height"]), mask_tone=mask_tone,
                    layer=layer, native_body=native_body)
                domain.insertNextLevelSetAsMaterial(
                    aligned, material_handle(ps, mapping["material"]), True)
                item = {
                    "layer": layer, "material": mapping["material"],
                    "baseHeight": base_height, "height": float(mapping["height"]),
                    "maskTone": mask_tone, "planarGridPadding": planar_padding,
                    "nativeBodyUsed": native_body is not None, **diagnostics}
                if blur_spec is not None:
                    item["blur"] = {**blur_spec, "nativeMethod": "GDSGeometry.addBlur",
                                    "blurLayer": True, "applied": True}
                layer_constructions.append(item)
            blur_readback = None
            if blur_spec is not None:
                blur_readback = {**blur_spec, "nativeMethod": "GDSGeometry.addBlur",
                                 "blurLayer": True, "blurred": True,
                                 "planarGridPadding": planar_padding,
                                 "semantics": "GDS proximity blur + threshold contour; not a resist/OPC model",
                                 "nativeBodiesUsed": sum(row["nativeBodyUsed"]
                                                         for row in layer_constructions)}
        transform.update({"sourceAssetId":g["assetId"],"selectedStructure":g["structure"],
                          "selectedLayers":selected_layers,"nativeLayers":actual_layers,
                          "selectedNativeLayers":selected_native_layers,
                          **support_metadata,
                          **({"layerConstructions": layer_constructions,
                              "construction": (NATIVE_CONSTRUCTION
                                               if all(row.get("nativeBodyUsed") for row in layer_constructions)
                                               else GDS_MASK_CONSTRUCTION)}
                             if project["dimension"] == 3 else {}),
                          "supportLayers":sorted(set(actual_layers) - set(selected_layers)),
                          "planarGridPadding":planar_padding,
                          **({"blur": blur_readback} if blur_readback is not None else {}),
                          "nativeUserUnitUm":physical_scale,"userUnitScaleAdjustment":float(g["unitScale"]),
                          "nativeBounds":bounds_readback,
                          "dimension":project["dimension"],
                          "coordinateMapping":"GDS XY -> Workbench x,h in 2D; GDS XY -> x,y and mapped layer heights -> z in 3D",
                          "layerMappings":g["layers"],"filteredFile":"gds-selected.gds",
                          "filteredFileSha256":hashlib.sha256(selected_bytes).hexdigest()})
        atomic_json(run_dir / "gds-transform.json", transform)
        native_geometry = ("viennaps.GDSReader + GDSGeometry.layerToLevelSet"
                           + (" + GDSGeometry.setBoundaryPadding" if planar_padding else "")
                           + (" + GDSGeometry.addBlur + viennals.d2.ToSurfaceMesh"
                              " + viennals.d2.FromSurfaceMesh(domain grid)"
                              if blur_readback is not None else "")
                           if project["dimension"] == 2 else
                           "viennaps.GDSReader + GDSGeometry.layerToLevelSet(3D when non-empty)"
                           " + viennaps.d2.GDSReader + GDSGeometry.layerToLevelSet"
                           " + viennals.Extrude/BooleanOperation fallback"
                           " + Domain.insertNextLevelSetAsMaterial(UNION)"
                           f" [{NATIVE_CONSTRUCTION} / {GDS_MASK_CONSTRUCTION}]")
        return {"nativeGeometry": native_geometry, **transform}
    if g["kind"]=="plane":
        ps.MakePlane(domain,height=float(g["height"]),material=material_handle(ps,g["substrateMaterial"]),addToExisting=False).apply()
        return {"nativeGeometry":"viennaps.MakePlane",
                "arguments":{"height":float(g["height"])},
                "substrateMaterial":g["substrateMaterial"]}
    elif g["kind"]=="trench":
        half_trench = bool(g.get("halfTrench"))
        ps.MakeTrench(domain=domain,trenchWidth=float(g["width"]),trenchDepth=float(g["depth"]),trenchTaperAngle=float(g["taperAngle"]),maskHeight=float(g["maskHeight"]),maskTaperAngle=float(g["maskTaperAngle"]),halfTrench=half_trench,material=material_handle(ps,g["substrateMaterial"]),maskMaterial=material_handle(ps,g.get("maskMaterial","Mask"))).apply()
        return {"nativeGeometry":"viennaps.MakeTrench",
                "arguments":{"width":float(g["width"]),"depth":float(g["depth"]),
                             "taperAngle":float(g["taperAngle"]),"maskHeight":float(g["maskHeight"]),
                             "maskTaperAngle":float(g["maskTaperAngle"]),
                             "halfTrench":half_trench},
                "requestedHalfTrench":g.get("halfTrench"),
                "actualHalfTrench":half_trench,
                "substrateMaterial":g["substrateMaterial"],"maskMaterial":g.get("maskMaterial","Mask")}
    else:
        hole_shape = str(g.get("holeShape") or "FULL")
        if hole_shape not in ("FULL", "HALF", "QUARTER"):
            raise RuntimeError(f"unsupported native HoleShape {hole_shape!r}")
        ps.MakeHole(domain=domain,holeRadius=float(g["radius"]),holeDepth=float(g["depth"]),holeTaperAngle=float(g["taperAngle"]),maskHeight=float(g["maskHeight"]),maskTaperAngle=float(g["maskTaperAngle"]),holeShape=getattr(ps.HoleShape,hole_shape),material=material_handle(ps,g["substrateMaterial"]),maskMaterial=material_handle(ps,g.get("maskMaterial","Mask"))).apply()
        return {"nativeGeometry":"viennaps.MakeHole",
                "arguments":{"radius":float(g["radius"]),"depth":float(g["depth"]),
                             "taperAngle":float(g["taperAngle"]),"maskHeight":float(g["maskHeight"]),
                             "maskTaperAngle":float(g["maskTaperAngle"]),
                             "holeShape":hole_shape},
                "requestedHoleShape":g.get("holeShape"),
                "actualHoleShape":hole_shape,
                "substrateMaterial":g["substrateMaterial"],"maskMaterial":g.get("maskMaterial","Mask")}
    return None


def material_surface_domain(ps: Any, domain: Any, requested: dict[str, Any]) -> tuple[Any, list[int]]:
    """Build a render-only domain with adjacent equal-material interfaces merged.

    Keep the simulation domain and its full material map intact. When several
    consecutive level sets have the same material, only the newest surface is
    a physical material boundary; older surfaces are internal to that material.
    """
    levelsets = domain.getLevelSets()
    material_ids = material_map(domain)
    if len(levelsets) != len(material_ids):
        raise RuntimeError("ViennaPS level-set count does not match its material map")

    if requested["dimension"] == 3:
        from mesh3d import make_domain
        render_domain = make_domain(ps, requested)
    else:
        render_domain = _make_render_domain_2d(ps, requested)

    for index, material_id in enumerate(material_ids):
        if index + 1 < len(material_ids) and material_id == material_ids[index + 1]:
            continue
        render_domain.insertNextLevelSetAsMaterial(levelsets[index], ps.Material(material_id), False)
    return render_domain, material_ids


def _make_render_domain_2d(ps, requested):
    boundary = (
        ps.BoundaryType.REFLECTIVE_BOUNDARY
        if requested["domain"]["xBoundary"] == "reflective"
        else ps.BoundaryType.PERIODIC_BOUNDARY
    )
    render_domain = ps.Domain(
        float(requested["domain"]["gridDelta"]),
        float(requested["domain"]["xExtent"]),
        boundary,
    )

    return render_domain


def save_snapshot(
    ps: Any,
    domain: Any,
    run_dir: Path,
    *,
    run_id: str,
    step_id: str,
    step_name: str,
    step_index: int,
    requested: dict[str, Any],
    no_op: bool = False,
    extra: dict[str, Any] | None = None,
    extra_outputs: list[dict[str, Any]] | None = None,
    defer_payload: bool = False,
    step: dict[str, Any] | None = None,
    previous_tops: dict[str, Any] | None = None,
    recorded_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract the surface once and persist one snapshot JSON per step.

    ``extra`` fields are merged into the snapshot payload before the single
    atomic write, and ``extra_outputs`` rows are appended to ``outputs``.
    Callers must not re-read and re-serialise the file afterwards: the old
    write -> json.loads -> write cycle cost a full copy of a multi-megabyte
    snapshot per step for no change other than one added key.

    ``step``/``previous_tops`` add the measured add/remove record for the step
    from the same surface mesh; ``recorded_change`` re-applies a record that
    was already written, so deferred snapshot materialisation stays
    byte-identical to the eager write.
    """
    v3 = requested["schemaVersion"] >= 3
    if v3:
        levelsets = domain.getLevelSets()
        material_ids = material_map(domain)
        if any(level_set.getNumberOfPoints() == 0 for level_set in levelsets):
            # A material can be fully removed by an earlier boolean/process.
            # The native multi-surface mesher rejects empty level sets, so
            # render only the surviving interfaces and keep their material IDs.
            if requested["dimension"] == 3:
                from mesh3d import make_domain
                render_domain = make_domain(ps, requested)
            else:
                render_domain = _make_render_domain_2d(ps, requested)
            levelset_ids = []
            for level_set, material_id in zip(levelsets, material_ids):
                if level_set.getNumberOfPoints() == 0:
                    continue
                render_domain.insertNextLevelSetAsMaterial(
                    level_set, ps.Material(material_id), False)
                levelset_ids.append(material_id)
            if not levelset_ids:
                raise RuntimeError("cannot render a snapshot with no non-empty level sets")
        else:
            render_domain, levelset_ids = domain, material_ids
    else:
        render_domain, levelset_ids = material_surface_domain(ps, domain, requested)
    is3d = requested["dimension"] == 3
    mesh = render_domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    if is3d:
        from mesh3d import mesh_report as mesh_report_3d, measure_initial, bounds, readback
    report = mesh_report_3d(mesh) if is3d else mesh_report(mesh)
    material_tops = measure_material_tops(report, is3d)
    measured_change: dict[str, Any] = {}
    if recorded_change is not None:
        measured_change = dict(recorded_change)
    elif step is not None:
        measured_change = measure_step_change(step, previous_tops, material_tops,
                                              float(requested["domain"]["gridDelta"])) or {}
    measured_geometry = None
    if step_id == "initial" and requested["geometry"]["kind"] in ("plane", "trench", "hole"):
        if is3d:
            measured_geometry = measure_initial(report, requested["geometry"], requested["domain"]["gridDelta"], MATERIAL_IDS[requested["geometry"]["substrateMaterial"]] if v3 else 10)
        else:
            measured_geometry = measure_initial_geometry(report, requested["geometry"], requested["domain"]["gridDelta"])
    if not is3d and requested["geometry"]["kind"] == "trench" and step_id == "initial":
        if not requested["geometry"].get("halfTrench") and not trench_has_two_walls(report, requested["domain"]["gridDelta"], MATERIAL_IDS[requested["geometry"]["substrateMaterial"]] if v3 else 10):
            raise RuntimeError("initial trench contour is incomplete: both positive and negative Si sidewalls are required")
    vtk_name = f"{step_id}.vtk"
    snapshot_name = f"{step_id}.json"
    output_settings = requested.get("outputSettings") or {}
    surface_vtk_enabled = bool(output_settings.get("surfaceMeshVtk", False))
    if surface_vtk_enabled:
        # The surface mesh was already extracted above. Writing it through the
        # level-set writer avoids a second ToSurfaceMesh pass; the bytes are
        # identical to Domain.saveSurfaceMesh (same writer, same arguments).
        ps.ls.VTKWriter(mesh, str(run_dir / vtk_name)).apply()
    output_rows: list[dict[str, Any]] = list(extra_outputs or [])
    artifact_dir = run_dir / "artifacts"
    cell_set_settings = output_settings.get("cellSet")
    cell_set_enabled = isinstance(cell_set_settings, dict) and bool(cell_set_settings.get("enabled"))
    if (any(output_settings.get(key, False) for key in ("levelSets", "levelSetMeshes", "volumeMesh", "nativeMetadata"))
            or cell_set_enabled):
        artifact_dir.mkdir(parents=True, exist_ok=True)
    if output_settings.get("levelSets"):
        prefix = artifact_dir / f"{step_id}-levelsets.vtk"
        render_domain.saveLevelSets(str(prefix))
        paths = sorted(artifact_dir.glob(prefix.name + "_layer*.lvst"), key=_levelset_index)
        if len(paths) != len(levelset_ids):
            raise RuntimeError(f"ViennaPS saveLevelSets produced {len(paths)} files for {len(levelset_ids)} level sets")
        for index, path in enumerate(paths):
            output_rows.append(output_artifact(path, run_dir, run_id, step_id, "levelSet", "VIENNALS_LVST",
                                               levelSetIndex=index, materialId=levelset_ids[index],
                                               gridDelta=float(domain.getGridDelta()), association="native level-set grid"))
    if output_settings.get("levelSetMeshes"):
        prefix = artifact_dir / f"{step_id}-levelset-mesh.vtk"
        # ViennaPS 4.6.2 Domain.saveLevelSetMesh() calls getLevelSetMesh(),
        # which expands the stored level sets in place. Export from a deep
        # copy so optional visualization output cannot change the Domain that
        # is subsequently persisted as .vpsd.
        mesh_export_domain = ps.Domain(render_domain)
        mesh_export_domain.saveLevelSetMesh(str(prefix), 1)
        paths = sorted(artifact_dir.glob(prefix.name + "_layer*.vtp"), key=_levelset_index)
        if len(paths) != len(levelset_ids):
            raise RuntimeError(f"ViennaPS saveLevelSetMesh produced {len(paths)} files for {len(levelset_ids)} level sets")
        for index, path in enumerate(paths):
            output_rows.append(output_artifact(path, run_dir, run_id, step_id, "levelSetMesh", "VTP",
                                               levelSetIndex=index, materialId=levelset_ids[index],
                                               association="native level-set mesh; not the surface mesh"))
    if output_settings.get("volumeMesh"):
        if not is3d:
            raise RuntimeError("native volume-mesh output is only available for 3D domains")
        prefix = artifact_dir / f"{step_id}-volume.vtk"
        volume_export_domain = ps.Domain(render_domain)
        volume_delta = float(output_settings.get("volumeMeshDelta") or 0.01)
        volume_export_domain.saveVolumeMesh(str(prefix), volume_delta)
        path = artifact_dir / f"{prefix.name}_volume.vtu"
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("ViennaPS saveVolumeMesh did not produce its native VTU file")
        output_rows.append(output_artifact(path, run_dir, run_id, step_id, "volumeMesh", "VTU",
                                           association="native volume mesh", dimension=3,
                                           wrappingLayerEpsilon=volume_delta))
    # Keep the native surface snapshot for the viewer. A full VTU is written
    # only when explicitly requested above; never create one just to derive a
    # second, closed material surface for the ordinary surface view.
    display_points = report["points"]
    display_triangles = report["triangles"] if is3d else report["lines"]
    display_material_ids = [int(value) for value in report["cellData"]["MaterialIds"]]
    if surface_vtk_enabled:
        output_rows.insert(0, output_artifact(
            run_dir / vtk_name, run_dir, run_id, step_id, "surfaceMesh", "VTK_LEGACY",
            dimension=requested["dimension"], topology="triangles" if is3d else "lines",
            pointCount=len(display_points), cellCount=len(display_triangles),
            association="native domain surface mesh",
        ))
    if output_settings.get("nativeMetadata"):
        native_metadata = domain.getMetaData()
        if any(not isinstance(name, str) or any(not math.isfinite(float(value)) for value in values)
               for name, values in native_metadata.items()):
            raise RuntimeError("ViennaPS Domain metadata contains an invalid name or non-finite value")
        metadata_path = artifact_dir / f"{step_id}-domain-metadata.json"
        atomic_json(metadata_path, {
            "schemaVersion": 1,
            "source": "viennaps.Domain.getMetaData",
            "metadataLevel": str(domain.getMetaDataLevel()),
            "nativeUnit": None,
            "association": "domain metadata; native binding declares no point/cell association",
            "values": native_metadata,
            "dimension": int(requested["dimension"]),
            "gridDelta": float(domain.getGridDelta()),
            "materialIdsByLevelSetIndex": levelset_ids,
        })
        output_rows.append(output_artifact(metadata_path, run_dir, run_id, step_id, "nativeMetadata", "JSON",
                                           association="domain metadata", nativeUnit=None,
                                           metadataLevel=str(domain.getMetaDataLevel())))
    if cell_set_enabled:
        surface_top, surface_bottom = _surface_vertical_bounds(report.get("points") or [], is3d=is3d)
        output_rows.extend(write_cell_set_outputs(
            ps, render_domain, run_dir, run_id, step_id, requested,
            cell_set_settings, surface_top=surface_top, surface_bottom=surface_bottom,
            is3d=is3d))
    check_artifact_budget(run_dir)
    ids = display_material_ids
    if is3d:
        surface_bounds = bounds(display_points)
    elif report["points"]:
        surface_bounds = [
            [min(float(point[0]) for point in report["points"]), min(float(point[1]) for point in report["points"]), 0.0],
            [max(float(point[0]) for point in report["points"]), max(float(point[1]) for point in report["points"]), 0.0],
        ]
    else:
        surface_bounds = None
    materials: dict[str, dict[str, Any]] = {}
    for index, material_id in enumerate(levelset_ids):
        key = str(material_id)
        row = {"name": MATERIAL_NAMES.get(material_id, "unknown"), "levelSetIndexes": [], "sourceStepId": None}
        if v3: row["color"] = MATERIAL_COLORS[material_id]
        materials.setdefault(key, row)
        materials[key]["levelSetIndexes"].append(index)
    for material_id in sorted(set(ids)):
        key = str(material_id)
        if key not in materials:
            row = {"name": MATERIAL_NAMES.get(material_id, "unknown"), "levelSetIndexes": [], "sourceStepId": None}
            if v3 and material_id in MATERIAL_COLORS:
                row["color"] = MATERIAL_COLORS[material_id]
            materials[key] = row
    cells_key = "triangles" if is3d else "lines"
    count_key = "triangleCount" if is3d else "lineCount"
    snapshot = {
        "schemaVersion": requested["schemaVersion"],
        "runId": run_id,
        "stepId": step_id,
        "stepName": step_name,
        "stepIndex": step_index,
        "axisMap": {"x": 0, "y": 1, "z": 2} if is3d else {"x": 0, "h": 1, "z": 2},
        "coordinateUnit": str((requested.get("units") or {}).get("length") or "um"),
        "points": display_points,
        cells_key: display_triangles,
        "cellData": {"materialIds": ids},
        "materials": materials,
        "mesh": {**({"vtk": vtk_name} if surface_vtk_enabled else {}),
                 "pointCount": len(display_points), count_key: len(display_triangles)},
        "gridDelta": float(domain.getGridDelta()),
        "materialIdsByLevelSetIndex": levelset_ids,
        "sameMaterialInternalInterfacesHidden": False if v3 else True,
        "outputs": output_rows,
        "noOp": no_op,
        "measuredGeometry": measured_geometry,
        "materialTops": material_tops,
        **measured_change,
    }
    if is3d:
        snapshot.update(dimension=3,topology="triangles",surfaceBounds=surface_bounds,domainReadback=readback(domain,requested,report))
        snapshot["materialSurfaceStatus"] = "unavailable"
        substrate_name = requested["geometry"].get("substrateMaterial")
        substrate_id = MATERIAL_IDS.get(substrate_name) if isinstance(substrate_name, str) else None
        if substrate_id is not None:
            snapshot["substrateMaterialId"] = substrate_id
        snapshot["mesh"]["topology"]="triangles"
    elif v3:
        snapshot.update(dimension=2, topology="lines", surfaceBounds=surface_bounds)
        snapshot["mesh"]["topology"]="lines"
    if v3 and requested["geometry"]["kind"]=="stack":
        snapshot["stackReadback"]={"requestedBaseHeight":float(requested["geometry"].get("baseHeight",0.0)),
                                   "requestedLayers":requested["geometry"]["layers"],
                                   "nativeMaterialMap":levelset_ids,
                                   "pattern":requested["geometry"]["pattern"]}
    if extra:
        snapshot.update(extra)
    metadata = {**({"dimension":3,"topology":"triangles"} if is3d else {}), "stepId": step_id, "name": step_name, "stepIndex": step_index, "snapshot": snapshot_name,
                **({"vtk": vtk_name} if surface_vtk_enabled else {}), "outputs": output_rows,
                "pointCount": len(display_points), count_key: len(display_triangles), "materialIds": sorted(set(ids)),
                "materialIdsByLevelSetIndex": levelset_ids, "noOp": no_op, "measuredGeometry": measured_geometry,
                "surfaceBounds": surface_bounds, "materialTops": material_tops, **measured_change, **(extra or {})}
    if defer_payload:
        # Keep the run's critical path free of the multi-megabyte JSON encode
        # and write. The document is regenerated from the step's .vpsd state
        # the first time a consumer asks for it (backend.snapshots).
        deferred = {"schemaVersion": 1, "deferred": True, "metadata": metadata}
        # .vpsd carries geometry but not process metadata. Persist the small
        # metadata map so materialisation can reproduce level-set VTP FieldData
        # and the nativeMetadata artifact byte-for-byte.
        if output_settings.get("nativeMetadata"):
            deferred["nativeDomainMetadata"] = {
                "level": str(domain.getMetaDataLevel()),
                "values": {key: list(value) for key, value in domain.getMetaData().items()},
            }
        atomic_json(run_dir / f"{step_id}.meta.json", deferred)
    else:
        atomic_json(run_dir / snapshot_name, snapshot)
    check_artifact_budget(run_dir)
    return metadata


def _event_path(step: dict[str, Any]) -> str:
    return str(step["id"])


def _gds_boolean_operand(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any], *,
                         config: dict[str, Any] | None, run_dir: Path | None) -> tuple[Any, dict[str, Any]]:
    """Build one GDS layer/datatype as a real 3D boolean operand.

    The operand is the selected polygon interior extruded through the requested
    z range and regridded onto the simulation grid by the same production helper
    used for stack masks. The project's geometry asset is re-read in place; no
    new upload path exists, and the placement/unit scale of the geometry is
    composed with the step's offset so a zero step position is aligned.

    Every failure is explicit: a missing asset, a layer/datatype that is absent
    or open, an empty mask and an empty boolean result all raise instead of
    degrading to a box operand or a no-op.
    """
    from backend.gds import parse_gds
    mask = step["gdsMask"]
    geometry = project["geometry"]
    if config is None or run_dir is None:
        raise RuntimeError("GDS boolean requires an isolated run input and artifact directory")
    source = Path(config.get("gdsAssetPath", "")).resolve()
    try:
        source.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("GDS boolean asset copy must remain inside the current run directory") from exc
    if not source.is_file() or file_sha256(source) != geometry["assetId"]:
        raise RuntimeError("GDS boolean asset is missing or does not match the geometry's SHA-256 reference")
    library = parse_gds(source.read_bytes())
    structure = str(geometry["structure"])
    pair = (int(mask["layer"]), int(mask["datatype"]))
    if pair not in set(library.effective_layer_datatypes(structure)):
        raise RuntimeError(f"GDS boolean layer/datatype {pair[0]}/{pair[1]} is not present in structure {structure!r}")
    if pair not in set(library.effective_mask_layer_datatypes(structure)):
        raise RuntimeError(f"GDS boolean layer/datatype {pair[0]}/{pair[1]} contains no closed boundary polygons")
    delta = float(project["domain"]["gridDelta"])
    physical_scale = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
    unit_scale = float(geometry["unitScale"]) * float(mask["unitScale"])
    position = [float(geometry["position"][0]) + float(mask["position"][0]),
                float(geometry["position"][1]) + float(mask["position"][1])]
    selected_bytes, transform = library.select_and_transform(
        structure, unit_scale=physical_scale * unit_scale, position_um=position,
        selected_layers=[pair[0]], selected_layer_datatypes=[pair])
    filtered = run_dir / f"gds-boolean-{step['id']}-selected.gds"
    filtered.write_bytes(selected_bytes)
    reflective = ps.BoundaryType.REFLECTIVE_BOUNDARY
    planar_geometry, planars = _gds_planar_layers(
        ps, filtered, delta, reflective, [pair[0]], padding=0.0, blur=None,
        three_d_source=True)
    if pair[0] not in {int(value) for value in planar_geometry.getAllLayers()}:
        raise RuntimeError(f"GDS boolean layer/datatype {pair[0]}/{pair[1]} was not read by ViennaPS")
    planar = planars[pair[0]]
    planar_padding = 0.0
    if planar_point_count(planar) == 0:
        # Same native frame defect as the geometry builder: a polygon that fills
        # the filtered GDS bounds has no inside/outside pair. Native boundary
        # padding recovers its real interior instead of inventing geometry.
        planar_padding = delta
        planar_geometry, planars = _gds_planar_layers(
            ps, filtered, delta, reflective, [pair[0]],
            padding=planar_padding, blur=None, three_d_source=True)
        planar = planars[pair[0]]
        if planar_point_count(planar) == 0:
            raise _gds_missing_interior_error([pair[0]], planar_padding)
    # The step's baseHeight is relative to the geometry frame, so the geometry's
    # z placement is added as well; this keeps a zero step position aligned with
    # the mapped 3D layers.
    base_height = float(mask["baseHeight"]) + float(mask["position"][2]) + float(geometry["position"][2])
    height = float(mask["height"])
    gds3 = _load_gds_geometry(ps, 3, filtered, delta, reflective,
                              padding=planar_padding, blur=None)
    native_body = native_layer_body(
        gds3, pair[0], base_height=base_height, height=height,
        mask_tone=False, blur_layer=True)
    operand, diagnostics = build_aligned_gds_mask(
        ps, domain, planar, base_height=base_height, height=height,
        retained_region=str(mask["retainedRegion"]), layer=pair[0],
        native_body=native_body)
    diagnostics["planarGridPadding"] = planar_padding
    diagnostics["nativeBodyUsed"] = native_body is not None
    transform.update({
        "sourceAssetId": geometry["assetId"], "selectedStructure": structure,
        "selectedLayers": [pair[0]], "selectedLayerDatatypes": [[pair[0], pair[1]]],
        "selectedNativeLayers": [pair[0]], "dimension": 3,
        "layer": pair[0], "datatype": pair[1],
        "requestedLayerDatatype": [int(mask["layer"]), int(mask["datatype"])],
        "unitScale": unit_scale, "geometryUnitScale": float(geometry["unitScale"]),
        "stepUnitScale": float(mask["unitScale"]),
        "stepPositionUm": [float(value) for value in mask["position"]],
        "composedPositionUm": position, "actualPositionUm": transform.get("actualPositionUm"),
        "geometryZOffset": float(geometry["position"][2]),
        "retainedRegion": str(mask["retainedRegion"]),
        "baseHeight": base_height,
        "requestedBaseHeight": float(mask["baseHeight"]),
        "height": height, "topHeight": base_height + height,
        "extrusionAxis": 2, "planarGridPadding": planar_padding,
        "filteredFile": filtered.name,
        "filteredFileSha256": hashlib.sha256(selected_bytes).hexdigest(),
        **diagnostics,
    })
    atomic_json(run_dir / f"gds-boolean-{step['id']}.json", transform)
    return operand, transform


def surface_state(domain: Any, dimension: int) -> dict[str, Any]:
    """Return surface bounds plus an exact digest of the exposed surface mesh.

    A particle-shape step is a no-op only when the surface really did not
    change. Axis-aligned bounds are not enough: a masked sphere etch on a
    patterned stack moved the opening floor while the protected mask top and
    the substrate pinned every axis bound, so the bounds stayed identical and a
    bounds-only check rejected a real etch (measured 48 -> 50 surface nodes in
    2D and 816 -> 850 in 3D). The digest therefore covers every node
    coordinate, the line/triangle topology, the per-cell material IDs and the
    domain material map; an unchanged surface hashes identically, any changed
    node, cell, material or level-set material mapping does not.
    """
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = mesh.getNodes()
    if not points:
        raise RuntimeError("the native surface mesh is empty; the particle-shape offset removed every point")
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    mapping = domain.getMaterialMap()
    material_map = [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]
    cell_data = mesh.getCellData()
    labels: list[str] = []
    material_ids: list[int] = []
    for index in range(cell_data.getScalarDataSize()):
        label = str(cell_data.getScalarDataLabel(index))
        labels.append(label)
        if label == "MaterialIds":
            material_ids = [int(value) for value in cell_data.getScalarData(index)]
    digest = hashlib.sha256()
    digest.update(struct.pack("<I", int(dimension)))
    digest.update(struct.pack("<Q", len(points)))
    for point in points:
        # ``+ 0.0`` folds -0.0 into 0.0 so an unchanged coordinate cannot flip
        # the digest through a signed zero.
        digest.update(struct.pack("<" + "d" * dimension,
                                  *(float(point[axis]) + 0.0 for axis in range(dimension))))
    digest.update(struct.pack("<Q", len(cells)))
    for cell in cells:
        digest.update(struct.pack("<Q", len(cell)))
        for node in cell:
            digest.update(struct.pack("<Q", int(node)))
    digest.update(struct.pack("<Q", len(material_ids)))
    for value in material_ids:
        digest.update(struct.pack("<q", value))
    digest.update(struct.pack("<Q", len(material_map)))
    for value in material_map:
        digest.update(struct.pack("<q", value))
    digest.update("\x00".join(labels).encode("utf-8"))
    return {
        "bounds": [[float(min(point[axis] for point in points)),
                    float(max(point[axis] for point in points))]
                   for axis in range(dimension)],
        "pointCount": len(points),
        "cellCount": len(cells),
        "digest": digest.hexdigest(),
    }


def apply_particle_shape(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """Run the native Box/Sphere geometric-advection distribution.

    Measured 4.6.2/4.7.0 behaviour (one bounded subprocess per case, see
    docs/particle-shape-distribution-contract.md):

    * negative (etch) displacement plus ``addMaskMaterial`` is the officially
      supported combination: ``GeometricProcessStrategy`` builds the mask from
      the material level set exactly like the native FinFET example, and the
      protected material does not move;
    * positive (deposit) displacement plus any non-empty mask SIGSEGVs in 2D
      and 3D, regardless of the mask construction or whether the mask overlaps
      the surface. ViennaLS 5.8.5/5.10.0 even carries the comment "There are
      still issues with positive box distributions if there is a mask used!".

    The step therefore applies the project's native mask materials for etches
    and records the mask it really passed. A deposition is always constructed
    with the no-mask overload and records the omitted mask materials and the
    crash-backed reason instead of pretending the mask was honoured.
    """
    dimension = int(project["dimension"])
    grid_delta = float(project["domain"]["gridDelta"])
    shape = str(step["shape"])
    sign = -1.0 if step["operation"] == "etch" else 1.0
    deposit_handle = None
    if sign > 0.0 and step.get("depositMaterial") is not None:
        deposit_handle = material_handle(ps, str(step["depositMaterial"]))
        domain.duplicateTopLevelSet(deposit_handle)
    if step.get("maskMaterials") is not None:
        if step.get("maskMode") == "allExcept":
            excluded = set(str(name) for name in step["maskMaterials"])
            registry = ps.MaterialRegistry.instance()
            requested_masks = []
            for handle in domain.getMaterialsInDomain():
                name = registry.getName(handle)
                if name not in excluded and name not in requested_masks:
                    requested_masks.append(name)
        else:
            requested_masks = [str(name) for name in step["maskMaterials"]]
    else:
        requested_masks = protected_material_names(project, step)
    applied_masks: list[tuple[str, Any]] = []
    omitted_masks: list[str] = []
    if sign > 0.0:
        omitted_masks = list(requested_masks)
    else:
        for name in requested_masks:
            handle = material_handle(ps, name)
            material_level_set = domain.getMaterialLevelSet(handle)
            if material_level_set is None or int(material_level_set.getNumberOfPoints()) == 0:
                # The geometry declared the material but the domain does not
                # carry it (e.g. maskHeight 0). Passing it would make the
                # native strategy log "not found" and silently stay maskless.
                omitted_masks.append(name)
                continue
            applied_masks.append((name, handle))
    requested: dict[str, Any]
    if shape == "box":
        axes = [float(value) for value in step["halfAxes"]]
        if len(axes) != dimension:
            raise RuntimeError(f"particleShape box needs {dimension} half-axes, got {axes}")
        for axis in axes:
            if not math.isfinite(axis) or axis <= 0.0 or axis < grid_delta - 1e-12:
                raise RuntimeError(
                    f"particleShape half-axis {axis} must be finite and at least one grid interval "
                    f"({grid_delta} um); native geometric advection is unsafe below that")
        signed = [sign * value for value in axes]
        # The native binding always takes a fixed-size 3 array; 2D consumes
        # only the first two entries, so the third stays outside the project
        # surface and cannot silently select a 3D shape.
        native_half_axes = signed + [0.0] if dimension == 2 else signed
        model = ps.BoxDistribution(native_half_axes)
        requested = {"halfAxes": axes}
        native_class = "BoxDistribution"
        expected_offset = max(axes)
    else:
        radius = float(step["radius"])
        if not math.isfinite(radius) or radius <= 0.0 or radius < grid_delta - 1e-12:
            raise RuntimeError(
                f"particleShape radius {radius} must be finite and at least one grid interval "
                f"({grid_delta} um); native geometric advection is unsafe below that")
        signed_radius = sign * radius
        model = ps.SphereDistribution(signed_radius)
        requested = {"radius": radius}
        native_class = "SphereDistribution"
        native_half_axes = None
        expected_offset = radius
    before_state = surface_state(domain, dimension)
    before = before_state["bounds"]
    counts_before = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
    for _name, handle in applied_masks:
        model.addMaskMaterial(handle)
    # The geometric process strategy ignores the duration entirely; measure it
    # as zero so the run record never implies a rate. The 4.6.2/4.7.0 Process
    # default is AUTO, which resolves and initialises a flux engine even for a
    # pure geometric distribution (`cuInit`); pin CPU_DISK like the other
    # non-particle steps so a CPU-only run never touches CUDA.
    process = ps.Process(domain, model, 0.0)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    after_state = surface_state(domain, dimension)
    after = after_state["bounds"]
    if before_state["digest"] == after_state["digest"]:
        raise RuntimeError(
            "native particle-shape distribution left the surface unchanged; refusing to record a "
            "silent no-op (check that the magnitude exceeds the grid resolution"
            + (f", and that the mask {[name for name, _ in applied_masks]} does not cover the whole "
               "exposed surface" if applied_masks else "") + ")")
    mapping = domain.getMaterialMap()
    return {
        "geometryOperation": "particleShape",
        "nativeClass": native_class,
        "nativeConstructor": (f"viennaps.d{dimension}.{native_class}("
                              + ("halfAxes" if shape == "box" else "radius") + ")"),
        "nativeMaskConstructor": ("addMaskMaterial" if applied_masks else None),
        "dimension": dimension,
        "operation": step["operation"],
        "shape": shape,
        **requested,
        **({"nativeSignedHalfAxesUm": signed} if shape == "box" else {"nativeSignedRadiusUm": signed_radius}),
        **({"nativeHalfAxesUm": native_half_axes} if shape == "box" else {}),
        "expectedOffsetUm": expected_offset,
        "surfaceBoundsBeforeUm": before,
        "surfaceBoundsAfterUm": after,
        "surfaceSignatureBefore": before_state["digest"],
        "surfaceSignatureAfter": after_state["digest"],
        "surfacePointCountBefore": before_state["pointCount"],
        "surfacePointCountAfter": after_state["pointCount"],
        "surfaceCellCountBefore": before_state["cellCount"],
        "surfaceCellCountAfter": after_state["cellCount"],
        "nativeProcessBackend": "CPU_DISK pinned; geometric distribution has no stochastic flux engine",
        "levelSetPointCountsBefore": counts_before,
        "levelSetPointCountsAfter": [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()],
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
        "gridDeltaUm": grid_delta,
        **({"depositMaterial": step["depositMaterial"],
            "depositMaterialId": int(deposit_handle) if deposit_handle is not None else None,
            "duplicateTopLevelSet": deposit_handle is not None}
           if step.get("depositMaterial") is not None else {}),
        "nativeMask": {
            "mode": ("allExcept" if step.get("maskMode") == "allExcept" else "material")
            if applied_masks or step.get("maskMaterials") is not None else "none",
            "applied": bool(applied_masks),
            "materials": [name for name, _ in applied_masks],
            "requestedMaterials": list(requested_masks),
            "maskMode": step.get("maskMode") or "explicit",
            "omittedMaterials": omitted_masks,
            "reason": (
                "the etch protects the project's native mask materials through addMaskMaterial; "
                "GeometricProcessStrategy builds the mask from their material level sets"
                if applied_masks else
                "ViennaPS 4.6.2/4.7.0 SIGSEGV on every non-empty mask with positive geometric "
                "advection (2D and 3D, material and raw viennals masks, overlapping or far away), so "
                "a deposition is always constructed with the no-mask overload and every exposed "
                "surface, including the mask material, grows"
                if sign > 0.0 else
                "the etch declares mask materials but none of them is present in the domain, so the "
                "native no-mask overload is used"
                if omitted_masks else
                "the project geometry declares no native mask material, so the no-mask overload is used"
            ),
        },
        "nativeProcessDurationSeconds": 0.0,
        "nativeDurationIgnored": ("the process duration is ignored: GeometricProcessStrategy calls "
                                  "GeometricAdvect without consulting it, so the displacement equals the "
                                  "radius/half-axes"),
        "scopedSurface": ("the top/exposed level set only; deposition grows the top level-set material and "
                          "etch intersects every lower level set with the new surface"),
    }


def apply_geometry_operation(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any],
                             previous_tops: dict[str, Any] | None = None, *,
                             config: dict[str, Any] | None = None,
                             run_dir: Path | None = None) -> dict[str, Any]:
    model = step["model"]
    if model == "particleShape":
        return apply_particle_shape(ps, domain, project, step)
    if model == "removeMaterial":
        material = material_handle(ps, step["material"])
        domain.removeMaterial(material)
        return {"geometryOperation": "removeMaterial", "material": step["material"], "materialId": MATERIAL_IDS[step["material"]]}
    if model == "planarize":
        stop_material = step.get("stopMaterial")
        if stop_material is None:
            cutoff = float(step["cutoffHeight"])
            ps.Planarize(domain, cutoff).apply()
            return {"geometryOperation": "planarize", "cutoffHeight": cutoff}
        # Material-aware CMP: the installed ViennaPS 4.6.2 Planarize only
        # accepts a height, so the stop level comes from the exposed top of the
        # stop material measured on the previous step's surface mesh. The
        # native call then removes every level set above that height; it is
        # still one global cut, not lateral selectivity.
        stop = (previous_tops or {}).get(stop_material) or {}
        stop_top = stop.get("p90TopHeight")
        stop_method = STOP_HEIGHT_METHOD
        if stop_top is None:
            # A recording made before the percentile existed only carries the
            # median; keep such runs working but say so instead of pretending
            # the robust rule was applied.
            stop_top = stop.get("medianTopHeight")
            stop_method = "area-weighted-median (p90 unavailable in the recorded surface)"
        if stop_top is None:
            raise RuntimeError(
                f"planarize stopMaterial {stop_material!r} has no exposed up-facing surface before step "
                f"{step['id']!r}; the CMP stop height cannot be derived. Deposit or expose that material "
                f"in an earlier enabled step, or use an explicit cutoffHeight instead")
        over_polish = float(step.get("overPolish") or 0.0)
        cutoff = float(stop_top) - over_polish
        ps.Planarize(domain, cutoff).apply()
        return {"geometryOperation": "planarize", "cutoffHeight": cutoff,
                "stopMaterial": stop_material,
                **({"stopMaterialId": MATERIAL_IDS[stop_material]} if stop_material in MATERIAL_IDS else {}),
                "measuredStopTopHeight": float(stop_top), "overPolish": over_polish,
                "stopHeight": float(stop_top), "stopHeightMethod": stop_method,
                **({"stopHeightPercentile": STOP_HEIGHT_PERCENTILE} if stop_method == STOP_HEIGHT_METHOD else {}),
                "stopMaterialUpFacingArea": stop.get("upFacingArea"),
                "stopMaterialMedianTopHeight": stop.get("medianTopHeight"),
                "stopMaterialHighestTopHeight": stop.get("topHeight"),
                "cutoffMode": "material-aware",
                "cutoffDerivation": (f"{stop_method} height of the up-facing stopMaterial surface at the "
                                     "start of this step minus overPolish; a large upper plateau wins over a "
                                     "larger lower one and a narrow tall sliver is ignored"),
                "nativePlanarizeScope": ("viennaps.Planarize(domain, cutoffHeight) cuts every level set "
                                         "above the derived height; lateral selectivity is not modeled")}
    if model == "boolean":
        factory = ps.GeometryFactory(domain.getSetup(), f"WorkbenchBoolean_{step['id']}")
        position = [float(value) for value in step["position"]]
        width = float(step["width"])
        height = float(step["height"])
        angle = float(step["angle"])
        if project["dimension"] == 2:
            operand = factory.makeBoxStencil(position[:2], width, height, angle)
        else:
            operand = factory.makeBoxStencil(position, width, height, angle, float(step["depth"]))
        enum_name = {"union": "UNION", "intersection": "INTERSECT", "difference": "RELATIVE_COMPLEMENT"}[step["booleanOperation"]]
        operation = getattr(ps.ls.BooleanOperationEnum, enum_name)
        domain.applyBooleanOperation(operand, operation, bool(step["applyToAll"]))
        return {"geometryOperation": "boolean", "booleanOperation": step["booleanOperation"],
                "operand": "box", "position": position, "width": width, "height": height,
                "depth": float(step["depth"]) if project["dimension"] == 3 else None,
                "angle": angle, "applyToAll": bool(step["applyToAll"]), "nativeBooleanEnum": enum_name}
    if model == "insertBox":
        handle = material_handle(ps, step["material"])
        before = int(domain.getNumberOfLevelSets())
        box = ps.LevelSet(domain.getGrid())
        ps.MakeGeometry(box, ps.Box([float(value) for value in step["min"]],
                                    [float(value) for value in step["max"]])).apply()
        domain.insertNextLevelSetAsMaterial(box, handle)
        return {"geometryOperation": "insertBox",
                "min": [float(value) for value in step["min"]],
                "max": [float(value) for value in step["max"]],
                "material": step["material"], "materialId": int(handle),
                "levelSetCountBefore": before,
                "levelSetCountAfter": int(domain.getNumberOfLevelSets()),
                "boxPointCount": int(box.getNumberOfPoints())}
    if model == "removeTopLevelSet":
        before = int(domain.getNumberOfLevelSets())
        if before <= 1:
            raise RuntimeError("removeTopLevelSet would remove the only level set; the domain would have no material surface")
        removed_id = int(domain.getMaterialMap().getMaterialIdAtIdx(before - 1))
        domain.removeTopLevelSet()
        return {"geometryOperation": "removeTopLevelSet", "removedMaterialId": removed_id,
                "levelSetCountBefore": before,
                "levelSetCountAfter": int(domain.getNumberOfLevelSets())}
    if model == "gdsBoolean":
        if project["dimension"] != 3 or project["geometry"]["kind"] != "gds":
            raise RuntimeError("GDS boolean requires a 3D project whose geometry.kind is gds")
        operand, transform = _gds_boolean_operand(ps, domain, project, step, config=config, run_dir=run_dir)
        enum_name = {"union": "UNION", "intersection": "INTERSECT", "difference": "RELATIVE_COMPLEMENT"}[step["booleanOperation"]]
        operation = getattr(ps.ls.BooleanOperationEnum, enum_name)
        apply_to_all = bool(step["applyToAll"])
        before = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
        domain.applyBooleanOperation(operand, operation, apply_to_all)
        remaining = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
        erased = [index for index, (was, now) in enumerate(zip(before, remaining))
                  if was > 0 and now == 0]
        if not remaining or erased:
            # A boolean that erases a previously non-empty level set is not a
            # usable pattern: fail loudly. A level set that was already empty
            # before the operation is unrelated (e.g. a material removed by an
            # earlier step) and must not block a top-only mask operation.
            raise RuntimeError(
                "GDS boolean operand erased a material level set; adjust the mask region, "
                f"retained region or operation (level-set point counts: {remaining}, erased: {erased})")
        return {"geometryOperation": "gdsBoolean", "booleanOperation": step["booleanOperation"],
                "operand": "gds-mask", "nativeBooleanEnum": enum_name,
                "applyToAll": apply_to_all,
                "mask": {"layer": transform["layer"], "datatype": transform["datatype"],
                         "retainedRegion": transform["retainedRegion"],
                         "baseHeight": transform["baseHeight"], "height": transform["height"],
                         "topHeight": transform["topHeight"],
                         "position": transform["stepPositionUm"]},
                "assetId": transform["sourceAssetId"], "structure": transform["selectedStructure"],
                "unitScale": transform["unitScale"],
                "levelSetPointCountsAfter": remaining,
                "nativeOperandConstruction": transform["construction"],
                "maskDiagnostics": transform}
    raise RuntimeError(f"unsupported geometry operation {model!r}")


def calculate_single_flux(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any],
                          engine: str, seed: int, run_dir: Path,
                          flux_strategy: str | None = None) -> dict[str, Any]:
    ion_count = (sum(row.get("kind") == "ion" for row in step.get("species", []))
                 if step.get("model") == "multiParticle" else 0)
    multi_ion_gpu = ion_count > 1 and engine in {"GPU_DISK", "GPU_TRIANGLE"}
    require_multi_ion_gpu_policy(step, engine)
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(material_handle(ps, step["depositMaterial"]))
    split_flux = None
    multi_actual = None
    specialized_actual = None
    callback_state = None
    special_process_parameters: list[Any] = []
    process_duration = float(step.get("duration") or 0.0)
    if step["model"] == "specialized":
        model, special_process_parameters, specialized_actual, process_duration = build_specialized_model(
            ps, domain, project, step, lambda name: material_handle(ps, name), MATERIAL_IDS,
            flux_engine=engine)
        rate = None
    elif step["model"] == "multiParticle":
        if engine == "GPU_DISK" and ion_count > 1 and flux_strategy == GPU_MULTIPARTICLE_FLUX_STRATEGY:
            split_flux = calculate_multi_particle_flux_by_species(ps, domain, project, step, engine, seed, run_dir)
            multi_actual = multi_particle_step_summary(step)
            callback_state = None
        else:
            model, multi_actual, callback_state = build_multi_particle_model(ps, step)
        rate = None
    else:
        rate, material_rates = signed_rates(step)
        if step.get("rateMode") == "material":
            model = ps.SingleParticleProcess(rate=rate, stickingProbability=float(step["stickingProbability"]),
                                             sourceExponent=float(step["sourceExponent"]), maskMaterials=[])
            for name, material_rate in material_rates.items():
                model.setMaterialRate(material_handle(ps, name), material_rate)
        elif project["schemaVersion"] >= 3:
            masks = [material_handle(ps, name) for name in protected_material_names(project, step)]
            model = ps.SingleParticleProcess(rate=rate, stickingProbability=float(step["stickingProbability"]),
                                             sourceExponent=float(step["sourceExponent"]), maskMaterials=masks)
        else:
            model = ps.SingleParticleProcess(rate=rate, stickingProbability=float(step["stickingProbability"]),
                                             sourceExponent=float(step["sourceExponent"]), maskMaterial=ps.Material.Mask)
    if engine not in {"CPU_DISK", "CPU_TRIANGLE", "GPU_DISK", "GPU_TRIANGLE"}:
        raise RuntimeError("single flux calculation has no verified concrete engine")
    if split_flux is None:
        process = ps.Process(domain, model, process_duration)
        process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
        rays, ray_actual = configure_rays(ps, project, seed)
        process.setParameters(rays)
        for process_parameters in special_process_parameters:
            process.setParameters(process_parameters)
        started = time.perf_counter()
        flux_mesh = process.calculateFlux()
        elapsed = time.perf_counter() - started
        report = native_mesh_report(flux_mesh)
    else:
        rays, ray_actual = configure_rays(ps, project, seed)
        report = split_flux["report"]
        elapsed = sum(float(row["elapsedSeconds"]) for row in split_flux["species"])
    if not report["points"]:
        raise RuntimeError("ViennaPS Process.calculateFlux returned an empty mesh")
    if flux_strategy == GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY or (multi_ion_gpu and split_flux is None):
        # A multi-ion GPU flux that did not take the per-species merge route is
        # one native call, whatever strategy it recorded (including None), so
        # the species fields must all be present.
        require_multi_ion_flux_fields(report, step, flux_strategy)
    for association in ("pointData", "cellData"):
        for kind in ("scalars", "vectors"):
            for label, values in report[association][kind].items():
                if any(not math.isfinite(value) for row in values for value in (row if isinstance(row, list) else [row])):
                    raise RuntimeError(f"ViennaPS flux field {label!r} contains non-finite values")
    vtk_name = "flux.vtp"
    vtk_path = run_dir / vtk_name
    if split_flux is None:
        writer = ps.ls.VTKWriter(flux_mesh, ps.ls.FileFormatEnum.VTP, str(vtk_path))
        writer.apply()
    else:
        merge_native_flux_vtp(split_flux["speciesPaths"], vtk_path)
    if not vtk_path.is_file() or vtk_path.stat().st_size <= 0:
        raise RuntimeError("ViennaLS VTKWriter did not create the native flux mesh export")
    result = {
        "schemaVersion": 1,
        "source": "viennaps.Process.calculateFlux",
        "dimension": int(project["dimension"]),
        "axisMap": {"x": 0, "y": 1, "z": 2} if project["dimension"] == 3 else {"x": 0, "h": 1, "z": 2},
        "coordinateUnit": str((project.get("units") or {}).get("length") or "um"),
        "fieldUnitPolicy": "native mesh labels do not declare units; nativeUnit is null rather than inferred",
        "meshIdentity": report["meshIdentity"],
        "mesh": {"pointCount": len(report["points"]),
                 "cellCounts": {name: len(rows) for name, rows in {key: report.get(key, []) for key in ("lines", "triangles", "tetras", "hexas", "vertices")}.items()},
                 "vtk": vtk_name, "format": "VTP"},
        "points": report["points"],
        "topology": {key: report[key] for key in ("lines", "triangles", "tetras", "hexas", "vertices") if key in report},
        "pointData": report["pointData"],
        "cellData": report["cellData"],
        "fields": report["fields"],
        "execution": {"stepId": step["id"], "stepName": step["name"], "model": step["model"],
                      "operation": step["operation"], "actualRate": rate, "actualEngine": engine,
                      "actualSeed": seed, "cpuThreads": int(project["solver"]["cpuThreads"]),
                      "rays": ray_actual, "elapsedSeconds": elapsed,
                      "processDuration": "not-used-by-single-pass-calculateFlux"},
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
    }
    if multi_actual is not None:
        result["execution"]["multiParticle"] = multi_actual
        if callback_state is not None:
            result["execution"]["rateCallback"] = callback_state
        if split_flux is not None:
            result["execution"]["multiParticleFlux"] = {
                **split_flux["execution"], "species": split_flux["species"],
            }
        elif flux_strategy == GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY:
            result["execution"]["multiParticleFlux"] = {
                "strategy": flux_strategy, "nativeProcessCount": 1,
                "species": [{"label": row["label"], "kind": row["kind"],
                             "actualEngine": engine, "actualSeed": int(seed)} for row in step["species"]],
            }
    if specialized_actual is not None:
        result["execution"]["nativeModel"] = {**specialized_actual, "actualSeed": seed,
                                                 "actualEngine": engine,
                                                 "cpuThreads": int(project["solver"]["cpuThreads"])}
    atomic_json(run_dir / "flux.json", result)
    check_artifact_budget(run_dir)
    return {"fileName": "flux.json", "vtk": vtk_name, "meshIdentity": report["meshIdentity"],
            "pointCount": len(report["points"]), "fieldCount": len(report["fields"]),
            "fieldNames": [field["name"] for field in report["fields"]], "actualEngine": engine, "actualSeed": seed}


def describe_intermediate_vtp(path: Path, *, step_id: str, run_id: str,
                              run_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    """Record native VTP arrays and topology without remapping them to a surface mesh."""
    root = ET.parse(path).getroot()
    if root.attrib.get("type") != "PolyData":
        raise RuntimeError(f"ViennaLS intermediate output {path.name} is not VTK PolyData")
    piece = root.find(".//Piece")
    if piece is None:
        raise RuntimeError(f"ViennaLS intermediate output {path.name} has no PolyData Piece")
    point_count = int(piece.attrib.get("NumberOfPoints", "0"))
    cell_counts = {name: int(piece.attrib.get(f"NumberOf{name}", "0"))
                   for name in ("Verts", "Lines", "Strips", "Polys")}
    fields = []
    for container, association in (("PointData", "pointData"), ("CellData", "cellData")):
        data = piece.find(container)
        if data is None:
            continue
        tuples = point_count if association == "pointData" else sum(cell_counts.values())
        for array in data.findall("DataArray"):
            name = array.attrib.get("Name")
            if not name:
                continue
            components = int(array.attrib.get("NumberOfComponents", "1"))
            array_tuples = int(array.attrib.get("NumberOfTuples", str(tuples)))
            if components < 1 or array_tuples != tuples:
                raise RuntimeError(f"ViennaLS intermediate field {name!r} does not align with its native {association} mesh")
            fields.append({"name": name, "association": association, "components": components,
                           "tuples": array_tuples, "nativeType": array.attrib.get("type"),
                           "nativeUnit": None, "unitStatus": "not-provided-by-native-vtk-array"})
    if point_count <= 0 or not fields:
        raise RuntimeError(f"ViennaLS intermediate output {path.name} has no points or named data arrays")
    relative = path.resolve().relative_to(run_dir.resolve())
    return {"stepId": step_id, "fileName": relative.as_posix(),
            "url": f"/api/runs/{run_id}/intermediate-outputs/{step_id}/{path.name}",
            "format": "VTP", "pointCount": point_count, "cellCounts": cell_counts,
            "coordinateUnit": str((project.get("units") or {}).get("length") or "um"), "axisMap": {"x": 0, "y": 1, "z": 2} if project["dimension"] == 3 else {"x": 0, "h": 1, "z": 2},
            "fields": fields, "sizeBytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "associationPolicy": "native VTP pointData/cellData retained; not projected onto the domain surface"}


CELL_SET_PROCESS_MODELS = ("implant", "anneal", "analysis")
INJECTION_MOMENT_UNIT = "um"
LENGTH_UNIT_NAME = "um"


def _scalar_digest(values: list[float]) -> str:
    """Deterministic digest of a native scalar field (little-endian float64).

    The worker records this for every published label, so a replay can prove
    that the native field is bit-identical without storing the field twice.
    """
    return hashlib.sha256(array("d", values).tobytes()).hexdigest()


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field_integral(values: list[float], *, grid_delta: float, dimension: int) -> float:
    """Native concentration integral over the cell grid (``sum · delta^D``).

    The native implant concentration is volumetric in both dimensions
    (``1/um^3``; the native cm^-3 conversion is 1e12). ``sum · delta^2`` is
    therefore an integral over the 2D cell area with unit ``1/um`` (e.g. one
    cell of ``2 um^-3`` with ``delta = 2 um`` gives ``8 um^-1``). The 3D value
    ``sum · delta^3`` is dimensionless and is not given a length unit. It is
    never converted to an areal cm^-2 dose, because a 2D native grid has no
    physical depth.
    """
    return sum(values) * (float(grid_delta) ** int(dimension))


def _material_field_rows(values: list[float], materials: list[float],
                         names: dict[int, str]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {}
    for value, material in zip(values, materials):
        key = int(material)
        row = rows.setdefault(str(key), {"count": 0, "max": 0.0, "sum": 0.0,
                                         "materialName": names.get(key, "unknown")})
        row["count"] += 1
        row["sum"] += float(value)
        row["max"] = max(row["max"], float(value))
    return rows


def _field_material_map(domain: Any) -> list[int]:
    """Return the native material map as a JSON-friendly list of IDs."""
    mapping = domain.getMaterialMap().getMaterialMap()
    values = mapping.getMaterialMap()
    return [int(value) for value in values]


def _stored_band_half_cells(domain: Any) -> int:
    """Return the exact stored level-set band half-width in grid cells.

    ``viennals.Expand(width)`` only grows a band; a level set that already
    carries a wider band (for example one produced by an earlier native
    operation or a continuation state) keeps it. The native cell-set bottom is
    therefore the surface minus the *stored* half-width, not the half-width
    requested by the field step. ``getLevelSetWidth()`` is the only exposed
    width API; take the widest level set because the dense grid spans all of
    them.
    """
    widest = 2
    for level_set in domain.getLevelSets():
        try:
            widest = max(widest, int(level_set.getLevelSetWidth()))
        except (AttributeError, TypeError, ValueError):
            continue
    # Measured on 4.7.0: Expand(width) yields a stored band of ``width`` and a
    # native bottom of ``floor(width / 2)`` cells below the surface (width 19
    # and 21 both give 9 and 10 cells, 201/202 give 100/101).
    return max(1, widest // 2)


def _field_surface_bounds(domain: Any, *, is3d: bool) -> tuple[float, float]:
    """Return the current ``(highest, lowest)`` surface coordinate."""
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                 minNodeDistanceFactor=0.01)
    return _surface_vertical_bounds(mesh.getNodes(), is3d=is3d)


def _cell_grid_from_native(cell_set: Any, *, dimension: int, delta: float) -> Any:
    """Describe a native ``DenseCellSet`` as a bounding lattice + active map.

    A GDS / multi-material geometry can omit cells from the bounding lattice
    (cover/void regions), so ``getNumberOfCells()`` is smaller than
    ``nx * ny * nz``. Dense grids keep the cheap identity layout; sparse grids
    get one ``getCellCenter`` call per native cell to derive the absolute
    ``(ix, iy, iz)`` key. The native cell order is recorded, not assumed.
    """
    from backend.field_remap import CellGrid

    grid = CellGrid.from_points(cell_set.getNodes(), dimension=int(dimension),
                                delta=float(delta), allow_gaps=True)
    cells = int(cell_set.getNumberOfCells())
    if cells == grid.cell_count:
        return grid
    origin = grid.origin
    nx, ny, nz = grid.shape
    keys = array("q")
    seen = bytearray(grid.cell_count)
    for index in range(cells):
        center = cell_set.getCellCenter(index)
        i = int(round((float(center[0]) - origin[0]) / delta - 0.5))
        j = int(round((float(center[1]) - origin[1]) / delta - 0.5))
        k = (int(round((float(center[2]) - origin[2]) / delta - 0.5))
             if int(dimension) == 3 else 0)
        if not (0 <= i < nx and 0 <= j < ny and 0 <= k < nz):
            raise RuntimeError(
                f"native cell {index} center {center} lies outside the cell-set bounding "
                f"lattice {grid.shape}; the native grid is not lattice aligned")
        key = i + nx * (j + ny * k)
        if seen[key]:
            raise RuntimeError(
                f"native cell {index} repeats lattice key {(i, j, k)}; the native cell "
                "order is not a one-to-one lattice map")
        seen[key] = 1
        keys.append(key)
    return grid.with_active(keys)


def _field_grid_cell_bound(requested: dict[str, Any], *, top: float, bottom: float,
                           delta: float) -> int:
    """Conservative cell count for a field grid spanning ``[bottom, top]``.

    The native generator allocates the full lateral domain grid between the
    widened level-set band bottom and the requested position. The lateral
    counts carry the same two-cell slack as the generic cell-set bound and the
    row count carries four spare rows, so the bound is never below the native
    allocation for a regular box.
    """
    x_extent = float(requested["domain"]["xExtent"])
    nx = max(1, math.ceil(x_extent / delta))
    if int(requested["dimension"]) == 3:
        ny = max(1, math.ceil(float(requested["domain"]["yExtent"]) / delta))
        lateral = (nx + 2) * (ny + 2)
    else:
        lateral = nx + 2
    rows = math.ceil((float(top) - float(bottom)) / delta) + 4
    return lateral * max(rows, 2)


def _generate_field_cell_set(ps: Any, source_domain: Any, project: dict[str, Any], *,
                             height: float, band_cells: int, cover: Any,
                             periodic: bool,
                             surface_bounds: tuple[float, float] | None = None,
                             ) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Build one native cell set on a widened copy of ``source_domain``.

    A ViennaLS level set only stores a narrow band around the surface, so the
    substrate is widened *on a deep copy of the Domain* (``viennals.Expand`` on
    every level set, which reinitialises the band without moving the zero
    contour). The simulated Domain, and therefore every surface snapshot, is
    untouched.

    Measured on 4.7.0, ``generateCellSet(position, ...)`` uses ``position`` as
    the absolute top of the grid and the widened band bottom as the lower
    bound. The required top/bottom and a conservative cell bound are therefore
    known *before* the native allocation; the request is refused above the
    construction limit and a ``height`` below the current surface is refused
    instead of letting the native generator walk the whole band.
    """
    delta = float(source_domain.getGridDelta())
    is3d = project["dimension"] == 3
    if height + 1e-12 < delta:
        raise RuntimeError(
            f"cellSet.height {height:g} um is below one grid interval ({delta:g} um); "
            "the installed DenseCellSet generator would crash")
    if band_cells < 2:
        raise RuntimeError(
            "conservative grid remap not implemented: the fixed field grid no longer covers the "
            f"current surface (a {band_cells}-point band is below the native minimum of 2); the "
            "surface moved below the captured substrateDepth. Increase cellSet.substrateDepth on "
            "the first field step or reduce the etch depth")
    if surface_bounds is None:
        surface_bounds = _field_surface_bounds(source_domain, is3d=is3d)
    surface_top, surface_bottom = float(surface_bounds[0]), float(surface_bounds[1])
    top_node = math.ceil(surface_top / delta - 1e-9) * delta
    if height + 1e-9 < top_node:
        raise RuntimeError(
            f"cellSet.height {height:g} um is below the highest surface point "
            f"({surface_top:g} um); the native generateCellSet position is an absolute "
            "coordinate, so the field grid would clip the surface (and, below the surface "
            "band, the native generator allocates the whole band between them). Set "
            "cellSet.height above the surface or lower the geometry")
    planned_top = float(height)
    stored_half = _stored_band_half_cells(source_domain)
    band_half = max(int(band_cells) // 2, stored_half)
    planned_bottom = (math.floor(surface_bottom / delta + 1e-9) * delta
                      - band_half * delta)
    bound = _field_grid_cell_bound(project, top=planned_top, bottom=planned_bottom,
                                   delta=delta)
    # GDS/multi-level-set bands can be wider than the analytic band estimate.
    # Use a conservative guard for the pre-allocation construction limit, then
    # enforce the hard cell cap on the measured native grid after generation.
    guard_bound = int(math.ceil(bound * 2.5))
    if guard_bound > MAX_CELL_SET_CELLS:
        raise RuntimeError(
            f"field cell grid needs at least {bound:,} cells between "
            f"{planned_bottom:g} and {planned_top:g} um ({guard_bound:,} with band safety margin), "
            f"above the {MAX_CELL_SET_CELLS:,}-cell construction limit; raise cellSet.height above "
            "the surface only as needed, reduce cellSet.substrateDepth, or coarsen gridDelta")
    field_domain = ps.Domain(source_domain)
    original_points = sum(int(level_set.getNumberOfPoints())
                          for level_set in source_domain.getLevelSets())
    for level_set in field_domain.getLevelSets():
        ps.ls.Expand(level_set, int(band_cells)).apply()
    field_domain.generateCellSet(height, cover, True)
    cell_set = field_domain.getCellSet()
    if cell_set is None:
        raise RuntimeError("Domain.generateCellSet did not store a native cell set")
    cells = int(cell_set.getNumberOfCells())
    if cells <= 0 or cells > MAX_CELL_SET_CELLS:
        raise RuntimeError(
            f"DenseCellSet generation produced {cells} cells, above the hard "
            f"{MAX_CELL_SET_CELLS:,}-cell limit (pre-allocation estimate {bound})")
    cell_set.buildNeighborhood()
    cell_set.setPeriodicBoundary([bool(periodic), bool(periodic), False] if is3d
                                 else [bool(periodic), False])
    grid = _cell_grid_from_native(cell_set, dimension=int(project["dimension"]),
                                  delta=delta)
    if grid.active_count != cells:
        raise RuntimeError(
            f"native cell set reports {cells} cells but its active map has "
            f"{grid.active_count}; the native grid is not a one-to-one lattice map")
    info = {
        "numberOfCells": cells,
        "activeCellCount": int(grid.active_count),
        "fullCellCount": int(grid.cell_count),
        "missingCellCount": int(grid.missing_count),
        "sparse": bool(grid.is_sparse),
        "cellsUpperBound": max(bound, cells),
        "preAllocationBound": bound,
        "constructionGuardBound": guard_bound,
        "surfaceTop": surface_top,
        "surfaceBottom": surface_bottom,
        "heightAboveSurface": height,
        "substrateBandExpandCells": int(band_cells),
        "storedBandHalfCells": int(stored_half),
        "bandHalfCells": int(band_half),
        "sourceLevelSetPoints": original_points,
        "grid": grid,
    }
    return field_domain, cell_set, grid, info


def _field_setup_record(step: dict[str, Any], state: dict[str, Any],
                        info: dict[str, Any], *, regenerated: bool) -> dict[str, Any]:
    """Compose the per-step ``cellSet`` record from the captured grid."""
    settings = step.get("cellSet") or {}
    captured = state["grid"]
    is3d = int(captured.dimension) == 3
    periodic = bool(settings.get("periodic", False))
    cover_name = str(settings.get("coverMaterial") or "GAS")
    setup = {
        "generated": True,
        "regenerated": bool(regenerated),
        "grid": captured.signature(),
        "currentGrid": (state.get("currentGrid") or captured).signature(),
        "gridCapturedAtStepId": state.get("gridStepId"),
        "numberOfCells": int(info["numberOfCells"]),
        "activeCellCount": int(info.get("activeCellCount", info["numberOfCells"])),
        "fullCellCount": int(info.get("fullCellCount", info["numberOfCells"])),
        "missingCellCount": int(info.get("missingCellCount", 0)),
        "sparse": bool(info.get("sparse", False)),
        "cellsUpperBound": int(info["cellsUpperBound"]),
        "surfaceTop": float(info["surfaceTop"]),
        "surfaceBottom": float(info["surfaceBottom"]),
        "heightAboveSurface": float(info["heightAboveSurface"]),
        "substrateDepth": float(state["substrateDepth"]),
        "substrateBandExpandCells": int(info["substrateBandExpandCells"]),
        "storedBandHalfCells": int(info.get("storedBandHalfCells") or 0),
        "bandHalfCells": int(info.get("bandHalfCells") or 0),
        "substrateConstruction": "viennals.Expand on a deep Domain copy; the simulated surface is unchanged",
        "sourceLevelSetPoints": int(info["sourceLevelSetPoints"]),
        "coverMaterial": cover_name,
        "coverMaterialId": int(state["coverMaterialId"]),
        "periodic": periodic,
        "periodicBoundary": [periodic, periodic, False] if is3d else [periodic, False],
        "materialMap": list(state["materialMap"]),
        "remapCount": len(state.get("remaps") or []),
        "remaps": list(state.get("remaps") or []),
    }
    if state.get("remaps"):
        setup["lastRemap"] = state["remaps"][-1]
    return setup


def remap_field_cell_set(ps: Any, domain: Any, project: dict[str, Any],
                         state: dict[str, Any], source_step: dict[str, Any]) -> dict[str, Any]:
    """Regenerate the field cell set and remap every scalar field.

    The grid captured at the first field step is the reference. The regenerated
    grid keeps the captured lateral lattice and bottom; it grows upwards when a
    deposit lifts the surface above the captured top and downwards (at the
    native minimum band) when an etch goes below the captured substrate. The
    scalar arrays are then copied cell-for-cell by absolute position; cells
    that left the solid lose their dopant and the loss is recorded.
    """
    from backend.field_remap import (GridRemapError, NATIVE_SYSTEM_LABELS,
                                     check_captured_grid, remap_cell_fields)

    captured = state.get("currentGrid") or state["grid"]
    reference = state["grid"]
    old_cell_set = state["cellSet"]
    if old_cell_set is None:
        raise RuntimeError("field cell-set remap requested before the grid was generated")
    settings = state["settings"]
    delta = float(captured.delta)
    cover_name = str(settings.get("coverMaterial") or "GAS")
    cover = ps.Material.GAS if cover_name == "GAS" else material_handle(ps, cover_name)

    labels = [str(label) for label in old_cell_set.getScalarDataLabels()
              if str(label) not in NATIVE_SYSTEM_LABELS]
    old_values = {label: [float(value) for value in old_cell_set.getScalarData(label)]
                  for label in labels}
    old_material = [float(value) for value in old_cell_set.getScalarData("Material")]

    # ``Domain.generateCellSet(position, ...)`` takes the *absolute* top
    # coordinate of the grid, not a height above the surface (measured on
    # 4.7.0: a plane at 0.05 with position 0.5 produced top 0.5). The grid
    # bottom is the widened band bottom, so the target extent is fully known
    # from the current surface before any native allocation:
    #   top    = max(captured top, highest lattice surface node)
    #   bottom = the captured bottom, or the shallowest reachable band bottom
    #            (two-point native band) when an etch went deeper.
    is3d = int(project["dimension"]) == 3
    surface_top, surface_bottom = _field_surface_bounds(domain, is3d=is3d)
    top_node = math.ceil(surface_top / delta - 1e-9) * delta
    bottom_node = math.floor(surface_bottom / delta + 1e-9) * delta
    requested_height = float(captured.top)
    required_top = max(float(captured.top), top_node)
    stored_half = _stored_band_half_cells(domain)
    lowest_reachable = bottom_node - max(1, stored_half) * delta
    if float(captured.bottom) <= lowest_reachable + 1e-9:
        required_bottom = float(captured.bottom)
        band_cells = max(2, 2 * int(round((bottom_node - required_bottom) / delta)))
    else:
        required_bottom = lowest_reachable
        band_cells = max(2, 2 * stored_half)
    height = required_top
    field_domain, cell_set, grid, info = _generate_field_cell_set(
        ps, domain, project, height=height, band_cells=band_cells,
        cover=cover, periodic=bool(state["settings"].get("periodic", False)),
        surface_bounds=(surface_top, surface_bottom))
    for _attempt in range(4):
        # Structured/GDS level sets can carry a band wider than the analytic
        # extent, so the native bounding lattice may exceed the captured grid.
        # Growing beyond it is conservative: the remap copies by lattice key
        # and starts the extra cells at zero. Only a grid that does not *cover*
        # the captured extent needs another native call.
        covers = (float(grid.top) + 1e-9 >= required_top
                  and float(grid.bottom) - 1e-9 <= required_bottom)
        if covers:
            break
        if float(grid.top) + 1e-9 < required_top:
            height += float(required_top) - float(grid.top)
        if float(grid.bottom) - 1e-9 > required_bottom:
            band_cells = max(2, band_cells + 2 * int(
                round((float(grid.bottom) - required_bottom) / delta)))
        field_domain, cell_set, grid, info = _generate_field_cell_set(
            ps, domain, project, height=height, band_cells=band_cells,
            cover=cover, periodic=bool(state["settings"].get("periodic", False)),
            surface_bounds=(surface_top, surface_bottom))
    else:
        raise GridRemapError(
            "conservative grid remap not implemented: the regenerated cell grid does not "
            f"cover the captured extent (bottom {required_bottom:g}/{grid.bottom:g} um, "
            f"top {required_top:g}/{grid.top:g} um)")

    check_captured_grid(captured, grid)
    new_material = [float(value) for value in cell_set.getScalarData("Material")]
    result = remap_cell_fields(
        captured, grid, old_values=old_values, old_material=old_material,
        new_material=new_material, cover_material_id=int(cover), labels=labels,
        source_step_id=str(source_step.get("id")),
        previous_remap_count=len(state.get("remaps") or []))
    record = result.record
    record["remapOfStepId"] = state.get("gridStepId")
    record["capturedGrid"] = reference.signature()
    record["sourceStepModel"] = source_step.get("model")
    record["sourceStepOperation"] = source_step.get("operation")
    record["requestedHeight"] = requested_height
    record["effectiveHeight"] = height
    record["bandCells"] = int(band_cells)
    record["surfaceTopAtRemap"] = float(info["surfaceTop"])
    record["surfaceBottomAtRemap"] = float(info["surfaceBottom"])
    record["materialMap"] = _field_material_map(domain)

    for label in labels:
        cell_set.addScalarData(label, 0.0)
    for label in labels:
        cell_set.setScalarData(label, result.labels[label])
    for label, values in result.labels.items():
        if len(values) != int(cell_set.getNumberOfCells()):
            raise RuntimeError(f"remapped scalar {label!r} does not match the new cell count")

    info["substrateBandExpandCells"] = int(band_cells)
    state.update({"domain": field_domain, "cellSet": cell_set, "grid": reference,
                  "currentGrid": grid, "info": info, "bandCells": int(band_cells)})
    state.setdefault("remaps", []).append(record)
    state["surfaceDirty"] = False
    return record


def ensure_field_cell_set(ps: Any, domain: Any, project: dict[str, Any],
                          step: dict[str, Any],
                          state: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    """Return ``(field_domain, cell_set, setup_record)`` for a field step.

    All implant/anneal/analysis steps share one native ``DenseCellSet`` grid.
    The grid captured at the first enabled field step is the reference: later
    field steps reuse it, and a surface-moving step in between regenerates the
    native cell set from the current level sets and conservatively remaps every
    scalar field (see :func:`remap_field_cell_set`).
    """
    settings = step.get("cellSet")
    if not isinstance(settings, dict):
        raise RuntimeError(f"cell-set step {step.get('id')!r} has no cellSet settings")
    if state.get("cellSet") is None:
        height = float(settings["height"])
        delta = float(domain.getGridDelta())
        if height + 1e-12 < delta:
            raise RuntimeError(
                f"cellSet.height {height:g} um is below one grid interval ({delta:g} um); "
                "the installed DenseCellSet generator would crash")
        substrate = settings.get("substrateDepth")
        if substrate is None:
            raise RuntimeError(
                "the first enabled cell-set field step must declare cellSet.substrateDepth; a narrow-band "
                "level set would otherwise produce a single substrate row")
        substrate = float(substrate)
        if substrate + 1e-12 < 2 * delta:
            raise RuntimeError(
                f"cellSet.substrateDepth must span at least two grid intervals ({2 * delta:g} um)")
        # ``Expand(width)`` widens the stored band symmetrically to ``width``
        # grid points, so a half-band of ``ceil(depth/delta)`` cells needs twice that.
        band_cells = 2 * int(math.ceil(substrate / delta)) + 2
        cover_name = str(settings.get("coverMaterial") or "GAS")
        cover = ps.Material.GAS if cover_name == "GAS" else material_handle(ps, cover_name)
        field_domain, cell_set, grid, info = _generate_field_cell_set(
            ps, domain, project, height=height, band_cells=band_cells,
            cover=cover, periodic=bool(settings.get("periodic", False)))
        state.update({
            "domain": field_domain,
            "cellSet": cell_set,
            "grid": grid,
            "currentGrid": grid,
            "info": info,
            "settings": dict(settings),
            "substrateDepth": substrate,
            "bandCells": band_cells,
            "coverMaterial": cover_name,
            "coverMaterialId": int(cover),
            "materialMap": _field_material_map(domain),
            "gridStepId": str(step.get("id")),
            "remaps": [],
            "surfaceDirty": False,
        })
        setup = _field_setup_record(step, state, info, regenerated=False)
        return field_domain, cell_set, setup
    if state.get("surfaceDirty"):
        remap_field_cell_set(ps, domain, project, state, step)
    setup = _field_setup_record(step, state, state["info"], regenerated=True)
    return state["domain"], state["cellSet"], setup


def build_implant_profile(ps: Any, profile: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Build the native profile model and return an exact-readback record."""
    def moments(row: dict[str, Any]) -> Any:
        params = ps.PearsonIVParameters()
        params.mu = float(row["mu"])
        params.sigma = float(row["sigma"])
        params.beta = float(row["beta"])
        params.gamma = float(row["gamma"])
        return params, {"mu": params.mu, "sigma": params.sigma,
                        "beta": params.beta, "gamma": params.gamma}

    head, head_readback = moments(profile["head"])
    kind = str(profile["kind"])
    record: dict[str, Any] = {
        "source": "manual",
        "kind": kind,
        "momentUnit": INJECTION_MOMENT_UNIT,
        "head": head_readback,
        "lateralMu": float(profile.get("lateralMu", 0.0)),
        "lateralSigma": float(profile["lateralSigma"]),
    }
    if kind == "pearsonIV":
        model = ps.ImplantPearsonIV(head, record["lateralMu"], record["lateralSigma"])
        record["nativeConstructor"] = "viennaps.ImplantPearsonIV"
    elif kind == "dualPearsonIV":
        tail, tail_readback = moments(profile["tail"])
        record.update({
            "tail": tail_readback,
            "headFraction": float(profile["headFraction"]),
            "tailLateralMu": float(profile["tailLateralMu"]),
            "tailLateralSigma": float(profile["tailLateralSigma"]),
        })
        model = ps.ImplantDualPearsonIV(
            head, tail, record["headFraction"], record["lateralMu"], record["lateralSigma"],
            record["tailLateralMu"], record["tailLateralSigma"])
        record["nativeConstructor"] = "viennaps.ImplantDualPearsonIV"
    else:
        record.update({
            "tailFraction": float(profile["tailFraction"]),
            "tailStartDepth": float(profile["tailStartDepth"]),
            "tailDecayLength": float(profile["tailDecayLength"]),
            "tailBlendWidth": float(profile.get("tailBlendWidth", 0.0)),
        })
        model = ps.ImplantPearsonIVChanneling(
            head, record["lateralMu"], record["lateralSigma"], record["tailFraction"],
            record["tailStartDepth"], record["tailDecayLength"], record["tailBlendWidth"])
        record["nativeConstructor"] = "viennaps.ImplantPearsonIVChanneling"
    return model, record


def apply_implant_step(ps: Any, domain: Any, project: dict[str, Any],
                       step: dict[str, Any], *, run_dir: Path, run_id: str,
                       state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run native ``IonImplantation`` on the field cell set (duration 0)."""
    from backend.implant_models import LENGTH_UNIT_CM
    field_domain, cell_set, cell_set_record = ensure_field_cell_set(ps, domain, project, step, state)
    profile_model, profile_record = build_implant_profile(ps, step["profile"])
    implant = ps.IonImplantation()
    implant.setImplantModel(profile_model)
    damage_record = None
    damage = step.get("damage")
    if damage is not None:
        damage_model = ps.ImplantDamageHobler(
            float(damage["projectedRange"]), float(damage["verticalSigma"]),
            float(damage["lambdaValue"]), float(damage["defectsPerIon"]),
            float(damage["lateralSigma"]), float(damage["lateralDeltaSigma"]))
        implant.setDamageModel(damage_model)
        damage_record = {
            "nativeConstructor": "viennaps.ImplantDamageHobler",
            "kind": damage["kind"],
            "projectedRange": float(damage["projectedRange"]),
            "verticalSigma": float(damage["verticalSigma"]),
            "lambdaValue": float(damage["lambdaValue"]),
            "defectsPerIon": float(damage["defectsPerIon"]),
            "lateralSigma": float(damage["lateralSigma"]),
            "lateralDeltaSigma": float(damage["lateralDeltaSigma"]),
        }
    implant.setDose(float(step["doseCm2"]))
    implant.setTiltAngle(float(step["tiltDeg"]))
    # The workbench declares ``units.length = um``; the native implant model
    # needs the same unit in centimetres (1 um = 1e-4 cm) so the tabulated /
    # analytical profile and the deposited concentration are dimensionally
    # consistent with the cell grid.
    implant.setLengthUnit(LENGTH_UNIT_CM)
    implant.setDoseControl(getattr(ps.ImplantDoseControl, str(step["doseControl"])))
    mask_names = list(step.get("maskMaterials") or [])
    screen_names = list(step.get("screenMaterials") or [])
    implant.setMaskMaterials([material_handle(ps, name) for name in mask_names])
    implant.setScreenMaterials([material_handle(ps, name) for name in screen_names])
    labels = step["labels"]
    implant.setConcentrationLabel(str(labels["concentration"]))
    if labels.get("damage"):
        implant.setDamageLabel(str(labels["damage"]))
    if labels.get("lastDamage"):
        implant.setLastDamageLabel(str(labels["lastDamage"]))
    if float(step.get("damageFactor", 1.0)) != 1.0:
        implant.setDamageFactor(float(step["damageFactor"]))

    process = ps.Process(field_domain, implant, 0.0)
    # The native model does not consume a ray flux engine; pin CPU_DISK so the
    # Process default never attempts CUDA initialization.
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    started = time.perf_counter()
    process.apply()
    elapsed = time.perf_counter() - started

    known = set(str(label) for label in cell_set.getScalarDataLabels())
    expected = [str(labels["concentration"])] + [
        str(labels[key]) for key in ("damage", "lastDamage") if labels.get(key)]
    missing = [label for label in expected if label not in known]
    if missing:
        raise RuntimeError("native IonImplantation did not write cell-set field(s): " + ", ".join(missing))

    record: dict[str, Any] = {
        "nativeModel": "viennaps.IonImplantation",
        "nativeProcess": "ps.Process(domain, model, 0.0); the level sets are not advanced",
        "requested": {
            "species": step["species"], "material": step["material"],
            "energyKeV": float(step["energyKeV"]), "doseCm2": float(step["doseCm2"]),
            "tiltDeg": float(step["tiltDeg"]), "rotationDeg": float(step["rotationDeg"]),
            "doseControl": step["doseControl"],
            "maskMaterials": mask_names, "screenMaterials": screen_names,
        },
        "effective": {
            "doseCm2": float(step["doseCm2"]), "tiltDeg": float(step["tiltDeg"]),
            "lengthUnitCm": LENGTH_UNIT_CM,
            "doseControl": step["doseControl"],
            "maskMaterialIds": [MATERIAL_IDS.get(name) for name in mask_names],
            "screenMaterialIds": [MATERIAL_IDS.get(name) for name in screen_names],
            "damageFactor": float(step.get("damageFactor", 1.0)),
            "concentrationLabel": str(labels["concentration"]),
            "damageLabel": labels.get("damage"),
            "lastDamageLabel": labels.get("lastDamage"),
        },
        # The manual Pearson IV path has no native energy/rotation argument;
        # both are recorded as request metadata, never as native inputs.
        "energyKeVNativeEffect": False,
        "rotationDegNativeEffect": False,
        "nativeDoseControl": f"viennaps.ImplantDoseControl.{step['doseControl']}",
        "profile": profile_record,
        "damage": damage_record,
        "cellSet": cell_set_record,
        "processSeconds": elapsed,
    }
    record["fields"] = _selected_field_rows(cell_set, project, step)
    record["fieldDigests"] = _selected_field_digests(cell_set, project, step)
    outputs = write_cell_set_field_outputs(
        ps, field_domain, cell_set, project, step, {"implant": record}, cell_set_record,
        run_dir=run_dir, run_id=run_id, field_rows=record["fields"],
        digests=record["fieldDigests"])
    return record, outputs


def apply_anneal_step(ps: Any, domain: Any, project: dict[str, Any],
                      step: dict[str, Any], *, run_dir: Path, run_id: str,
                      state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run native ``Anneal`` (or its zero-time activation) on the cell set."""
    field_domain, cell_set, cell_set_record = ensure_field_cell_set(ps, domain, project, step, state)
    species_label = str(step["speciesLabel"])
    known = set(str(label) for label in cell_set.getScalarDataLabels())
    if species_label not in known:
        raise RuntimeError(
            f"anneal speciesLabel {species_label!r} is not present in the native cell set; "
            "an earlier implant step must produce it")
    anneal = ps.Anneal()
    schedule = step["schedule"]
    if schedule["kind"] == "isothermal":
        anneal.setTemperature(float(schedule["temperatureK"]))
        anneal.setDuration(float(schedule["durationSeconds"]))
        schedule_record = {"kind": "isothermal",
                           "temperatureK": float(schedule["temperatureK"]),
                           "durationSeconds": float(schedule["durationSeconds"]),
                           "nativeCall": "setTemperature + setDuration"}
    else:
        durations = [float(value) for value in schedule["durations"]]
        temperatures = [float(value) for value in schedule["temperatures"]]
        anneal.setTemperatureSchedule(durations, temperatures)
        schedule_record = {"kind": "schedule", "durations": durations,
                           "temperatures": temperatures,
                           "nativeCall": "setTemperatureSchedule"}
    diffusivity = step["diffusivity"]
    if diffusivity["kind"] == "arrhenius":
        anneal.setArrheniusParameters(float(diffusivity["D0"]), float(diffusivity["Ea_eV"]))
        diffusivity_record = {"kind": "arrhenius", "D0": float(diffusivity["D0"]),
                              "Ea_eV": float(diffusivity["Ea_eV"]),
                              "nativeCall": "setArrheniusParameters"}
    else:
        anneal.setDiffusionCoefficient(float(diffusivity["value"]))
        diffusivity_record = {"kind": "constant", "value": float(diffusivity["value"]),
                              "nativeCall": "setDiffusionCoefficient"}
    mode = str(step["mode"])
    anneal.setMode(getattr(ps.AnnealMode, mode))
    solver_record: dict[str, Any] = {"mode": mode, "nativeCall": f"setMode(viennaps.AnnealMode.{mode})"}
    if step.get("implicitSolver") is not None:
        options = step["implicitSolver"]
        anneal.setImplicitSolverOptions(int(options["maxIterations"]),
                                        float(options["relativeTolerance"]),
                                        float(options["relaxation"]))
        solver_record["implicitSolver"] = {
            "maxIterations": int(options["maxIterations"]),
            "relativeTolerance": float(options["relativeTolerance"]),
            "relaxation": float(options["relaxation"]),
        }
    if step.get("timeStep") is not None:
        anneal.setTimeStep(float(step["timeStep"]))
        solver_record["timeStep"] = float(step["timeStep"])
    if step.get("stabilityFactor") is not None:
        anneal.setStabilityFactor(float(step["stabilityFactor"]))
        solver_record["stabilityFactor"] = float(step["stabilityFactor"])
    diffusion_names = list(step.get("diffusionMaterials") or [])
    blocking_names = list(step.get("blockingMaterials") or [])
    anneal.setDiffusionMaterials([material_handle(ps, name) for name in diffusion_names])
    anneal.setBlockingMaterials([material_handle(ps, name) for name in blocking_names])
    anneal.setSpeciesLabel(species_label)
    active_label = str(step["activeLabel"])
    anneal.setActiveLabel(active_label)
    activation = step["solidActivation"]
    anneal.enableSolidActivation(True)
    anneal.setSolidSolubilityArrhenius(float(activation["solubilityC0"]),
                                       float(activation["solubilityEa_eV"]))
    activation_only = bool(step.get("activationOnly", False))
    started = time.perf_counter()
    if activation_only:
        anneal.applyActivation(field_domain)
        elapsed = time.perf_counter() - started
        native_call = "Anneal.applyActivation(domain)"
    else:
        process = ps.Process(field_domain, anneal, 0.0)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        process.apply()
        elapsed = time.perf_counter() - started
        native_call = "ps.Process(domain, model, 0.0)"
    known = set(str(label) for label in cell_set.getScalarDataLabels())
    if active_label not in known:
        raise RuntimeError(
            f"native Anneal did not write the active-concentration field {active_label!r}")
    record: dict[str, Any] = {
        "nativeModel": "viennaps.Anneal",
        "nativeProcess": native_call,
        "activationOnly": activation_only,
        "requested": {
            "mode": mode, "speciesLabel": species_label, "activeLabel": active_label,
            "diffusionMaterials": diffusion_names, "blockingMaterials": blocking_names,
        },
        "effective": {
            "diffusionMaterialIds": [MATERIAL_IDS.get(name) for name in diffusion_names],
            "blockingMaterialIds": [MATERIAL_IDS.get(name) for name in blocking_names],
            "solidActivation": {"solubilityC0": float(activation["solubilityC0"]),
                                "solubilityEa_eV": float(activation["solubilityEa_eV"]),
                                "nativeCall": "enableSolidActivation + setSolidSolubilityArrhenius"},
        },
        "schedule": schedule_record,
        "diffusivity": diffusivity_record,
        "solver": solver_record,
        "cellSet": cell_set_record,
        "processSeconds": elapsed,
    }
    record["fields"] = _selected_field_rows(cell_set, project, step)
    record["fieldDigests"] = _selected_field_digests(cell_set, project, step)
    species_values = [float(value) for value in cell_set.getScalarData(species_label)]
    active_values = [float(value) for value in cell_set.getScalarData(active_label)]
    total = sum(species_values)
    record["activation"] = {
        "speciesLabel": species_label,
        "activeLabel": active_label,
        "totalConcentrationSum": total,
        "activeConcentrationSum": sum(active_values),
        "activeFractionOfTotal": (sum(active_values) / total) if total > 0.0 else None,
        "note": "sums are over every native cell; activeFraction is a global ratio, not a local activation model",
    }
    outputs = write_cell_set_field_outputs(
        ps, field_domain, cell_set, project, step, {"anneal": record}, cell_set_record,
        run_dir=run_dir, run_id=run_id, field_rows=record["fields"],
        digests=record["fieldDigests"])
    return record, outputs


def apply_analysis_step(ps: Any, domain: Any, project: dict[str, Any],
                        step: dict[str, Any], *, run_dir: Path, run_id: str,
                        state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run native ``NetDoping`` and ``SheetResistance`` on the current cell set."""
    from backend.implant_models import LENGTH_UNIT_CM
    field_domain, cell_set, cell_set_record = ensure_field_cell_set(ps, domain, project, step, state)
    known = set(str(label) for label in cell_set.getScalarDataLabels())
    record: dict[str, Any] = {"nativeModel": "viennaps.NetDoping" if step.get("netDoping") else None,
                              "cellSet": cell_set_record}
    net = step.get("netDoping")
    if net is not None:
        inputs = [*(net.get("donorLabels") or []), *(net.get("acceptorLabels") or [])]
        missing = [label for label in inputs if label not in known]
        if missing:
            raise RuntimeError("native NetDoping input field(s) are missing: " + ", ".join(missing))
        net_doping = ps.NetDoping()
        net_doping.setCellSet(cell_set)
        for label in net.get("donorLabels") or []:
            net_doping.addDonorLabel(str(label))
        for label in net.get("acceptorLabels") or []:
            net_doping.addAcceptorLabel(str(label))
        net_doping.setOutputLabel(str(net["outputLabel"]))
        surface_position = net.get("surfacePosition")
        if surface_position is not None:
            net_doping.setSurfacePosition(float(surface_position))
        net_doping.apply()
        output_label = str(net["outputLabel"])
        if output_label not in set(str(label) for label in cell_set.getScalarDataLabels()):
            raise RuntimeError(f"native NetDoping did not write {output_label!r}")
        values = [float(value) for value in cell_set.getScalarData(output_label)]
        scan = step.get("scanDepth")
        lateral = None
        if scan is not None:
            lateral = _finite_or_none(net_doping.lateralJunctionPosition(float(scan)))
        record["netDoping"] = {
            "donorLabels": list(net.get("donorLabels") or []),
            "acceptorLabels": list(net.get("acceptorLabels") or []),
            "outputLabel": output_label,
            "surfacePosition": float(surface_position) if surface_position is not None else 0.0,
            "surfacePositionSource": "request" if surface_position is not None else "native-default-0",
            "junctionDepth": _finite_or_none(net_doping.junctionDepth()),
            "junctionDepths": [_finite_or_none(value) for value in net_doping.junctionDepths()],
            "junctionCount": int(net_doping.junctionCount()),
            "scanDepth": float(scan) if scan is not None else None,
            "lateralJunctionPosition": lateral,
            "outputField": {"count": len(values), "min": min(values), "max": max(values),
                            "sum": sum(values),
                            "integralNative": _field_integral(values, grid_delta=float(domain.getGridDelta()),
                                                              dimension=int(project["dimension"]))},
            "junctionDepthNote": "depth in the project length unit (um); inf means no sign change",
        }
        record["nativeModel"] = "viennaps.NetDoping"
    sheet_rows: list[dict[str, Any]] = []
    for entry in step.get("sheetResistance") or []:
        label = str(entry["concentrationLabel"])
        if label not in known:
            raise RuntimeError(f"native SheetResistance input field {label!r} is missing")
        resistance = ps.SheetResistance()
        resistance.setCellSet(cell_set)
        resistance.setLengthUnit(LENGTH_UNIT_CM)
        resistance.setConcentrationLabel(label)
        surface_position = entry.get("surfacePosition")
        if surface_position is not None:
            resistance.setSurfacePosition(float(surface_position))
        value = (resistance.computeElectron() if entry["carrier"] == "electron"
                 else resistance.computeHole())
        sheet_rows.append({
            "concentrationLabel": label,
            "carrier": entry["carrier"],
            "nativeMethod": ("viennaps.SheetResistance.computeElectron" if entry["carrier"] == "electron"
                             else "viennaps.SheetResistance.computeHole"),
            "lengthUnitCm": LENGTH_UNIT_CM,
            "concentrationUnitCm3": 1e12,
            "surfacePosition": float(surface_position) if surface_position is not None else 0.0,
            "surfacePositionSource": "request" if surface_position is not None else "native-default-0",
            "sheetResistanceOhmPerSquare": _finite_or_none(value),
        })
    if sheet_rows:
        record["sheetResistance"] = sheet_rows
        if record["nativeModel"] is None:
            record["nativeModel"] = "viennaps.SheetResistance"
    record["fields"] = _selected_field_rows(cell_set, project, step)
    record["fieldDigests"] = _selected_field_digests(cell_set, project, step)
    outputs = write_cell_set_field_outputs(
        ps, field_domain, cell_set, project, step, {"analysis": record}, cell_set_record,
        run_dir=run_dir, run_id=run_id, field_rows=record["fields"],
        digests=record["fieldDigests"])
    return record, outputs


def _selected_labels(cell_set: Any, step: dict[str, Any]) -> list[str]:
    labels = [str(label) for label in cell_set.getScalarDataLabels()]
    settings = step.get("cellSet") or {}
    selected = settings.get("scalarLabels")
    if selected is None:
        return labels
    missing = sorted(set(selected) - set(labels))
    if missing:
        raise RuntimeError("native cell set is missing requested scalar label(s): " + ", ".join(missing))
    return [str(label) for label in selected]


def _selected_field_rows(cell_set: Any, project: dict[str, Any], step: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # GAS (3) and Air (2) are native enum values that the curated workbench
    # material catalog does not list; the cover cells carry one of them.
    material_names = {**MATERIAL_NAMES, 2: "Air", 3: "GAS"}
    material_values = [float(value) for value in cell_set.getScalarData("Material")]
    rows: dict[str, dict[str, Any]] = {}
    for label in _selected_labels(cell_set, step):
        if label == "Material":
            rows[label] = _cell_set_field_row(label, material_values, material_names)
            continue
        if label == "FillingFraction":
            values = [float(value) for value in cell_set.getFillingFractions()]
        else:
            values = [float(value) for value in cell_set.getScalarData(label)]
        row = _cell_set_field_row(label, values, material_names)
        if len(values) > 200_000:
            # ``_cell_set_field_row`` builds a Python set of every value for
            # distinctCount; above this size the memory/time cost outweighs the
            # information, so the compact summary omits it.
            row.pop("distinctCount", None)
        row["integralNative"] = _field_integral(
            values, grid_delta=float(cell_set.getGridDelta()), dimension=int(project["dimension"]))
        if label not in ("FillingFraction",):
            row["byMaterial"] = _material_field_rows(values, material_values, material_names)
        rows[label] = row
    return rows


def _selected_field_digests(cell_set: Any, project: dict[str, Any], step: dict[str, Any]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for label in _selected_labels(cell_set, step):
        if label == "FillingFraction":
            values = [float(value) for value in cell_set.getFillingFractions()]
        else:
            values = [float(value) for value in cell_set.getScalarData(label)]
        digests[label] = _scalar_digest(values)
    return digests


def write_cell_set_field_outputs(ps: Any, domain: Any, cell_set: Any, project: dict[str, Any],
                                 step: dict[str, Any], record: dict[str, Any],
                                 cell_set_record: dict[str, Any], *, run_dir: Path,
                                 run_id: str, field_rows: dict[str, dict[str, Any]],
                                 digests: dict[str, str]) -> list[dict[str, Any]]:
    """Write the native field cell set as a VTU and a compact JSON summary.

    Both artifacts are budgeted before the VTU is written, so an oversized grid
    is refused instead of being silently truncated or written past the cap.
    """
    settings = step.get("cellSet") or {}
    format_ = str(settings.get("format") or "both")
    if format_ not in {"vtu", "json", "both"}:
        raise RuntimeError(f"unsupported cellSet.format {format_!r}")
    dimension = int(project["dimension"])
    delta = float(domain.getGridDelta())
    cells = int(cell_set.getNumberOfCells())
    step_id = str(step["id"])
    if cells <= 0:
        raise RuntimeError("native cell set has no cells")
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    target_paths: list[Path] = []
    if format_ in {"vtu", "both"}:
        target_paths.append(artifact_dir / f"{step_id}-cell-set-fields.vtu")
    if format_ in {"json", "both"}:
        target_paths.append(artifact_dir / f"{step_id}-cell-set-fields.json")
    estimate = 0
    if format_ in {"vtu", "both"}:
        estimate += cells * CELL_SET_VTU_BYTES_PER_CELL + 8192
    if format_ in {"json", "both"}:
        # The summary carries per-label statistics plus a per-material breakdown
        # and a 64-byte digest per selected label; 64 KiB is the generic cell-set
        # JSON bound and is conservative for these compact rows.
        estimate += CELL_SET_JSON_BYTES
    replaced = sum(path.stat().st_size for path in target_paths if path.is_file())
    cap = artifact_limit_bytes()
    used = artifact_bytes_used(run_dir) - replaced
    if used + estimate > cap:
        raise RuntimeError(
            f"cell-set field output would need about {estimate / (1024 * 1024):.1f} MiB but only "
            f"{(cap - used) / (1024 * 1024):.1f} MiB remain under the {artifact_limit_label()} cap; "
            "reduce cellSet.height, use format=json, or coarsen gridDelta")

    labels = _selected_labels(cell_set, step)
    bounds = json.loads(json.dumps(cell_set.getBoundingBox(), default=float))
    association = ("native Domain cell set shared by every implant/anneal/analysis step; "
                   "separate from the returned surface mesh")
    rows: list[dict[str, Any]] = []
    if format_ in {"vtu", "both"}:
        path = artifact_dir / f"{step_id}-cell-set-fields.vtu"
        cell_set.writeVTU(str(path))
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("native DenseCellSet.writeVTU did not produce a nonempty VTU file")
        rows.append(output_artifact(
            path, run_dir, run_id, step_id, "cellSetFields", "VTU",
            association=association, dimension=dimension, gridDelta=delta,
            numberOfCells=cells, coverMaterial=settings.get("coverMaterial", "GAS"),
            heightAboveSurface=float(settings["height"]), periodic=bool(settings.get("periodic", False)),
            scalarLabels=labels))
    if format_ in {"json", "both"}:
        payload = {
            "schemaVersion": 1,
            "source": "viennaps.Domain.getCellSet() after the native process",
            "nativeType": "DenseCellSet",
            "dimension": dimension,
            "association": association,
            "stepId": step_id,
            "nativeModel": (next(iter(record.values())) or {}).get("nativeModel") if record else None,
            "setup": {
                "heightAboveSurface": float(settings["height"]),
                "coverMaterial": settings.get("coverMaterial", "GAS"),
                "periodic": bool(settings.get("periodic", False)),
                "scalarLabels": labels,
                "nativeScalarLabels": [str(label) for label in cell_set.getScalarDataLabels()],
            },
            "numberOfCells": cells,
            "numberOfNodes": len(cell_set.getNodes()),
            "grid": {
                "gridDelta": delta,
                "nodes": len(cell_set.getNodes()),
                "elements": len(cell_set.getElements()),
                "elementNodes": len(cell_set.getElement(0)),
                "nativeElementType": "tetra" if dimension == 2 else "hexahedron",
                "logicalCellShape": "quad" if dimension == 2 else "hexahedron",
                "bounds": bounds,
            },
            "fields": field_rows,
            "fieldDigests": digests,
            "cellSetSetup": cell_set_record,
            # The native implant writes a volumetric concentration in both
            # dimensions (SheetResistance converts it with 1e12 cm^-3), so the
            # unit is 1/um^3 in 2D as well. ``integralNative`` is sum*delta^D:
            # with C in 1/um^3 the 2D value has unit 1/um, while the 3D value is
            # dimensionless and is omitted instead of being labelled.
            "concentrationUnit": f"1/{LENGTH_UNIT_NAME}^3",
            "concentrationUnitCm3": 1e12,
            **({"integralUnit": f"1/{LENGTH_UNIT_NAME}"} if dimension == 2 else {}),
            "process": record,
        }
        path = artifact_dir / f"{step_id}-cell-set-fields.json"
        atomic_json(path, payload)
        rows.append(output_artifact(
            path, run_dir, run_id, step_id, "cellSetFieldsSummary", "JSON",
            association=association, dimension=dimension, gridDelta=delta,
            numberOfCells=cells, coverMaterial=settings.get("coverMaterial", "GAS"),
            heightAboveSurface=float(settings["height"]), periodic=bool(settings.get("periodic", False)),
            scalarLabels=labels))
    return rows


def execute(input_path: Path, run_dir: Path) -> None:
    input_path = input_path.resolve()
    run_dir = run_dir.resolve()
    input_path.relative_to(ROOT)
    run_dir.relative_to(ROOT)
    config = json.loads(input_path.read_text(encoding="utf-8"), parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {item}")))
    project = config["project"]
    kind = config["kind"]
    run_id = config["runId"]
    run_seed = int(config["runSeed"]) if config.get("runSeed") is not None else None
    engine_preference = project["solver"]["enginePreference"]
    actual_steps = [step for step in project["steps"] if step["enabled"]] if kind == "simulate" else []
    if kind == "simulate" and any(step.get("model") == "plugin" for step in actual_steps):
        from process_models.v2.dispatch import normalize_plugin_step

        expanded = []
        for step in actual_steps:
            if step.get("model") == "plugin":
                normalized = dict(normalize_plugin_step(step))
                normalized["_pluginStep"] = True
                expanded.append(normalized)
            else:
                expanded.append(step)
        actual_steps = expanded
    step_engines = dict(config.get("stepEngines", {}))
    if kind == "simulate" and any(step.get("model") in CELL_SET_PROCESS_MODELS for step in actual_steps):
        # The native cell grid is captured once and reused by every later field
        # step. A hand-edited input.json never passes through the API validator,
        # so the shared-grid and pre-allocation rules run here, before any
        # native call.
        from backend.implant_models import field_geometry_issues, field_sequence_issues
        domain = project.get("domain") or {}
        domain_periodic = bool(domain.get("boundaryXY") == "periodic" if project["dimension"] == 3
                               else domain.get("xBoundary") == "periodic")
        preflight_messages = [
            message for _index, _path, message in field_sequence_issues(
                actual_steps, grid_delta=float(domain["gridDelta"]),
                domain_periodic=domain_periodic)
        ]
        preflight_messages.extend(
            message for _index, _path, message in field_geometry_issues(
                actual_steps, geometry=project.get("geometry"),
                dimension=int(project["dimension"]), grid_delta=float(domain["gridDelta"]),
                x_extent=float(domain["xExtent"]),
                y_extent=float(domain.get("yExtent", 0.0)),
                from_domain_state=project.get("continuationState") is not None)
        )
        if preflight_messages:
            raise RuntimeError("; ".join(preflight_messages))
    # Resolve every particle step before CUDA setup. This makes an omitted
    # stepEngines entry inherit an explicit solver.gpuEngine instead of
    # silently selecting GPU_DISK, and gates GPU_TRIANGLE exactly like the API.
    if kind == "simulate":
        engine_steps = [step for step in actual_steps if uses_flux(step)]
    elif kind == "flux":
        flux_id = config.get("analysisStepId")
        candidate = next((step for step in project["steps"] if step.get("id") == flux_id), None)
        engine_steps = [candidate] if candidate is not None and candidate.get("enabled") and uses_flux(candidate) else []
    else:
        engine_steps = []
    for step in engine_steps:
        resolved = requested_engine_for_step(project, step, step_engines)
        verify_worker_engine_evidence(project, step, resolved)
        step_engines[step["id"]] = resolved
    uses_cuda_engine = any(isinstance(engine, str) and engine.startswith("GPU_") for engine in step_engines.values())
    selected_gpu_device = None
    cuda_readback = None
    gpu_sample: dict[str, Any] | None = None
    if uses_cuda_engine:
        configured_device = config.get("selectedGpuDevice")
        if isinstance(configured_device, dict) and configured_device.get("uuid"):
            # The supervisor already enumerated and pinned this device; its
            # nvidia-smi inventory call costs 0.27-0.75 s on this host. The
            # CUDA Runtime readback below still verifies PCI bus and UUID.
            inventory = {"status": "available", "devices": [configured_device],
                         "preferredGpuDeviceId": configured_device.get("uuid"), "reason": None,
                         "probe": {"source": "supervisor-selected-device"}}
        else:
            inventory = cuda_devices()
        requested_device = project.get("solver", {}).get("gpuDeviceId")
        if configured_device and requested_device is None:
            requested_device = configured_device.get("uuid")
        selected_gpu_device = resolve_gpu_device(
            requested_device, inventory)
        if configured_device and configured_device.get("uuid") != selected_gpu_device["uuid"]:
            raise RuntimeError("worker device selection differs from the saved project's explicit GPU UUID")
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = selected_gpu_device["uuid"]
    flux_step = None
    if kind == "flux":
        flux_step_id = config.get("analysisStepId")
        flux_step = next((step for step in project["steps"] if step.get("id") == flux_step_id), None)
        if project["schemaVersion"] < 7 or not flux_step or not flux_step.get("enabled") or not uses_flux(flux_step):
            raise RuntimeError("single flux calculation requires an enabled schema-7 step with a native particle flux model")

    import viennaps as ps

    native_engine_fingerprint = config.get("nativeEngineFingerprint")
    if native_engine_fingerprint is not None:
        from backend.native_build import fingerprint_package
        observed = fingerprint_package(Path(ps.__file__).resolve().parent)
        if observed != native_engine_fingerprint:
            raise RuntimeError("worker loaded a different patched ViennaPS engine build")

    if uses_cuda_engine:
        cuda_readback = cuda_runtime_readback(selected_gpu_device)

    if project.get("schemaVersion", 1) >= 3:
        # Native ViennaPS is unit-agnostic; the declared project unit labels the
        # native metadata and the emitted snapshots without rescaling values.
        units = project.get("units") or {}
        ps.Length.setUnit(str(units.get("length") or "um"))
        ps.Time.setUnit(str(units.get("time") or "s"))

    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    ps_version = version("viennaps")
    ls_version = version("viennals")
    if ps_version != config["expectedVersions"]["viennaps"] or ls_version != config["expectedVersions"]["viennals"]:
        raise RuntimeError(f"installed package version changed: ViennaPS {ps_version}, ViennaLS {ls_version}")
    if (ps_version != "4.7.0"
            and any(step.get("model") in CELL_SET_PROCESS_MODELS
                    for step in project["steps"] if step.get("enabled"))):
        raise RuntimeError(
            f"ViennaPS {ps_version} does not provide the native IonImplantation / Anneal / NetDoping / "
            "SheetResistance classes; cell-set field steps require the ViennaPS 4.7.0 runtime")
    configure_native_materials(ps, config.get("customMaterials", []))
    if project.get("schemaVersion") == 7 and project.get("outputSettings", {}).get("nativeMetadata"):
        metadata_level = ps.MetaDataLevel.FULL
    else:
        metadata_level = None

    initial_state_path = None
    state_ref = project.get("continuationState")
    if state_ref is not None:
        if project["schemaVersion"] not in (6, 7):
            raise RuntimeError("native Domain continuation requires schemaVersion 6 or 7")
        initial_state_path = Path(config.get("initialStatePath", "")).resolve()
        try:
            initial_state_path.relative_to(ROOT)
        except ValueError as exc:
            raise RuntimeError("initial Domain state path escapes the workbench root") from exc
        if not initial_state_path.is_file() or file_sha256(initial_state_path) != state_ref["stateId"]:
            raise RuntimeError("initial Domain state file is missing or its SHA-256 does not match the project reference")
        if state_ref["viennapsVersion"] != ps_version or state_ref["viennalsVersion"] != ls_version:
            raise RuntimeError("initial Domain state engine versions do not match the installed ViennaPS/ViennaLS")

    geometry_readback = None
    initial_geometry_phase = phase("initialDomainAndGeometry")
    if project["dimension"] == 3:
        from mesh3d import make_domain, make_geometry
        import mesh3d
        mesh3d.configure_material_catalog(MATERIAL_ROWS)
        domain=make_domain(ps,project)
        if initial_state_path is not None:
            ps.Reader(domain, str(initial_state_path)).apply()
        elif project["schemaVersion"] >= 3:
            geometry_readback = make_v3_geometry(ps,domain,project,config=config,run_dir=run_dir)
        else:
            make_geometry(ps,domain,project["geometry"])
        readback_delta=float(domain.getGridDelta())
    else:
        boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"]["xBoundary"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
        # Keyword form is required: viennaps 4.7.0 binds the three positional
        # arguments to the (gridDelta, xExtent, yExtent) overload, silently
        # dropping the boundary (4.6.2 bound the (gridDelta, xExtent, boundary)
        # overload). Both versions accept the keyword form.
        domain = ps.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]), boundary=boundary)
        if initial_state_path is not None:
            ps.Reader(domain, str(initial_state_path)).apply()
        else:
            geometry = project["geometry"]
            if project["schemaVersion"] >= 3:
                geometry_readback = make_v3_geometry(ps,domain,project,config=config,run_dir=run_dir)
            elif geometry["kind"] == "plane":
                ps.MakePlane(domain, height=float(geometry["height"]), material=ps.Material.Si, addToExisting=False).apply()
            else:
                ps.MakeTrench(
                    domain=domain,
                    trenchWidth=float(geometry["width"]),
                    trenchDepth=float(geometry["depth"]),
                    trenchTaperAngle=float(geometry["taperAngle"]),
                    maskHeight=float(geometry["maskHeight"]),
                    maskTaperAngle=float(geometry["maskTaperAngle"]),
                    halfTrench=False,
                    material=ps.Material.Si,
                    maskMaterial=ps.Material.Mask,
                ).apply()
        readback_delta = float(domain.getGridDelta())
        if not math.isclose(readback_delta, float(project["domain"]["gridDelta"]), rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError(f"Domain gridDelta readback {readback_delta} differs from the requested value")
    initial_geometry_phase.stop()

    if metadata_level is not None:
        domain.enableMetaData(metadata_level)

    if state_ref is not None:
        observed_map = material_map(domain)
        if observed_map != state_ref["materialIdsByLevelSetIndex"]:
            raise RuntimeError("Reader material map differs from the saved Domain state reference")
        if int(domain.getNumberOfLevelSets()) != state_ref["levelSetCount"]:
            raise RuntimeError("Reader level-set count differs from the saved Domain state reference")
        if not math.isclose(float(domain.getGridDelta()), state_ref["gridDelta"], rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError("Reader gridDelta differs from the saved Domain state reference")
        observed_boundaries = ["reflective" if str(value) == "0" else "periodic" if str(value) == "2" else "infinite" if str(value) == "1" else f"unknown:{value}" for value in domain.getBoundaryConditions()]
        if observed_boundaries != state_ref["boundaryConditions"]:
            raise RuntimeError("Reader boundary conditions differ from the saved Domain state reference")

    # In ViennaPS 4.6.2 getBoundaryConditions() is only safe after a geometry
    # has been applied. Its enum instances also must not be converted with
    # int(); their stable string values are recorded after checking the enum.
    boundary_values = [str(value) for value in domain.getBoundaryConditions()]
    expected_x_boundary = "0" if project["domain"].get("xBoundary", project["domain"].get("boundaryXY")) == "reflective" else "2"
    if boundary_values != ([expected_x_boundary,expected_x_boundary,"1"] if project["dimension"]==3 else [expected_x_boundary,"1"]):
        raise RuntimeError(f"Domain boundary readback {boundary_values} differs from the requested x boundary and infinite h boundary")

    print("note: 'Process time: X / Y s' lines below report simulated process time, "
          "not wall-clock seconds.", flush=True)
    event(run_dir, {"type": "initializing", "runId": run_id})
    initial_name = f"新工艺起始状态 · {state_ref['sourceStepId'] if state_ref and state_ref.get('sourceStepId') else '导入的 Domain 状态'}" if state_ref else "初始结构"
    with phase("initialSnapshot"):
        initial = save_snapshot(ps, domain, run_dir, run_id=run_id, step_id="initial", step_name=initial_name, step_index=0,
                                requested=project,
                                extra={"geometryReadback": geometry_readback} if geometry_readback is not None else None)
    with phase("initialNativeState"):
        initial["nativeState"] = save_native_state(ps, domain, run_dir, project=project, run_id=run_id,
                                                   step_id="initial", viennaps_version=ps_version, viennals_version=ls_version)
    snapshots = [initial]
    # The native field cell set lives on a widened deep copy of the Domain and
    # is shared by every implant/anneal/analysis step of this run.
    field_state: dict[str, Any] = {}
    actual_execution: list[dict[str, Any]] = []
    intermediate_outputs: list[dict[str, Any]] = []
    event(run_dir, {"type": "snapshot", **initial})
    flux_result = None
    slice_result = None
    if kind == "slice":
        slice_request = config.get("sliceRequest")
        if project["dimension"] != 3 or not isinstance(slice_request, dict):
            raise RuntimeError("native Slice requires a 3D source domain")
        try:
            axis_bounds = slice_axis_bounds(project, slice_request)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        axis_label = axis_bounds["axisName"]
        slice_axis = axis_bounds["axis"]
        output_extent = axis_bounds["outputExtent"]
        lateral_axis, vertical_axis = axis_bounds["lateralAxis"], axis_bounds["verticalAxis"]
        normal = axis_bounds["normal"]
        reflect_x = bool(slice_request.get("reflectX", False))
        slice_position = float(slice_request["position"])
        if not math.isfinite(slice_position):
            raise RuntimeError("native Slice position must be finite")
        delta = float(project["domain"]["gridDelta"])
        # Native ViennaLS maps the two remaining coordinates into the output
        # x/h plane in ascending source-axis order:
        #   x-normal -> (y, z), y-normal -> (x, z), z-normal -> (x, y).
        # The slice position moves along the normal axis, so x/y are bounded
        # by that axis' (possibly halved) domain extent.
        if axis_bounds["min"] is not None:
            if not (axis_bounds["min"] - 1e-12 <= slice_position <= axis_bounds["max"] + 1e-12):
                note = f" ({axis_bounds['restriction']} limit)" if axis_bounds.get("restriction") else ""
                raise RuntimeError(
                    f"native Slice {axis_label} position {slice_position:g} lies outside the source "
                    f"{axis_label} range [{axis_bounds['min']:g}, {axis_bounds['max']:g}]{note}")
        else:
            # The z computational boundary is infinite; use the measured
            # source surface range instead of a lateral extent.
            bounds = initial.get("surfaceBounds") or []
            if len(bounds) == 2:
                axis_min, axis_max = float(bounds[0][2]), float(bounds[1][2])
                if slice_position < axis_min - delta or slice_position > axis_max + delta:
                    raise RuntimeError(
                        f"native Slice z position {slice_position:g} lies outside the source surface "
                        f"z range [{axis_min:g}, {axis_max:g}]")
        # Native ViennaLS uses std::round(): half away from zero. Python's
        # round() is ties-to-even and floor(q + 0.5) loses precision at
        # 0.49999999999999994, which would record a different position than
        # the plane native Slice actually selects.
        native_position = native_slice_position(slice_position, delta)
        ps.setDimension(2)
        slice_boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"]["boundaryXY"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
        sliced_domain = ps.d2.Domain(delta, output_extent, slice_boundary)
        slice_operation = ps.Slice(domain, sliced_domain, slice_axis, slice_position)
        if reflect_x:
            slice_operation.setReflectX(True)
        slice_operation.apply()
        if int(sliced_domain.getNumberOfLevelSets()) <= 0:
            raise RuntimeError(
                f"ViennaPS Slice returned no level sets at {axis_label}={slice_position:g}")
        slice_project = json.loads(json.dumps(project, ensure_ascii=False))
        slice_project["dimension"] = 2
        slice_project["domain"] = {"gridDelta":delta,
                                    "xExtent":output_extent,
                                    "xBoundary":project["domain"]["boundaryXY"]}
        slice_project.pop("view3D", None)
        mapped = f"{lateral_axis}/{vertical_axis} mapped to x/h"
        slice_readback = {"nativeOperation":"viennaps.Slice", "axis":slice_axis,
                          "axisLabel":f"{axis_label}-normal",
                          "axisName":axis_label,
                          "planeNormal":normal,
                          "outputCoordinateMapping":mapped,
                          "reflectX":reflect_x,
                          "requestedPosition":slice_position,
                          "position":native_position,
                          "positionAdjustedToGrid":native_position != slice_position,
                          "sourceDimension":3, "outputDimension":2,
                          "outputLateralExtent":output_extent,
                          "sourceAxisRange":[axis_bounds["min"], axis_bounds["max"]]
                          if axis_bounds["min"] is not None else "infinite",
                          "viewerClipping":False, "levelSetCount":int(sliced_domain.getNumberOfLevelSets()),
                          "materialIdsByLevelSetIndex":material_map(sliced_domain)}
        slice_result = save_snapshot(ps, sliced_domain, run_dir, run_id=run_id, step_id="true-slice",
                                     step_name="ViennaPS 真实 3D 几何切片", step_index=1, requested=slice_project,
                                     extra={"sliceReadback": slice_readback})
        snapshots.append(slice_result)
        event(run_dir, {"type":"snapshot", **slice_result})
        ps.setDimension(3)
    if flux_step is not None:
        selected_engine = step_engines.get(flux_step["id"])
        if selected_engine is None:
            selected_engine = requested_engine_for_step(project, flux_step, step_engines)
        verify_worker_engine_evidence(project, flux_step, selected_engine)
        if run_seed is None:
            raise RuntimeError("single flux calculation requires a recorded fixed random seed")
        flux_multi_ion = (flux_step.get("model") == "multiParticle"
                          and sum(row.get("kind") == "ion" for row in flux_step.get("species", [])) > 1
                          and selected_engine == "GPU_DISK")
        actual_flux_strategy = config.get("stepFluxStrategies", {}).get(flux_step["id"])
        if (flux_multi_ion and actual_flux_strategy not in {GPU_MULTIPARTICLE_FLUX_STRATEGY, GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY}
                or not flux_multi_ion and actual_flux_strategy is not None):
            raise RuntimeError("single-pass GPU MultiParticle flux strategy does not match the exact worker configuration")
        if actual_flux_strategy == GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY and not native_engine_fingerprint:
            raise RuntimeError("native multi-ion GPU flux requires a pinned engine build fingerprint")
        expected_flux_strategy = actual_flux_strategy
        if config.get("stepExecutionStrategies", {}).get(flux_step["id"]) is not None:
            raise RuntimeError("single-pass flux must not claim the process-apply operator-splitting strategy")
        event(run_dir, {"type": "step-started", "stepId": flux_step["id"], "index": 1, "total": 1, "name": flux_step["name"]})
        signed_rate = None if flux_step["model"] in {"multiParticle", "specialized"} else signed_rates(flux_step)[0]
        flux_result = calculate_single_flux(ps, domain, project, flux_step, selected_engine, run_seed, run_dir,
                                            flux_strategy=actual_flux_strategy)
        if selected_engine.startswith("GPU_"):
            post_flux_cuda_readback = cuda_runtime_readback(selected_gpu_device)
            gpu_usage = nvidia_process_readback(selected_gpu_device["uuid"])
            if gpu_sample is None:
                gpu_sample = nvidia_gpu_sample()
            flux_result["cudaDeviceReadback"] = {**cuda_readback, "afterCalculateFlux": post_flux_cuda_readback,
                                                  "process": gpu_usage, "gpuSample": gpu_sample}
            flux_path = run_dir / flux_result["fileName"]
            flux_payload = json.loads(flux_path.read_text(encoding="utf-8"))
            flux_payload["execution"]["cudaDeviceReadback"] = flux_result["cudaDeviceReadback"]
            if expected_flux_strategy is not None:
                flux_payload["execution"]["gpuDeviceId"] = selected_gpu_device["uuid"]
                flux_payload["execution"]["multiParticleFlux"]["gpuDeviceId"] = selected_gpu_device["uuid"]
                for species_record in flux_payload["execution"]["multiParticleFlux"]["species"]:
                    species_record["gpuDeviceId"] = selected_gpu_device["uuid"]
            atomic_json(flux_path, flux_payload)
            actual_execution.append({"stepId": flux_step["id"], "engine": selected_engine,
                                     "seed": run_seed, "gpuDeviceId": selected_gpu_device["uuid"],
                                     **({"fluxStrategy": expected_flux_strategy} if expected_flux_strategy else {})})
        else:
            actual_execution.append({"stepId": flux_step["id"], "engine": selected_engine, "seed": run_seed})
        flux_execution=json.loads((run_dir / flux_result["fileName"]).read_text(encoding="utf-8"))["execution"]
        event(run_dir, {"type": "step-configured", "stepId": flux_step["id"], "operation": "flux",
                        "model": flux_step["model"], "noOp": False, "actualEngine": selected_engine,
                        "engineSelectionReason": "pinned concrete engine for ViennaPS Process.calculateFlux; no fallback",
                        "requestedEnginePreference": engine_preference, "actualSeed": run_seed, "actualRate": signed_rate,
                        "duration": None, "actualParameters": {"result": "single-pass flux mesh; process.apply was not called",
                                                                   "rays": flux_execution["rays"],
                                                                   **({"nativeModel": flux_execution["nativeModel"]} if "nativeModel" in flux_execution else {}),
                                                                   **({"multiParticle": flux_execution["multiParticle"]}
                                                                      if "multiParticle" in flux_execution else {}),
                                                                   **({"rateCallback": flux_execution["rateCallback"]}
                                                                      if "rateCallback" in flux_execution else {}),
                                                                   **({"multiParticleFlux": flux_execution["multiParticleFlux"]}
                                                                      if "multiParticleFlux" in flux_execution else {}),
                                                                   **({"cudaDeviceReadback": flux_result["cudaDeviceReadback"]}
                                                                      if "cudaDeviceReadback" in flux_result else {}),
                                                                   "calculateFluxSeconds": flux_execution["elapsedSeconds"]}})
        if selected_engine.startswith("GPU_"):
            event(run_dir, {"type": "step-device-readback", "stepId": flux_step["id"],
                            "cudaDeviceReadback": flux_result["cudaDeviceReadback"]})
        event(run_dir, {"type": "flux-result", **flux_result})

    ctx = RunContext(
        project=project,
        ps=ps,
        domain=domain,
        run_dir=run_dir,
        run_id=run_id,
        config=config,
        actual_steps=actual_steps,
        step_engines=step_engines,
        engine_preference=engine_preference,
        run_seed=run_seed,
        selected_gpu_device=selected_gpu_device,
        cuda_readback=cuda_readback,
        gpu_sample=gpu_sample,
        native_engine_fingerprint=native_engine_fingerprint,
        ps_version=ps_version,
        ls_version=ls_version,
        snapshots=snapshots,
        field_state=field_state,
        actual_execution=actual_execution,
        intermediate_outputs=intermediate_outputs,
    )
    process_handles: ProcessStepResult | None = None
    for index, step in enumerate(actual_steps, start=1):
        event(run_dir, {"type": "step-started", "stepId": step["id"], "index": index, "total": len(actual_steps), "name": step["name"]})
        geometry_step = is_geometry_step(step)
        no_op = not geometry_step and not has_motion(step) and not step.get("_pluginStep")
        if step.get("model") in CELL_SET_PROCESS_MODELS:
            metadata = run_cell_set_step(ctx, index, step)
        elif geometry_step:
            metadata = run_geometry_step(ctx, index, step)
        elif no_op:
            metadata = run_no_op_step(ctx, index, step)
        else:
            process_handles = run_process_step(ctx, index, step)
            metadata = process_handles.metadata
        # A surface-moving step invalidates the native field cell set. When a
        # later field step still needs it, regenerate the grid now and remap the
        # scalar fields, so the record names the exact step that moved the
        # surface (a lazy remap would collapse several moves into one entry).
        if (field_state.get("grid") is not None
                and step.get("model") not in CELL_SET_PROCESS_MODELS
                and (geometry_step or not no_op)):
            # Defensive: even when no field step follows, remember that the
            # captured grid no longer matches the level sets.
            field_state["surfaceDirty"] = True
            if any(future.get("model") in CELL_SET_PROCESS_MODELS
                   for future in actual_steps[index:]):
                remap_record = remap_field_cell_set(ps, domain, project, field_state, step)
                event(run_dir, {"type": "cell-set-remap", "afterStepId": step["id"],
                                **remap_record})
        snapshots.append(metadata)
        event(run_dir, {"type": "snapshot", **metadata})
        future_gpu_process = any(
            uses_flux(future)
            and str(step_engines.get(
                future["id"], "GPU_DISK" if engine_preference == "gpu" else "CPU_DISK")).startswith("GPU_")
            for future in actual_steps[index:]
        )
        if actual_execution[-1]["engine"].startswith("GPU_") and future_gpu_process:
            # ViennaRay 0fe9 reuses a registered OptiX context, but its second
            # disk-geometry build was reported to fail after the Domain has
            # changed. Dropping the completed native objects is always safe;
            # tearing down the global context costs a fresh CUDA/OptiX context
            # plus kernel-module/JIT setup for the next GPU step (0.3-0.9 s).
            # worker/profile_segments.py --mode reuse reproduces two consecutive
            # GPU steps in one process; with the installed engine build the
            # second build succeeds, so context reuse can be requested
            # explicitly. The conservative reset stays the default until reuse
            # is verified for the full per-run engine matrix.
            process_handles = None
            import gc
            gc.collect()
            if GPU_CONTEXT_REUSE:
                event(run_dir, {"type": "gpu-context-reuse", "afterStepId": step["id"],
                                "nextGpuStepPending": True,
                                "registeredDevices": [int(value) for value in ps.gpu.Context.getRegisteredDeviceIDs()]})
            else:
                reset = reset_native_gpu_context(ps, selected_gpu_device)
                event(run_dir, {"type": "gpu-context-reset", "afterStepId": step["id"],
                                "nextGpuStepPending": True, "context": reset})

    worker_events=read_worker_events(run_dir)
    step_records=[record for record in worker_events if record.get("type")=="step-configured"]
    step_records_by_id={record.get("stepId"):record for record in step_records}
    for device_record in (record for record in worker_events if record.get("type") == "step-device-readback"):
        configured = step_records_by_id.get(device_record.get("stepId"))
        if configured is not None:
            configured.setdefault("actualParameters", {}).update({
                key: value for key, value in device_record.items()
                if key not in {"type", "stepId"}
            })
    for performance_record in (record for record in worker_events if record.get("type") == "step-performance"):
        configured = step_records_by_id.get(performance_record.get("stepId"))
        if configured is not None:
            configured.setdefault("actualParameters", {})["processApplySeconds"] = performance_record["processApplySeconds"]
    for callback_record in (record for record in worker_events if record.get("type")=="multi-particle-rate-callback"):
        configured=step_records_by_id.get(callback_record.get("stepId"))
        if configured is not None:
            configured.setdefault("actualParameters",{})["rateCallbackSummary"]={
                "invocationCount":callback_record.get("invocationCount"),
                "materials":callback_record.get("materials"),
                "samples":callback_record.get("samples"),
                "fluxSums":callback_record.get("fluxSums"),
                "fluxMaxima":callback_record.get("fluxMaxima"),
                "meanFluxBySpecies":callback_record.get("meanFluxBySpecies"),
                **({"callbackCountBySpecies": callback_record["callbackCountBySpecies"]}
                   if callback_record.get("callbackCountBySpecies") else {}),
            }
    for split_record in (record for record in worker_events if record.get("type") == "multi-particle-gpu-split"):
        configured = step_records_by_id.get(split_record.get("stepId"))
        if configured is not None:
            configured.setdefault("actualParameters", {})["multiParticleExecution"] = split_record["execution"]
    worker_result = {
        "schemaVersion": 1,
        "runId": run_id,
        "kind": kind,
        "versions": {"viennaps": ps_version, "viennals": ls_version},
        **({"nativeEngineFingerprint": native_engine_fingerprint} if native_engine_fingerprint else {}),
        "actualSeed": run_seed,
        "cpuThreads": int(project["solver"]["cpuThreads"]),
        "selectedGpuDevice": selected_gpu_device,
        "cudaDeviceReadback": cuda_readback,
        "actualSteps": actual_execution,
        "stepRecords": step_records,
        "fluxResult": flux_result,
        "sliceResult": slice_result,
        "intermediateOutputs": intermediate_outputs,
        "snapshots": snapshots,
        "gridDeltaReadback": readback_delta,
        "xExtentRequested": float(project["domain"]["xExtent"]),
        "boundaryValuesReadback": boundary_values,
        # Domain.getBoundingBox() calls ToDiskMesh(), whose ViennaLS 5.8.5
        # expansion mutates the source level set in candidate builds. The
        # saved snapshot already contains safe surface points; derive bounds
        # from those points instead of invoking the mutating native getter.
        "boundingBoxReadback": snapshots[-1].get("surfaceBounds") if snapshots else None,
        "boundingBoxReadbackSource": "saved snapshot mesh points; mutating native getter omitted",
        "initialGeometryReadback": geometry_readback,
    }
    with phase("workerResultWrite"):
        atomic_json(run_dir / "worker-result.json", worker_result)
    if PROFILE_ENABLED:
        event(run_dir, {"type": "segment-timing", "phases": PROFILE_PHASES,
                        "note": "worker-side phases only; process.apply() is timed separately"})
    event(run_dir, {"type": "worker-complete", "snapshotCount": len(snapshots)})


def read_worker_events(run_dir: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in (run_dir / "events.ndjson").read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError):
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        execute(args.input, args.run_dir)
        print("WORKER_COMPLETE", flush=True)
        return 0
    except BaseException as exc:
        try:
            event(args.run_dir.resolve(), {"type": "worker-failed", "error": f"{type(exc).__name__}: {exc}"})
        except BaseException:
            pass
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
