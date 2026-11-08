"""Native ``viennaps.CSVFileProcess`` construction for schema-7 csvProcess steps.

The worker never accepts a path from submitted JSON: the supervisor copies the
content-addressed asset into the isolated run directory and passes that path
through ``input.json``. This module re-verifies the copy's SHA-256, re-parses
and re-canonicalizes the table with the same bounded parser used at upload time,
and only then calls the real native constructor. A missing, non-canonical or
malformed table raises instead of letting ``RateGrid`` fall back to a zero
velocity field or an unsafe LINEAR parse.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from backend.csv_assets import RateCsvError, canonicalize_rate_csv
from process_models.v2.engines.viennaps import protected_material_names


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_csv_process(ps: Any, project: dict[str, Any], step: dict[str, Any], *,
                      config: dict[str, Any] | None, run_dir: Path | None,
                      material_handle: Any, material_ids: dict[str, int]) -> tuple[Any, dict[str, Any]]:
    if config is None or run_dir is None:
        raise RuntimeError("csvProcess requires an isolated run input and artifact directory")
    asset_id = str(step["assetId"])
    configured = (config.get("csvAssets") or {}).get(asset_id)
    if not isinstance(configured, str) or not configured:
        raise RuntimeError("csvProcess asset copy is missing from the run input")
    source = Path(configured).resolve()
    try:
        source.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise RuntimeError("csvProcess asset copy must remain inside the current run directory") from exc
    if not source.is_file():
        raise RuntimeError("csvProcess asset copy is missing from the run directory")
    observed = _sha256(source)
    if observed != asset_id:
        raise RuntimeError("csvProcess asset copy does not match the project's SHA-256")

    dimension = int(project["dimension"])
    try:
        raw = source.read_bytes()
        canonical, summary = canonicalize_rate_csv(raw, dimension)
    except (OSError, RateCsvError) as exc:
        raise RuntimeError(f"csvProcess asset is not a valid {dimension}D rate table: {exc}") from exc
    if canonical != raw:
        raise RuntimeError("csvProcess asset copy is not in the canonical ASCII/LF format; re-upload the table")
    if hashlib.sha256(canonical).hexdigest() != observed:
        raise RuntimeError("csvProcess canonical table SHA-256 differs from the run copy")
    mode = str(step["interpolationMode"])
    if mode == "LINEAR" and not summary["linearCompatible"]:
        raise RuntimeError("csvProcess LINEAR interpolation requires a table the native RateGrid can interpolate")
    if mode == "IDW" and int(step.get("idwNeighbors") or 0) > int(summary["rowCount"]):
        raise RuntimeError("csvProcess idwNeighbors exceeds the table's row count")

    raw_direction = [float(value) for value in step["direction"]]
    length = math.sqrt(sum(value * value for value in raw_direction))
    if not math.isfinite(length) or length <= 1e-12:
        raise RuntimeError("csvProcess direction must be a finite nonzero vector")
    native_direction = [value / length for value in raw_direction]
    offset = [float(value) for value in step["offset"]]
    mask_names = sorted(protected_material_names(project))
    mask_materials = [material_handle(name) for name in mask_names]
    visibility = bool(step["calculateVisibility"])

    model = ps.CSVFileProcess(
        str(source), native_direction, offset,
        float(step["isotropicComponent"]), float(step["directionalComponent"]),
        mask_materials, visibility)
    # The mode is mandatory in the schema; CUSTOM/custom interpolators are not
    # expressible, so this is the only interpolation setter the worker calls.
    model.setInterpolationMode(mode)
    if mode == "IDW":
        model.setIDWNeighbors(int(step["idwNeighbors"]))
    model.setOffset(offset)

    resolved = {
        "nativeClass": "CSVFileProcess",
        "nativeRateGridClass": "RateGrid",
        "assetId": asset_id,
        "assetSha256": observed,
        "assetFileName": str(step["fileName"]),
        "assetCopy": source.name,
        "canonicalFormat": summary["canonicalFormat"],
        "dimension": dimension,
        "csvSchema": summary["columns"],
        "csvRowCount": summary["rowCount"],
        "csvXRange": summary["x"],
        **({"csvYRange": summary["y"]} if "y" in summary else {}),
        "csvRateRange": summary["rate"],
        "csvLinearCompatible": summary["linearCompatible"],
        "requestedDirection": raw_direction,
        "nativeUnitDirection": native_direction,
        "offset": offset,
        "offsetSemantics": ("RateGrid evaluates the table at coordinate+offset; "
                            "the 2D grid has one lateral axis so offset[1] is fixed at 0"),
        "isotropicComponent": float(step["isotropicComponent"]),
        "directionalComponent": float(step["directionalComponent"]),
        "interpolationMode": mode,
        "interpolationModeSource": "explicit schema field; setCustomInterpolator/custom Python is unreachable",
        "idwNeighbors": int(step["idwNeighbors"]) if mode == "IDW" else None,
        "calculateVisibility": visibility,
        "maskMaterials": mask_names,
        "maskMaterialIds": [material_ids.get(name) for name in mask_names],
        "operation": "native",
        "signConvention": ("positive CSV rates with a downward direction move the surface outward "
                           "(deposit); negative rates etch. The native model has no separate "
                           "etch/deposit parameter."),
        "nativeProcessEngine": "not-applicable; CSVFileProcess has no stochastic flux engine",
    }
    return model, resolved
