"""Command-line entry point for the multi-engine comparison & benchmark platform.

Examples::

    python -m engines.cli engines
    python -m engines.cli algorithms
    python -m engines.cli set-algorithm voxel mask/litho voxel.litho.gaussian_aerial
    python -m engines.cli run job.json --engines voxel,spx --algo mask/litho=voxel.litho.gaussian_aerial --out var/cmp
    python -m engines.cli run job.json --engines voxel,spx --benchmark --out var/bench
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines.algorithms import catalog, format_table, set_active_default  # noqa: E402
from engines.protocol import JobSpec  # noqa: E402
from engines.registry import describe  # noqa: E402
from engines.scheduler import compare  # noqa: E402


def _parse_algo_overrides(raw_items: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in raw_items or []:
        if "=" not in item:
            raise SystemExit(
                f"invalid --algo {item!r}: expected '<kind>/<mechanism>=<model_id>' "
                f"(e.g. 'mask/litho=voxel.litho.gaussian_aerial')"
            )
        slot, model_id = item.split("=", 1)
        slot, model_id = slot.strip(), model_id.strip()
        if not slot or not model_id:
            raise SystemExit(f"invalid --algo {item!r}: empty slot or model_id")
        overrides[slot] = model_id
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engines")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("engines", help="list all discovered engines and capabilities")

    algos = sub.add_parser(
        "algorithms", help="inspect concrete step algorithms, origins, and active defaults"
    )
    algos.add_argument("--engine", default=None, help="filter by engine id (e.g. voxel)")
    algos.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    set_algo = sub.add_parser(
        "set-algorithm",
        help="validate and persist the default algorithm for an engine slot in engines/algorithms.json",
    )
    set_algo.add_argument("engine", help="engine id (e.g. voxel or viennaps)")
    set_algo.add_argument("slot", help="step slot '<kind>/<mechanism>' (e.g. mask/litho)")
    set_algo.add_argument("model_id", help="registered algorithm model_id")

    run = sub.add_parser("run", help="run one job on one or more engines")
    run.add_argument("job", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument(
        "--engines",
        default="viennaps,spx",
        help="comma-separated engine ids (e.g. voxel,viennaps,spx)",
    )
    run.add_argument(
        "--reference",
        default=None,
        help="reference engine id for accuracy & speedup (default: spx if present)",
    )
    run.add_argument(
        "--algo",
        action="append",
        default=[],
        metavar="SLOT=MODEL_ID",
        help="override step algorithm for this run (repeatable, e.g. --algo mask/litho=voxel.litho.gaussian_aerial)",
    )
    run.add_argument("--timeout", type=float, default=1800.0)
    run.add_argument(
        "--probe-points",
        type=int,
        default=3000,
        help="shared probe lattice size (0 disables occupancy probing)",
    )
    run.add_argument(
        "--sequential",
        action="store_true",
        help="run engines sequentially to avoid CPU/GPU contention during speed benchmarks",
    )
    run.add_argument("--no-frames", action="store_true", help="skip mesh/PNG frame rendering")
    run.add_argument(
        "--no-checkpoints", action="store_true", help="skip per-step checkpoint writes"
    )
    run.add_argument(
        "--no-section",
        action="store_true",
        help="skip cross-section interface binary-search queries",
    )
    run.add_argument(
        "--benchmark",
        action="store_true",
        help="pure solver speed benchmark: implies --sequential --no-frames --no-checkpoints --no-section --probe-points 0",
    )

    args = parser.parse_args(argv)
    if args.command == "engines":
        print(json.dumps(describe(), indent=2))
        return 0

    if args.command == "algorithms":
        cat = catalog(engine_filter=args.engine)
        if args.json:
            print(json.dumps(cat, indent=2, ensure_ascii=False))
        else:
            print(format_table(cat))
        return 0

    if args.command == "set-algorithm":
        try:
            updated = set_active_default(args.engine, args.slot, args.model_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(updated, indent=2, ensure_ascii=False))
        return 0

    job = JobSpec.from_json(json.loads(args.job.read_text(encoding="utf-8")))
    cli_algos = _parse_algo_overrides(args.algo)
    if cli_algos:
        job = replace(job, algorithms={**job.algorithms, **cli_algos})

    engine_ids = [value.strip() for value in args.engines.split(",") if value.strip()]

    sequential = bool(args.sequential or args.benchmark)
    emit_frames = not (args.no_frames or args.benchmark)
    emit_checkpoints = not (args.no_checkpoints or args.benchmark)
    enable_section = not (args.no_section or args.benchmark)
    probe_points = 0 if args.benchmark else args.probe_points

    payload = compare(
        job,
        engine_ids,
        args.out,
        timeout=args.timeout,
        max_probe_points=probe_points,
        reference=args.reference,
        parallel=not sequential,
        emit_frames=emit_frames,
        emit_checkpoints=emit_checkpoints,
        enable_section=enable_section,
    )
    summary = payload["summary"]
    ref = summary["referenceEngine"]
    print(
        f"case         {payload['caseId']}  digest {payload['jobDigest']}  mode={payload['executionMode']}"
    )
    print(
        f"steps        {summary['stepCount']}  fully aligned {summary['fullyAlignedSteps']}  ref={ref}"
    )
    print("timing (ms)  engine        solve_ms    frame_ms    probe_ms    total_ms   speedup_vs_ref")
    for engine_id in engine_ids:
        info = payload["engines"][engine_id]
        solve_s = f"{info['solveWallMs']:10.1f}" if info["solveWallMs"] is not None else "       n/a"
        sp = summary["speedupVsReference"].get(engine_id)
        sp_s = f"{sp:8.2f}x" if sp is not None else "      n/a"
        print(
            f"             {engine_id:<12} {solve_s}  {info['frameWallMs']:10.1f}  "
            f"{info['probeWallMs']:10.1f}  {info['processWallMs']:10.1f}   {sp_s}  (rc={info['returncode']})"
        )
    for pair_key, acc in summary.get("accuracy", {}).items():
        print(
            f"accuracy     {pair_key:<18} all={acc['meanAgreement']:.4f} (med {acc['medianAgreement']:.4f})  "
            f"active={acc['meanActiveAgreement']:.4f}  feature={acc['meanFeatureAgreement']:.4f}  "
            f"1.00={acc['exactSteps']}/{acc['measuredSteps']}"
        )
    print(f"compare.json {args.out / 'compare.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
