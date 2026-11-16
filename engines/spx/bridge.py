"""SPX (Sentaurus Process Explorer) adapter.

This module runs inside the **SPX-private Python 3.11** interpreter
(``$SPX/linux64/bin/python3``), because ``spx`` links its own libpython and its own
shared objects. It never imports the workbench, the backend or viennaps.

Entry point::

    $SPX/linux64/bin/python3 engines/spx/bridge.py job.json out_dir

Contract: reads :class:`engines.protocol.JobSpec`, writes ``result.json``
(:class:`engines.protocol.EngineInfo` + one ``StepOutcome`` per step) and one PNG
per step under ``out_dir/frames/``.

Two measured facts shape this adapter (see docs/engine-abstraction.md):

* ``spxwebapi`` alone never initialises a session licence, so every WebSocket
  session fails with ``License check failed``. ``init_session_licenses`` before
  ``import spx.procedural`` is mandatory.
* a 88-step example route runs in ~3.3 s headless and one ``meas_screenshot``
  takes ~0.5 s, so the whole flow is executed once and frames are pulled from the
  cached per-step clip states afterwards.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# The adapter is launched by path with the SPX interpreter, so the repository
# root is not on sys.path. Add it explicitly to import ``engines.protocol``.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engines import limits as engine_limits  # noqa: E402
from engines.camera import OrbitCamera, orbit_to_free  # noqa: E402
from engines.normalize import canonical  # noqa: E402

# Material names used by the SPX step templates differ from the workbench
# catalogue. The table below is the single place that maps them; it is mirrored
# in engines/normalize.json so the ViennaPS side and the comparability report use
# the same names.
SPX_MATERIAL = {
    "Si": "Silicon",
    "SiO2": "Oxide",
    "Si3N4": "Nitride",
    "PolySi": "PolySilicon",
    "SiGe": "SiGe",
    "Al2O3": "AlO",
    "HfO2": "HfO2",
    "TiN": "TiN",
    "W": "Tungsten",
    "Cu": "Copper",
    "PMMA": "Photoresist",
    "Mask": "Photoresist",
    "SiN": "SiN",
    "Metal": "TiN",
    "Dielectric": "Oxide",
}
DEFAULT_LAYOUT = "/root/SPX/spx/V-2024.03/linux64/examples/Logic1_Layout.oasis"
# Measured: the Logic1 layout only covers [0.035, 0, 0.175, 0.288] um, and a clip
# outside that extent makes spx.run() fail with an empty message. Phase A reuses
# this window; a parametric layout generator arrives with the GDS mask step.
DEFAULT_WINDOW = (0.035, 0.0, 0.175, 0.288)
# Measured: meas_screenshot validates View against this enum; anything else is an
# error, so the adapter maps unknown values to the isometric default.
SPX_VIEWS = {"free", "iso", "iso90", "iso180", "iso270", "xy", "xz", "yx", "yz", "zx", "zy"}
SPX_COLOR = {
    "Silicon": "#78909C", "Oxide": "#00e5ff", "Nitride": "#dce775",
    "PolySilicon": "#ff3d00", "Silicon": "#78909C", "AlO": "#72abab",
    "HfO2": "#ff1744", "TiN": "#9e9e9e", "Tungsten": "#607d8b",
    "Copper": "#b87333", "Photoresist": "#fff59d", "SiN": "#55aaff",
    "SiGe": "#00bfa5",
}


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


def _material_filter(name: str):
    return [[("Material", "equals", name)]]


class SpxSession:
    def __init__(self, job: dict[str, Any], out_dir: Path) -> None:
        import spx.api as spx_api

        spx_api.init_session_licenses(spx_api.LicenseFeature.all, 4)
        import spx.procedural as sp  # after the session licences, never before
        self.sp = sp
        self.job = job
        self.out = out_dir
        self.route = f"cmp_{job['case_id']}"
        self.flow = "F"
        self.module = "M0"
        self.materials: list[dict[str, Any]] = []
        self.origin = (0.0, 0.0)
        # The job's first step is the initial structure, but SPX needs one step
        # per deposited film. Frames/probes for that job step must use the last
        # of those, otherwise the oxide is missing from the "initial" picture.
        self.initial_location: str | None = None
        # step_id -> reason for steps this adapter cannot lower. Building stops at
        # the first one: later frames would silently describe a different structure.
        self.unsupported: dict[str, str] = {}
        self.out_of_domain: dict[str, tuple[str, dict[str, Any]]] = {}
        self.blocked_after: str | None = None
        self.blocked_kind = "unsupported"

    # -- materials ---------------------------------------------------------
    def _ensure_material(self, name: str) -> str:
        spx_name = SPX_MATERIAL.get(name, name)
        if any(m["spx"] == spx_name for m in self.materials):
            return spx_name
        color = SPX_COLOR.get(spx_name, "#888888")
        self.sp.material(Material=spx_name, Color=color)
        self.materials.append({"name": name, "spx": spx_name, "color": color})
        return spx_name

    def step_location(self, step_id: str, initial: bool) -> dict[str, str]:
        return self._loc(self.initial_location if initial and self.initial_location else step_id)

    def _loc(self, step_id: str) -> dict[str, str]:
        return {"Route": self.route, "Flow": self.flow, "Module": self.module, "Step": step_id}

    # -- build -------------------------------------------------------------
    def build(self) -> None:
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
        # A route must own a clip, and a clip must reference a layout. Phase A
        # uses the shipped empty layout window; parametric masks arrive with the
        # GDS mask step.
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

        substrate = self._ensure_material(initial["substrate"])
        first = job["steps"][0]
        # ColumnSizeX/Y is the periodic unit cell, not the drawn window: SPX
        # derives the structure extent from the clip (measured with
        # spxwebapi.utils._get_bounding_box). Keep it at the example's 1 nm cell.
        self.initial_location = first["step_id"]
        sp.substrate(Location=self._loc(first["step_id"]),
                     ColumnSizeX=_um(0.001),
                     ColumnSizeY=_um(0.001),
                     Thickness=_um(initial["substrate_thickness"]))
        # substrate step material is the substrate: rename via filter is not
        # needed, SPX defaults the substrate to Silicon when it is registered.
        for index, film in enumerate(initial.get("films", [])):
            spx_name = self._ensure_material(film["material"])
            step_id = f"{first['step_id']}_film{index}"
            self.initial_location = step_id
            sp.depo_isotropic(Location=self._loc(step_id),
                              MaterialFilter=_material_filter(spx_name),
                              Rate=_rate(1.0),
                              Time=_minutes(float(film["thickness"])))
        from engines.protocol import JobSpec, StepIR

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
                    # SPX takes a sidewall angle: 90 deg is vertical (anisotropy 1),
                    # smaller angles widen the profile. The mapping is geometric,
                    # not physical -- comparability is 'approximate'.
                    angle = min(max(90.0 * split, 1.0), 90.0)
                    sp.etch_anisotropic(Location=self._loc(step["step_id"]),
                                        MaterialDependent=dependent,
                                        Angle=(float(angle), qupy_unit("deg")),
                                        Time=_minutes(step["duration"]))
            elif kind == "deposit":
                # SPX depo takes one material filter per call, so a multi-rate
                # deposit becomes one call per material (first one wins the
                # surface). comparability is 'approximate' for that reason.
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
                # Measured: each entry is (filter, vertical_rate, lateral_rate) as three
                # separate positional values, not a nested pair.
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
            # Measured (docs/engine-abstraction.md §5.3): SPX ``etch_cmp`` does not
            # cut at an absolute height. With stop=Oxide and Offset=0 it removed a
            # whole 0.1 um oxide layer, so the stop is not "the exposed top" and a
            # negative offset appeared to add material. Until that model is mapped
            # correctly, CMP stays a ViennaPS-only step instead of a silent
            # mismatch.
            raise NotImplementedError(
                "SPX etch_cmp offset semantics are not mapped to an absolute cutoff; "
                "run this step on viennaps or mark it no_counterpart")
        else:
            raise NotImplementedError(f"mechanism {mechanism!r} has no SPX lowering")

    # -- run + frames ------------------------------------------------------
    def run(self) -> dict[str, Any]:
        start = time.perf_counter()
        self.sp.run(Route=self.route, Start={}, Stop={})
        return {"runMs": (time.perf_counter() - start) * 1000.0}

    def probe(self, location: dict[str, str], points: list[list[float]]) -> list[str]:
        """Ask SPX which material occupies each point (``meas_probe``, ~1.6 ms)."""
        labels: list[str] = []
        for x, y, z in points:
            try:
                out = self.sp.measure(Type="meas_probe", Height=_um(z), Location=location,
                                      PointName=["", "", [float(x) + self.origin[0],
                                                          float(y) + self.origin[1]]],
                                      GroupBy="region")
                raw = out.get("Output")
                name = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else raw
                labels.append(canonical(name, "spx"))
            except Exception:  # a point outside the structure is void, not a crash
                labels.append("void")
        return labels

    def _camera_args(self, camera: dict[str, Any]) -> dict[str, Any]:
        """Map an orbit camera onto SPX's free camera, or fall back to a preset."""
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

    def section(self, location: dict[str, str], spec: dict[str, Any]) -> dict[str, list[float | None]]:
        """Interface height per material per column, by binary search on probes.

        ``meas_probe`` returns a material label; for a z-monotone stack the label
        is monotone in z for a fixed column, so ~12 probes localise each interface
        to z_range / 2**12. Probes cost ~1.3 ms, so a 33-column, 3-material section
        is ~1.3 s -- far cheaper than anything that needs the HDF5 export.
        """
        materials = [str(name) for name in spec["materials"]]
        z_min, z_max = float(spec["z_min"]), float(spec["z_max"])
        heights: dict[str, list[float | None]] = {name: [] for name in materials}
        for x, y in spec["columns"]:
            for material in materials:
                heights[material].append(
                    self._top_of(location, float(x), float(y), material, z_min, z_max))
        return heights

    def _top_of(self, location: dict[str, str], x: float, y: float, material: str,
                z_min: float, z_max: float) -> float | None:
        """Highest z in (z_min, z_max] whose label is ``material``.

        A coarse ladder finds a bracket first: probing exactly on the bottom face
        returns nothing (measured), so the search starts one epsilon inside and
        walks up until the material appears, then bisects against the next sample.
        """
        spx_material = SPX_MATERIAL.get(material, material)
        span = z_max - z_min
        low_z = z_min + span * 1e-6
        ladder = [low_z + span * (k + 1) / 25.0 for k in range(25)]
        bracket_low = None
        bracket_high = None
        for z in ladder:
            if self._probe_is(location, x, y, z, spx_material):
                bracket_low = z
            elif bracket_low is not None:
                bracket_high = z
                break
        if bracket_low is None:
            return None
        if bracket_high is None:
            return round(z_max, 6)
        low, high = bracket_low, bracket_high
        for _ in range(12):
            middle = (low + high) / 2.0
            if self._probe_is(location, x, y, middle, spx_material):
                low = middle
            else:
                high = middle
        return round(low, 6)

    def _probe_is(self, location: dict[str, str], x: float, y: float, z: float,
                  spx_material: str) -> bool:
        try:
            out = self.sp.measure(Type="meas_probe", Height=_um(z), Location=location,
                                  PointName=["", "", [x + self.origin[0], y + self.origin[1]]],
                                  GroupBy="region")
            raw = out.get("Output")
            name = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else raw
            return str(name) == spx_material
        except Exception:  # outside the structure is simply "not this material"
            return False

    def frame(self, step_id: str, location: dict[str, str]) -> dict[str, Any]:
        from io import BytesIO

        from PIL import Image

        render = self.job.get("render") or {}
        view = str(render.get("view") or "iso")
        if view not in SPX_VIEWS:
            view = "iso"
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
    session.build()
    run_stats = session.run()

    probe_points = list((job.get("probe") or {}).get("points") or [])
    section_spec = job.get("section")
    outcomes = []
    print(json.dumps({"event": "run_started", **run_stats}), flush=True)
    for index, step in enumerate(job_spec.steps):
        started = time.perf_counter()
        if step.step_id in session.out_of_domain:
            reason, estimate = session.out_of_domain[step.step_id]
            outcomes.append({
                "step_id": step.step_id, "status": "out_of_domain",
                "wall_ms": 0.0, "frame": None, "checkpoint": None,
                "metrics": {"estimate": estimate}, "reason": reason,
                "probe": None, "section": None,
            })
            print(json.dumps({"event": "step_out_of_domain", "stepId": step.step_id}),
                  flush=True)
            continue
        if step.step_id in session.unsupported:
            outcomes.append({
                "step_id": step.step_id, "status": "unsupported",
                "wall_ms": 0.0, "frame": None, "checkpoint": None,
                "metrics": {}, "reason": session.unsupported[step.step_id],
                "probe": None, "section": None,
            })
            print(json.dumps({"event": "step_unsupported", "stepId": step.step_id}),
                  flush=True)
            continue
        try:
            location = session.step_location(step.step_id, initial=index == 0)
            payload = session.frame(step.step_id, location)
            probe_started = time.perf_counter()
            labels = session.probe(location, probe_points) if probe_points else None
            section_started = time.perf_counter()
            section = session.section(location, section_spec) if section_spec else None
            outcomes.append({
                "step_id": step.step_id, "status": "ok",
                "wall_ms": payload["frameMs"],
                "frame": payload["frame"],
                "checkpoint": None,
                "metrics": {"frameMs": payload["frameMs"],
                            "probeMs": (time.perf_counter() - probe_started) * 1000.0,
                            "probePoints": len(probe_points)},
                "reason": None,
                "probe": labels,
                "section": section,
                "sectionMs": (time.perf_counter() - section_started) * 1000.0
                             if section_spec else 0.0,
            })
            print(json.dumps({"event": "step_done", "stepId": step.step_id}), flush=True)
        except Exception as exc:  # a missing clip state must not kill the run
            outcomes.append({
                "step_id": step.step_id, "status": "failed",
                "wall_ms": (time.perf_counter() - started) * 1000.0,
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
        "mechanisms": ("initial", "isotropic", "anisotropic", "cmp"),
        "frame_kinds": ("raster",),
        "adapter": "engines.spx.bridge",
        "limits": {"session_licence": "spx_all", "python": sys.version.split()[0]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
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
