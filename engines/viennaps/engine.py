"""ViennaPS adapter built on ``engines.sdk``.

Runs inside the workbench virtualenv (ViennaPS 4.7.0 / ViennaLS 5.10.0).

Key fixes in the multi-engine architecture:
* **Exact multi-level-set vertical ray-winding probe** (``probe_method='ray_winding'``):
  replaces the single-``max(z)``-per-material ``height_column`` dict, which collapsed
  multi-layer stacks whenever the same material (e.g. ``SiO2`` x 8 in Memory1) appeared
  at multiple heights and could not represent re-entrant cavities;
* **Buried-sheet protection**: patterned and blanket etches only cut level-set sheets
  whose surface intersects the etched z-band instead of mutating every historical sheet
  of that material down to the substrate;
* **Opening lifecycle**: ``_openings`` resets on planarising ``fill`` and ``cmp`` steps
  so subsequent blanket etchback steps do not inherit stale mask polygons;
* **Safe ``.vpsd`` checkpoints**: empty level sets (``getNumberOfPoints() == 0`` left
  behind by ``Planarize`` / ``BooleanOperation``) are pruned before ``ps.Writer`` so
  ``ps.Reader`` never fails with ``Reading Grid from stream failed``;
* **StepModelRegistry + EngineAdapterBase**: strict separation of ``solve_ms``,
  ``frame_ms``, ``probe_ms``, ``section_ms``, and ``checkpoint_ms``, plus pluggable
  per-step ``model`` overrides.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines.normalize import color as canonical_color  # noqa: E402
from engines.protocol import EngineInfo, Frame, JobSpec, SectionSpec, StepIR  # noqa: E402
from engines.sdk import (  # noqa: E402
    EngineAdapterBase,
    StepModelContext,
    StepModelRegistry,
    run_adapter_cli,
)


def _ccw_ring(polygon: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ring = list(polygon)
    if len(ring) > 3 and ring[0] == ring[-1]:
        ring.pop()
    area = 0.0
    for index in range(len(ring)):
        x0, y0 = ring[index]
        x1, y1 = ring[(index + 1) % len(ring)]
        area += x0 * y1 - x1 * y0
    if area < 0:
        ring.reverse()
    return ring


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
        self.stack: list[str] = []
        self._ls: dict[str, list[Any]] = {}
        self._mesh = None
        self._ray_bins: list[tuple[str, float, dict[tuple[int, int], list[tuple[float, ...]]]]] | None = None
        self._openings: list[list[tuple[float, float]]] = []

    def invalidate_caches(self) -> None:
        self._mesh = None
        self._ray_bins = None

    def open(self) -> None:
        ps = self.ps
        d = self.job.domain
        bounds = [
            0.0, d.width, 0.0, d.depth,
            -self.job.initial.substrate_thickness, d.height,
        ]
        self.domain = ps.Domain(
            bounds,
            [
                ps.BoundaryType.PERIODIC_BOUNDARY,
                ps.BoundaryType.PERIODIC_BOUNDARY,
                ps.BoundaryType.INFINITE_BOUNDARY,
            ],
            d.grid_delta,
        )
        if self.job.restore:
            ps.Reader(self.domain, str(self.job.restore)).apply()
            self._sync_domain_refs()
            return
        initial = self.job.initial
        if initial.kind != "plane":
            raise NotImplementedError(
                f"initial structure {initial.kind!r} is not wired for the ViennaPS adapter yet"
            )
        height = 0.0
        self._add_sheet(initial.substrate, height)
        for film in initial.films:
            height += film.thickness
            self._add_sheet(film.material, height)

    def _sync_domain_refs(self) -> None:
        """Rebuild ``self.materials``, ``self.stack``, and ``self._ls`` from ``self.domain``."""
        ps = self.ps
        reg = ps.MaterialRegistry.instance()
        mmap = self.domain.getMaterialMap()
        lss = list(self.domain.getLevelSets())
        self._ls = {}
        self.stack = []
        for idx, ls_dom in enumerate(lss):
            mid = int(mmap.getMaterialIdAtIdx(idx))
            if mid not in self.materials:
                try:
                    info = reg.getInfo(ps.Material(mid))
                    name = str(info.name) if info and info.name else str(mid)
                except Exception:  # noqa: BLE001
                    name = str(mid)
                self.materials[mid] = name
            name = self.materials[mid]
            self._ls.setdefault(name, []).append(ls_dom)
            if name not in self.stack:
                self.stack.append(name)
        self.invalidate_caches()

    def _prune_empty_level_sets(self) -> None:
        """Rebuild ``self.domain`` without 0-point level sets that corrupt ``ps.Writer``."""
        lss = list(self.domain.getLevelSets())
        if not lss or all(ls_dom.getNumberOfPoints() > 0 for ls_dom in lss):
            return
        mmap = self.domain.getMaterialMap()
        kept: list[tuple[Any, Any]] = []
        for idx, ls_dom in enumerate(lss):
            if ls_dom.getNumberOfPoints() > 0:
                kept.append((ls_dom, mmap.getMaterialAtIdx(idx)))
        if not kept:
            return
        rebuilt = self.ps.Domain(self.domain)
        rebuilt.clear()
        for ls_dom, handle in kept:
            rebuilt.insertNextLevelSetAsMaterial(ls_dom, handle, False)
        self.domain = rebuilt
        self._sync_domain_refs()

    def _add_sheet(self, material: str, height: float) -> None:
        ps = self.ps
        ps.MakePlane(
            self.domain,
            height=float(height),
            material=_material_handle(ps, material),
            addToExisting=bool(self.stack),
        ).apply()
        self._register(material)
        self._ls.setdefault(material, []).append(self.domain.getLevelSets()[-1])
        if material not in self.stack:
            self.stack.append(material)
        self.invalidate_caches()

    def _register(self, name: str) -> None:
        material = _material_handle(self.ps, name)
        self.materials[int(material)] = name

    def checkpoint(self, step_id: str) -> str | None:
        self._prune_empty_level_sets()
        target = self.out / "state" / f"{step_id}.vpsd"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.ps.Writer(self.domain, str(target)).apply()
            return str(target.relative_to(self.out))
        except Exception:  # noqa: BLE001
            return None

    def _surface_top(self) -> float:
        mesh = self._surface_mesh()
        nodes = mesh.getNodes()
        if not nodes:
            return 0.0
        return max(float(point[2]) for point in nodes)

    def _window_box(self, z0: float, z1: float):
        box = self.ps.LevelSet(self.domain.getGrid())
        self.ps.MakeGeometry(
            box,
            self.ps.Box(
                [0.0, 0.0, float(z0)],
                [float(self.job.domain.width), float(self.job.domain.depth), float(z1)],
            ),
        ).apply()
        return box

    def _sheet_bounds(self, sheet: Any) -> tuple[float, float] | None:
        if sheet.getNumberOfPoints() == 0:
            return None
        mesh = self.ps.ls.Mesh()
        conv = self.ps.ls.ToSurfaceMesh(sheet, mesh)
        conv.apply()
        nodes = [[float(v) for v in n] for n in mesh.getNodes()]
        del conv
        del mesh
        if not nodes:
            return None
        zs = [n[2] for n in nodes]
        return min(zs), max(zs)

    def _member_top(self, name: str) -> float | None:
        sheets = self._ls.get(name) or []
        best: float | None = None
        for sheet in sheets:
            bounds = self._sheet_bounds(sheet)
            if bounds is not None:
                best = bounds[1] if best is None else max(best, bounds[1])
        return best

    def _top_band_thickness(self, name: str) -> tuple[float | None, float]:
        """Return ``(top_z, thickness)`` of the highest sheet of ``name``."""
        lss = list(self.domain.getLevelSets())
        mmap = self.domain.getMaterialMap()
        best_top: float | None = None
        best_idx = -1
        for idx, ls_dom in enumerate(lss):
            mid = int(mmap.getMaterialIdAtIdx(idx))
            if self.materials.get(mid) != name:
                continue
            b = self._sheet_bounds(ls_dom)
            if b is not None and (best_top is None or b[1] >= best_top):
                best_top = b[1]
                best_idx = idx
        if best_top is None:
            return None, 0.0
        lower_top = -float(self.job.initial.substrate_thickness)
        for idx in range(best_idx):
            b = self._sheet_bounds(lss[idx])
            if b is not None and b[1] <= best_top + 1e-9:
                lower_top = max(lower_top, b[1])
        return best_top, max(0.0, best_top - lower_top)

    def _etch_duration(self, step: StepIR, rates: dict[Any, tuple[float, float]]) -> tuple[float, dict[str, Any] | None]:
        duration = float(step.duration)
        limit = duration
        for material, (vertical, lateral) in rates.items():
            name = self.materials.get(int(material))
            if name is None:
                continue
            rate = max(abs(float(vertical)), abs(float(lateral)))
            if rate <= 0:
                continue
            top_z, thickness = self._top_band_thickness(name)
            if top_z is not None and thickness > 0:
                limit = min(limit, thickness / rate)
        note = {"requested": duration, "applied": round(limit, 6)} if limit < duration else None
        return limit, note

    def _material_top(self, name: str) -> float | None:
        return self._member_top(name)

    def _blanket_etch(self, step: StepIR, duration: float) -> dict[str, Any]:
        planes: list[float] = []
        limit = float(self.job.domain.height) + 1.0
        for name, rate in step.rates.items():
            if float(rate) >= 0:
                continue
            top = self._member_top(name)
            if top is None:
                continue
            cutoff = top - abs(float(rate)) * float(duration)
            planes.append(cutoff)
            # Only cut sheets whose top actually sits above ``cutoff`` (typically the
            # uppermost sheet of ``name``), never buried sheets below ``cutoff``.
            for sheet in (self._ls.get(name) or []):
                b = self._sheet_bounds(sheet)
                if b is not None and b[1] > cutoff + 1e-9:
                    self.ps.ls.BooleanOperation(
                        sheet,
                        self._window_box(cutoff, limit),
                        self.ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT,
                    ).apply()
        self.invalidate_caches()
        if not planes:
            return {"etchClamp": {"blanket": "no exposed material with a rate"}}
        return {"etchClamp": {"blanketCutoff": round(float(min(planes)), 6)}}

    def _deposit_shell(self, material: str, thickness: float) -> str:
        if thickness <= 0:
            return "noop"
        budget = int(self.job.render.camera.get("shell_triangles", 0) or 0)
        if budget <= 0:
            self._add_sheet(material, self._surface_top() + thickness)
            return "planar"
        ps = self.ps
        top = self.domain.getLevelSets()[-1]
        mesh = ps.ls.Mesh()
        ps.ls.ToSurfaceMesh(top, mesh).apply()
        nodes = [[float(v) for v in node] for node in mesh.getNodes()]
        triangles = [list(tri) for tri in mesh.getTriangles()]
        if not nodes or not triangles or len(triangles) > budget:
            self._add_sheet(material, self._surface_top() + thickness)
            return "planar"
        original_top = max(float(node[2]) for node in nodes)
        original_low = min(float(node[2]) for node in nodes)
        if (original_top - original_low) < 2.0 * float(self.job.domain.grid_delta):
            self._add_sheet(material, self._surface_top() + thickness)
            return "planar"

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
        offset_positions: list[list[float]] = []
        for index, (x, y, z) in enumerate(nodes):
            nx, ny, nz = normals[index]
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length <= 1e-20:
                position = [x, y, z]
            else:
                position = [
                    x + nx / length * thickness,
                    y + ny / length * thickness,
                    z + nz / length * thickness,
                ]
            offset_positions.append(position)
            moved.append(outward.insertNextNode(position))
        for a, b, c in triangles:
            outward.insertNextTriangle([moved[a], moved[b], moved[c]])
        shell = ps.LevelSet(self.domain.getGrid())
        reconstruction = ps.ls.FromSurfaceMesh(shell, outward)
        reconstruction.setRemoveBoundaryTriangles(True)
        reconstruction.apply()
        shell_top = max((point[2] for point in offset_positions), default=float("-inf"))
        if shell.getNumberOfPoints() < 10 or shell_top < original_top + 0.6 * thickness:
            self._add_sheet(material, self._surface_top() + thickness)
            return "planar"
        self.domain.insertNextLevelSetAsMaterial(shell, _material_handle(ps, material))
        self._register(material)
        self._ls.setdefault(material, []).append(self.domain.getLevelSets()[-1])
        if material not in self.stack:
            self.stack.append(material)
        self.invalidate_caches()
        return "shell"

    def _etched_openings(
        self,
        step: StepIR,
        rates: dict[Any, tuple[float, float]],
        *,
        openings: list[list[tuple[float, float]]],
    ) -> None:
        ps = self.ps
        margin = float(self.job.domain.grid_delta)
        duration = float(step.duration)
        # Group cuts per material using its uppermost sheet band so buried sheets
        # of repeated materials are untouched.
        cuts: list[tuple[str, float, float, Any]] = []
        for material, (vertical, _lateral) in rates.items():
            name = self.materials.get(int(material))
            if name is None:
                raise RuntimeError(f"step names unknown material {material}")
            req_depth = round(abs(float(vertical)) * duration, 9)
            if req_depth <= 0:
                continue
            top_z, band_thick = self._top_band_thickness(name)
            if top_z is None or band_thick <= 0:
                continue
            depth = min(req_depth, band_thick)
            sheets = self._ls.get(name) or []
            target_sheet = sheets[-1] if sheets else None
            if target_sheet is not None and depth > 0:
                cuts.append((name, top_z, depth, target_sheet))

        for _name, top_z, depth, target_sheet in cuts:
            z1 = top_z + margin
            z0 = max(
                top_z - depth - margin * 0.25,
                -float(self.job.initial.substrate_thickness) + margin,
            )
            mesh = ps.ls.Mesh()

            def node(x, y, z):
                return mesh.insertNextNode([float(x), float(y), float(z)])

            for polygon in openings:
                ring = _ccw_ring(polygon)
                if len(ring) < 3:
                    continue
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
            reconstruction.setRemoveBoundaryTriangles(True)
            reconstruction.apply()
            if cutter.getNumberOfPoints() == 0:
                continue
            ps.ls.BooleanOperation(
                target_sheet, cutter, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT
            ).apply()
        self.invalidate_caches()

    # -- Exact interface ray-crossing probe on domain.getSurfaceMesh -------
    def _ensure_ray_bins(self):
        if self._ray_bins is not None:
            return self._ray_bins
        ps = self.ps
        reg = ps.MaterialRegistry.instance()
        mesh = self._surface_mesh()
        nodes = mesh.getNodes()
        tris = mesh.getTriangles()
        data = mesh.getCellData()
        ids = None
        for index in range(data.getScalarDataSize()):
            if str(data.getScalarDataLabel(index)) == "MaterialIds":
                ids = list(data.getScalarData(index))
        if not nodes or not tris or ids is None:
            self._ray_bins = []
            return self._ray_bins

        bw = max(float(self.job.domain.grid_delta) * 4.0, 0.02)
        bins: dict[tuple[int, int], list[tuple[float, ...]]] = {}
        for (a, b, c), raw_mid in zip(tris, ids):
            mid = int(raw_mid)
            name = self.materials.get(mid)
            if name is None:
                try:
                    info = reg.getInfo(ps.Material(mid))
                    name = str(info.name) if info and info.name else str(mid)
                except Exception:  # noqa: BLE001
                    name = str(mid)
                self.materials[mid] = name
            x0, y0, z0 = float(nodes[a][0]), float(nodes[a][1]), float(nodes[a][2])
            x1, y1, z1 = float(nodes[b][0]), float(nodes[b][1]), float(nodes[b][2])
            x2, y2, z2 = float(nodes[c][0]), float(nodes[c][1]), float(nodes[c][2])
            det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
            if abs(det) < 1e-18:
                continue
            ix0 = int(min(x0, x1, x2) // bw)
            ix1 = int(max(x0, x1, x2) // bw)
            iy0 = int(min(y0, y1, y2) // bw)
            iy1 = int(max(y0, y1, y2) // bw)
            tri = (x0, y0, z0, x1, y1, z1, x2, y2, z2, det, float(mid))
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    bins.setdefault((ix, iy), []).append(tri)
        self._ray_bins = [("", bw, bins)]
        return self._ray_bins

    def _column_interfaces(self, ox: float, oy: float) -> list[tuple[float, int, str]]:
        """Sorted ``(zhit, sign_nz, material_name)`` along vertical ray ``(ox, oy)``."""
        entries = self._ensure_ray_bins()
        if not entries:
            return []
        _unused, bw, bins = entries[0]
        w = float(self.job.domain.width)
        d = float(self.job.domain.depth)
        qx = min(max(float(ox) + 1.37e-8, 1e-8), max(w - 1e-8, 1e-8))
        qy = min(max(float(oy) + 2.19e-8, 1e-8), max(d - 1e-8, 1e-8))
        hits: list[tuple[float, int, str]] = []
        for x0, y0, z0, x1, y1, z1, x2, y2, z2, det, mid_f in bins.get(
            (int(qx // bw), int(qy // bw)), ()
        ):
            u = ((qx - x0) * (y2 - y0) - (x2 - x0) * (qy - y0)) / det
            v = ((x1 - x0) * (qy - y0) - (qx - x0) * (y1 - y0)) / det
            if u >= 0.0 and v >= 0.0 and u + v <= 1.0:
                zhit = z0 + u * (z1 - z0) + v * (z2 - z0)
                name = self.materials.get(int(mid_f), str(int(mid_f)))
                hits.append((zhit, 1 if det > 0.0 else -1, name))
        hits.sort(key=lambda item: item[0])
        # Deduplicate coincident interface hits within 1e-6 um (e.g. zero-thickness wrapper)
        return hits

    def probe(self, points: list[list[float]]) -> list[str]:
        if not points:
            return []
        col_cache: dict[tuple[float, float], list[tuple[float, int, str]]] = {}
        labels: list[str] = []
        for x, y, z in points:
            key = (float(x), float(y))
            hits = col_cache.get(key)
            if hits is None:
                hits = self._column_interfaces(key[0], key[1])
                col_cache[key] = hits
            zf = float(z)
            owner = "void"
            for zhit, sign, name in hits:
                if zhit > zf:
                    owner = name if sign > 0 else "void"
                    break
            labels.append(owner)
        return labels

    def section(self, spec: SectionSpec) -> dict[str, list[float | None]]:
        heights: dict[str, list[float | None]] = {name: [] for name in spec.materials}
        for x, y in spec.columns:
            hits = self._column_interfaces(float(x), float(y))
            tops: dict[str, float] = {}
            for zhit, sign, name in hits:
                if sign > 0 and spec.z_min <= zhit <= spec.z_max:
                    if name not in tops or zhit > tops[name]:
                        tops[name] = round(zhit, 6)
            for name in spec.materials:
                heights[name].append(tops.get(name))
        return heights

    def _surface_mesh(self):
        if self._mesh is None:
            self._mesh = self.domain.getSurfaceMesh(
                addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01
            )
        return self._mesh

    def frame(self, step_id: str) -> Frame:
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
        for value in ids:
            if value not in self.materials:
                raise RuntimeError(f"surface mesh reports unregistered material id {value}")
        payload = {
            "stepId": step_id,
            "kind": "mesh",
            "dimension": self.job.domain.dimensions,
            "points": points,
            "triangles": triangles,
            "materialIds": ids,
            "materials": [
                {"id": mid, "name": name, "color": canonical_color(name)}
                for mid, name in sorted(self.materials.items())
            ],
            "bbox": [
                0.0, 0.0, -self.job.initial.substrate_thickness,
                self.job.domain.width, self.job.domain.depth, self.job.domain.height,
            ],
        }
        frames = self.out / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        target = frames / f"{step_id}-viennaps.mesh.json"
        target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return Frame(
            step_id=step_id,
            kind="mesh",
            path=str(target.relative_to(self.out)),
            materials=payload["materials"],
            bbox=payload["bbox"],
        )


def build_viennaps_models() -> StepModelRegistry[ViennapsSession]:
    registry: StepModelRegistry[ViennapsSession] = StepModelRegistry("viennaps")

    @registry.register("deposit", "isotropic")
    def depo_iso(session: ViennapsSession, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        present = set(session.stack)
        top = session.stack[-1] if session.stack else None
        new = {
            name: rate for name, rate in step.rates.items()
            if float(rate) > 0 and (name not in present or name != top)
        }
        growth = {
            name: rate for name, rate in step.rates.items()
            if abs(float(rate)) > 0 and name == top
        }
        depo_model = "planar"
        for material, rate in growth.items():
            session._add_sheet(
                material, session._surface_top() + abs(float(rate)) * float(step.duration)
            )
        for material, rate in new.items():
            depo_model = session._deposit_shell(
                material, abs(float(rate)) * float(step.duration)
            )
        session._openings = []
        return {"depositModel": depo_model}

    @registry.register("etch", "isotropic")
    def etch_iso(session: ViennapsSession, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        ps = session.ps
        duration, clamp_note = session._etch_duration(
            step, {_material_handle(ps, n): (r, r) for n, r in step.rates.items()}
        )
        notes = session._blanket_etch(step, duration)
        if clamp_note:
            notes["etchDurationClamp"] = clamp_note
        return notes

    @registry.register("etch", "anisotropic")
    def etch_aniso(session: ViennapsSession, step: StepIR, ctx: StepModelContext) -> dict[str, Any]:
        ps = session.ps
        openings = session._openings
        if not openings and step.parameters.get("openings_layer") is not None:
            openings = ctx.layer_polygons(int(step.parameters["openings_layer"]))
        if openings and step.parameters.get("patterned"):
            rates = {
                _material_handle(ps, name): (float(rate), 0.0)
                for name, rate in step.rates.items()
            }
            session._etched_openings(step, rates, openings=openings)
            return {"etchMode": "patterned_openings"}
        split = min(max(float(step.anisotropy), 0.0), 1.0)
        rates = {
            _material_handle(ps, mat): (float(r) * split, float(r) * (1.0 - split))
            for mat, r in step.rates.items()
        }
        if not rates:
            raise NotImplementedError("anisotropic etch step has no rates")
        duration, clamp_note = session._etch_duration(step, rates)
        notes = session._blanket_etch(step, duration)
        if clamp_note:
            notes["etchDurationClamp"] = clamp_note
        return notes

    @registry.register("deposit", "anisotropic")
    def depo_aniso(session: ViennapsSession, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        ps = session.ps
        split = min(max(float(step.anisotropy), 0.0), 1.0)
        rates = {
            _material_handle(ps, mat): (float(r) * split, float(r) * (1.0 - split))
            for mat, r in step.rates.items()
        }
        if not rates:
            raise NotImplementedError("anisotropic deposit step has no rates")
        direction = [float(v) for v in (step.parameters.get("direction") or [0.0, 0.0, 1.0])]
        model = ps.DirectionalProcess(direction, rates, 0.0, 0.0)
        ps.Process(session.domain, model, float(step.duration)).apply()
        session._openings = []
        session.invalidate_caches()
        return {}

    @registry.register("strip", "strip")
    def strip_step(session: ViennapsSession, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        ps = session.ps
        names = [str(name) for name in (step.parameters.get("materials") or [])]
        if not names:
            raise NotImplementedError("strip step lists no materials")
        for name in names:
            session.domain.removeMaterial(_material_handle(ps, name))
            session._ls.pop(name, None)
            session.stack = [entry for entry in session.stack if entry != name] or session.stack
        session.invalidate_caches()
        return {"stripped": names}

    @registry.register("etch", "ellipsoidal")
    def etch_ellipsoidal(
        session: ViennapsSession, step: StepIR, ctx: StepModelContext
    ) -> dict[str, Any]:
        ps = session.ps
        rates = {}
        for name, pair in (step.parameters.get("rates") or {}).items():
            vertical, lateral = pair
            rates[_material_handle(ps, name)] = (float(vertical), float(lateral))
        if not rates:
            raise NotImplementedError("ellipsoidal step lists no rates")
        openings = session._openings
        if not openings and step.parameters.get("openings_layer") is not None:
            openings = ctx.layer_polygons(int(step.parameters["openings_layer"]))
        masks = [_material_handle(ps, name) for name in (step.parameters.get("mask") or [])]
        if openings and step.parameters.get("patterned"):
            session._etched_openings(step, rates, openings=openings)
            return {"etchMode": "patterned_openings"}
        if masks:
            for material, (vertical, lateral) in rates.items():
                model = ps.DirectionalProcess([0.0, 0.0, 1.0], vertical, lateral, masks)
                ps.Process(session.domain, model, float(step.duration)).apply()
        else:
            model = ps.DirectionalProcess([0.0, 0.0, 1.0], rates, 0.0, 0.0)
            ps.Process(session.domain, model, float(step.duration)).apply()
        session.invalidate_caches()
        return {}

    @registry.register("deposit", "fill")
    def depo_fill(session: ViennapsSession, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        material = str(step.parameters["material"])
        offset = float(step.parameters.get("offset", 0.0))
        thickness = float(step.parameters.get("thickness", 0.0) or 0.0)
        depth = offset if offset > 0 else (thickness if thickness > 0 else 0.0)
        if depth <= 0:
            raise ValueError(f"fill of {material!r} has no thickness")
        session._add_sheet(material, session._surface_top() + depth)
        session._openings = []
        return {}

    @registry.register("mask", "litho")
    def mask_litho(session: ViennapsSession, step: StepIR, ctx: StepModelContext) -> dict[str, Any]:
        ps = session.ps
        layer = int(step.parameters["layer"])
        polarity = str(step.parameters.get("polarity", "negative")).lower()
        thickness = float(step.parameters["thickness"])
        coat = float(step.parameters.get("coat", 0.1))
        material = str(step.parameters.get("material", "Mask"))
        margin = float(session.job.domain.grid_delta)

        z_base = session._surface_top()
        z_top = z_base
        if polarity == "negative" and coat > 0:
            slab = ps.LevelSet(session.domain.getGrid())
            ps.MakeGeometry(
                slab,
                ps.Box(
                    [-margin, -margin, z_top],
                    [
                        float(session.job.domain.width) + margin,
                        float(session.job.domain.depth) + margin,
                        z_top + coat,
                    ],
                ),
            ).apply()
            if session._ls.get(material):
                ps.ls.BooleanOperation(
                    session._ls[material][-1], slab, ps.ls.BooleanOperationEnum.UNION
                ).apply()
            else:
                session.domain.insertNextLevelSetAsMaterial(
                    slab, _material_handle(ps, material)
                )
                session._register(material)
                session._ls.setdefault(material, []).append(session.domain.getLevelSets()[-1])
                if material not in session.stack:
                    session.stack.append(material)
            z_top += coat

        z0 = z_base if polarity == "negative" else z_top - thickness
        z1 = z_top
        polygons = [poly for poly in ctx.layer_polygons(layer) if len(poly) >= 3]
        session._openings = polygons
        if not polygons:
            raise NotImplementedError(f"litho layer {layer} has no polygons")
        mesh = ps.ls.Mesh()
        node = lambda x, y, z: mesh.insertNextNode([float(x), float(y), float(z)])  # noqa: E731
        for polygon in polygons:
            ring = _ccw_ring(polygon)
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
        body = ps.LevelSet(session.domain.getGrid())
        reconstruction = ps.ls.FromSurfaceMesh(body, mesh)
        reconstruction.setRemoveBoundaryTriangles(False)
        reconstruction.apply()
        if body.getNumberOfPoints() == 0:
            raise NotImplementedError(f"litho layer {layer} produced an empty level set")
        if polarity == "positive":
            session.domain.insertNextLevelSetAsMaterial(body, _material_handle(ps, material))
            session._register(material)
            session._ls.setdefault(material, []).append(session.domain.getLevelSets()[-1])
            if material not in session.stack:
                session.stack.append(material)
        else:
            target = session._ls.get(material) or [session.domain.getLevelSets()[-1]]
            ps.ls.BooleanOperation(
                target[-1], body, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT
            ).apply()
        session.invalidate_caches()
        return {"layer": layer, "polygons": len(polygons)}

    @registry.register("planarize", "cmp")
    def planarize_cmp(
        session: ViennapsSession, step: StepIR, _ctx: StepModelContext
    ) -> dict[str, Any]:
        params = step.parameters
        if params.get("stop_material"):
            stop = str(params["stop_material"])
            stop_top = session._material_top(stop)
            if stop_top is None:
                session._openings = []
                return {"cmpNoOp": True, "reason": f"stop material {stop!r} absent; cmp is a no-op"}
            cutoff = stop_top + float(params.get("offset", 0.0))
        else:
            cutoff = float(params["cutoff"])
        session.ps.Planarize(session.domain, cutoff).apply()
        session._prune_empty_level_sets()
        session._openings = []
        session.invalidate_caches()
        return {"cutoffUm": round(cutoff, 6)}

    return registry


class ViennapsEngineAdapter(EngineAdapterBase[ViennapsSession]):
    def __init__(self) -> None:
        super().__init__(build_viennaps_models())

    def info(self) -> EngineInfo:
        from backend.engine import installed_version_map

        versions = installed_version_map()
        return EngineInfo(
            engine_id="viennaps",
            version=versions["viennaps"],
            dimensions=(3,),
            mechanisms=self.models.supported_mechanisms(),
            frame_kinds=("mesh",),
            adapter="engines.viennaps.engine",
            solve_mode="stepwise",
            probe_method="ray_winding",
            limits={
                "viennals": versions["viennals"],
                "python": sys.version.split()[0],
                "max_active_points": 5.0e6,
                "max_ratio": 200.0,
                "max_area_um2": 100.0,
            },
        )

    def engine_limits(self) -> dict[str, float]:
        return {"max_active_points": 5.0e6, "max_ratio": 200.0, "max_area_um2": 100.0}

    def open_state(self, job: JobSpec, out_dir: Path) -> ViennapsSession:
        session = ViennapsSession(job, out_dir)
        session.open()
        return session

    def extract_frame(
        self, state: ViennapsSession, _job: JobSpec, _out_dir: Path, step_id: str
    ) -> Frame:
        return state.frame(step_id)

    def probe_points(self, state: ViennapsSession, points: list[list[float]]) -> list[str]:
        return state.probe(points)

    def section_heights(
        self, state: ViennapsSession, spec: SectionSpec
    ) -> dict[str, list[float | None]]:
        return state.section(spec)

    def save_checkpoint(
        self, state: ViennapsSession, _out_dir: Path, step_id: str
    ) -> str | None:
        return state.checkpoint(step_id)


def run_job(job: JobSpec, out_dir: Path) -> dict[str, Any]:
    return ViennapsEngineAdapter().run_job(job, out_dir)


def main(argv: list[str] | None = None) -> int:
    return run_adapter_cli(ViennapsEngineAdapter(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
