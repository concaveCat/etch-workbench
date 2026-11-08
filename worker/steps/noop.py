"""Zero-rate (no-op) steps: copy the predecessor snapshot without a native call."""

from __future__ import annotations

import json
import math
import os
import shutil
from typing import Any

from worker.artifacts import atomic_json, check_artifact_budget
from worker.steps.context import RunContext, simulate_module


def run_no_op_step(ctx: RunContext, index: int, step: dict[str, Any]) -> dict[str, Any]:
    simulate = simulate_module()
    if os.environ.get("VIENNAPS_WORKBENCH_V2_EXECUTION") == "1":
        from process_models.v2.dispatch import dispatch_step
        from worker.v2_execution import ensure_session

        decision = dispatch_step(ensure_session(ctx).registry, ctx.project, step)
        ctx.event(
            {
                "type": "v2-execution-noop",
                "stepId": step["id"],
                "dispatch": decision.kind,
                "reason": decision.error or decision.reason,
            }
        )
    no_op_parameters: dict[str, Any] = {}
    if ctx.project["schemaVersion"] in (4, 5, 6, 7) and step.get("rateMode") == "material":
        fallback, no_op_rates = simulate.signed_rates(step)
        no_op_parameters = {
            "rateMode": step["rateMode"],
            "actualDefaultRate": fallback,
            "actualMaterialRatesByName": no_op_rates,
            "actualMaterialRatesById": {str(simulate.MATERIAL_IDS[name]): value for name, value in no_op_rates.items()},
            "nativeRateTable": [
                {"name": name, "id": simulate.MATERIAL_IDS[name], "rate": value}
                for name, value in sorted(no_op_rates.items())
            ],
        }
    if ctx.project["schemaVersion"] in (5, 6, 7) and step.get("model") == "directional":
        if step.get("rateSets") is not None:
            records = simulate.directional_rate_set_records(ctx.project, step)
            for record in records:
                record["calculateVisibility"] = "not-applicable: no rate is active"
            no_op_parameters = {
                "mode": "rateSets",
                "nativeOverload": simulate.RATE_SET_OVERLOAD,
                "rateSets": records,
                "rateSetCount": len(records),
                "maskMaterials": records[0]["maskMaterials"] if records else [],
                "maskMaterialIds": records[0]["maskMaterialIds"] if records else [],
                "maskSource": records[0]["maskSource"] if records else None,
                "nativeProcess": "viennaps.DirectionalProcess (not called: every rate is zero)",
            }
        elif simulate.directional_material_mode(step):
            material_record = simulate.directional_material_rate_records(step)
            raw_direction = [float(value) for value in (step.get("direction") or [0.0, 0.0, 0.0])]
            length = math.sqrt(sum(value * value for value in raw_direction))
            no_op_parameters = {
                "mode": "materialRates",
                "rateMode": "material",
                "nativeClass": "DirectionalProcess",
                "nativeOverload": simulate.DIRECTIONAL_MATERIAL_OVERLOAD,
                "direction": raw_direction,
                "nativeUnitDirection": [value / length for value in raw_direction] if length > 1e-12 else None,
                "materialRates": material_record["rows"],
                "defaultMaterialRate": material_record["requestedDefault"],
                "defaultMaterialRateOmitted": material_record["defaultOmitted"],
                "nativeDefaultDirectionalRate": material_record["nativeDefault"][0],
                "nativeDefaultIsotropicRate": material_record["nativeDefault"][1],
                "directionalSign": material_record["directionalSign"],
                "isotropicSign": material_record["isotropicSign"],
                "calculateVisibility": "not-applicable: no rate is active",
                "nativeProcess": "viennaps.DirectionalProcess (not called: every rate is zero)",
            }
        else:
            native_directional, native_isotropic = simulate.signed_directional_rates(step)
            raw_direction = [float(value) for value in step["direction"]]
            length = math.sqrt(sum(value * value for value in raw_direction))
            no_op_parameters = {
                "direction": raw_direction,
                "nativeUnitDirection": [value / length for value in raw_direction],
                "directionalRateMagnitude": float(step["directionalRateMagnitude"]),
                "isotropicRateMagnitude": float(step["isotropicRateMagnitude"]),
                "nativeDirectionalVelocity": native_directional,
                "nativeIsotropicVelocity": native_isotropic,
                "calculateVisibility": "not-applicable: no rate is active",
            }
    if step.get("model") == "multiParticle":
        no_op_parameters = {
            "nativeProcess": "viennaps.MultiParticleProcess (not called: zero coefficient)",
            "species": step["species"],
            "rateCoefficients": step["rateCoefficients"],
            "rateFunction": "not-applicable: all configured coefficients are zero",
        }
    previous = ctx.snapshots[-1]
    next_vtk_name = f"{step['id']}.vtk"
    if previous.get("vtk"):
        shutil.copyfile(ctx.run_dir / previous["vtk"], ctx.run_dir / next_vtk_name)
    previous_path = ctx.run_dir / previous["snapshot"]
    copied = None
    if previous_path.is_file():
        copied = json.loads(previous_path.read_text(encoding="utf-8"))
        copied_mesh = dict(copied["mesh"])
        if previous.get("vtk"):
            copied_mesh["vtk"] = next_vtk_name
        else:
            copied_mesh.pop("vtk", None)
        copied.update(
            {"stepId": step["id"], "stepName": step["name"], "stepIndex": index, "noOp": True, "mesh": copied_mesh}
        )
        # A no-op is not a v2 execution, even when the preceding step was.
        # Do not carry its plan/metadata/output provenance into this snapshot.
        for key in ("v2Plan", "v2Metadata", "v2Outputs", "cellSetProcess"):
            copied.pop(key, None)
        # The copied surface is the previous step's; its measured
        # add/remove record belongs to that step, not to this no-op.
        for key in ("measuredRemoval", "measuredAdded"):
            copied.pop(key, None)
    # A zero-rate step still owns its requested output settings. Build
    # this step's real cell set from the unchanged Domain. Only the
    # generic cell-set rows are replaced/relocated to this step; every
    # unrelated output row (nativeMetadata, level sets, surface mesh,
    # ...) stays exactly as copied from the predecessor.
    no_op_cell_set_settings = (ctx.project.get("outputSettings") or {}).get("cellSet")
    no_op_cell_set_enabled = isinstance(no_op_cell_set_settings, dict) and bool(no_op_cell_set_settings.get("enabled"))
    no_op_outputs: list[dict[str, Any]] = []
    inherited_outputs = (
        [row for row in (copied.get("outputs") or []) if row.get("kind") not in {"cellSet", "cellSetSummary"}]
        if copied is not None
        else []
    )
    if no_op_cell_set_enabled:
        no_op_mesh = ctx.domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        no_op_top, no_op_bottom = simulate._surface_vertical_bounds(
            no_op_mesh.getNodes(), is3d=ctx.project["dimension"] == 3
        )
        no_op_outputs = simulate.write_cell_set_outputs(
            ctx.ps,
            ctx.domain,
            ctx.run_dir,
            ctx.run_id,
            step["id"],
            ctx.project,
            no_op_cell_set_settings,
            surface_top=no_op_top,
            surface_bottom=no_op_bottom,
            is3d=ctx.project["dimension"] == 3,
        )
        no_op_outputs = no_op_outputs + inherited_outputs
    if copied is not None:
        if no_op_cell_set_enabled:
            copied["outputs"] = no_op_outputs
        atomic_json(ctx.run_dir / f"{step['id']}.json", copied)
    check_artifact_budget(ctx.run_dir)
    metadata = {
        "stepId": step["id"],
        "name": step["name"],
        "stepIndex": index,
        "snapshot": f"{step['id']}.json",
        **({"vtk": next_vtk_name} if previous.get("vtk") else {}),
        "pointCount": previous["pointCount"],
        **{k: previous[k] for k in ("lineCount", "triangleCount", "topology", "dimension") if k in previous},
        "materialIds": previous["materialIds"],
        "materialIdsByLevelSetIndex": previous["materialIdsByLevelSetIndex"],
        "noOp": True,
        "measuredGeometry": (copied or previous).get("measuredGeometry"),
        "materialTops": previous.get("materialTops"),
    }
    if no_op_cell_set_enabled:
        metadata["outputs"] = no_op_outputs
    if copied is None:
        # Previous step is deferred: this no-op step shares its native
        # state and is materialised from state-<step>.vpsd on first read.
        atomic_json(
            ctx.run_dir / f"{step['id']}.meta.json", {"schemaVersion": 1, "deferred": True, "metadata": metadata}
        )
    metadata["nativeState"] = ctx.save_native_state(ctx.domain, step["id"])
    ctx.event(
        {
            "type": "step-configured",
            "stepId": step["id"],
            "operation": step["operation"],
            "model": step["model"],
            "noOp": True,
            "actualEngine": "not-applicable",
            "engineSelectionReason": "zero rate; ViennaPS Process was not called",
            "actualSeed": None,
            "actualRate": 0.0,
            "duration": step["duration"],
            "actualParameters": no_op_parameters,
        }
    )
    ctx.actual_execution.append({"stepId": step["id"], "engine": "not-applicable", "seed": None})
    return metadata
