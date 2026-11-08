"""Segment profiler for one recorded run input (read-only; writes no run artifacts).

The workbench worker only records one aggregate ``process.apply()`` duration.
This harness replays the *exact* saved input of a run and times the pieces the
worker cannot separate:

  context      CUDA/OptiX device context creation (``ps.gpu.Context``)
  geometry     ViennaPS domain + initial 3D geometry construction
  snapshot     surface-mesh extraction, snapshot JSON serialisation, VTK write
  flux         one ``Process.calculateFlux()`` pass (engine init + JIT +
               acceleration-structure build + ray tracing)
  apply        full ``Process.apply()`` (N time steps of flux + advection)

Ray cost and the ray-independent part of a flux pass are separated by sweeping
``raysPerPoint``: ``T(rays) = fixed + rays * slope``.

Usage (one cold process per measurement):

  python worker/profile_segments.py var/runs/<id>/input.json --mode context --report out.json
  python worker/profile_segments.py var/runs/<id>/input.json --mode flux --rays 256 --report out.json
  python worker/profile_segments.py var/runs/<id>/input.json --mode apply --threads 4 --report out.json

Diagnostics go to stdout, the machine-readable result is written to --report.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

import worker.simulate as sim  # noqa: E402


def build_domain(ps, config, project, scratch: Path):
    """Recreate the worker's initial Domain (see simulate.execute)."""
    timings = {}
    started = time.perf_counter()
    from mesh3d import make_domain
    domain = make_domain(ps, project)
    timings["domainConstruction"] = time.perf_counter() - started
    started = time.perf_counter()
    sim.make_v3_geometry(ps, domain, project, config=config, run_dir=scratch)
    timings["initialGeometry"] = time.perf_counter() - started
    return domain, timings


def make_process(ps, domain, project, step, rays: int | None):
    """Mirror the worker's SingleParticleProcess + Process construction."""
    deposit_material = sim.material_handle(ps, step["depositMaterial"])
    domain.duplicateTopLevelSet(deposit_material)
    rate, _ = sim.signed_rates(step)
    model = ps.SingleParticleProcess(
        rate=rate,
        stickingProbability=float(step["stickingProbability"]),
        sourceExponent=float(step["sourceExponent"]),
        maskMaterials=[sim.material_handle(ps, name) for name in sim.protected_material_names(project, step)],
    )
    started = time.perf_counter()
    process = ps.Process(domain, model, float(step["duration"]))
    construction = time.perf_counter() - started
    process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
    # Keep any native intermediate output inside the throwaway scratch dir.
    process.setIntermediateOutputPath(str(Path(tempfile.gettempdir()) / "segment-profile-intermediate"))
    advection, _ = sim.configure_advection(ps, project)
    process.setParameters(advection)
    ray_parameters, _ = sim.configure_rays(ps, project, int(config_seed(project)))
    if rays is not None:
        ray_parameters.raysPerPoint = int(rays)
    process.setParameters(ray_parameters)
    return process, construction


def config_seed(project):
    random = project["solver"]["random"]
    return int(random.get("seed") or 0)


def run_apply(process) -> float:
    started = time.perf_counter()
    process.apply()
    return time.perf_counter() - started


def import_ps():
    import viennaps as ps
    return ps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="recorded run input.json")
    parser.add_argument("--mode", choices=["context", "geometry", "snapshot", "flux", "apply", "reuse"], default="apply")
    parser.add_argument("--threads", type=int, default=None, help="override solver.cpuThreads")
    parser.add_argument("--rays", type=int, default=None, help="override solver.raysPerPoint")
    parser.add_argument("--pre-context", action="store_true",
                        help="create the CUDA/OptiX context before the timed section")
    parser.add_argument("--repeat", type=int, default=1, help="repeat the timed section (warm repeats)")
    parser.add_argument("--timing-log", action="store_true",
                        help="raise the ViennaPS log level to TIMING (adds native flux/advection split)")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    config = json.loads(args.input.read_text(encoding="utf-8"))
    project = config["project"]
    step = next(entry for entry in project["steps"] if entry["enabled"])
    threads = int(args.threads if args.threads is not None else project["solver"]["cpuThreads"])

    result = {"mode": args.mode, "threads": threads, "rays": args.rays,
              "preContext": bool(args.pre_context), "timings": {}, "notes": []}

    ps = import_ps()
    if args.timing_log:
        ps.Logger.setLogLevel(ps.LogLevel.TIMING)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(3)
    ps.setNumThreads(threads)
    sim.configure_native_materials(ps, config.get("customMaterials", []))
    from mesh3d import configure_material_catalog
    configure_material_catalog(sim.MATERIAL_ROWS)

    scratch = Path(tempfile.mkdtemp(prefix="segment-profile-"))
    result["scratch"] = str(scratch)

    if args.mode == "context":
        repeats = []
        for _ in range(max(args.repeat, 1)):
            started = time.perf_counter()
            # registerInGlobal=False keeps each repeat independent: the Python
            # reference is the only owner, so dropping it frees CUDA/OptiX.
            context = ps.gpu.Context.createContext(registerInGlobal=False)
            repeats.append(time.perf_counter() - started)
            del context
            gc.collect()
        result["timings"]["contextCreate"] = repeats
        result["registeredDevices"] = [int(value) for value in ps.gpu.Context.getRegisteredDeviceIDs()]
        return emit(result, args.report)

    domain, build_timings = build_domain(ps, config, project, scratch)
    result["timings"].update(build_timings)

    if args.mode == "geometry":
        return emit(result, args.report)

    if args.mode == "snapshot":
        from mesh3d import mesh_report as mesh_report_3d
        started = time.perf_counter()
        mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        result["timings"]["surfaceMeshExtraction"] = time.perf_counter() - started
        started = time.perf_counter()
        report = mesh_report_3d(mesh)
        result["timings"]["meshReportPythonConversion"] = time.perf_counter() - started
        payload = {"points": report["points"], "triangles": report["triangles"],
                   "cellData": report["cellData"], "mesh": {}}
        started = time.perf_counter()
        encoded = json.dumps(payload, allow_nan=False)
        result["timings"]["jsonDumps"] = time.perf_counter() - started
        result["jsonBytes"] = len(encoded)
        target = scratch / "snapshot.json"
        started = time.perf_counter()
        sim.atomic_json(target, payload)
        result["timings"]["jsonWrite"] = time.perf_counter() - started
        target_vtk = scratch / "surface.vtk"
        started = time.perf_counter()
        domain.saveSurfaceMesh(str(target_vtk), addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        result["timings"]["surfaceVtkWriteSecondExtraction"] = time.perf_counter() - started
        result["vtkBytes"] = target_vtk.stat().st_size
        result["points"] = len(report["points"])
        result["triangles"] = len(report["triangles"])
        return emit(result, args.report)

    if args.pre_context:
        started = time.perf_counter()
        context = ps.gpu.Context.createContext()
        result["timings"]["contextCreateBeforeTimedSection"] = time.perf_counter() - started
        del context
        gc.collect()

    process, process_construction = make_process(ps, domain, project, step, args.rays)
    result["timings"]["processConstruction"] = process_construction

    repeats = max(args.repeat, 1)
    if args.mode == "reuse":
        # Reproduce two consecutive GPU steps in one process: step 1 apply,
        # then a second Process on the *changed* Domain WITHOUT tearing the
        # OptiX context down (worker/simulate.py currently always resets).
        result["timings"]["step1Apply"] = [run_apply(process)]
        second = None
        try:
            second, construction = make_process(ps, domain, project, step, args.rays)
            result["timings"]["step2ProcessConstruction"] = construction
            result["timings"]["step2Apply"] = [run_apply(second)]
            result["status"] = "second-apply-succeeded-without-context-reset"
        except BaseException as exc:  # noqa: BLE001 - the failure mode is the result
            result["status"] = "second-apply-failed-without-context-reset"
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["levelSets"] = int(domain.getNumberOfLevelSets())
        result["registeredDevices"] = [int(value) for value in ps.gpu.Context.getRegisteredDeviceIDs()]
        return emit(result, args.report)

    if args.mode == "flux":
        samples = []
        for index in range(repeats):
            started = time.perf_counter()
            mesh = process.calculateFlux()
            samples.append(time.perf_counter() - started)
            result.setdefault("fluxPointCounts", []).append(len(mesh.getNodes()) if mesh is not None else None)
        result["timings"]["calculateFlux"] = samples
    else:
        samples = []
        for index in range(repeats):
            started = time.perf_counter()
            process.apply()
            samples.append(time.perf_counter() - started)
        result["timings"]["processApply"] = samples
        set_count = int(domain.getNumberOfLevelSets())
        result["levelSets"] = set_count
    return emit(result, args.report)


def emit(result: dict, report: Path | None) -> int:
    text = json.dumps(result, indent=2, sort_keys=True)
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")
    print("SEGMENT_PROFILE " + json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
