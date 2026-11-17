"""ViennaPS adapter: the reference engine for the shared geometric subset.

Runs inside the workbench virtualenv (ViennaPS 4.7.0 / ViennaLS 5.10.0). It reuses
``process_models.v2.engines.viennaps`` for material handles instead of duplicating
catalogue knowledge, and it never imports the backend, the storage layer or the
worker entry point.

Entry point::

    .venv/bin/python engines/viennaps/engine.py job.json out_dir

Scope is deliberately the same shared subset the SPX adapter implements
(isotropic etch/deposit with per-material rates, plane initial structure). A step
this engine cannot lower is reported as ``unsupported``; it is never skipped
silently.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines import limits as engine_limits  # noqa: E402
from engines.protocol import Frame, JobSpec, StepIR, StepOutcome, write_json  # noqa: E402

MATERIAL_COLORS = {
    "Si": "#78909C", "SiO2": "#00e5ff", "Si3N4": "#dce775", "PolySi": "#ff3d00",
    "SiGe": "#00bfa5", "Al2O3": "#72abab", "HfO2": "#ff1744", "TiN": "#9e9e9e",
    "W": "#607d8b", "Cu": "#b87333", "PMMA": "#fff59d", "Mask": "#fff59d",
    "SiN": "#55aaff", "Metal": "#9e9e9e", "Dielectric": "#00e5ff",
}


def _material_ids(ps: Any, name: str) -> int:
    from process_models.v2.engines.viennaps import material_handle
    return int(material_handle(ps, name))


class ViennapsSession:
    def __init__(self, job: JobSpec, out_dir: Path) -> None:
        import viennaps as ps

        self.ps = ps
        self.job = job
        self.out = out_dir
        from engines.materials import ensure_registered

        ensure_registered(ps)
        ps.setDimension(job.domain.dimensions)
        ps.Length.setUnit("um")
        ps.Time.setUnit("min")
        self.domain = None
        self.materials: dict[int, str] = {}
        # Level-set order equals stack order (substrate first, films bottom-up):
        # the first level set whose filling fraction at a point is <= 0 owns it.
        self.stack: list[str] = []
        self._mesh = None
        self._columns: dict[tuple[int, int], dict[str, float]] | None = None
        self.column = max(float(job.domain.grid_delta) * 2.0, 1e-4)
        self._geometry = None
        # polygons opened by the last litho step; a masked directional etch cuts
        # through exactly these openings
        self._openings: list[list[tuple[float, float]]] = []

    def open(self) -> None:
        ps = self.ps
        d = self.job.domain
        bounds = [0.0, d.width, 0.0, d.depth,
                  -self.job.initial.substrate_thickness, d.height]
        self.domain = ps.Domain(
            bounds,
            [ps.BoundaryType.PERIODIC_BOUNDARY, ps.BoundaryType.PERIODIC_BOUNDARY,
             ps.BoundaryType.INFINITE_BOUNDARY],
            d.grid_delta,
        )
        if self.job.restore:
            # Resume from a native checkpoint written by checkpoint(): the domain
            # geometry (bounds, delta) must match the one the state was written
            # with, which is why the bounds are rebuilt first.
            ps.Reader(self.domain, str(self.job.restore)).apply()
            for name in [self.job.initial.substrate,
                         *[film.material for film in self.job.initial.films]]:
                self._register(name)
                self.stack.append(name)
            return
        initial = self.job.initial
        if initial.kind != "plane":
            raise NotImplementedError(
                f"initial structure {initial.kind!r} is not wired for the ViennaPS adapter yet")
        height = 0.0
        ps.MakePlane(self.domain, height=height,
                     material=_material_handle(ps, initial.substrate),
                     addToExisting=False).apply()
        self._register(initial.substrate)
        self.stack.append(initial.substrate)
        for film in initial.films:
            height += film.thickness
            ps.MakePlane(self.domain, height=height,
                         material=_material_handle(ps, film.material),
                         addToExisting=True).apply()
            self._register(film.material)
            self.stack.append(film.material)

    def _register(self, name: str) -> None:
        from process_models.v2.engines.viennaps import material_handle
        material = material_handle(self.ps, name)
        self.materials[int(material)] = name

    def checkpoint(self, step_id: str) -> str | None:
        """Write the native level-set state; ``None`` when the binding has no writer."""
        target = self.out / "state" / f"{step_id}.vpsd"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.ps.Writer(self.domain, str(target)).apply()
            return str(target.relative_to(self.out))
        except Exception:  # noqa: BLE001 - a missing writer is not a failed step
            return None

    def apply(self, step: StepIR) -> StepOutcome:
        ps = self.ps
        started = time.perf_counter()
        try:
            if step.mechanism == "isotropic" and step.kind == "deposit":
                present = {name for name in self.stack}
                new = {name: rate for name, rate in step.rates.items()
                       if name not in present and float(rate) > 0}
                growth = {name: rate for name, rate in step.rates.items()
                          if name in present and abs(float(rate)) > 0}
                if growth:
                    model = ps.IsotropicProcess(rate=0.0, maskMaterials=[])
                    for material, rate in growth.items():
                        model.setMaterialRate(_material_handle(ps, material), float(rate))
                    ps.Process(self.domain, model, float(step.duration)).apply()
                for material, rate in new.items():
                    # A new material cannot be grown by IsotropicProcess (it only
                    # advects materials that already exist), so the shell is built
                    # from the current top surface offset outward by t.
                    self._deposit_shell(material, abs(float(rate)) * float(step.duration))
            elif step.mechanism == "isotropic" and step.kind == "etch":
                model = ps.IsotropicProcess(rate=0.0, maskMaterials=[])
                for material, rate in step.rates.items():
                    model.setMaterialRate(_material_handle(ps, material), float(rate))
                ps.Process(self.domain, model, float(step.duration)).apply()
            elif (step.mechanism == "anisotropic" and step.kind == "etch"
                  and step.parameters.get("mask") and self._openings):
                # Same representation limit as the ellipsoidal branch: a masked
                # directional etch of a buried layer is cut geometrically through
                # the openings instead of advecting the whole stack.
                rates = {}
                for name, rate in step.rates.items():
                    rates[_material_handle(ps, name)] = (float(rate), 0.0)
                self._etched_openings(step, rates, openings=self._openings)
            elif step.mechanism == "anisotropic" and step.kind in ("etch", "deposit"):
                # StepIR.anisotropy splits each rate into a directional and an
                # isotropic part; 1.0 is a perfectly directional process.
                split = min(max(float(step.anisotropy), 0.0), 1.0)
                rates = {_material_handle(ps, material):
                         (float(rate) * split, float(rate) * (1.0 - split))
                         for material, rate in step.rates.items()}
                if not rates:
                    raise NotImplementedError("anisotropic step has no rates")
                direction = [float(v) for v in (step.parameters.get("direction")
                                                or [0.0, 0.0, 1.0])]
                model = ps.DirectionalProcess(direction, rates, 0.0, 0.0)
                ps.Process(self.domain, model, float(step.duration)).apply()
            elif step.mechanism == "strip":
                names = [str(name) for name in step.parameters.get("materials") or []]
                if not names:
                    raise NotImplementedError("strip step lists no materials")
                for name in names:
                    self.domain.removeMaterial(_material_handle(ps, name))
                    self.stack = [entry for entry in self.stack if entry != name] or self.stack
            elif step.mechanism == "ellipsoidal":
                rates = {}
                for name, pair in (step.parameters.get("rates") or {}).items():
                    vertical, lateral = pair
                    rates[_material_handle(ps, name)] = (float(vertical), float(lateral))
                if not rates:
                    raise NotImplementedError("ellipsoidal step lists no rates")
                masks = [_material_handle(ps, name)
                         for name in (step.parameters.get("mask") or [])]
                if step.parameters.get("mask") and self._openings:
                    # Native masked DirectionalProcess segfaults on a domain whose
                    # top level set carries the litho openings, so the exposed area
                    # is cut geometrically: through each opening, remove the top
                    # ``v*duration`` of every material that has a vertical rate.
                    self._etched_openings(step, rates, openings=self._openings)
                elif masks:
                    # A per-material rate map and a mask cannot be combined in one
                    # native constructor, so each material is etched in its own
                    # pass with the mask applied. Without the mask ViennaPS etches a
                    # buried interface and the layer above simply follows it down
                    # (measured: the resist thickened while the oxide thinned).
                    for material, (vertical, lateral) in rates.items():
                        model = ps.DirectionalProcess([0.0, 0.0, 1.0], vertical, lateral, masks)
                        ps.Process(self.domain, model, float(step.duration)).apply()
                else:
                    model = ps.DirectionalProcess([0.0, 0.0, 1.0], rates, 0.0, 0.0)
                    ps.Process(self.domain, model, float(step.duration)).apply()
            elif step.mechanism == "fill":
                self._fill(step)
            elif step.mechanism == "litho":
                self._litho(step)
            elif step.mechanism == "cmp":
                params = step.parameters
                if params.get("stop_material"):
                    # Same form as SPX: polish down to the stop material's exposed
                    # top plus the (usually negative) offset.
                    stop = str(params["stop_material"])
                    buckets = self._height_columns()
                    tops = [bucket[stop] for bucket in buckets.values() if stop in bucket]
                    if not tops:
                        raise RuntimeError(
                            f"cmp stop material {stop!r} has no exposed surface")
                    # The field (highest exposed stop surface) is what SPX polishes
                    # down to; a percentile cut left the field standing.
                    cutoff = max(tops) + float(params.get("offset", 0.0))
                else:
                    cutoff = float(params["cutoff"])
                ps.Planarize(self.domain, cutoff).apply()
                self._mesh = None
                self._columns = None
            else:
                raise NotImplementedError(
                    f"{step.kind}/{step.mechanism} has no ViennaPS lowering yet")
        except NotImplementedError as exc:
            return StepOutcome(step_id=step.step_id, status="unsupported",
                               wall_ms=(time.perf_counter() - started) * 1000.0,
                               reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return StepOutcome(step_id=step.step_id, status="failed",
                               wall_ms=(time.perf_counter() - started) * 1000.0,
                               reason=f"{type(exc).__name__}: {exc}")
        self._mesh = None
        self._columns = None
        return StepOutcome(step_id=step.step_id, status="ok",
                           wall_ms=(time.perf_counter() - started) * 1000.0)

    # -- SPX-parity geometry operations ------------------------------------
    def _surface_top(self) -> float:
        mesh = self._surface_mesh()
        return max(float(point[2]) for point in mesh.getNodes())

    def _surface_bottom(self) -> float:
        mesh = self._surface_mesh()
        return min(float(point[2]) for point in mesh.getNodes())

    def _fill(self, step: StepIR) -> None:
        """Fill the void below ``z_top + offset`` with a new material.

        Level-set construction: a box covering the window is intersected with the
        complement of every existing solid, then inserted as the new material.
        Measured on a 0.08 um trench: the fill reaches the trench bottom and leaves
        a cap above the field, which is what SPX ``depo_fill`` produces.
        """
        ps = self.ps
        material = str(step.parameters["material"])
        offset = float(step.parameters.get("offset", 0.0))
        z_top = self._surface_top() + offset
        z_bottom = self._surface_bottom() - float(self.job.domain.grid_delta)
        margin = float(self.job.domain.grid_delta)
        box = ps.LevelSet(self.domain.getGrid())
        ps.MakeGeometry(box, ps.Box(
            [-margin, -margin, z_bottom],
            [float(self.job.domain.width) + margin,
             float(self.job.domain.depth) + margin, z_top])).apply()
        for level_set in self.domain.getLevelSets():
            ps.ls.BooleanOperation(box, level_set,
                                   ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        self.domain.insertNextLevelSetAsMaterial(box, _material_handle(ps, material))
        self._register(material)
        if material not in self.stack:
            self.stack.append(material)
        self._mesh = None
        self._columns = None

    def _gds_geometry(self):
        if self._geometry is not None:
            return self._geometry
        ps = self.ps
        layout = (self.job.layout or {}).get("path")
        if not layout:
            raise NotImplementedError("litho needs job.layout.path pointing at a GDS layout")
        geometry = ps.GDSGeometry(float(self.job.domain.grid_delta))
        geometry.setBoundaryConditions([
            ps.BoundaryType.REFLECTIVE_BOUNDARY, ps.BoundaryType.REFLECTIVE_BOUNDARY,
            ps.BoundaryType.INFINITE_BOUNDARY])
        ps.GDSReader(geometry, str(layout)).apply()
        self._geometry = geometry
        return geometry

    def _litho(self, step: StepIR) -> None:
        """Pattern (positive) or cut (negative) a material from a layout layer.

        The mask body is built as a closed triangular prism per polygon and turned
        into a level set with ``FromSurfaceMesh``. Two alternatives were measured
        and rejected: ``GDSGeometry.layerToLevelSet`` returns a level set on the
        layout frame and crashes the binding when combined with the simulation
        domain, and ``GeometryFactory.makeBoxStencil`` only covers axis-aligned
        rectangles (Memory1's PLG/PC/M1 layers are polygons).
        """
        ps = self.ps
        layer = int(step.parameters["layer"])
        polarity = str(step.parameters.get("polarity", "negative")).lower()
        thickness = float(step.parameters["thickness"])
        coat = float(step.parameters.get("coat", 0.1))
        material = str(step.parameters.get("material", "Mask"))
        margin = float(self.job.domain.grid_delta)

        z_base = self._surface_top()
        z_top = z_base
        if polarity == "negative" and coat > 0:
            slab = ps.LevelSet(self.domain.getGrid())
            ps.MakeGeometry(slab, ps.Box(
                [-margin, -margin, z_top],
                [float(self.job.domain.width) + margin,
                 float(self.job.domain.depth) + margin, z_top + coat])).apply()
            if self.stack and self.stack[-1] == material:
                # The coat is the same photoresist as the layer underneath, so it is
                # unioned into the existing top level set. Inserting a second level
                # set with the same material confused the stack: the following etch
                # removed the whole film stack instead of only the exposed material.
                self.domain.applyBooleanOperation(slab, ps.ls.BooleanOperationEnum.UNION, False)
            else:
                self.domain.insertNextLevelSetAsMaterial(slab, _material_handle(ps, material))
                self._register(material)
                if material not in self.stack:
                    self.stack.append(material)
            z_top += coat

        # Measured: SPX's litho clears the openings through the *coat* only and
        # leaves the pre-existing resist intact (inside 0.11..0.15 um, outside
        # 0.11..0.25 with a 0.1 um coat). Cutting the whole stack made every
        # following etch go through the oxide.
        z0 = z_base if polarity == "negative" else z_top - thickness
        z1 = z_top
        polygons = [poly for poly in self._layer_polygons(layer) if len(poly) >= 3]
        self._openings = polygons
        if not polygons:
            raise NotImplementedError(f"litho layer {layer} has no polygons")
        mesh = ps.ls.Mesh()
        node = lambda x, y, z: mesh.insertNextNode([float(x), float(y), float(z)])  # noqa: E731
        for polygon in polygons:
            ring = polygon[:-1] if len(polygon) > 3 and polygon[0] == polygon[-1] else polygon
            if len(ring) < 3:
                continue
            top = [node(x, y, z1) for x, y in ring]
            bottom = [node(x, y, z0) for x, y in ring]
            for index in range(1, len(ring) - 1):
                mesh.insertNextTriangle([top[0], top[index], top[index + 1]])
                mesh.insertNextTriangle([bottom[0], bottom[index + 1], bottom[index]])
            for index in range(len(ring)):
                nxt = (index + 1) % len(ring)
                mesh.insertNextTriangle([bottom[index], bottom[nxt], top[nxt]])
                mesh.insertNextTriangle([bottom[index], top[nxt], top[index]])
        body = ps.LevelSet(self.domain.getGrid())
        reconstruction = ps.ls.FromSurfaceMesh(body, mesh)
        reconstruction.setRemoveBoundaryTriangles(False)
        reconstruction.apply()
        if body.getNumberOfPoints() == 0:
            raise NotImplementedError(f"litho layer {layer} produced an empty level set")
        if polarity == "positive":
            self.domain.insertNextLevelSetAsMaterial(body, _material_handle(ps, material))
            self._register(material)
            if material not in self.stack:
                self.stack.append(material)
        else:
            self.domain.applyBooleanOperation(body, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,
                                              False)
        self._mesh = None
        self._columns = None

    def _deposit_shell(self, material: str, thickness: float) -> None:
        """Insert ``material`` as a conformal shell over the current top surface."""
        ps = self.ps
        if thickness <= 0:
            return
        top = self.domain.getLevelSets()[-1]
        mesh = ps.ls.Mesh()
        ps.ls.ToSurfaceMesh(top, mesh).apply()
        nodes = [[float(v) for v in node] for node in mesh.getNodes()]
        triangles = [list(tri) for tri in mesh.getTriangles()]
        if not nodes or not triangles:
            raise RuntimeError(f"cannot deposit {material}: the top surface is empty")
        normals = [[0.0, 0.0, 0.0] for _ in nodes]
        for a, b, c in triangles:
            ax, ay, az = nodes[a]
            bx, by, bz = nodes[b]
            cx, cy, cz = nodes[c]
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            for index in (a, b, c):
                normals[index][0] += nx
                normals[index][1] += ny
                normals[index][2] += nz
        outward = ps.ls.Mesh()
        moved = []
        for index, (x, y, z) in enumerate(nodes):
            nx, ny, nz = normals[index]
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length <= 1e-20:
                moved.append(outward.insertNextNode([x, y, z]))
                continue
            moved.append(outward.insertNextNode([x + nx / length * thickness,
                                                 y + ny / length * thickness,
                                                 z + nz / length * thickness]))
        for a, b, c in triangles:
            outward.insertNextTriangle([moved[a], moved[b], moved[c]])
        shell = ps.LevelSet(self.domain.getGrid())
        reconstruction = ps.ls.FromSurfaceMesh(shell, outward)
        reconstruction.setRemoveBoundaryTriangles(False)
        reconstruction.apply()
        if shell.getNumberOfPoints() == 0:
            raise RuntimeError(f"conformal shell for {material} came out empty")
        self.domain.insertNextLevelSetAsMaterial(shell, _material_handle(ps, material))
        self._register(material)
        if material not in self.stack:
            self.stack.append(material)
        self._mesh = None
        self._columns = None

    def _etched_openings(self, step: StepIR, rates: dict[Any, tuple[float, float]],
                         *, openings: list[list[tuple[float, float]]]) -> None:
        """Cut ``|v| * duration`` of each material inside the litho openings."""
        ps = self.ps
        margin = float(self.job.domain.grid_delta)
        columns = self._height_columns()
        # per-material reference tops (planar stack: take the highest column top)
        tops: dict[str, float] = {}
        for bucket in columns.values():
            for name, top in bucket.items():
                tops[name] = max(tops.get(name, top), top)
        duration = float(step.duration)
        for material, (vertical, _lateral) in rates.items():
            name = self.materials.get(int(material))
            if name is None:
                raise RuntimeError(f"ellipsoidal step names unknown material {material}")
            depth = abs(float(vertical)) * duration
            if depth <= 0:
                continue
            top = tops.get(name)
            if top is None:
                continue
            mesh = ps.ls.Mesh()
            def node(x, y, z):
                return mesh.insertNextNode([float(x), float(y), float(z)])
            for polygon in openings:
                ring = polygon[:-1] if len(polygon) > 3 and polygon[0] == polygon[-1] else polygon
                if len(ring) < 3:
                    continue
                z1 = top + margin
                z0 = top - depth
                top_nodes = [node(x, y, z1) for x, y in ring]
                bottom_nodes = [node(x, y, z0) for x, y in ring]
                for index in range(1, len(ring) - 1):
                    mesh.insertNextTriangle([top_nodes[0], top_nodes[index], top_nodes[index + 1]])
                    mesh.insertNextTriangle([bottom_nodes[0], bottom_nodes[index + 1], bottom_nodes[index]])
                for index in range(len(ring)):
                    nxt = (index + 1) % len(ring)
                    mesh.insertNextTriangle([bottom_nodes[index], bottom_nodes[nxt], top_nodes[nxt]])
                    mesh.insertNextTriangle([bottom_nodes[index], top_nodes[nxt], top_nodes[index]])
            if len(mesh.getNodes()) == 0:
                continue
            cutter = ps.LevelSet(self.domain.getGrid())
            reconstruction = ps.ls.FromSurfaceMesh(cutter, mesh)
            reconstruction.setRemoveBoundaryTriangles(False)
            reconstruction.apply()
            if cutter.getNumberOfPoints() == 0:
                continue
            self.domain.applyBooleanOperation(cutter, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,
                                              False)
        self._mesh = None
        self._columns = None

    def _layer_polygons(self, layer: int) -> list[list[tuple[float, float]]]:
        path = (self.job.layout or {}).get("path")
        if not path:
            raise NotImplementedError("litho needs job.layout.path")
        from engines.layout import read_gds_polygons

        _, _, polygons = read_gds_polygons(Path(path))
        return [points for poly_layer, _datatype, points in polygons if poly_layer == layer]

    def probe(self, points: list[list[float]]) -> list[str]:
        """Material label per point from the per-column material height field.

        ViennaLS exposes no point query on a level set, and a dense cell set at the
        level-set resolution is not usable (measured: 14.8 M cells / 107 s for one
        0.002 um domain), so the probe is derived from the surface mesh we already
        extract:

        * bin every triangle into a column by its centroid,
        * keep ``max z`` per material per column,
        * a point belongs to the material with the **lowest top above the point**.

        Exact for a z-monotone stack (plane, iso etch/deposit, CMP) -- the only
        class this adapter claims. Re-entrant shapes would need volumetric
        sampling, which is why ``compare.json.probeMethod`` records the method.
        """
        columns = self._height_columns()
        labels: list[str] = []
        for x, y, z in points:
            key = (int(float(x) // self.column), int(float(y) // self.column))
            tops = columns.get(key) or {}
            owner = "void"
            best: float | None = None
            for material, top in tops.items():
                if z <= top and (best is None or top < best):
                    best = top
                    owner = material
            labels.append(owner)
        return labels

    def section(self, spec: Any) -> dict[str, list[float | None]]:
        """Interface height per material per column, straight from the mesh."""
        columns = self._height_columns()
        heights: dict[str, list[float | None]] = {name: [] for name in spec.materials}
        for x, y in spec.columns:
            key = (int(float(x) // self.column), int(float(y) // self.column))
            tops = columns.get(key) or {}
            for name in spec.materials:
                heights[name].append(tops.get(name))
        return heights

    def _height_columns(self) -> dict[tuple[int, int], dict[str, float]]:
        """Per-column material tops, cached for the current step."""
        if self._columns is not None:
            return self._columns
        mesh = self._surface_mesh()
        points = mesh.getNodes()
        triangles = mesh.getTriangles()
        data = mesh.getCellData()
        ids = None
        for index in range(data.getScalarDataSize()):
            if str(data.getScalarDataLabel(index)) == "MaterialIds":
                ids = list(data.getScalarData(index))
        if ids is None:
            raise RuntimeError("surface mesh has no MaterialIds")
        columns: dict[tuple[int, int], dict[str, float]] = {}
        for triangle, material_id in zip(triangles, ids):
            name = self.materials.get(int(material_id))
            if name is None:
                raise RuntimeError(f"unknown material id {material_id}")
            xs = [float(points[i][0]) for i in triangle]
            ys = [float(points[i][1]) for i in triangle]
            zs = [float(points[i][2]) for i in triangle]
            key = (int((sum(xs) / 3) // self.column), int((sum(ys) / 3) // self.column))
            bucket = columns.setdefault(key, {})
            top = max(zs)
            if name not in bucket or top > bucket[name]:
                bucket[name] = top
        self._columns = columns
        return columns

    def _surface_mesh(self):
        if self._mesh is None:
            self._mesh = self.domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                                    minNodeDistanceFactor=0.01)
        return self._mesh

    def frame(self, step_id: str) -> Frame:
        ps = self.ps
        mesh = self._surface_mesh()
        points = [[float(v) for v in p] for p in mesh.getNodes()]
        triangles = [[int(v) for v in t] for t in mesh.getTriangles()]
        data = mesh.getCellData()
        ids = None
        for index in range(data.getScalarDataSize()):
            if str(data.getScalarDataLabel(index)) == "MaterialIds":
                ids = [int(v) for v in data.getScalarData(index)]
        if ids is None or len(ids) != len(triangles):
            raise RuntimeError("surface mesh has no aligned MaterialIds")
        self._seen_materials(ids)
        payload = {
            "stepId": step_id, "kind": "mesh", "dimension": self.job.domain.dimensions,
            "points": points, "triangles": triangles,
            "materialIds": ids,
            "materials": [{"id": mid, "name": name,
                           "color": MATERIAL_COLORS.get(name, "#888888")}
                          for mid, name in sorted(self.materials.items())],
            "bbox": [0.0, 0.0, -self.job.initial.substrate_thickness,
                     self.job.domain.width, self.job.domain.depth, self.job.domain.height],
        }
        frames = self.out / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        target = frames / f"{step_id}-viennaps.mesh.json"
        target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return Frame(step_id=step_id, kind="mesh", path=str(target.relative_to(self.out)),
                     materials=payload["materials"], bbox=payload["bbox"])

    def _seen_materials(self, ids: list[int]) -> None:
        for value in ids:
            name = self.materials.get(value)
            if name is None:
                raise RuntimeError(f"surface mesh reports unregistered material id {value}")


def _as_rectangle(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """Axis-aligned rectangle -> (x0, y0, x1, y1); anything else -> None."""
    xs = sorted({round(x, 9) for x, _ in points})
    ys = sorted({round(y, 9) for _, y in points})
    cleaned = points[:-1] if len(points) > 1 and points[0] == points[-1] else list(points)
    if len(xs) != 2 or len(ys) != 2 or len(cleaned) != 4:
        return None
    return xs[0], ys[0], xs[1], ys[1]


def _material_handle(ps: Any, name: str) -> Any:
    from process_models.v2.engines.viennaps import material_handle
    return material_handle(ps, name)


def engine_info() -> dict[str, Any]:
    from backend.engine import installed_version_map
    versions = installed_version_map()
    return {
        "engine_id": "viennaps", "version": versions["viennaps"],
        "dimensions": (3,), "mechanisms": ("initial", "isotropic"),
        "frame_kinds": ("mesh",), "adapter": "engines.viennaps.engine",
        "limits": {"viennals": versions["viennals"], "python": sys.version.split()[0]},
    }


def run_job(job: JobSpec, out_dir: Path) -> dict[str, Any]:
    session = ViennapsSession(job, out_dir)
    session.open()
    probe_points = list(job.probe.points) if job.probe is not None else []
    blocked: str | None = None
    section_spec = job.section
    outcomes: list[StepOutcome] = []
    for index, step in enumerate(job.steps):
        if blocked is not None:
            outcomes.append(StepOutcome(step_id=step.step_id, status="out_of_domain",
                                        wall_ms=0.0,
                                        reason=f"skipped: an earlier step ({blocked}) left "
                                               f"this engine's domain"))
            continue
        if index != 0:
            reason, estimate = engine_limits.check("viennaps", job, step)
            if reason is not None:
                blocked = step.step_id
                outcomes.append(StepOutcome(step_id=step.step_id, status="out_of_domain",
                                            wall_ms=0.0, metrics={"estimate": estimate},
                                            reason=reason))
                print(json.dumps({"event": "step_out_of_domain", "stepId": step.step_id}),
                      flush=True)
                continue
        if index == 0:
            out = StepOutcome(step_id=step.step_id, status="ok", wall_ms=0.0)
        else:
            out = session.apply(step)
        if out.status == "unsupported":
            outcomes.append(out)
            print(json.dumps({"event": "step_unsupported", "stepId": step.step_id}), flush=True)
            continue
        started = time.perf_counter()
        try:
            frame = session.frame(step.step_id)
            probe_started = time.perf_counter()
            labels = session.probe(probe_points) if probe_points else None
            section = session.section(section_spec) if section_spec is not None else None
            out = StepOutcome(step_id=step.step_id, status=out.status,
                              wall_ms=out.wall_ms,
                              frame=frame, checkpoint=session.checkpoint(step.step_id),
                              metrics={"frame_ms": (time.perf_counter() - started) * 1000.0,
                                       "probe_ms": (time.perf_counter() - probe_started) * 1000.0,
                                       "probe_points": len(probe_points)},
                              reason=out.reason, probe=labels, section=section)
        except Exception as exc:  # noqa: BLE001
            out = StepOutcome(step_id=step.step_id, status="failed",
                              wall_ms=out.wall_ms, reason=f"{type(exc).__name__}: {exc}")
        outcomes.append(out)
        print(json.dumps({"event": "step_done", "stepId": step.step_id,
                          "status": out.status}), flush=True)
    return {"engine": engine_info(), "outcomes": outcomes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    job = JobSpec.from_json(json.loads(args.job.read_text(encoding="utf-8")))
    try:
        result = run_job(job, args.out_dir)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        result = {"engine": engine_info(), "outcomes": [], "error": f"{type(exc).__name__}: {exc}"}
    write_json(args.out_dir / "result.json", _as_json(result))
    return 0 if "error" not in result else 1


def _as_json(payload: Any) -> Any:
    from dataclasses import asdict, is_dataclass
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    if isinstance(payload, dict):
        return {k: _as_json(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_as_json(v) for v in payload]
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
