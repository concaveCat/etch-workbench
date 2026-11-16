"""Command-line entry point for the engine layer.

Examples::

    python -m engines.cli engines                  # list registrations
    python -m engines.cli run job.json --out var/engines/case1
    python -m engines.cli run job.json --engines viennaps,spx --out var/engines/case1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engines.protocol import JobSpec
from engines.registry import describe
from engines.scheduler import compare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engines")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("engines", help="list registered engines")

    run = sub.add_parser("run", help="run one job on one or more engines")
    run.add_argument("job", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--engines", default="viennaps,spx")
    run.add_argument("--timeout", type=float, default=1800.0)

    args = parser.parse_args(argv)
    if args.command == "engines":
        print(json.dumps(describe(), indent=2))
        return 0

    job = JobSpec.from_json(json.loads(args.job.read_text(encoding="utf-8")))
    engine_ids = [value.strip() for value in args.engines.split(",") if value.strip()]
    payload = compare(job, engine_ids, args.out, timeout=args.timeout)
    summary = payload["summary"]
    print(f"case         {payload['caseId']}  digest {payload['jobDigest']}")
    print(f"steps        {summary['stepCount']}  fully aligned {summary['fullyAlignedSteps']}")
    for engine_id, wall in summary["totalWallMs"].items():
        info = payload["engines"][engine_id]
        print(f"  {engine_id:<10} {wall:9.0f} ms  v{info['version']}  rc={info['returncode']}")
    print(f"compare.json {args.out / 'compare.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
