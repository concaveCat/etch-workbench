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
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# The adapter is launched by path with the SPX interpreter, so the repository
# root is not on sys.path. Add it explicitly to import ``engines.protocol``.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
SPX_COLOR = {
    "Silicon": "#78909C", "Oxide": "#00e5ff", "Nitride": "#dce775",
    "PolySilicon": "#ff3d00", "Silicon": "#78909C", "AlO": "#72abab",
    "HfO2": "#ff1744", "TiN": "#9e9e9e", "Tungsten": "#607d8b",
    "Copper": "#b87333", "Photoresist": "#fff59d", "SiN": "#55aaff",
    "SiGe": "#00bfa5",
}


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
        for step in job["steps"][1:]:
            self._apply(step)

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
            if kind == "etch":
                if mechanism == "isotropic":
                    sp.etch_isotropic(Location=self._loc(step["step_id"]),
                                      MaterialDependent=dependent,
                                      Time=_minutes(step["duration"]))
                else:
                    sp.etch_anisotropic(Location=self._loc(step["step_id"]),
                                        MaterialDependent=dependent,
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
        elif mechanism == "cmp":
            params = step.get("parameters") or {}
            stop = params.get("stop_material")
            sp.etch_cmp(Location=self._loc(step["step_id"]),
                        EtchstopMaterialsFilter=_material_filter(
                            self._ensure_material(stop)) if stop else [[]],
                        MaterialFilter=[[]],
                        Offset=_um(float(params.get("offset", 0.0))))
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
                from engines.normalize import canonical
                labels.append(canonical(name, "spx"))
            except Exception:  # a point outside the structure is void, not a crash
                labels.append("void")
        return labels

    def frame(self, step_id: str, location: dict[str, str]) -> dict[str, Any]:
        from io import BytesIO

        from PIL import Image

        render = self.job.get("render") or {}
        width = int(render.get("width", 900))
        height = int(render.get("height", 700))
        started = time.perf_counter()
        out = self.sp.measure(Location=location, Type="meas_screenshot",
                              ImageWidth=width, ImageHeight=height)
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
    outcomes = []
    print(json.dumps({"event": "run_started", **run_stats}), flush=True)
    for index, step in enumerate(job_spec.steps):
        started = time.perf_counter()
        try:
            location = session.step_location(step.step_id, initial=index == 0)
            payload = session.frame(step.step_id, location)
            probe_started = time.perf_counter()
            labels = session.probe(location, probe_points) if probe_points else None
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
