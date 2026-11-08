"""Audit product per-species GPU flux output against independent native CPU flux.

This file reads saved artifacts only. Native CPU/GPU work is executed in
separate calls through scripts/bounded_child.py.
"""
from __future__ import annotations

import base64
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FLOAT_SIZES = {"Float32": 4, "Float64": 8, "Int8": 1, "UInt8": 1, "Int16": 2,
               "UInt16": 2, "Int32": 4, "UInt32": 4, "Int64": 8, "UInt64": 8}
FLOAT_STRUCTS = {"Float32": "f", "Float64": "d"}


def guard_ok(path: Path) -> bool:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (row.get("returnCode") == 0 and not row.get("timedOut") and not row.get("rssLimited")
            and not row.get("logLimited") and not row.get("remainingOwnedGroupMembers"))


def _payload(root) -> str:
    node = root.find("AppendedData")
    text = "".join((node.text or "").split()) if node is not None else ""
    if not text.startswith("_"):
        raise ValueError("VTP has no appended-data sentinel")
    return text[1:]


def _chunks(root, payload: str) -> dict[Any, str]:
    arrays = [node for node in root.findall(".//DataArray") if node.attrib.get("format") == "appended"]
    offsets = {node: int(node.attrib["offset"]) for node in arrays}
    sorted_offsets = sorted(set(offsets.values()))
    result = {}
    for node, start in offsets.items():
        end = next((offset for offset in sorted_offsets if offset > start), len(payload))
        result[node] = payload[start:end]
    return result


def decode_vtk_array(root, array, chunks: dict[Any, str]) -> bytes:
    if array.attrib.get("format") != "appended":
        raise ValueError(f"expected appended VTK DataArray {array.attrib.get('Name')!r}")
    chunk = chunks[array]
    header_type = root.attrib.get("header_type", "UInt32")
    if header_type not in ("UInt32", "UInt64"):
        raise ValueError(f"unsupported VTK header type {header_type}")
    header_width = 4 if header_type == "UInt32" else 8
    integer_code = "I" if header_width == 4 else "Q"
    order = "<" if root.attrib.get("byte_order", "LittleEndian") == "LittleEndian" else ">"
    # The VTK compressed block header starts with (numBlocks, blockSize,
    # lastBlockSize, compressedSize[]). Its header and compressed blocks are
    # separately base64 encoded by ViennaLS, so decode them independently.
    prefix = struct.unpack_from(order + integer_code,
                                base64.b64decode(chunk[:8] if header_width == 4 else chunk[:12]))[0]
    if prefix < 1 or prefix > 100000:
        raise ValueError(f"invalid compressed VTK block count {prefix}")
    header_bytes = header_width * (3 + prefix)
    header_chars = ((header_bytes + 2) // 3) * 4
    header = base64.b64decode(chunk[:header_chars])
    values = struct.unpack(order + integer_code * (3 + prefix), header)
    num_blocks, block_size, last_size, *compressed_sizes = values
    if num_blocks != prefix or any(size <= 0 for size in compressed_sizes):
        raise ValueError("invalid compressed VTK block header")
    data = chunk[header_chars:]
    cursor = 0
    decoded = []
    for compressed_size in compressed_sizes:
        chars = ((compressed_size + 2) // 3) * 4
        compressed = base64.b64decode(data[cursor:cursor + chars])
        if len(compressed) != compressed_size:
            raise ValueError("compressed VTK block length does not match its header")
        decoded.append(zlib.decompress(compressed))
        cursor += chars
    if cursor != len(data):
        raise ValueError("compressed VTK array contains trailing or missing bytes")
    output = b"".join(decoded)
    expected = (num_blocks - 1) * block_size + last_size
    if len(output) != expected:
        raise ValueError("decompressed VTK array length does not match its block header")
    scalar_size = FLOAT_SIZES.get(array.attrib.get("type", ""))
    if scalar_size is None or len(output) % scalar_size:
        raise ValueError(f"VTK array {array.attrib.get('Name')!r} has an invalid scalar byte length")
    return output


def vtk_report(path: Path) -> dict[str, Any]:
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    if root.attrib.get("type") != "PolyData":
        raise ValueError("combined flux VTP is not PolyData")
    piece = root.find(".//Piece")
    if piece is None:
        raise ValueError("combined flux VTP has no Piece")
    payload = _payload(root)
    chunks = _chunks(root, payload)
    counts = {"pointData": int(piece.attrib.get("NumberOfPoints", 0)),
              "cellData": sum(int(piece.attrib.get(f"NumberOf{name}", 0))
                              for name in ("Verts", "Lines", "Strips", "Polys"))}
    result = {"pointCount": counts["pointData"], "cellCount": counts["cellData"],
              "arrays": {"pointData": {}, "cellData": {}, "fieldData": {}},
              "decompressedDataArrayCount": 0, "appendedPayloadCharacters": len(payload)}
    for container, association in (("PointData", "pointData"), ("CellData", "cellData"), ("FieldData", "fieldData")):
        data = piece.find(container)
        if data is None:
            continue
        for array in data.findall("DataArray"):
            name = array.attrib.get("Name")
            if not name or name in result["arrays"][association]:
                raise ValueError(f"VTP {association} has a missing or duplicate field name")
            values = decode_vtk_array(root, array, chunks)
            result["arrays"][association][name] = {
                "type": array.attrib["type"],
                "components": int(array.attrib.get("NumberOfComponents", 1)),
                "tuples": len(values) // FLOAT_SIZES[array.attrib["type"]]
                           // int(array.attrib.get("NumberOfComponents", 1)),
                "bytes": values,
            }
            result["decompressedDataArrayCount"] += 1
    return result


def relative_errors(reference: list[float], candidate: list[float]) -> dict[str, float]:
    if len(reference) != len(candidate) or not reference:
        raise ValueError("CPU and GPU native flux fields have different tuple counts")
    errors = [abs(float(left) - float(right)) / max(abs(float(left)), 1e-12)
              for left, right in zip(reference, candidate)]
    return {"meanRelativeError": sum(errors) / len(errors), "maxRelativeError": max(errors),
            "referenceMinimum": min(reference), "referenceMaximum": max(reference)}


def main(project_path: Path, cpu_path: Path, worker_dir: Path, cpu_guard_path: Path,
         gpu_guard_path: Path, output_path: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    worker = json.loads((worker_dir / "worker-result.json").read_text(encoding="utf-8"))
    flux = json.loads((worker_dir / "flux.json").read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row.get("enabled"))
    vtp = vtk_report(worker_dir / "flux.vtp")
    device = worker["selectedGpuDevice"]
    readback = flux["execution"].get("cudaDeviceReadback", {})
    particle_flux = flux["execution"].get("multiParticleFlux", {})
    ray_count = int(project["solver"]["raysPerPoint"])
    max_relative_error = max(0.08, 0.15 * math.sqrt(256 / ray_count))
    species_comparisons = []
    field_set_checks = []
    for index, species in enumerate(step["species"]):
        label = species["label"]
        cpu_field = next((name for name in cpu["cellData"]["scalars"]
                          if name == f"{label}{index}"), None)
        gpu_field = next((row["name"] for row in particle_flux["species"][index]["fields"]
                          if row["association"] == "cellData" and label in row["name"]
                          and row["name"] not in {"LSValues", "MaterialIds"}), None)
        if cpu_field is None or gpu_field is None:
            species_comparisons.append({"label": label, "status": "missing-field"})
            continue
        reference_values = cpu["cellData"]["scalars"][cpu_field]
        actual_values = flux["cellData"]["scalars"].get(gpu_field)
        if actual_values is None:
            species_comparisons.append({"label": label, "status": "missing-merged-field"})
            continue
        error = relative_errors(reference_values, actual_values)
        species_vtk = vtp["arrays"]["cellData"].get(gpu_field)
        decoded_match = False
        if species_vtk and species_vtk["type"] in FLOAT_STRUCTS:
            fmt = "<" + FLOAT_STRUCTS[species_vtk["type"]] * len(actual_values)
            decoded = list(struct.unpack(fmt, species_vtk["bytes"]))
            decoded_match = (len(decoded) == len(actual_values)
                             and max(abs(float(a) - float(b)) for a, b in zip(decoded, actual_values)) <= 1e-5)
        species_comparisons.append({"label": label, "cpuField": cpu_field, "gpuField": gpu_field,
                                    **error, "withinRaySamplingTolerance": error["maxRelativeError"] <= max_relative_error,
                                    "mergedVtpValuesMatchFluxJson": decoded_match})
        field_set_checks.append(bool(species_vtk and decoded_match))

    expected_fields = {(field["association"], field["name"]): field for field in flux["fields"]}
    vtp_fields = {(association, name): row for association in ("pointData", "cellData")
                  for name, row in vtp["arrays"][association].items()}
    arrays_align = all(key in vtp_fields
                       and vtp_fields[key]["tuples"] == field["tuples"]
                       and vtp_fields[key]["components"] == field["components"]
                       for key, field in expected_fields.items())
    checks = {
        "versionsInstalled": worker["versions"] == {"viennaps": "4.6.2", "viennals": "5.8.5"}
                             and cpu["versions"] == {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "cpuAndGpuBoundedChildrenClean": guard_ok(cpu_guard_path) and guard_ok(gpu_guard_path),
        "gpuDiskOnPhysicalRtx3060": flux["execution"]["actualEngine"] == "GPU_DISK"
                                   and device["uuid"] == "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
        "cudaReadbacksMatchSelectedUuid": (readback.get("readbackStatus") == "verified"
                                           and readback.get("uuid") == device["uuid"]
                                           and readback.get("afterCalculateFlux", {}).get("readbackStatus") == "verified"
                                           and readback.get("afterCalculateFlux", {}).get("uuid") == device["uuid"]),
        "usesVerifiedPerSpeciesNativeFluxStrategy": particle_flux.get("strategy") == "per-species-native-mesh-merge-v1"
                                                 and particle_flux.get("speciesOrder") == [row["label"] for row in step["species"]]
                                                 and particle_flux.get("allNativeMeshesIdentical") is True,
        "cpuGpuFluxMeshCoordinatesAndTopologyMatch": (flux["points"] == cpu["points"]
                                                       and flux["topology"] == cpu["topology"]
                                                       and flux["meshIdentity"] == worker["fluxResult"]["meshIdentity"]),
        "materialIdsAndLevelSetValuesMatch": (flux["cellData"]["scalars"].get("MaterialIds")
                                              == cpu["cellData"]["scalars"].get("MaterialIds")
                                              and flux["cellData"]["scalars"].get("LSValues")
                                              == cpu["cellData"]["scalars"].get("LSValues")),
        "everySpeciesHasNativeMergedVtpField": len(field_set_checks) == len(step["species"])
                                               and all(field_set_checks),
        "allVtpFieldsPreserveAssociationAndTupleCounts": arrays_align,
        "cpuReferenceAgreementWithinRaySamplingTolerance": len(species_comparisons) == len(step["species"])
                                                          and all(row.get("withinRaySamplingTolerance")
                                                                  for row in species_comparisons),
        "didNotApplyProcess": flux["execution"]["processDuration"] == "not-used-by-single-pass-calculateFlux",
    }
    report = {"status": "passed" if all(checks.values()) else "failed",
              "scope": "bounded product worker GPU per-species calculateFlux and merged native VTP versus independent direct CPU MultiParticle calculateFlux",
              "input": {"project": str(project_path), "dimension": project["dimension"],
                        "operation": step["operation"], "geometryKind": project["geometry"]["kind"],
                        "species": step["species"], "rateCoefficients": step["rateCoefficients"],
                        "raysPerPoint": ray_count, "seed": flux["execution"]["actualSeed"],
                        "maxRelativeErrorAccepted": max_relative_error},
              "device": device, "speciesComparisons": species_comparisons,
              "nativeVtp": {key: value for key, value in vtp.items() if key != "arrays" and key != "decompressedDataArrayCount"},
              "checks": checks, "guards": {"cpu": str(cpu_guard_path), "gpu": str(gpu_guard_path)},
              "artifacts": {"cpuDirect": str(cpu_path), "productWorker": str(worker_dir / "worker-result.json"),
                            "productFlux": str(worker_dir / "flux.json"), "mergedVtp": str(worker_dir / "flux.vtp")}}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "speciesComparisons": species_comparisons, "output": str(output_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 7:
        raise SystemExit("usage: compare_multiparticle_product_flux.py PROJECT CPU_JSON WORKER_DIR CPU_GUARD GPU_GUARD OUTPUT")
    main(*(Path(value) for value in sys.argv[1:]))
