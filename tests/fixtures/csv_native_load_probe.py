"""Direct native RateGrid probe for canonical CSV assets.

Always run through scripts/bounded_child.py: the parent test passes a stored
canonical table and checks that the installed loader accepts it and returns a
known interpolation value. This catches BOM/tab/comment/format disagreement
that a Python-only parser test cannot see.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps


def main() -> None:
    source = Path(sys.argv[1])
    dimension = int(sys.argv[2])
    mode = sys.argv[3]
    coordinate = [float(value) for value in json.loads(sys.argv[4])]
    destination = Path(sys.argv[5])
    ps.setDimension(dimension)
    grid = ps.RateGrid()
    loaded = bool(grid.loadFromCSV(str(source)))
    grid.setInterpolationMode(mode)
    if mode == "IDW":
        grid.setIDWNeighbors(int(sys.argv[6]))
    value = float(grid.interpolate(coordinate))
    destination.write_text(json.dumps({
        "loaded": loaded,
        "dimension": dimension,
        "mode": mode,
        "coordinate": coordinate,
        "value": value,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"loaded": loaded, "value": value}))


if __name__ == "__main__":
    main()
