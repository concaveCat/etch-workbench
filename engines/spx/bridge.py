"""SPX (Sentaurus Process Explorer) reference adapter.

Runs inside the **SPX-private Python 3.11** interpreter (``$SPX/linux64/bin/python3``).
Never imports viennaps or the backend.

Upgrades in the multi-engine architecture:
* **Fast native column-profile probing (``meas_thickness``, ~100x-400x speedup)**:
  SPX stores structures internally as ``geocolumns``; a single ``meas_thickness`` call
  at ``(x, y)`` returns the complete bottom-to-top stack of ``(region, material, thickness)``
  intervals **including buried ``Gas`` cavities**. An 8x9x73 (5,256-point) lattice plus
  a 33-column x 12-material cross-section now needs only ``72 + 33 = 105`` SPX queries
  per step (~110 ms) instead of ``5,256 + 14,652 = 19,908`` ``meas_probe`` queries
  (~25 s/step), cutting 88-step full comparison wall time from ~20 min to ~20 s;
* Uses ``engines.normalize`` (``normalize.json``) as the single source of truth for
  SPX material names and colours;
* Separates ``run.solveMs`` (batch ``sp.run()``) from ``frameMs``, ``probeMs``, and
  ``sectionMs``, and honours ``job.emit_frames=False``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engines import limits as engine_limits  # noqa: E402
from engines.camera import OrbitCamera, orbit_to_free  # noqa: E402
from engines.normalize import VOID, canonical, color as canonical_color, to_spx  # noqa: E402

DEFAULT_LAYOUT = "/root/SPX/spx/V-2024.03/linux64/examples/Logic1_Layout.oasis"
DEFAULT_WINDOW = (0.035, 0.0, 0.175, 0.288)
SPX_VIEWS = {"free", "iso", "iso90", "iso180", "iso270", "xy", "xz", "yx", "yz", "zx", "zy"}


def qupy_unit(name: str):
    import qupy
    return qupy.Unit(name)


def _um(value: float):
    import qupy
    return (float(value), qupy.Unit("um"))


def _rate(value: float):
    import qupy
    return (float(value), qupy.Unit("um min^-1"))


def _minutes(value: float):
    import qupy
    return (float(value), qupy.Unit("min"))


def _to_um_scalar(value: Any, default_unit: Any) -> float:
    import qupy

    if isinstance(value, (tuple, list)) and len(value) == 2:
        return float(qupy.Quantity_double(float(value[0]), value[1]).to(qupy.Unit("um"))[0])
    if default_unit is not None and getattr(default_unit, "name", "um") != "um":
        return float(qupy.Quantity_double(float(value), default_unit).to(qupy.Unit("um"))[0])
    return float(value)


def _material_filter(name: str):
    return [[("Material", "equals", name)]]


def intervals_from_thickness_output(
    output_top_to_bottom: list[Any],
    z_bottom_um: float,
    default_unit: Any = None,
) -> list[tuple[float, float, str]]:
    """Convert SPX ``meas_thickness['Output']`` into bottom-up ``(z0, z1, canonical_mat)``.

    SPX ``meas_thickness`` lists every vertical segment from top to bottom, including
    ``('Gas', 'Gas', gap_thickness)`` inside buried overhang cavities. Reversing it
    from ``z_bottom_um`` reconstructs exact double-precision z intervals.
    """
    cur_z = float(z_bottom_um)
    intervals: list[tuple[float, float, str]] = []
    for entry in reversed(output_top_to_bottom):
        raw_mat = entry[1]
        raw_thick = entry[2]
        thick_um = (
            float(raw_thick)
            if default_unit is None and isinstance(raw_thick, (int, float))
            else _to_um_scalar(raw_thick, default_unit)
        )
        if thick_um <= 0.0:
            continue
        next_z = cur_z + thick_um
        mat = canonical(raw_mat, "spx")
        if intervals and intervals[-1][2] == mat and abs(intervals[-1][1] - cur_z) <= 1e-9:
            intervals[-1] = (intervals[-1][0], next_z, mat)
        else:
            intervals.append((cur_z, next_z, mat))
        cur_z = next_z
    return intervals


def label_at_z(intervals: list[tuple[float, float, str]], z: float) -> str:
    """Lookup canonical material at ``z`` inside bottom-up ``intervals``."""
    zf = float(z)
    for z0, z1, mat in intervals:
        if z0 - 1e-9 <= zf <= z1 + 1e-9:
            # At an exact shared boundary, match SPX meas_probe convention (lower/inner wins
            # unless z is strictly above z1).
            if zf <= z1:
                return mat
    return VOID


def top_of_material_in_intervals(
    intervals: list[tuple[float, float, str]],
    material: str,
    z_min: float,
    z_max: float,
) -> float | None:
    """Highest ``z1`` in ``[z_min, z_max]`` occupied by ``material``."""
    best: float | None = None
    for _z0, z1, mat in intervals:
        if mat == material and z_min <= z1 <= z_max:
            best = z1 if best is None else max(best, z1)
    return round(best, 6) if best is not None else None


class SpxSession:
    def __init__(self, job: dict[str, Any], out_dir: Path) -> None:
        import spx.api as spx_api

        spx_api.init_session_licenses(spx_api.LicenseFeature.all, 4)
        import spx.procedural as sp  # after session licences, never before

        self.sp = sp
        self.job = job
        self.out = out_dir
        self.route = f"cmp_{job['case_id']}"
        self.flow = "F"
        self.module = "M0"
        self.materials: list[dict[str, Any]] = []
        self.origin = (0.0, 0.0)
        self.initial_location: str | None = None
        self.unsupported: dict[str, str] = {}
        self.out_of_domain: dict[str, tuple[str, dict[str, Any]]] = {}
        self.blocked_after: str | None = None
        self.blocked_kind = "unsupported"

    def _ensure_material(self, name: str) -> str:
        spx_name = to_spx(name)
        if any(m["spx"] == spx_name for m in self.materials):
            return spx_name
        hex_color = canonical_color(name)
        self.sp.material(Material=spx_name, Color=hex_color)
        self.materials.append({"name": name, "spx": spx_name, "color": hex_color})
        return spx_name

    def step_location(self, step_id: str, initial: bool) -> dict[str, str]:
        return self._loc(self.initial_location if initial and self.initial_location else step_id)

    def _loc(self, step_id: str) -> dict[str, str]:
        return {"Route": self.route, "Flow": self.flow, "Module": self.module, "Step": step_id}

    def build(self) -> float:
        started = time.perf_counter()
        sp = self.sp
        job = self.job
        initial = job["initial"]
        if initial["kind"] != "plane":
            raise NotImplementedError(
                f"initial structure {initial['kind']!r} is not wired for SPX yet")
        domain = job["domain"]
        if domain.get("dimensions", 3) != 3:
            raise NotImplementedError("SPX adapter currently renders 3D only")

        sp.route(Route=self.route)
        layout = self.job.get("layout") or {}
        layout_path = layout.get("path") or DEFAULT_LAYOUT
        origin = domain.get("origin_xy")
        if origin:
            window = [float(origin[0]), float(origin[1]),
                      float(origin[0]) + float(domain["width"]),
                      float(origin[1]) + float(domain["depth"])]
        else:
            window = layout.get("window") or DEFAULT_WINDOW
        self.origin = (float(window[0]), float(window[1]))
        sp.layout(Layout="layout", Path=layout_path)
        sp.clip(Location={"Route": self.route, "Clip": "C0"},
                Coords=[float(v) for v in window],
                LayoutFile="layout", Name="H1")
        import spx.util as su
        su.update_clip_layout({"Route": self.route}, "layout")
        sp.flow(Location={"Route": self.route, "Flow": self.flow})
        sp.module(Location={"Route": self.route, "Flow": self.flow, "Module": self.module})

        self._ensure_material(initial["substrate"])
        first = job["steps"][0]
        self.initial_location = first["step_id"]
        sp.substrate(Location=self._loc(first["step_id"]),
                     ColumnSizeX=_um(0.001),
                     ColumnSizeY=_um(0.001),
                     Thickness=_um(initial["substrate_thickness"]))
        for index, film in enumerate(initial.get("films", [])):
            spx_name = self._ensure_material(film["material"])
            step_id = f"{first['step_id']}_film{index}"
            self.initial_location = step_id
            sp.depo_isotropic(Location=self._loc(step_id),
                              MaterialFilter=_material_filter(spx_name),
                              Rate=_rate(1.0),
                              Time=_minutes(float(film["thickness"])))
        from engines.protocol import JobSpec

        spec = JobSpec.from_json(job)
        for step, ir in zip(job["steps"][1:], spec.steps[1:]):
            if self.blocked_after is not None:
                self.unsupported[step["step_id"]] = (
                    f"skipped: an earlier step ({self.blocked_after}) is "
                    f"{self.blocked_kind}")
                continue
            reason, estimate = engine_limits.check("spx", spec, ir)
            if reason is not None:
                self.out_of_domain[step["step_id"]] = (reason, estimate)
                self.blocked_after = step["step_id"]
                self.blocked_kind = "out_of_domain"
                continue
            try:
                self._apply(step)
            except NotImplementedError as exc:
                self.unsupported[step["step_id"]] = str(exc)
                self.blocked_after = step["step_id"]
                self.blocked_kind = "unsupported"
        return (time.perf_counter() - started) * 1000.0

    def _apply(self, step: dict[str, Any]) -> None:
        sp = self.sp
        kind, mechanism = step["kind"], step["mechanism"]
        rates = {k: float(v) for k, v in (step.get("rates") or {}).items() if float(v) != 0.0}
        if mechanism in ("isotropic", "anisotropic"):
            if not rates:
                sp.control_pause(Location=self._loc(step["step_id"]))
                return
            dependent = []
            for material, rate in rates.items():
                spx_name = self._ensure_material(material)
                dependent.append((_material_filter(spx_name), _rate(abs(rate))))
            split = min(max(float(step.get("anisotropy", 1.0)), 0.0), 1.0)
            if kind == "etch":
                if mechanism == "isotropic":
                    sp.etch_isotropic(Location=self._loc(step["step_id"]),
                                      MaterialDependent=dependent,
                                      Time=_minutes(step["duration"]))
                else:
                    angle = min(max(90.0 * split, 1.0), 90.0)
                    sp.etch_anisotropic(Location=self._loc(step["step_id"]),
                                        MaterialDependent=dependent,
                                        Angle=(float(angle), qupy_unit("deg")),
                                        Time=_minutes(step["duration"]))
            elif kind == "deposit":
                step_ids = [step["step_id"]]
                for material, rate in rates.items():
                    if rate <= 0:
                        continue
                    spx_name = self._ensure_material(material)
                    step_id = step_ids.pop(0) if step_ids else f"{step['step_id']}_{spx_name.lower()}"
                    if mechanism == "isotropic":
                        sp.depo_isotropic(Location=self._loc(step_id),
                                          MaterialFilter=_material_filter(spx_name),
                                          Rate=_rate(rate),
                                          Time=_minutes(step["duration"]))
                    else:
                        sp.depo_anisotropic(Location=self._loc(step_id),
                                            MaterialFilter=_material_filter(spx_name),
                                            Rate=_rate(rate),
                                            Time=_minutes(step["duration"]))
            else:
                raise NotImplementedError(f"{kind}/{mechanism} is not a rate step")
        elif mechanism == "strip":
            materials = [str(name) for name in (step.get("parameters") or {}).get("materials") or []]
            if not materials:
                raise NotImplementedError("strip step lists no materials")
            sp.etch_strip(Location=self._loc(step["step_id"]),
                          MaterialFilter=[_material_filter(self._ensure_material(name))[0]
                                          for name in materials])
        elif mechanism == "fill":
            params = step.get("parameters") or {}
            material = self._ensure_material(str(params["material"]))
            sp.depo_fill(Location=self._loc(step["step_id"]),
                         MaterialFilter=_material_filter(material),
                         Offset=_um(float(params.get("offset", 0.0))),
                         ReferenceMaterialsFilter=[[]])
        elif mechanism == "ellipsoidal":
            params = step.get("parameters") or {}
            dependent = []
            for name, pair in (params.get("rates") or {}).items():
                vertical, lateral = pair
                dependent.append((_material_filter(self._ensure_material(str(name))),
                                  _rate(abs(float(vertical))),
                                  _rate(abs(float(lateral)))))
            if not dependent:
                raise NotImplementedError("ellipsoidal step lists no rates")
            sp.etch_ellipsoidal(Location=self._loc(step["step_id"]),
                                MaterialDependent=dependent,
                                Time=_minutes(step["duration"]))
        elif mechanism == "litho":
            params = step.get("parameters") or {}
            layer_name = str(params.get("layer_name") or params.get("layer"))
            sp.litho_patterning(Location=self._loc(step["step_id"]), LayerNames=[layer_name])
        elif mechanism == "cmp":
            params = step.get("parameters") or {}
            stop = params.get("stop_material")
            if not stop:
                raise NotImplementedError(
                    "SPX etch_cmp is only mapped for a stop material; an absolute "
                    "cutoff has no measured equivalent")
            sp.etch_cmp(Location=self._loc(step["step_id"]),
                        EtchstopMaterialsFilter=_material_filter(self._ensure_material(stop)),
                        MaterialFilter=[[]],
                        Offset=_um(float(params.get("offset", 0.0))))
        else:
            raise NotImplementedError(f"mechanism {mechanism!r} has no SPX lowering")

    def run(self) -> dict[str, Any]:
        start = time.perf_counter()
        self.sp.run(Route=self.route, Start={}, Stop={})
        elapsed = (time.perf_counter() - start) * 1000.0
        return {"solveMode": "batch_route", "solveMs": elapsed, "runMs": elapsed}

    def _bbox_bottom_and_unit(self, location: dict[str, str]) -> tuple[float, Any]:
        coords, unit = self.sp.clipstate(Location=location, Query="bounding_box")
        z_bottom = _to_um_scalar(coords[2], unit)
        return z_bottom, unit

    def _single_probe(self, location: dict[str, str], x: float, y: float, z: float) -> str:
        try:
            out = self.sp.measure(
                Type="meas_probe",
                Height=_um(z),
                Location=location,
                PointName=["", "", [float(x) + self.origin[0], float(y) + self.origin[1]]],
                GroupBy="region",
            )
            raw = out.get("Output")
            name = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else raw
            return canonical(name, "spx")
        except Exception:
            return VOID

    def _column_intervals(
        self,
        location: dict[str, str],
        x: float,
        y: float,
        z_bottom_um: float,
        bbox_unit: Any,
        cache: dict[tuple[float, float], list[tuple[float, float, str]] | None],
    ) -> list[tuple[float, float, str]] | None:
        key = (round(float(x), 9), round(float(y), 9))
        if key in cache:
            return cache[key]
        try:
            out = self.sp.measure(
                Type="meas_thickness",
                Location=location,
                PointName=["", "", [float(x) + self.origin[0], float(y) + self.origin[1]]],
            )
            raw_list = out.get("Output")
            if raw_list is None:
                cache[key] = None
                return None
            intervals = intervals_from_thickness_output(raw_list, z_bottom_um, bbox_unit)
            cache[key] = intervals
            return intervals
        except Exception:
            cache[key] = None
            return None

    def probe(
        self,
        location: dict[str, str],
        points: list[list[float]],
        *,
        col_cache: dict[tuple[float, float], list[tuple[float, float, str]] | None] | None = None,
        bbox_info: tuple[float, Any] | None = None,
    ) -> list[str]:
        """Answer all ``points`` via cached ``meas_thickness`` 1D column profiles."""
        if not points:
            return []
        cache = col_cache if col_cache is not None else {}
        z_bottom_um, bbox_unit = bbox_info or self._bbox_bottom_and_unit(location)
        labels: list[str] = []
        for x, y, z in points:
            intervals = self._column_intervals(location, x, y, z_bottom_um, bbox_unit, cache)
            if intervals is not None:
                labels.append(label_at_z(intervals, z))
            else:
                labels.append(self._single_probe(location, x, y, z))
        return labels

    def section(
        self,
        location: dict[str, str],
        spec: dict[str, Any],
        *,
        col_cache: dict[tuple[float, float], list[tuple[float, float, str]] | None] | None = None,
        bbox_info: tuple[float, Any] | None = None,
    ) -> dict[str, list[float | None]]:
        """Exact interface heights per column via ``meas_thickness`` (1 query/column)."""
        materials = [str(name) for name in spec["materials"]]
        z_min, z_max = float(spec["z_min"]), float(spec["z_max"])
        cache = col_cache if col_cache is not None else {}
        z_bottom_um, bbox_unit = bbox_info or self._bbox_bottom_and_unit(location)
        heights: dict[str, list[float | None]] = {name: [] for name in materials}
        for x, y in spec["columns"]:
            intervals = self._column_intervals(
                location, float(x), float(y), z_bottom_um, bbox_unit, cache
            )
            for material in materials:
                if intervals is not None:
                    heights[material].append(
                        top_of_material_in_intervals(intervals, material, z_min, z_max)
                    )
                else:
                    heights[material].append(None)
        return heights

    def _camera_args(self, camera: dict[str, Any]) -> dict[str, Any]:
        render = self.job.get("render") or {}
        view = str(render.get("view") or "iso")
        if view not in SPX_VIEWS:
            view = "iso"
        try:
            orbit = OrbitCamera(float(camera.get("azimuth")),
                                float(camera.get("elevation")),
                                float(camera.get("zoom") or 1.0))
        except (TypeError, ValueError):
            return {"View": view}
        domain = self.job["domain"]
        initial = self.job["initial"]
        z_low = -float(initial["substrate_thickness"])
        z_high = sum(float(film["thickness"]) for film in initial.get("films", [])) or 0.0
        centre = (self.origin[0] + float(domain["width"]) / 2.0,
                  self.origin[1] + float(domain["depth"]) / 2.0,
                  (z_low + z_high) / 2.0)
        extent = max(float(domain["width"]), float(domain["depth"]), z_high - z_low)
        free = orbit_to_free(centre, extent, orbit)
        if free is None:
            return {"View": view}
        return {
            "View": "free",
            "CameraFocalPosition": ([float(v) for v in free.focal], qupy_unit("um")),
            "CameraPosition": ([float(v) for v in free.position], qupy_unit("um")),
            "OrthographicProjection": True,
        }

    def frame(self, step_id: str, location: dict[str, str]) -> dict[str, Any]:
        from io import BytesIO
        from PIL import Image

        render = self.job.get("render") or {}
        camera_args = self._camera_args(render.get("camera") or {})
        width = int(render.get("width", 900))
        height = int(render.get("height", 700))
        started = time.perf_counter()
        out = self.sp.measure(Location=location, Type="meas_screenshot",
                              ImageWidth=width, ImageHeight=height,
                              **camera_args)
        image = Image.frombytes("RGBA", (width, height), bytes(out["Output"]))
        frames = self.out / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        target = frames / f"{step_id}-spx.png"
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        target.write_bytes(buffer.getvalue())
        return {
            "frame": {
                "step_id": step_id, "kind": "raster",
                "path": str(target.relative_to(self.out)),
                "width": width, "height": height,
                "camera": render.get("camera") or {},
                "materials": self.materials,
                "bbox": [0.0, 0.0, 0.0, float(self.job["domain"]["width"]),
                         float(self.job["domain"]["depth"]),
                         float(self.job["domain"]["height"])],
            },
            "frameMs": (time.perf_counter() - started) * 1000.0,
        }


def run_job(job: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    from engines.protocol import JobSpec

    job_spec = JobSpec.from_json(job)
    session = SpxSession(job, out_dir)
    build_ms = session.build()
    run_stats = session.run()
    run_stats["buildMs"] = round(build_ms, 3)

    probe_points = list((job.get("probe") or {}).get("points") or [])
    section_spec = job.get("section")
    emit_frames = bool(job.get("emit_frames", True))
    outcomes = []
    print(json.dumps({"event": "run_started", **run_stats}), flush=True)
    for index, step in enumerate(job_spec.steps):
        started = time.perf_counter()
        if step.step_id in session.out_of_domain:
            reason, estimate = session.out_of_domain[step.step_id]
            outcomes.append({
                "step_id": step.step_id, "status": "out_of_domain",
                "wall_ms": 0.0, "solve_ms": None, "frame_ms": 0.0,
                "probe_ms": 0.0, "section_ms": 0.0,
                "frame": None, "checkpoint": None,
                "metrics": {"estimate": estimate}, "reason": reason,
                "probe": None, "section": None,
            })
            print(json.dumps({"event": "step_out_of_domain", "stepId": step.step_id}),
                  flush=True)
            continue
        if step.step_id in session.unsupported:
            outcomes.append({
                "step_id": step.step_id, "status": "unsupported",
                "wall_ms": 0.0, "solve_ms": None, "frame_ms": 0.0,
                "probe_ms": 0.0, "section_ms": 0.0,
                "frame": None, "checkpoint": None,
                "metrics": {}, "reason": session.unsupported[step.step_id],
                "probe": None, "section": None,
            })
            print(json.dumps({"event": "step_unsupported", "stepId": step.step_id}),
                  flush=True)
            continue
        try:
            location = session.step_location(step.step_id, initial=index == 0)
            frame_obj = None
            frame_ms = 0.0
            if emit_frames:
                payload = session.frame(step.step_id, location)
                frame_obj = payload["frame"]
                frame_ms = payload["frameMs"]

            col_cache: dict[tuple[float, float], list[tuple[float, float, str]] | None] = {}
            bbox_info = (
                session._bbox_bottom_and_unit(location)
                if (probe_points or section_spec)
                else None
            )

            probe_started = time.perf_counter()
            labels = (
                session.probe(location, probe_points, col_cache=col_cache, bbox_info=bbox_info)
                if probe_points
                else None
            )
            probe_ms = (time.perf_counter() - probe_started) * 1000.0 if probe_points else 0.0

            section_started = time.perf_counter()
            section_data = (
                session.section(location, section_spec, col_cache=col_cache, bbox_info=bbox_info)
                if section_spec
                else None
            )
            section_ms = (time.perf_counter() - section_started) * 1000.0 if section_spec else 0.0

            outcomes.append({
                "step_id": step.step_id,
                "status": "ok",
                "wall_ms": frame_ms,
                "solve_ms": None,
                "frame_ms": frame_ms,
                "probe_ms": probe_ms,
                "section_ms": section_ms,
                "frame": frame_obj,
                "checkpoint": None,
                "metrics": {
                    "solveMode": "batch_route",
                    "probeColumns": len(col_cache),
                    "frameMs": round(frame_ms, 4),
                    "probeMs": round(probe_ms, 4),
                    "sectionMs": round(section_ms, 4),
                    "probePoints": len(probe_points),
                },
                "reason": None,
                "probe": labels,
                "section": section_data,
            })
            print(json.dumps({"event": "step_done", "stepId": step.step_id}), flush=True)
        except Exception as exc:
            outcomes.append({
                "step_id": step.step_id, "status": "failed",
                "wall_ms": (time.perf_counter() - started) * 1000.0,
                "solve_ms": None, "frame_ms": 0.0, "probe_ms": 0.0, "section_ms": 0.0,
                "frame": None, "checkpoint": None, "metrics": {},
                "reason": f"{type(exc).__name__}: {exc}",
            })
            print(json.dumps({"event": "step_failed", "stepId": step.step_id,
                              "reason": str(exc)}), flush=True)
    return {"engine": engine_info(), "outcomes": outcomes, "run": run_stats}


def engine_info() -> dict[str, Any]:
    import spx
    return {
        "engine_id": "spx",
        "version": str(spx.release_name() if callable(spx.release_name) else spx.release_name),
        "dimensions": (3,),
        "mechanisms": (
            "initial", "isotropic", "anisotropic", "cmp",
            "strip", "fill", "ellipsoidal", "litho",
        ),
        "frame_kinds": ("raster",),
        "adapter": "engines.spx.bridge",
        "solve_mode": "batch_route",
        "probe_method": "meas_thickness",
        "limits": {"session_licence": "spx_all", "python": sys.version.split()[0]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", nargs="?", type=Path)
    parser.add_argument("out_dir", nargs="?", type=Path)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    if args.describe:
        print(json.dumps(engine_info(), indent=2))
        return 0
    if args.job is None or args.out_dir is None:
        parser.error("job and out_dir are required unless --describe is passed")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    job = json.loads(args.job.read_text(encoding="utf-8"))
    try:
        result = run_job(job, args.out_dir)
    except Exception as exc:
        traceback.print_exc()
        result = {"engine": engine_info(), "outcomes": [], "error": f"{type(exc).__name__}: {exc}"}
    (args.out_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
