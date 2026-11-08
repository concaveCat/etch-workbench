# Implant / anneal / doping capability (schema 7, ViennaPS 4.7.0 only)

Status: **implemented on `task/capability-implant` (base `d4e1cf1`)**. Three
additive schema-7 step models expose the official ViennaPS 4.7.0 analytical
implant capability:

| step model | native classes | surface motion | output |
|---|---|---|---|
| `implant` | `IonImplantation`, `ImplantPearsonIV`, `ImplantDualPearsonIV`, `ImplantPearsonIVChanneling`, `ImplantDamageHobler` | none | cell-set dopant + damage fields |
| `anneal` | `Anneal`, `AnnealMode`, solid activation | none | cell-set active / diffused field |
| `analysis` | `NetDoping`, `SheetResistance` | none | cell-set `net_doping` field + Rsh scalar |

The 4.6.2 runtime does not provide any of these classes. A project that
contains one of these steps is rejected with HTTP 422 during
`validate_project()` **before** a worker is created; the worker repeats the
version check so a hand-written `input.json` cannot reach the native layer.

## 1. Native evidence

All probes are bounded (300 s / 4 GiB, one process per dimension) and
reproducible with:

```bash
.venv/bin/python worker/probe_implant_native.py
```

The committed probe output (`.venv`: ViennaPS **4.7.0**, ViennaLS **5.10.0**):

```
planeCellSet: cells=2100  siCells=100  gasCells=2000
  -> an unmodified Domain cell set has ONE substrate row: the ViennaLS
     level set only stores a narrow band around the surface.
2D: 5100 cells (3100 Si), implant 0.086 s, anneal 0.024 s,
    labels [Material, FillingFraction, P_total, P_damage, P_damage_last,
            P_active, net_doping],
    digests P_total 5ee9c2d97596172a / P_damage 7a207490369c7d1b /
            P_active c91f8ba1bfc06bed / net_doping c91f8ba1bfc06bed,
    junctionDepth inf (single dopant, no sign change), Rsh 6.585e8 ohm/sq,
    surface unchanged: True
3D: 65000 cells (40000 Si), implant 1.256 s, anneal 0.305 s,
    same label set, distinct digests, Rsh 6.585e8 ohm/sq,
    surface unchanged: True
modelDbRoot: ''        wheelHasModelDb: False
ImplantTableModel('/nonexistent/implant.csv', ...) -> RuntimeError
    "Implant table file could not be opened"
```

Confirmed API surface (from the same probe):

* `IonImplantation`: `setImplantModel`, `setDamageModel`, `setDose`,
  `setTiltAngle`, `setLengthUnit`, `setDoseControl`, `setMaskMaterials`,
  `setScreenMaterials`, `setVoidMaterials`, `setConcentrationLabel`,
  `setDamageLabel`, `setLastDamageLabel`, `setBeamHitsLabel`,
  `enableBeamHits`, `setOutputConcentrationInCm3`, `setDamageFactor`,
  `enableEmbeddedBoundaries`. **There is no rotation setter.**
* `AnnealMode`: `Explicit`, `GaussSeidel`.
  `ImplantDoseControl`: `Off`, `WaferDose`, `BeamDose`.
* `Anneal`: 47 setters (schedule, Arrhenius/constant diffusivity, material
  roles, labels, solid activation, damage-gated activation, defect coupling,
  TED/Fermi enhancement, clustering, segregation/trapping, diagnostics,
  `applyActivation`).
* `NetDoping`: `setCellSet`, `add/setDonorLabels`, `add/setAcceptorLabels`,
  `setOutputLabel`, `setDepthAxis`, `setSurfacePosition`, `apply`,
  `junctionDepth`, `junctionDepths`, `junctionCount`,
  `lateralJunctionPosition(s)`.
* `SheetResistance`: `setCellSet`, `setConcentrationLabel`, `setDepthAxis`,
  `setSurfacePosition`, `setLengthUnit`, `setConcentrationUnit`,
  `computeElectron`, `computeHole`.

`ps.Process(domain, model, 0.0)` runs the implant/anneal callbacks without
moving a level set. `Domain.generateCellSet(position, coverMaterial,
isAboveSurface, withEmbeddedBoundaries)` is still the only Python cell-set
constructor; `DenseCellSet.fromLevelSets(levelSets, materialMap, depth)` takes
the same depth as `position` above the surface.

### Why the worker widens the level-set band

A ViennaLS level set stores only a narrow band (2 cells by default), so the
native grid contains a single substrate row and an implanted profile would have
no depth. The worker therefore builds the field grid on a **deep copy** of the
Domain and calls `viennals.Expand` on every level set (half-band =
`ceil(substrateDepth / gridDelta)` cells) before `generateCellSet`. The probe
shows the expanded copy's surface mesh is identical to the original, and the
run's own snapshots come from the untouched domain. `cellSet.substrateDepth`
is a required request field for this reason.

## 2. Schema

All three steps are additive to `StepV7`; a project without them keeps its
historical request hash (pinned by
`test_omitting_the_new_steps_keeps_the_historical_hash`).

### `cellSet` block (all three models)

```jsonc
"cellSet": {
  "height": 0.5,              // absolute top of the grid (native position argument)
  "substrateDepth": 0.4,      // required on the first enabled field step
  "coverMaterial": "GAS",     // must be GAS for field steps (see below)
  "periodic": false,          // must equal the domain lateral boundary
  "format": "both",           // vtu | json | both
  "scalarLabels": ["P_total"] // optional subset; omitted = every native label
}
```

Every enabled field step shares the grid generated by the first one; a later
step that asks for a different `height`, `coverMaterial`, `periodic` or
`substrateDepth` is rejected rather than silently ignored. The same shared-grid
check runs in the worker preflight, so a hand-edited `input.json` cannot bypass
it.

Measured on 4.7.0, `Domain.generateCellSet(position, ...)` treats `position` as
the **absolute** top coordinate of the grid, not a height above the surface: a
plane at `z = 0.05` with `position = 0.5` produced a cell set spanning
`[-0.36, 0.50]`. For the default flat plane geometry (`z = 0`) the value equals
the height above the surface, which is why the field is named `height`. The
captured grid stores both the absolute bottom (`band bottom`, driven by
`substrateDepth`) and the absolute top, so later steps can rebuild exactly the
same grid.

A surface-moving step is allowed **between** field steps. The worker then
regenerates the native cell set on the captured grid and conservatively remaps
every scalar field; see §3.1.

`coverMaterial` is fixed to `GAS`. The native generator treats the cover
material as the phase above the surface and skips cells of that material; a
cover equal to the implanted substrate therefore voids the target. Measured on
4.7.0: `coverMaterial="Si"` on a Si substrate returned `P_total` max 0 in both
2D and 3D, while `GAS` kept the substrate visible. No non-GAS cover has been
proven to keep the target visible, so every other value is rejected.

Step-owned cell-set labels (`labels.concentration/damage/lastDamage`,
`speciesLabel`, `activeLabel`, `netDoping` input/output labels and
`sheetResistance[].concentrationLabel`) may not use the native system labels
`Material` or `FillingFraction`. The native model writes the requested scalar
in place; `labels.concentration="Material"` overwrote the per-cell material ID
(measured: 1500 cells got IDs around 5.6e8). Both the schema validator and the
worker guard reject such a label.

### `implant`

```jsonc
{
  "id": "implant_step_01", "name": "P implant", "enabled": true,
  "model": "implant",
  "species": "P", "material": "Si",          // canonical tokens, recorded
  "energyKeV": 10.0, "doseCm2": 1e15,
  "tiltDeg": 7.0, "rotationDeg": 0.0,        // rotation must be 0 (no native setter)
  "doseControl": "WaferDose",
  "profile": {
    "source": "manual",
    "kind": "pearsonIV",                     // pearsonIV | dualPearsonIV | pearsonIVChanneling
    "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
    "lateralMu": 0.0, "lateralSigma": 0.005,
    // dualPearsonIV:        tail, headFraction, tailLateralMu, tailLateralSigma
    // pearsonIVChanneling:  tailFraction, tailStartDepth, tailDecayLength, tailBlendWidth
  },
  "damage": {                                 // optional Hobler damage
    "kind": "hobler", "projectedRange": 0.027, "verticalSigma": 0.018,
    "lambdaValue": 0.024, "defectsPerIon": 142.9, "lateralSigma": 0.013,
    "lateralDeltaSigma": -0.02
  },
  "maskMaterials": ["Mask"], "screenMaterials": ["SiO2"],
  "labels": {"concentration": "P_total"},     // optional; defaults P_total/P_damage/P_damage_last
  "damageFactor": 1.0,
  "cellSet": { ... }
}
```

* Moments are in the project length unit (**um**); `lengthUnitCm` is fixed at
  `1e-4` and recorded. The manual path has no native energy/rotation argument
  (only `ImplantTableModel` reads them), so the run record states
  `energyKeVNativeEffect: false`, `rotationDegNativeEffect: false`.
* `doseControl` maps 1:1 to `viennaps.ImplantDoseControl`.
* Labels are validated to be distinct and, when a damage model is absent,
  damage labels are refused.

### `anneal`

```jsonc
{
  "id": "anneal_step_01", "name": "anneal", "enabled": true,
  "model": "anneal",
  "mode": "GaussSeidel",                      // Explicit | GaussSeidel
  "schedule": {"kind": "isothermal", "temperatureK": 1273.15, "durationSeconds": 30.0},
  // or {"kind": "schedule", "durations": [9,5,9], "temperatures": [873,1323,1323,873]}
  "diffusivity": {"kind": "arrhenius", "D0": 1.23e13, "Ea_eV": 3.566},
  "implicitSolver": {"maxIterations": 400, "relativeTolerance": 1e-6, "relaxation": 1.0},
  "diffusionMaterials": ["Si"], "blockingMaterials": ["SiO2"],
  "speciesLabel": "P_total", "activeLabel": "P_active",
  "solidActivation": {"solubilityC0": 1.521e2, "solubilityEa_eV": 0.7102},
  "activationOnly": false,
  "cellSet": { ... }
}
```

`activationOnly: true` calls the native `Anneal.applyActivation(domain)`
(zero-time activation, the pattern of the official `pnJunction` example)
instead of `Process.apply`. Both write the active field.

### `analysis`

```jsonc
{
  "id": "analysis_step_01", "name": "analysis", "enabled": true,
  "model": "analysis",
  "netDoping": {
    "donorLabels": ["P_active"], "acceptorLabels": ["B_active"],
    "outputLabel": "net_doping",
    "surfacePosition": 0.0                   // optional; native default 0
  },
  "sheetResistance": [
    {"concentrationLabel": "P_active", "carrier": "electron"},
    {"concentrationLabel": "B_active", "carrier": "hole"}
  ],
  "scanDepth": 0.05,                         // NetDoping lateral scan (um)
  "cellSet": { ... }
}
```

`SheetResistance` is always called with `setLengthUnit(1e-4)` (um → cm), which
also sets the matching concentration unit `1e12 cm^-3`; the values are
recorded in the run document. Labels must be produced by an earlier implant or
anneal step. The exported units are the same in 2D and 3D: the native implant
concentration is volumetric (`1/um^3`, `concentrationUnitCm3 = 1e12`). The
`integralNative` value is `sum · gridDelta^D`: with a `1/um^3` concentration
the 2D value has unit `1/um` (one cell of `2 um^-3` with `gridDelta = 2 um`
gives `8 um^-1`), and the 3D value is dimensionless (the summary omits
`integralUnit` there).

## 3. Worker integration

* A run-wide `field_state` holds the widened deep-copy Domain and the native
  `DenseCellSet`; the simulated Domain (and every surface snapshot) is
  untouched.
* The worker preflight (`field_sequence_issues` in
  `backend/implant_models.py`) repeats the API's shared-grid checks before the
  first native call. A later field step with a different
  `height`/`substrateDepth`/`coverMaterial`/`periodic` fails the worker instead
  of silently reusing a stale grid. Per-step numeric and label guards run again
  immediately before each native constructor.
* Each step writes `artifacts/<step>-cell-set-fields.vtu` (kind
  `cellSetFields`) and `artifacts/<step>-cell-set-fields.json` (kind
  `cellSetFieldsSummary`). `format` selects VTU, JSON or both.
* The compact summary contains per-label statistics (`min/max/mean/sum/
  nonzeroCount/distinctCount`), a per-material breakdown for dopant labels,
  `integralNative` (`sum · gridDelta^D`), a SHA-256 `fieldDigests` entry per
  label (little-endian float64), and the full requested/effective/native
  record (`nativeConstructor`, dose, tilt, labels, material IDs,
  cell-set construction, junction depth, Rsh).
* The artifact cap is enforced *before* the VTU is written
  (`cells · 24 B + JSON`), using the existing `DenseCellSet` budget pattern;
  the preflight adds one such cell set per enabled field step
  (`implant/anneal/analysis cellSet（至多 N 个单元/步）`).
* `NetDoping` can return `inf` (no junction); the worker records `null` and
  keeps `junctionCount`/`junctionDepths`.
* Snapshots stay lazy; `cellSetProcess` is carried through
  `worker/materialize_snapshot.py` so a materialised snapshot is identical to
  the eager one, and the two artifact rows are carried as recorded references.

### 3.1 Cell-set regeneration and conservative remap

The first enabled field step captures an absolute grid: origin, top/bottom,
bounding cell counts, `gridDelta`, `coverMaterial`/id, `periodic`, the native
material map and the `substrateDepth`-derived band width (`cellSet.grid` /
`gridCapturedAtStepId`). The grid lives on the domain lattice, so every later
regeneration is described by whole-cell offsets.

A GDS / multi-material / structured geometry does **not** produce a full box:
the native `DenseCellSet` omits cover/void cells from the bounding lattice
(measured 1,608,870 active cells inside a 1,860,000-cell FinFET bounding
lattice). The grid is therefore stored as

* the bounding lattice (`origin`, `shape`, `gridDelta`), and
* an **active map**: the absolute integer key `i + nx·(j + ny·k)` of every
  native cell, in native cell order (`CellGrid.active`).

Plane/trench geometries are dense (`active = None`, position == key). Sparse
geometries build the map once from `getCellCenter()`; `nx·ny·nz` is **not**
required to equal `getNumberOfCells()`.

After every surface-moving step that is followed by another enabled field step
(and lazily if one is ever skipped) the worker:

1. builds a fresh `DenseCellSet` from the **current** level sets on a widened
   deep copy, passing the captured absolute top as the native `position`
   argument and choosing the band width that pins the captured absolute bottom
   (a band of `2` points is the native minimum);
2. if the surface grew above the captured top it grows the grid upwards, and if
   an etch went below the captured bottom it grows the grid downwards (band
   minimum); both are integer cell extensions, never a rescale. A native
   bounding lattice that is *larger* than the captured extent is accepted as a
   conservative superset (the extra active cells start at `0`);
3. reads the native `Material`/`FillingFraction` (and therefore the material IDs
   and native filling) unchanged from the regenerated grid;
4. remaps every non-system scalar label **by lattice key**
   (`backend/field_remap.py`):
   * **identical active layout** -> ordered index copy, `O(N)`;
   * **integer shift / grow / shrink** -> key overlap copy with `cellOffset`
     `{x,y,z}` in cells; matching active cells keep their exact value, every
     new active cell is `0`, and an old active cell with no new key is removed
     (`removedByGrid`, mass recorded);
   * new cells that were inactive before (a void becoming solid) start at `0`;
   * a lateral change, a spacing change or a non-integer offset raises
     `conservative grid remap not implemented` (HTTP 422 semantics: the worker
     fails before a native process call, no silent interpolation);
5. drops dopant from cells that became cover material (new material is the
   cover id, old material was not). Every other cell keeps its value, including
   cover cells that receive a small native implant tail. Nothing is
   redistributed to neighbours.

Each remap appends one record to `cellSet.remaps` (also emitted as a
`cell-set-remap` event) with: `sourceStepId`, `remapOfStepId`,
`oldGrid`/`newGrid`/`capturedGrid` signatures, `method`, `exact`, `sparse`,
active/full/missing counts (`oldCellCount`, `oldFullCellCount`,
`oldMissingCellCount`, likewise `new*`), `remappedCells`, `newCells`,
`removedCells` (`removedByGrid` + `removedByMaterial`), `reactivatedCells`,
`cellOffset`, `cellVolume`, `labels`, per-label `mass.before/after/lost`
(native `sum · gridDelta^D` units), `fieldDigests` and the current
`materialMap`. The record is deterministic: replaying a project reproduces the
same record and digests.

Conservation rule: `mass.after = mass.before - mass.lost`, exactly. `lost` is
the sum over active cells that became cover or have no new key; new cells
contribute `0`. The per-label `lost` is therefore the amount physically removed
by the etch (`removedCells`) and not a numerical residual.

### 3.2 Pre-allocation bounds

`Domain.generateCellSet(position, ...)` allocates the dense grid before any
post-check can run, so the worker computes the extent **before** the native
call. The required grid is fully determined by the current surface and the
captured grid:

```
top_node       = ceil(highest_surface / delta) * delta
bottom_node    = floor(lowest_surface / delta) * delta
required_top   = max(captured.top, top_node)
stored_half    = floor(widest_stored_level_set_width / 2)
band_half      = max(requested_band_cells // 2, stored_half)
planned_bottom = bottom_node - band_half * delta
rows           = ceil((required_top - planned_bottom) / delta) + 4
lateral        = (ceil(xExtent/delta) + 2) * (ceil(yExtent/delta) + 2)   # +2 is 2D
bound          = lateral * max(rows, 2)
construction   = ceil(bound * 2.5)        # GDS/multi-level-set band guard
```

`viennals.Expand(width)` only grows a band: a level set that already carries a
wider stored band (an earlier native operation or a Domain continuation) keeps
it. The bound therefore uses `getLevelSetWidth()` across **all** level sets;
with a 202-point stored band, a requested 18-point Expand still allocates the
2220-cell `[-5.05, 0.5]` grid and the bound is 2552 instead of 506.

`construction` is checked against `MAX_CELL_SET_CELLS` (2,000,000) before
`generateCellSet`; the bound carries two lateral cells of slack per axis and
four spare rows, which covers the measured native extent for plane, trench,
fin, stack, Boolean-grown surfaces and pre-expanded bands. The hard cap is
enforced again on the measured **active** native cell count after generation
(the storage is active-cell based). If a stored band
forces the bottom below the captured grid, the remap accepts the downward
growth as an integer-offset overlap (new cells `0`) instead of failing.

On the first field step the native top is the absolute `cellSet.height`, so a
`height` below the highest surface point is refused before allocation instead
of letting the native generator walk the whole surface band between the
requested position and the surface. For parametric geometries
(plane/trench/hole/fin/stack) the API performs the same check statically from
the geometry **when the field step is the first enabled operation**, so the
101 um plane/0.5 um height case is a 422 before a worker starts. An earlier
enabled step (planarize, boolean, process, ...) can move the surface
arbitrarily, so a static geometry check would reject valid sequences such as
`plane(2) → planarize(0) → implant`; those rely on the worker's live
pre-allocation check. GDS geometry and Domain continuations likewise have no
project-level vertical extent.

## 4. Rejection rules (HTTP 422, no worker, no native call)

| condition | message theme |
|---|---|
| runtime is not 4.7.0 | `does not provide the native IonImplantation / Anneal / NetDoping / SheetResistance classes` |
| `profile.source = "table"` | the 4.7.0 wheel ships no model database; use manual Pearson IV moments |
| unknown key / non-finite / out-of-range | Pydantic strict fields and the shared `numeric_guard_messages` |
| nonzero `rotationDeg` on the manual path | the manual model has no native rotation parameter |
| cluster of `labels.damage` without a damage model | damage labels require a damage model |
| missing `substrateDepth` on the first field step | the native band is one row deep |
| `cellSet.height` below one grid interval | the native generator crashes |
| `cellSet.coverMaterial` other than `GAS` | the native generator skips that material, voiding the target |
| a step label equal to `Material`/`FillingFraction` | the native model would overwrite the system scalar |
| a later field step with a different grid | `every cell-set step shares the grid generated by the first field step` |
| cell estimate above `MAX_CELL_SET_CELLS` | same hard limit as the generic cell set |
| `periodic` differs from the domain | the cell set can only mirror the domain boundary |
| analysis before any implant/anneal | `analysis requires an earlier implant or anneal step` |
| consumed label not produced earlier | `cell-set label(s) not produced by an earlier step` |
| `cellSet.height` below the initial surface top (parametric geometry, first enabled step) | `cellSet.height ... is below the highest initial surface point ... the native generateCellSet position is an absolute coordinate` |
| surface-aware grid above 2,000,000 cells (API) | `the field grid would need about N cells ... above the 2,000,000-cell construction limit` |
| regenerated grid above 2,000,000 cells (worker) | `field cell grid needs at least N cells ... above the 2,000,000-cell construction limit` (before allocation) |
| a regenerated grid off the captured lattice | `conservative grid remap not implemented` (worker, no native process call) |
| `outputSettings.cellSet` together with field steps | the generic output cannot carry dopant fields |
| material not in `/api/materials` | same material gate as every other step |

## 5. Limitations / intentionally not exposed

* **Model database tables (`ImplantTableModel`, `DamageTableModel`)** are not
  exposed. `viennaps.getModelDbRoot()` is empty and the public wheel ships no
  `modeldb/`; `ImplantTableModel` needs an explicit CSV path and raises on a
  missing file. The manual Pearson IV / dual / channeling + Hobler damage path
  is fully supported instead. A calibrated table would need its own uploaded
  asset store and range validation; the request is refused, never mocked.
* **`rotationDeg`** exists only in the table model and is therefore fixed at 0.
* **`setOutputConcentrationInCm3`** is not exposed: the workbench keeps
  concentrations in the native length unit so `NetDoping`/`SheetResistance`
  unit handling stays fixed and recorded.
* **`enableBeamHits` / `setBeamHitsLabel`** (debug hit counters) are not
  exposed; they add a per-cell diagnostic field with no physical meaning.
* **`enableEmbeddedBoundaries`** is not exposed; the worker always builds the
  grid with the plain band.
* **Anneal defect coupling, TED / Fermi enhancement, defect equilibrium,
  clustering, segregation/trapping, diagnostics and source-field injection**
  are not exposed. Their setters were probed and run, but they are separate
  calibrated models whose parameter provenance the request cannot validate;
  only the schedule, mode, diffusivity, material roles, labels and solid
  activation are part of the schema.
* `NetDoping` reports the raw native depth/lateral junction values in project
  length units (um); the native docs call them "nm" for nm-unit domains.
* Void/cover cells can receive small dopant amounts from the native profile
  (masked-trench probe: ~2 % of the dose in the open trench volume). The
  per-material breakdown exposes this instead of hiding it; `NetDoping` and
  `SheetResistance` integrate over the whole native cell set exactly as the
  native classes do.
* 3D works (65k-cell probe: 1.26 s implant, 0.31 s anneal) but a 3D field grid
  is bounded by the same 2,000,000-cell cap and the preflight byte estimate.
* A remap keeps a **fixed** lateral grid for the whole run. A surface that grows
  above the captured top grows the grid upwards (new cells `0`); a surface that
  is etched below the captured substrate band bottom grows it downwards at the
  native minimum band width (also new cells `0`). The grid never changes its
  lateral origin, spacing or `coverMaterial` inside a run; changing those is a
  hard refusal, not an interpolation.
* Interface cells are copied as a whole cell: a cell that keeps its solid
  material keeps its full dopant even if its filling fraction changed. Only a
  cell that becomes the cover material loses its dopant. This is conservative
  (no redistribution); a sub-cell partial-fill model is not implemented.
* The remap is exact on the lattice, not a volume/area-weighted
  re-projection. A future grid with a different `gridDelta` or lateral extent
  would need a genuine conservative intersection algorithm and is refused today.
* A sparse native grid stores one active-map entry per cell. `getCellCenter()`
  is called once per native cell when the map is built (~1.5 M calls/s on this
  host; 1.6 M cells take about one second). The map is kept for the run and
  reused by every remap, and the dense plane/trench case stores no map at all.
* The hard commitment is on **active** cells (`MAX_CELL_SET_CELLS`); the
  bounding lattice may be larger for a sparse geometry. The pre-allocation
  guard uses the analytic dense bound with a 2.5x band margin, so a geometry
  whose full bounding lattice is much larger than the bound can still be
  refused before allocation even when its active count would fit.

## 6. Tests

`tests/test_implant.py` (30 tests, all green on 4.7.0):

* schema defaults, label resolution, strict keys, non-finite and range
  rejection, table-source refusal, ordering/provenance, `substrateDepth`
  rules, periodic matching, generic cell-set conflict, 4.6.2 gate, 4.7.0
  acceptance, historical hash stability, preflight accounting;
* reserved native labels are rejected in the schema (implant concentration/
  damage/lastDamage, anneal species/active, NetDoping input/output and
  SheetResistance labels) and by the shared worker guard;
* `coverMaterial` other than `GAS` is rejected for every field step in 2D and
  3D, and the shared worker guard rejects it too;
* real 2D `implant → anneal → analysis` run (manual Pearson IV + Hobler
  damage): native VTU cell labels, per-material dopant split (cover cells
  undoped), activation fraction, junction/null handling, Rsh, exported
  concentration `1/um^3` (`1e12 cm^-3`) and 2D integral `1/um`, and an
  unchanged surface payload against a run without the field steps;
* JSON-only output path, real 3D run (hexahedral grid; volumetric
  concentration and no `integralUnit`), replay to identical field digests and
  Rsh, and the 422-before-worker path;
* direct-worker regressions that bypass the API: reserved labels, non-GAS
  cover in 2D/3D, a later field step with a different `height`/substrate grid,
  an accepted `implant → etch → anneal` run whose analysis artifact records the
  remap, plus a positive shared-grid direct run;
* `_field_integral` unit regression: `C=2 um^-3`, `delta=2 um` gives
  `8 um^-1` in 2D and the exported 2D `integralUnit` is `1/um` (3D omits it).

`tests/test_field_remap.py` (37 tests):

* pure-Python mapping: `CellGrid` round-trip from native node points, ordered
  same-grid copy (`O(N)`, exact digests), integer-shift overlap, grid growth
  (new cells `0`), shrink (`removedByGrid`), solid→cover mass loss with no
  redistribution, cover→solid reactivation, deterministic record, the
  pre-allocation bound formula, and refusals for a non-integer offset, a
  lateral/spacing change and a gapped lattice;
* sparse mapping: same-layout key copy, new active cells `0`, removed active
  cells (`removedByGrid`) with mass loss, and an integer shift over a sparse
  active map;
* native sparse reproduction: a fin (`finHeight=0.3`, `maskHeight=0.2`)
  produces a 20x33 / 12x12x33 bounding lattice with 517 / 3794 active cells
  instead of being rejected as "not a single regular box";
* real runs: 2D and 3D `implant → etch → anneal → analysis` (same-grid remap,
  recorded `sourceStepId`, per-label mass balance, labels preserved, Rsh still
  computed), `implant → deposit → anneal` (grid unchanged, `mass.lost = 0`),
  sparse `implant → deposit → anneal` (2D) and `implant → etch → anneal` (3D)
  on the fin geometry, a deep etch that grows the grid downwards
  conservatively, a Boolean union
  that lifts the surface from 0 to 2 um (380 → 980 cells inside the
  pre-allocation bound), `plane(2) → planarize(0) → implant → anneal →
  analysis` in 2D (380 cells) and 3D (18,400 cells), replay reproducing the
  remap record and every field digest bit-identically;
* limits: a Boolean growth whose regenerated grid would need ~2.4 M cells
  fails with the construction-limit message *before* allocation (`run.log`
  never contains `DenseCellSet generation produced`); a pre-expanded 202-point
  stored band is bounded (2220 cells, bound 2552) and refused before any
  native `Domain` is created when the cap is 1000, and an odd 19-point band
  uses the measured `floor(width/2)` half-width; the 3D 101 um
  plane/0.5 um height request (field step first) is a 422, a hand-edited
  worker input is refused by the same preflight check, and GDS/continuation
  geometry is left to the worker's live check;
* schema: a surface step between field steps is accepted, a later step with a
  different `height` is still rejected, and analysis before an implant is still
  rejected.

Full suite: the non-evidence tests pass (21 skipped). The remaining
errors/failures in this worktree are the pre-existing missing
`docs/*-evidence/` files (not in git) and are unchanged in nature from the base
commit.
