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
            if step.mechanism == "isotropic" and step.kind in ("etch", "deposit"):
                model = ps.IsotropicProcess(rate=0.0, maskMaterials=[])
                for material, rate in step.rates.items():
                    model.setMaterialRate(_material_handle(ps, material), float(rate))
                ps.Process(self.domain, model, float(step.duration)).apply()
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
            elif step.mechanism == "cmp":
                cutoff = float(step.parameters.get("cutoff"))
                ps.Planarize(self.domain, cutoff).apply()
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
