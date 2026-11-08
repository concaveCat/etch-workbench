# FinFET.py → workbench project mapping

The acceptance fixture keeps the upstream script verbatim at
`reference/FinFET.py` (SHA-256
`32ec9f2d180836a37fda0ea7bfe81a92ee83ed0c3014d61f3a7ab4238142c867`).
`reference/run_reference.py` executes that file unchanged and records one
canonical checkpoint after every `saveSurfaceMesh`, plus the final
`saveVolumeMesh`.

`reference/FinFET_um.py` is a mechanically unit-scaled twin: every length,
radius, half-axis, cutoff and rate is the original value × 0.001, while
durations and the orientation multipliers `rate111`/`rate100` are unchanged.
It is retained only as a cross-check for the native scale sensitivity
documented at the end of this file. The acceptance fixture itself runs in the
original nanometre frame.

The workbench project is `project.json` (25 enabled steps). It is not a new
script executor: every operation is a schema-7 step dispatched through the
normal v2 process/geometry registry.

## Unit contract

Schema 7 declares its native length unit in `units`:

```json
{"length": "nm", "time": "s", "rate": "nm/s", "angle": "deg"}
```

ViennaPS is unit-agnostic: the values reach the native constructors unchanged.
The workbench therefore does **not** rescale the source. Nanometre projects
are validated with the same physical limits as micrometre projects through a
unit-aware validation context, and snapshots/records carry
`coordinateUnit = "nm"`.

| Source quantity | Project value | Rule |
|---|---|---|
| `bounds` | `[0, 90, 0, 100, 0, 70]` | unchanged (nm) |
| `gridDelta` | `0.79` | unchanged (nm) |
| box / plane heights | nm | unchanged |
| sphere radius / box half-axes | nm | unchanged |
| `Planarize` cutoff | nm | unchanged |
| directional / isotropic / epitaxy rate | nm/s | unchanged |
| `duration` | s | unchanged |
| `saveVolumeMesh(..., 0.05)` | `volumeMeshDelta = 0.05` | unchanged (nm) |

The source domain uses absolute bounds and a 90 × 100 nm window. The native
grid rounding is asymmetric (`floor(min/delta)`, `ceil(max/delta)`), so the
centered `xExtent`/`yExtent` constructor cannot reproduce it (it always makes
an odd number of points per axis). Schema 7 therefore accepts an optional
`domain.bounds` array; the project sets the native bounds verbatim so every
grid point, including the 128-point y axis, is identical to the reference.

## Step mapping

Surface indices refer to `FinFET_<n>.vtp`; surface 0 is the initial plane.

| Surface | Source lines | Native operation | Project step |
|---|---|---|---|
| 0 | 25–27 | `MakePlane(domain, 70, Si)` | initial `geometry.kind = "plane"`, `height = 70` |
| 1 | 29–35 | `LevelSet` + `MakeGeometry(Box)` + `insertNextLevelSetAsMaterial(Mask)` | `step_dp_mask` (`insertBox`) |
| 2 | 37–44 | `duplicateTopLevelSet(Metal)` + `SphereDistribution(4)` | `step_dp_metal` (`particleShape`, `depositMaterial = Metal`) |
| 3 | 46–53 | `BoxDistribution([-δ, -δ, -6])` + mask `Si` | `step_dp_etch` (`particleShape` box etch, `maskMaterials = [Si]`) |
| 4 | 55–57 | `removeMaterial(Mask)` | `step_dp_unmask` (`removeMaterial`) |
| 5 | 59–68 | `DirectionalProcess([0,0,-1], {Si: [1.0, 0.1]})`, duration 90 | `step_si_pattern` (`directional`, material table, `rateSigns = native`) |
| 6 | 70–72 | `removeTopLevelSet()` | `step_metal_strip` (`removeTopLevelSet`) |
| 7 | 74–81 | `duplicateTopLevelSet(SiO2)` + `SphereDistribution(35)` | `step_sti_dep` (`particleShape`, `depositMaterial = SiO2`) |
| 8 | 83–85 | `Planarize(80)` | `step_sti_cmp` (`planarize`, `cutoffHeight = 80`) |
| 9 | 87–93 | `SphereDistribution(-35)` + mask `Si` | `step_sti_etch` (`particleShape` sphere etch) |
| 10 | 95–102 | `duplicateTopLevelSet(HfO2)` + `SphereDistribution(2)` | `step_hfo2_dep` |
| 11 | 104–110 | `duplicateTopLevelSet(PolySi)` + `SphereDistribution(104)` | `step_poly_dep` |
| 12 | 112–114 | `Planarize(150)` | `step_gate_cmp` (`cutoffHeight = 150`) |
| 13 | 116–122 | box mask `[-10,30,145]..[100,70,175]` | `step_gate_mask` (`insertBox`) |
| 14 | 124–133 | `BoxDistribution([-δ, -δ, -110])`, masks `Mask, Si, SiO2` | `step_gate_etch` (`particleShape` box etch, explicit masks) |
| 15 | 135–137 | `removeTopLevelSet()` | `step_gate_unmask` |
| 16 | 139–146 | `duplicateTopLevelSet(Si3N4)` + `SphereDistribution(10)` | `step_spacer_dep` |
| 17 | 148–157 | `getMaterialsInDomain()` minus `Si3N4`, box etch 50 | `step_spacer_etch` (`maskMode = "allExcept"`, `maskMaterials = [Si3N4]`) |
| 18 | 159–169 | `IsotropicProcess(rate=0)` + `setMaterialRate(Si, -1)`, spatial `LAX_FRIEDRICHS_2ND_ORDER`, 5 s | `step_fin_release` (`isotropic`, `rateMode = material`, step `advection`) |
| 19 | 171–184 | `SelectiveEpitaxy`, `setMaterialRate(Si/SiGe, 2)`, spatial `STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER`, `maxDissipation = 100`, 14 s | `step_sd_epi` (`specialized:selectiveEpitaxy`, step `advection.maxDissipation`) |
| 20 | 186–193 | `duplicateTopLevelSet(Dielectric)` + `SphereDistribution(50)` | `step_ild_dep` |
| 21 | 195–197 | `Planarize(90)` | `step_ild_cmp` (`cutoffHeight = 90`) |
| 22 | 199–201 | `removeMaterial(PolySi)` | `step_poly_remove` |
| 23 | 203–210 | `duplicateTopLevelSet(TiN)` + `SphereDistribution(4)` | `step_tin_dep` |
| 24 | 212–218 | `duplicateTopLevelSet(PolySi)` + `SphereDistribution(40)` | `step_rmg_poly` |
| 25 | 220–222 | `Planarize(90)` | `step_rmg_cmp` (`cutoffHeight = 90`) |
| volume | 224 | `saveVolumeMesh("FinFET_Final", 0.05)` | `outputSettings.volumeMesh = true`, `volumeMeshDelta = 0.05` |

## Rate-sign note

The native directional material table is a raw pair, not an
operation-signed pair. `FinFET.py` passes `{Si: [1.0, 0.1]}` with
`direction = [0, 0, -1]`: the positive directional component etches along
−z while the positive isotropic component grows the surface. The workbench's
historical `operation` sign convention flips the isotropic component for an
etch, so this fixture sets `rateSigns = "native"` to pass both values to the
native constructor unchanged. The project records the native magnitudes 1.0 and 0.1 nm/s
unchanged.

## Solver contract

- `solver.timeStepRatio = 0.4999` (the installed native
  `AdvectionParameters()` default; the script does not override it).
- Project advection keeps the native default
  `ENGQUIST_OSHER_1ST_ORDER` / `FORWARD_EULER`; only the two steps above carry
  a step-level override.
- `solver.cpuThreads = 1`, fixed seed 271828 (no stochastic flux is used in
  this fixture), `enginePreference = cpu`.

## Reference checkpoints

`reference/run_reference.py` records, for every surface:

- point count, triangle count;
- native material ID per level-set index (order) and per triangle;
- a canonical SHA-256 over rounded point coordinates, triangles and material
  IDs (translation/rotation of vertex lists and row order do not matter);
- bounds in nm.

The final volume records node/cell counts, tetrahedral cell type, material
distribution and its canonical digest. The original script and the workbench
both record nanometres, so no coordinate conversion is applied.

## Unit-frame cross-check

`expected.json` (nm) is the acceptance reference. `expected-um.json` and
`FinFET_um.py` remain committed only to document a measured native
limitation: the two native runs agree on surfaces 0–13 and diverge at surface
14 because ViennaPS's level-set mask booleans are not exactly scale
invariant. Now that the project runs natively in nm, the workbench matches
`expected.json` directly and no scaled reference is needed for acceptance.
`tests/test_finfet_reference.py` pins the relationship between the two native
frames.
