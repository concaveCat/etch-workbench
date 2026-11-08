# Per-layer stack trench (`MakeTrench(MaterialLayer[])`)

This contract is limited to the **installed native binding**, not a future
upstream API. It was checked against ViennaPS 4.6.2 (production) and 4.7.0.

## Native evidence

`viennaps.d2.MakeTrench` and `viennaps.d3.MakeTrench` expose a third overload:

```text
MakeTrench(domain, materialLayers: Sequence[MaterialLayer], halfTrench=False)
```

`MakeTrench.MaterialLayer` has exactly:

| field | meaning |
|---|---|
| `height` | thickness of this native layer |
| `width` | cutout width for this layer (top width) |
| `taperAngle` | cutout taper in degrees; positive widens the layer bottom |
| `material` | native material of the layer |
| `isMask` | `true` cuts an opening through this layer; `false` leaves it intact |

Runtime probes and the shipped `tests/trench/trench.cpp` show:

- The sequence is consumed **bottom to top**.
- The first non-mask layer is inserted with `makeSubstrate(currentBase + height)`;
  every later layer is a finite slab from `currentBase` to
  `currentBase + height`.
- `isMask` is evaluated per layer, immediately after that layer is inserted.
  It is not the stack layer's etch-protection `role`.
- A positive per-layer `taperAngle` widens the layer bottom by
  `2 * height * tan(angle)`. This is the opposite visual direction from the
  legacy single-trench `trenchTaperAngle`, which is why the sign is passed
  through unchanged to `MaterialLayer` rather than reused from the old stencil.
- The constructor clears the domain before building, so a preceding
  `MakePlane` would be discarded.

`MakeHole` in both 4.6.2 and 4.7.0 has only the radius/depth constructors. It
has **no** `MaterialLayer[]` overload, so per-layer holes are rejected instead
of being emulated with a top-only stencil or a different geometry.

## Schema

`geometry.pattern` keeps all existing fields. The only addition is an optional
`layers` array:

```json
{
  "kind": "trench",
  "layers": [
    {"layerIndex": 0, "width": 1.6, "taperAngle": 0, "isMask": true},
    {"layerId": "nitride_layer", "width": 0.8, "taperAngle": 5, "isMask": true}
  ]
}
```

- Each entry references exactly one stack layer by zero-based `layerIndex` or
  by `layerId`; supplying both or neither is rejected.
- The list must cover **every** stack layer exactly once, ordered bottom to
  top. Partial lists are rejected with a 422 even when they only name the top
  layer, because the native top cutout extends below its own layer.
- The top stack layer must have `isMask=true`.
- `width` must be positive, `taperAngle` must be in `[-45, 45]`, and both must
  be finite JSON numbers.
- `isMask` may open film or mask layers. `role` remains the process
  protection role and is not silently rewritten.
- The legacy `pattern.width` / `pattern.taperAngle` pair may still be supplied
  together. If present, it must equal the top entry so the request cannot
  describe two different openings. If omitted, the per-layer entries are the
  only opening specification.

Omitting `layers` keeps the historical `MakePlane` + top-layer
`GeometryFactory` stencil path, including request hashes and snapshots.

### Stack mapping

The Workbench substrate is the half-space below `baseHeight`. The native
constructor starts at `-1e-4`, so the worker prepends a native substrate layer
with `height = baseHeight + 1e-4`, then one explicit native layer per stack
layer with its declared thickness. The first native layer is the non-mask
substrate; the top entry must be a mask. `baseHeight` must be non-negative in
this mode.

The installed constructor anchors every cutout at `currentBase - 1e-4` and
extends it into the layer below. For layers thinner than about `1e-4` that
bleed can remove the entire lower layer even when its `isMask=false`. The
schema now makes every layer explicit and rejects partial lists, but it does
not hide this native fixed-epsilon behavior. The legacy omitted-block path
keeps its own `gridDelta * 1e-3` stencil and is unchanged.

The initial snapshot's `geometryReadback.perLayerPattern` records the native
constructor, the actual native layer sequence, all heights, widths, tapers,
materials and `isMask` flags.

## Rejected combinations

- `pattern.kind` other than `trench`
- per-layer holes (no installed native overload)
- `pattern.layers` with `kind=none` or `kind=gds`
- a GDS mask overlay (`geometry.gdsMask`) or a `source="gds-mask"` stack layer
- negative `baseHeight`
- unknown, duplicate, out-of-order or ambiguous layer references
- a partial list that omits any stack layer, including an unreferenced lower
  film below the top cutout
- a missing top layer or `isMask=false` on the top layer
- cutouts that close under taper or exceed the lateral domain
- unknown keys, non-finite numbers, zero/negative widths and out-of-range angles

## Verification

`tests/test_capability_layers.py` covers:

- 2D and 3D equality with an independent native `MaterialLayer[]` call
- measured per-layer widths, taper and `isMask=false` versus `true`
- replay determinism
- hard-coded pre-feature request hashes and artifact equality for omitted
  blocks
- the exact reviewer partial-list repro rejected before native
- direct-worker `"nan"`, `"inf"`, bool and huge-int values rejected before any
  numeric conversion or native constructor
- negative schema and incompatible-geometry cases

The repository-wide suite in a worktree without the untracked `docs/*-evidence`
trees reports only those missing-evidence failures; there are no per-layer
regressions.
