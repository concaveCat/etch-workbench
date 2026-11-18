"""Common probe lattice and cross-engine occupancy metrics.

SPX has no grid and ViennaPS has no analytic surface, so "point by point" only
means something on a lattice every engine answers at: sample the same points in
each engine, ask which canonical material occupies each point, then compare labels.

Metrics report three complementary views of accuracy so empty air above the wafer
never inflates the score:

* ``agreement``        - fraction of all sampled points with identical labels;
* ``activeAgreement``  - agreement over points where at least one engine is non-void
                         (eliminates void-air dilution when domain.height >> stack);
* ``featureAgreement`` - agreement over non-void points at ``z >= 0`` (the fabricated
                         thin-film / trench stack above the bulk substrate);
* ``iou``              - per-material intersection over union;
* ``topConfusions``    - top mismatched ``(left, right)`` material pairs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

VOID = "void"


@dataclass(frozen=True)
class Lattice:
    points: list[list[float]]
    shape: tuple[int, int, int]

    def as_json(self) -> dict[str, Any]:
        return {"points": self.points, "shape": list(self.shape)}


def stack_layer_midplanes(job: Any) -> list[float]:
    """Approximate z mid-planes of thin layers created across the flow.

    Tracks approximate stack top across deposits, fills, litho coats, strips, and
    stop-on-material CMPs so mid-planes stay inside the physical wafer envelope
    instead of drifting monotonically above the structure.
    """
    height = 0.0
    layer_tops: dict[str, float] = {str(job.initial.substrate): 0.0}
    layer_thickness: dict[str, float] = {}
    for film in getattr(job.initial, "films", ()) or ():
        t = float(film.thickness)
        height += t
        layer_tops[str(film.material)] = height
        layer_thickness[str(film.material)] = t

    mids: list[float] = []
    for step in job.steps[1:]:
        params = step.parameters or {}
        if step.kind == "deposit" and step.mechanism in ("isotropic", "anisotropic"):
            for name, rate in step.rates.items():
                thickness = float(rate) * float(step.duration)
                if thickness > 0:
                    mids.append(height + thickness / 2.0)
                    height += thickness
                    layer_tops[str(name)] = height
                    layer_thickness[str(name)] = thickness
        elif step.mechanism == "fill":
            offset = float(params.get("offset", 0.0))
            if offset > 0:
                mat = str(params.get("material", ""))
                mids.append(height + offset / 2.0)
                height += offset
                if mat:
                    layer_tops[mat] = height
                    layer_thickness[mat] = offset
        elif step.mechanism == "litho":
            coat = float(params.get("coat", 0.1))
            mat = str(params.get("material", "PMMA"))
            if coat > 0:
                mids.append(height + coat / 2.0)
                height += coat
                layer_tops[mat] = height
                layer_thickness[mat] = coat
        elif step.mechanism == "strip":
            for mat in params.get("materials") or ():
                mat = str(mat)
                if mat in layer_thickness and layer_tops.get(mat) == height:
                    height = max(0.0, height - layer_thickness[mat])
                layer_tops.pop(mat, None)
                layer_thickness.pop(mat, None)
        elif step.mechanism == "cmp":
            stop = params.get("stop_material")
            if stop and str(stop) in layer_tops:
                height = max(0.0, layer_tops[str(stop)] + float(params.get("offset", 0.0)))
            elif "cutoff" in params:
                height = max(0.0, float(params["cutoff"]))
    return mids


def build_lattice(
    width: float,
    depth: float,
    below: float,
    above: float,
    max_points: int = 20000,
    extra_z: Sequence[float] = (),
) -> Lattice:
    """Regular lattice over ``[0,width] x [0,depth] x [-below, above]``."""
    volume = max(width, 1e-9) * max(depth, 1e-9) * max(below + above, 1e-9)
    step = max((volume / max(max_points, 1)) ** (1 / 3), 1e-4)
    nx = max(2, min(80, int(round(width / step)) + 1))
    ny = max(2, min(80, int(round(depth / step)) + 1))
    nz_base = max(2, min(120, int(round((below + above) / step)) + 1))
    # Half-offset sampling avoids landing exactly on interfaces.
    xs = [(i + 0.5) * width / nx for i in range(nx)]
    ys = [(j + 0.5) * depth / ny for j in range(ny)]
    zs = [-below + (k + 0.5) * (below + above) / nz_base for k in range(nz_base)]
    for value in extra_z:
        if -below < float(value) < above:
            zs.append(round(float(value), 6))
    zs = sorted(set(zs))
    points = [[x, y, z] for z in zs for y in ys for x in xs]
    return Lattice(points=points, shape=(len(xs), len(ys), len(zs)))


def _top_heights(
    points: Sequence[Sequence[float]],
    labels: Sequence[str],
    materials: Sequence[str],
) -> dict[str, float | None]:
    heights: dict[str, float | None] = {}
    for material in materials:
        zs = [points[index][2] for index, value in enumerate(labels) if value == material]
        heights[material] = round(max(zs), 5) if zs else None
    return heights


def occupancy_metrics(
    points: Sequence[Sequence[float]],
    left: Sequence[str],
    right: Sequence[str],
    left_name: str = "viennaps",
    right_name: str = "spx",
) -> dict[str, Any]:
    """Compare two label sequences sampled on the same points."""
    if len(points) != len(left) or len(points) != len(right):
        return {
            "status": "invalid",
            "reason": (
                f"probe length mismatch: points={len(points)} "
                f"{left_name}={len(left)} {right_name}={len(right)}"
            ),
        }
    total = len(points)
    if total == 0:
        return {"status": "empty"}

    same = sum(1 for a, b in zip(left, right) if a == b)
    active_indices = [
        idx for idx, (a, b) in enumerate(zip(left, right))
        if not (a == VOID and b == VOID)
    ]
    active_same = sum(1 for idx in active_indices if left[idx] == right[idx])

    feature_indices = [
        idx for idx in active_indices if float(points[idx][2]) >= -1e-9
    ]
    feature_same = sum(1 for idx in feature_indices if left[idx] == right[idx])

    materials = sorted({value for value in list(left) + list(right) if value != VOID})
    iou: dict[str, float] = {}
    for material in materials:
        a = {index for index, value in enumerate(left) if value == material}
        b = {index for index, value in enumerate(right) if value == material}
        union = len(a | b)
        iou[material] = 1.0 if union == 0 else round(len(a & b) / union, 4)

    mismatches = [index for index, (a, b) in enumerate(zip(left, right)) if a != b]
    confusions = Counter((left[idx], right[idx]) for idx in mismatches)

    return {
        "status": "measured",
        "leftEngine": left_name,
        "rightEngine": right_name,
        "points": total,
        "agreement": round(same / total, 4),
        "activePoints": len(active_indices),
        "activeAgreement": (
            round(active_same / len(active_indices), 4) if active_indices else 1.0
        ),
        "featurePoints": len(feature_indices),
        "featureAgreement": (
            round(feature_same / len(feature_indices), 4) if feature_indices else 1.0
        ),
        "iou": iou,
        "mismatchCount": len(mismatches),
        "topConfusions": [
            {left_name: pair[0], right_name: pair[1], "count": count}
            for pair, count in confusions.most_common(8)
        ],
        "mismatchExamples": [
            {
                "point": [round(v, 5) for v in points[index]],
                left_name: left[index],
                right_name: right[index],
            }
            for index in mismatches[:5]
        ],
        "topHeightUm": {
            left_name: _top_heights(points, left, materials),
            right_name: _top_heights(points, right, materials),
        },
    }
