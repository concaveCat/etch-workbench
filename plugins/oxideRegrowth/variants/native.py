"""Native oxide-regrowth variant.

The variant reproduces ``worker/specialized.py`` for ``oxideRegrowth`` exactly:
it first derives a native ``DenseCellSet`` from the domain surface (the model
requires it), adds the ``byproductSum`` scalar field, builds the cell
neighborhood, constructs ``ps.OxideRegrowth`` with its twelve float arguments,
wraps it in a pinned ``CPU_DISK`` ``ps.Process``, applies the shared advection
parameters with ``ignoreVoids=True``, and calls ``Process.apply()``.

No flux engine or ray tracing is configured: ``OxideRegrowth`` is not a flux
model. Only ``CPU_DISK`` is advertised because the installed native model has no
verified GPU path on this host.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path; the executor is
responsible for checkpointing before calling it. The native ``DenseCellSet``
created before the run is exposed after ``apply()`` as step metadata and as an
in-memory JSON artifact. It is deliberately not promoted to a generic
``CellSetState``: the primary delta of this process is the surface geometry and
its materials.

It never imports the worker, :mod:`backend`, or a native library: ``ps`` always
comes from the engine lease.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    StateProfile,
    VariantCapabilities,
)
from process_models.v2.engine import EngineLease, EngineRequirement, ExecutionProfile
from process_models.v2.engines import viennaps as _engine_helpers
from process_models.v2.engines.viennaps import (
    ViennaPSMaterialRegistry,
    apply_execution_profile,
    configure_advection,
    material_map,
)
from process_models.v2.state import (
    Artifact,
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import OxideRegrowthParams

__all__ = ["OxideRegrowthNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")
_CELL_SET_SOURCE = "viennaps.Domain.getCellSet after OxideRegrowth.Process.apply"


class OxideRegrowthNativeVariant(EngineVariant):
    """Run one native ``ps.OxideRegrowth`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=False`` because the native model
    has no mask input. ``allow_new_materials=True`` because the native model can
    change oxide composition even though it does not add a level set.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=False,
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=True),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: OxideRegrowthParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, and the positive duration are already
        enforced by :class:`~plugins.oxideRegrowth.params.OxideRegrowthParams`,
        so this hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: OxideRegrowthParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one oxide-regrowth step and return the committed state change.

        The native call sequence matches ``worker/specialized.py`` for this
        model: derive the ``DenseCellSet`` from the surface, construct
        ``OxideRegrowth``, wrap it in a ``ps.Process`` with the step duration,
        pin ``CPU_DISK``, apply advection with ``ignoreVoids``, then
        ``Process.apply()``.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        derived = _derive_cell_set(ps, domain, geometry)
        model = ps.OxideRegrowth(*params.flat_arguments())

        duration = float(params.duration)
        process = ps.Process(domain, model, duration)
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        # OxideRegrowth tracks voids in its cell set; the worker has always
        # ignored them for this model, so both paths agree.
        advection.ignoreVoids = True
        advection_actual["ignoreVoids"] = True
        advection_actual["ignoreVoidsSource"] = "oxideRegrowth native requirement"
        process.setParameters(advection)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        names = _material_names(ps, state.materials.names, material_order)
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(order=material_order, names=names)

        cell_set_payload = _cell_set_payload(domain, geometry, derived)
        artifact = Artifact(
            kind="json",
            name="nativeCellSet",
            schema_version=1,
            metadata=cell_set_payload,
        )

        metadata: dict[str, Any] = {
            "nativeClass": "ps.OxideRegrowth",
            "nativeConstructor": "twelve-argument constructor",
            "operation": "native",
            "parameters": params.native_values(),
            "duration": duration,
            "durationDerivation": "step.duration",
            "derivedCellSet": derived,
            "cellSet": {
                "source": _CELL_SET_SOURCE,
                "nativeType": "DenseCellSet",
                "numberOfCells": cell_set_payload["numberOfCells"],
                "fieldLabels": cell_set_payload["fieldLabels"],
                "fieldStatistics": cell_set_payload["fieldStatistics"],
                "materialCounts": cell_set_payload["materialCounts"],
            },
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "ignoreVoids": True,
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "rayTracing": None,
            "dimension": geometry.dimension,
            "gridDelta": geometry.grid_delta,
            "levelSetCount": int(domain.getNumberOfLevelSets()),
            "materialMap": list(material_order),
        }

        return StepResult(
            delta=StateDelta(geometry=delta_geometry, materials=delta_materials),
            outputs=OutputSet(artifacts=(artifact,)),
            metadata=metadata,
            warnings=[],
        )


def _derive_cell_set(ps: Any, domain: Any, geometry: LevelSetState) -> dict[str, Any]:
    """Create the native ``DenseCellSet`` exactly like the worker does.

    The surface mesh is taken before ``Process.apply()``; its top node defines
    the cell-set height with a padding of ``max(1.0, 5 * gridDelta)``. The
    vertical axis is ``1`` in 2D and ``2`` in 3D. The cell set is filled with
    ``GAS``, seeded with ``byproductSum = 0``, and given a built neighborhood.
    """
    surface = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    vertical_axis = 1 if int(geometry.dimension) == 2 else 2
    top = max(float(point[vertical_axis]) for point in surface.getNodes())
    padding = max(1.0, 5.0 * float(geometry.grid_delta))
    cell_set_height = top + padding

    domain.generateCellSet(cell_set_height, ps.Material.GAS, True)
    cell_set = domain.getCellSet()
    if cell_set is None:
        raise RuntimeError("ViennaPS Domain.generateCellSet returned no DenseCellSet")
    cell_set.addScalarData("byproductSum", 0.0)
    cell_set.buildNeighborhood()

    return {
        "nativeType": "DenseCellSet",
        "material": "GAS",
        "height": float(cell_set_height),
        "verticalAxis": vertical_axis,
        "top": float(top),
        "padding": float(padding),
        "initialField": "byproductSum=0",
        "neighborhoodBuilt": True,
    }


def _cell_set_payload(domain: Any, geometry: LevelSetState, derived: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe snapshot of the native ``DenseCellSet``.

    The full scalar arrays are included in the artifact payload because the
    worker's ``nativeCellSet`` artifact carries the same data; the step
    metadata keeps only counts and per-field statistics.
    """
    cell_set = domain.getCellSet()
    if cell_set is None:
        raise RuntimeError("ViennaPS OxideRegrowth lost its generated DenseCellSet")

    labels = [str(label) for label in cell_set.getScalarDataLabels()]
    fields = {label: [float(value) for value in cell_set.getScalarData(label)] for label in labels}
    material_counts: dict[str, int] = {}
    for value in fields.get("Material", []):
        key = str(int(value))
        material_counts[key] = material_counts.get(key, 0) + 1

    return {
        "schemaVersion": 1,
        "source": _CELL_SET_SOURCE,
        "nativeType": "DenseCellSet",
        "dimension": int(geometry.dimension),
        "gridDelta": float(geometry.grid_delta),
        "association": "DenseCellSet scalar grid; separate from the returned surface mesh",
        "derivedSetup": dict(derived),
        "numberOfCells": int(cell_set.getNumberOfCells()),
        "fieldLabels": labels,
        "fieldStatistics": {
            label: {
                "count": len(values),
                "min": (min(values) if values else None),
                "max": (max(values) if values else None),
            }
            for label, values in fields.items()
        },
        "materialCounts": material_counts,
        "fields": fields,
        "nativeUnit": None,
    }


def _material_names(ps: Any, declared: Mapping[int, str], material_order: Iterable[int]) -> dict[int, str]:
    """Return names for every level set, resolving unknown ids natively."""
    names = {int(material_id): name for material_id, name in declared.items()}
    registry = ViennaPSMaterialRegistry(ps)
    order = [int(material_id) for material_id in material_order]
    for material_id in order:
        if material_id not in names:
            native_name = registry.name_for(material_id)
            if native_name is not None:
                names[material_id] = native_name
    order_set = set(order)
    return {material_id: name for material_id, name in names.items() if material_id in order_set}


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing.

    The shared advection helper reads the project schema version and the solver
    block. Silently substituting defaults would make the run record describe a
    project that was never used, so a partial context fails loudly.
    """
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "oxideRegrowth native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
