"""One native geometry engine carrying the supported operation capabilities.

``GeometryEngine`` operates on the canonical level-set ``Domain``. It never
constructs a ``DenseCellSet`` and never calls a physical process model. The
per-operation parameter models are supplied by the thin geometry variants; a
variant delegates here instead of reimplementing native calls.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping
from typing import Any

from ..contract import Issue, ProcessContext
from ..engine import EngineLease
from ..engines.viennaps import (
    geometry_mask_material_names,
    material_handle,
    material_map,
)
from ..state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)
from .params import (
    BooleanParams,
    GdsBooleanParams,
    InsertBoxParams,
    ParticleShapeParams,
    PlanarizeParams,
    RemoveMaterialParams,
)

__all__ = ["GeometryEngine", "GeometryEngineError"]


class GeometryEngineError(RuntimeError):
    """Raised when a geometry operation cannot run on the current domain."""


class GeometryEngine:
    """Native ViennaPS geometry operations on one level-set domain."""

    id = "viennaps-geometry"
    version = "1.1.0"
    capabilities = frozenset(
        {
            "planarize",
            "particleShape",
            "boolean",
            "gdsBoolean",
            "removeMaterial",
            "insertBox",
            "removeTopLevelSet",
        }
    )

    def validate(self, operation: str, ctx: ProcessContext, params: Any) -> list[Issue]:
        """Return semantic issues the per-operation parameters cannot express."""
        if operation not in self.capabilities:
            raise GeometryEngineError(f"unsupported geometry operation {operation!r}")
        if operation == "planarize":
            return self._validate_planarize(ctx, params)
        if operation == "particleShape":
            return self._validate_particle_shape(ctx, params)
        if operation == "boolean":
            return self._validate_boolean(ctx, params)
        if operation == "gdsBoolean":
            return self._validate_gds_boolean(ctx, params)
        if operation == "insertBox":
            return self._validate_insert_box(ctx, params)
        return []

    def execute(
        self,
        operation: str,
        state: ProcessState,
        params: Any,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one geometry operation to the canonical domain."""
        if operation not in self.capabilities:
            raise GeometryEngineError(f"unsupported geometry operation {operation!r}")
        geometry = state.require("geometry")
        domain = geometry.handle
        ps = engine.handle
        warnings: list[str] = []
        inserted_id: int | None = None
        inserted_name: str | None = None
        if operation == "planarize":
            metadata = self._planarize(ps, domain, ctx, params)
        elif operation == "particleShape":
            metadata, warnings = self._particle_shape(ps, domain, state, ctx, params)
            if metadata.get("depositMaterialId") is not None:
                inserted_id = int(metadata["depositMaterialId"])
                inserted_name = str(metadata["depositMaterial"])
        elif operation == "boolean":
            metadata = self._boolean(ps, domain, ctx, params)
        elif operation == "gdsBoolean":
            metadata = self._gds_boolean(ps, domain, ctx, params)
        elif operation == "insertBox":
            metadata = self._insert_box(ps, domain, params)
            inserted_id = int(metadata["materialId"])
            inserted_name = params.material
        elif operation == "removeTopLevelSet":
            metadata = self._remove_top_level_set(domain)
        else:
            metadata = self._remove_material(ps, domain, params)

        order = tuple(material_map(domain))
        names = {material_id: name for material_id, name in state.materials.names.items() if material_id in order}
        if inserted_id is not None and inserted_name is not None and inserted_id in order:
            names[inserted_id] = inserted_name
        materials = MaterialState(order=order, names=names)
        metadata.update(
            {
                "dimension": int(geometry.dimension),
                "gridDelta": float(geometry.grid_delta),
                "materialMap": list(order),
                "levelSetPointCounts": [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()],
            }
        )
        return StepResult(
            delta=StateDelta(
                geometry=LevelSetState(
                    handle=domain,
                    dimension=geometry.dimension,
                    grid_delta=geometry.grid_delta,
                ),
                materials=materials,
            ),
            metadata=metadata,
            warnings=warnings,
        )

    # -- validation ------------------------------------------------------

    def _validate_planarize(self, ctx: ProcessContext, params: PlanarizeParams) -> list[Issue]:
        if params.cutoffHeight is not None or params.stopMaterial is None:
            return []
        try:
            self._stop_top(ctx, params.stopMaterial)
        except GeometryEngineError:
            return [
                Issue(
                    path="stopMaterial",
                    message=(
                        f"planarize stopMaterial {params.stopMaterial!r} has no exposed up-facing "
                        "surface in the previous step's materialTops"
                    ),
                )
            ]
        return []

    def _validate_particle_shape(self, ctx: ProcessContext, params: ParticleShapeParams) -> list[Issue]:
        issues: list[Issue] = []
        dimension = int(ctx.dimension)
        if params.shape == "box":
            axes = [float(value) for value in params.halfAxes or ()]
            if len(axes) != dimension:
                issues.append(
                    Issue(
                        path="halfAxes",
                        message=f"box particleShape needs {dimension} half-axes, got {len(axes)}",
                    )
                )
            for index, axis in enumerate(axes):
                if not math.isfinite(axis) or axis < float(ctx.grid_delta) - 1e-12:
                    issues.append(
                        Issue(
                            path=f"halfAxes[{index}]",
                            message=f"half-axis {axis} must be at least one grid interval ({ctx.grid_delta})",
                        )
                    )
        else:
            radius = float(params.radius) if params.radius is not None else 0.0
            if not math.isfinite(radius) or radius < float(ctx.grid_delta) - 1e-12:
                issues.append(
                    Issue(
                        path="radius",
                        message=f"radius {radius} must be at least one grid interval ({ctx.grid_delta})",
                    )
                )
        if params.operation == "deposit":
            masks = list(params.maskMaterials) if params.maskMaterials is not None else self._requested_masks(ctx)
            if masks:
                issues.append(
                    Issue(
                        path="operation",
                        message=(
                            "masked particleShape deposition is not supported: ViennaPS 4.6.2/4.7.0 "
                            f"SIGSEGV on a non-empty mask with positive geometric advection (mask materials {masks})"
                        ),
                    )
                )
        return issues

    def _validate_insert_box(self, ctx: ProcessContext, params: InsertBoxParams) -> list[Issue]:
        issues: list[Issue] = []
        minimum = float(ctx.grid_delta)
        for name in ("min", "max"):
            for index, value in enumerate(getattr(params, name)):
                if abs(float(value)) / minimum > 1_000_000:
                    issues.append(
                        Issue(path=f"{name}[{index}]", message="insertBox coordinate exceeds the application budget")
                    )
        for index, (low, high) in enumerate(zip(params.min, params.max)):
            if abs(float(high) - float(low)) < minimum - 1e-12:
                issues.append(
                    Issue(
                        path=f"max[{index}]",
                        message=f"insertBox axis {index} must span at least one grid interval ({minimum})",
                    )
                )
        return issues

    def _validate_gds_boolean(self, ctx: ProcessContext, params: GdsBooleanParams) -> list[Issue]:
        issues: list[Issue] = []
        if int(ctx.dimension) != 3:
            issues.append(Issue(path="model", message="GDS boolean requires a 3D project"))
        geometry = ctx.project.get("geometry") if isinstance(ctx.project, Mapping) else None
        if not isinstance(geometry, Mapping) or geometry.get("kind") != "gds":
            issues.append(Issue(path="geometry", message="GDS boolean requires geometry.kind=gds"))
        if not callable(ctx.assets.get("gdsBooleanOperand")):
            issues.append(
                Issue(
                    path="assets.gdsBooleanOperand",
                    message="GDS boolean requires the run's verified GDS operand provider",
                )
            )
        if float(params.gdsMask.height) < 2.0 * float(ctx.grid_delta) - 1e-12:
            issues.append(
                Issue(
                    path="gdsMask.height",
                    message=(
                        f"GDS mask height {params.gdsMask.height} must span at least two grid "
                        f"intervals ({2.0 * float(ctx.grid_delta)})"
                    ),
                )
            )
        return issues

    def _validate_boolean(self, ctx: ProcessContext, params: BooleanParams) -> list[Issue]:
        issues: list[Issue] = []
        dimension = int(ctx.dimension)
        if dimension == 2:
            if abs(float(params.position[2])) > 1e-12:
                issues.append(Issue(path="position", message="2D boolean position must be [x, h, 0]"))
            if params.depth is not None:
                issues.append(Issue(path="depth", message="depth applies only to 3D boolean"))
            if params.shape == "cylinder":
                issues.append(Issue(path="shape", message="cylinder boolean is supported only in 3D"))
        elif params.shape == "box" and params.depth is None:
            issues.append(Issue(path="depth", message="3D box boolean requires depth"))
        if params.shape == "cylinder" and params.radius is None:
            issues.append(Issue(path="radius", message="cylinder boolean requires radius"))
        if params.shape == "cylinder" and params.depth is not None:
            issues.append(Issue(path="depth", message="depth applies only to a box boolean"))
        return issues

    @staticmethod
    def _requested_masks(ctx: ProcessContext) -> list[str]:
        geometry = ctx.project.get("geometry") if isinstance(ctx.project, Mapping) else None
        if not isinstance(geometry, Mapping):
            return []
        return geometry_mask_material_names(dict(geometry))

    # -- native operations ----------------------------------------------

    @staticmethod
    def _planarize(ps: Any, domain: Any, ctx: ProcessContext, params: PlanarizeParams) -> dict[str, Any]:
        metadata: dict[str, Any]
        if params.cutoffHeight is not None:
            cutoff = float(params.cutoffHeight)
            metadata = {
                "geometryOperation": "planarize",
                "cutoffHeight": cutoff,
                "cutoffMode": "explicit",
            }
        else:
            if params.stopMaterial is None:
                raise GeometryEngineError("planarize requires cutoffHeight or stopMaterial")
            stop_top, method = GeometryEngine._stop_top(ctx, params.stopMaterial)
            over_polish = float(params.overPolish or 0.0)
            cutoff = stop_top - over_polish
            metadata = {
                "geometryOperation": "planarize",
                "cutoffHeight": cutoff,
                "stopMaterial": params.stopMaterial,
                "measuredStopTopHeight": stop_top,
                "overPolish": over_polish,
                "stopHeight": stop_top,
                "stopHeightMethod": method,
                "cutoffMode": "material-aware",
                "cutoffDerivation": (
                    f"{method} height of the up-facing {params.stopMaterial} surface at the "
                    "start of this step minus overPolish"
                ),
            }
        ps.Planarize(domain, cutoff).apply()
        return metadata

    @staticmethod
    def _stop_top(ctx: ProcessContext, material: str) -> tuple[float, str]:
        entry = ctx.previous_tops.get(material)
        if not isinstance(entry, Mapping):
            raise GeometryEngineError(f"planarize stopMaterial {material!r} has no previous material-top record")
        for key, method in (
            ("p90TopHeight", "area-weighted-p90"),
            ("medianTopHeight", "area-weighted-median (p90 unavailable)"),
        ):
            value = entry.get(key)
            if value is None:
                continue
            number = float(value)
            if math.isfinite(number):
                return number, method
        raise GeometryEngineError(f"planarize stopMaterial {material!r} has no finite p90TopHeight or medianTopHeight")

    def _particle_shape(
        self,
        ps: Any,
        domain: Any,
        state: ProcessState,
        ctx: ProcessContext,
        params: ParticleShapeParams,
    ) -> tuple[dict[str, Any], list[str]]:
        del state
        dimension = int(ctx.dimension)
        sign = -1.0 if params.operation == "etch" else 1.0
        deposit_handle = None
        if sign > 0.0 and params.depositMaterial is not None:
            deposit_handle = material_handle(ps, params.depositMaterial)
            domain.duplicateTopLevelSet(deposit_handle)
        if params.maskMaterials is not None:
            if params.maskMode == "allExcept":
                excluded = set(params.maskMaterials)
                registry = ps.MaterialRegistry.instance()
                requested_masks: list[str] = []
                for handle in domain.getMaterialsInDomain():
                    name = registry.getName(handle)
                    if name not in excluded and name not in requested_masks:
                        requested_masks.append(name)
            else:
                requested_masks = list(params.maskMaterials)
        else:
            requested_masks = self._requested_masks(ctx)
        applied_masks: list[tuple[str, Any]] = []
        omitted_masks: list[str] = []
        warnings: list[str] = []
        if sign > 0.0:
            if requested_masks:
                raise GeometryEngineError(
                    "masked particleShape deposition is not supported on this runtime: " + ", ".join(requested_masks)
                )
        else:
            for name in requested_masks:
                handle = material_handle(ps, name)
                level_set = domain.getMaterialLevelSet(handle)
                if level_set is None or int(level_set.getNumberOfPoints()) == 0:
                    omitted_masks.append(name)
                    continue
                applied_masks.append((name, handle))

        if params.shape == "box":
            axes = [float(value) for value in params.halfAxes or ()]
            if len(axes) != dimension:
                raise GeometryEngineError(f"particleShape box needs {dimension} half-axes, got {axes}")
            signed = [sign * value for value in axes]
            native_half_axes = signed + [0.0] if dimension == 2 else signed
            model = ps.BoxDistribution(native_half_axes)
            native_class = "BoxDistribution"
            expected_offset = max(axes)
        else:
            radius = float(params.radius) if params.radius is not None else 0.0
            signed_radius = sign * radius
            model = ps.SphereDistribution(signed_radius)
            native_class = "SphereDistribution"
            native_half_axes = None
            expected_offset = radius

        before = _surface_digest(domain)
        before_counts = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
        for _name, handle in applied_masks:
            model.addMaskMaterial(handle)
        process = ps.Process(domain, model, 0.0)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        process.apply()
        after = _surface_digest(domain)
        if before == after:
            raise GeometryEngineError(
                "native particle-shape distribution left the surface unchanged; refusing to record a silent no-op"
            )
        if omitted_masks:
            warnings.append(
                "particleShape omitted mask materials with no exposed level set: " + ", ".join(omitted_masks)
            )
        metadata = {
            "geometryOperation": "particleShape",
            "nativeClass": native_class,
            "dimension": dimension,
            "operation": params.operation,
            "shape": params.shape,
            "expectedOffsetUm": expected_offset,
            "surfaceSignatureBefore": before,
            "surfaceSignatureAfter": after,
            "levelSetPointCountsBefore": before_counts,
            "nativeMask": {
                "mode": ("allExcept" if params.maskMode == "allExcept" else "material")
                if applied_masks or params.maskMaterials is not None
                else "none",
                "applied": bool(applied_masks),
                "materials": [name for name, _ in applied_masks],
                "requestedMaterials": list(requested_masks),
                "maskMode": params.maskMode or "explicit",
                "omittedMaterials": omitted_masks,
            },
            "nativeDurationIgnored": True,
        }
        if params.depositMaterial is not None:
            metadata["depositMaterial"] = params.depositMaterial
            metadata["depositMaterialId"] = int(deposit_handle) if deposit_handle is not None else None
            metadata["duplicateTopLevelSet"] = deposit_handle is not None
        if params.shape == "box":
            metadata["halfAxes"] = [float(value) for value in params.halfAxes or ()]
            metadata["nativeSignedHalfAxesUm"] = [sign * value for value in (params.halfAxes or ())]
            if native_half_axes is not None:
                metadata["nativeHalfAxesUm"] = native_half_axes
        else:
            metadata["radius"] = float(params.radius) if params.radius is not None else 0.0
            metadata["nativeSignedRadiusUm"] = signed_radius
        return metadata, warnings

    @staticmethod
    def _boolean(ps: Any, domain: Any, ctx: ProcessContext, params: BooleanParams) -> dict[str, Any]:
        dimension = int(ctx.dimension)
        step_id = str(ctx.step.get("id") or "geometry")
        factory = ps.GeometryFactory(domain.getSetup(), f"WorkbenchBoolean_{step_id}")
        position = [float(value) for value in params.position]
        if dimension == 2:
            if params.shape == "cylinder":
                raise GeometryEngineError("cylinder boolean is supported only in 3D")
            operand = factory.makeBoxStencil(
                position[:2],
                float(params.width),
                float(params.height),
                float(params.angle),
            )
            operand_kind = "box"
        elif params.shape == "cylinder":
            if params.radius is None:
                raise GeometryEngineError("cylinder boolean requires radius")
            operand = factory.makeCylinderStencil(
                position,
                float(params.radius),
                float(params.height),
                float(params.angle),
            )
            operand_kind = "cylinder"
        else:
            if params.depth is None:
                raise GeometryEngineError("3D box boolean requires depth")
            operand = factory.makeBoxStencil(
                position,
                float(params.width),
                float(params.height),
                float(params.angle),
                float(params.depth),
            )
            operand_kind = "box"
        enum_name = {
            "union": "UNION",
            "intersection": "INTERSECT",
            "difference": "RELATIVE_COMPLEMENT",
        }[params.booleanOperation]
        operation = getattr(ps.ls.BooleanOperationEnum, enum_name)
        domain.applyBooleanOperation(operand, operation, bool(params.applyToAll))
        return {
            "geometryOperation": "boolean",
            "booleanOperation": params.booleanOperation,
            "operand": operand_kind,
            "position": position,
            "width": float(params.width),
            "height": float(params.height),
            "depth": float(params.depth) if params.depth is not None else None,
            "radius": float(params.radius) if params.radius is not None else None,
            "angle": float(params.angle),
            "applyToAll": bool(params.applyToAll),
            "nativeBooleanEnum": enum_name,
        }

    @staticmethod
    def _gds_boolean(
        ps: Any,
        domain: Any,
        ctx: ProcessContext,
        params: GdsBooleanParams,
    ) -> dict[str, Any]:
        provider = ctx.assets.get("gdsBooleanOperand")
        if not callable(provider):
            raise GeometryEngineError("GDS boolean requires a callable gdsBooleanOperand asset")
        canonical_step = {
            "id": str(ctx.step.get("id") or "gdsBoolean"),
            "model": "gdsBoolean",
            **params.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        operand, transform = provider(ps, domain, ctx.project, canonical_step, ctx)
        enum_name = {
            "union": "UNION",
            "intersection": "INTERSECT",
            "difference": "RELATIVE_COMPLEMENT",
        }[params.booleanOperation]
        operation = getattr(ps.ls.BooleanOperationEnum, enum_name)
        before = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
        domain.applyBooleanOperation(operand, operation, bool(params.applyToAll))
        remaining = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
        erased = [index for index, (was, now) in enumerate(zip(before, remaining)) if was > 0 and now == 0]
        if not remaining or erased:
            raise GeometryEngineError(
                "GDS boolean operand erased a material level set; adjust the mask region, "
                f"retained region or operation (level-set point counts: {remaining}, erased: {erased})"
            )
        return {
            "geometryOperation": "gdsBoolean",
            "booleanOperation": params.booleanOperation,
            "operand": "gds-mask",
            "nativeBooleanEnum": enum_name,
            "applyToAll": bool(params.applyToAll),
            "mask": transform,
            "assetId": transform.get("sourceAssetId"),
            "structure": transform.get("selectedStructure"),
            "unitScale": transform.get("unitScale"),
            "levelSetPointCountsAfter": remaining,
        }

    @staticmethod
    def _remove_material(ps: Any, domain: Any, params: RemoveMaterialParams) -> dict[str, Any]:
        handle = material_handle(ps, params.material)
        domain.removeMaterial(handle)
        return {
            "geometryOperation": "removeMaterial",
            "material": params.material,
            "materialId": int(handle),
        }

    @staticmethod
    def _insert_box(ps: Any, domain: Any, params: InsertBoxParams) -> dict[str, Any]:
        handle = material_handle(ps, params.material)
        before = int(domain.getNumberOfLevelSets())
        box = ps.LevelSet(domain.getGrid())
        ps.MakeGeometry(
            box, ps.Box([float(value) for value in params.min], [float(value) for value in params.max])
        ).apply()
        domain.insertNextLevelSetAsMaterial(box, handle)
        return {
            "geometryOperation": "insertBox",
            "min": [float(value) for value in params.min],
            "max": [float(value) for value in params.max],
            "material": params.material,
            "materialId": int(handle),
            "levelSetCountBefore": before,
            "levelSetCountAfter": int(domain.getNumberOfLevelSets()),
            "boxPointCount": int(box.getNumberOfPoints()),
        }

    @staticmethod
    def _remove_top_level_set(domain: Any) -> dict[str, Any]:
        before = int(domain.getNumberOfLevelSets())
        if before <= 1:
            raise GeometryEngineError(
                "removeTopLevelSet would remove the only level set; the domain would have no material surface"
            )
        removed_id = int(domain.getMaterialMap().getMaterialIdAtIdx(before - 1))
        domain.removeTopLevelSet()
        return {
            "geometryOperation": "removeTopLevelSet",
            "removedMaterialId": removed_id,
            "levelSetCountBefore": before,
            "levelSetCountAfter": int(domain.getNumberOfLevelSets()),
        }


def _surface_digest(domain: Any) -> str:
    """Hash the exposed surface nodes without relying on object identity."""
    mesh = domain.getSurfaceMesh(
        addInterfaces=True,
        sharpCorners=False,
        minNodeDistanceFactor=0.01,
    )
    points = sorted(
        (round(float(point[0]), 9), round(float(point[1]), 9), round(float(point[2]), 9)) for point in mesh.getNodes()
    )
    digest = hashlib.sha256()
    for point in points:
        digest.update(struct.pack("<ddd", *point))
    return digest.hexdigest()
