"""Cross-section metrics: interface heights along a fixed line of columns.

Why a section and not a 3D interface distance: SPX can export a structure
(``meas_save_3D``, formats ``tdr``/``native``/``gcns``/``stl_3dprint``) but the
useful ones are HDF5, and the workbench virtualenv has no HDF5 reader. Rather than
add a binary dependency for one metric, both engines are asked for the interface
height of every material along the same columns:

* ViennaPS answers exactly from its surface mesh (per-column material top);
* SPX answers by binary search on ``meas_probe`` labels (~1.3 ms per probe).

The result is a z-direction interface distance (mean / max / L2) plus the two
polylines, which the comparison UI draws on top of each other. Metrics are only
reported for materials both engines actually resolved.
"""

from __future__ import annotations

import math
from typing import Any, Sequence


def section_metrics(columns: Sequence[Sequence[float]],
                    materials: Sequence[str],
                    left: dict[str, list[float | None]],
                    right: dict[str, list[float | None]],
                    left_name: str = "viennaps",
                    right_name: str = "spx") -> dict[str, Any]:
    """Per-material interface-height differences on a shared set of columns."""
    if not columns or not materials:
        return {"status": "empty"}
    per_material: dict[str, Any] = {}
    worst = 0.0
    for material in materials:
        left_heights = list(left.get(material) or [])
        right_heights = list(right.get(material) or [])
        if len(left_heights) != len(columns) or len(right_heights) != len(columns):
            per_material[material] = {"status": "length_mismatch"}
            continue
        deltas = [abs(a - b) for a, b in zip(left_heights, right_heights)
                  if a is not None and b is not None]
        missing = sum(1 for a, b in zip(left_heights, right_heights)
                      if a is None or b is None)
        if not deltas:
            per_material[material] = {"status": "not_resolved",
                                      "missingColumns": missing,
                                      "columns": len(columns)}
            continue
        worst = max(worst, max(deltas))
        per_material[material] = {
            "status": "measured",
            "columns": len(columns),
            "resolved": len(deltas),
            "missingColumns": missing,
            "meanAbsUm": round(sum(deltas) / len(deltas), 6),
            "maxAbsUm": round(max(deltas), 6),
            # L2 over the section, normalised by the number of resolved columns.
            "l2Um": round(math.sqrt(sum(d * d for d in deltas) / len(deltas)), 6),
        }
    measured = [row for row in per_material.values() if row.get("status") == "measured"]
    return {
        "status": "measured" if measured else "not_resolved",
        "columns": len(columns),
        "materials": per_material,
        "worstAbsUm": round(worst, 6),
        "heights": {left_name: left, right_name: right},
    }
