> Superseded by the accepted bare-wafer demo: [`docs/finfet22-bare-wafer/README.md`](finfet22-bare-wafer/README.md).

# 22 nm FinFET geometry-only flow (wafer to M0)

This run is a backend/API-only geometry flow built on the engine-complete archive.
It starts from the GDS support-layer wafer stack and stops at M0. No `frontend/`
file was changed for this flow.

| Item | Value |
|---|---|
| Project ID | `bd8879ba30148652b1a88e9a` |
| GDS asset | `2495db9a943865cf544b7ae78672d2efe35650a45b4262c17f96e214abd0ea44` (`finfet22_workbench.gds`) |
| Original run | `1acfde1cd45f0dbdd2f53689` |
| Replay run | `9d2deaaf4f434b0ebb8eaf14` |
| Result | `succeeded`, 6 enabled steps, 382,844 points / 800,520 triangles |
| Replay check | all 7 surface-mesh VTK SHA-256 values match the original run |
| External archive | `/root/archives/finfet22-geometry-m0-20261030.tar.gz` |
| Full local record | `/root/spx_examples/finfet22/workbench/geometry_m0/` |

## Flow

Initial GDS stack: SUB Si (`-0.050…0`), Fin Si (`0…0.040`), STI SiO2 (`0…0.036`),
Gate PolySi (`0.036…0.096`) µm. Domain is 3D, `1 × 1 µm`, `gridDelta=0.004`,
reflective lateral boundary.

| # | Step | Native action | Parameters |
|---:|---|---|---|
| 1 | `gm0_ild_dep` | Isotropic SiO2 deposition | `0.05 µm/s`, 2.0 s (100 nm) |
| 2 | `gm0_ild_cut` | ILD geometry cutoff (`planarize`, not CMP) | `cutoffHeight=0.136` |
| 3 | `gm0_cb_open` | `gdsBoolean` difference, CB layer 4 | `baseHeight=0.036`, `height=0.10`, `applyToAll=false` |
| 4 | `gm0_w_dep` | Isotropic W deposition | `0.03 µm/s`, 1.0 s (30 nm) |
| 5 | `gm0_m0_trim` | `gdsBoolean` intersection, M0 layer 5 | `baseHeight=0.136`, `height=0.05`, `applyToAll=false` |
| 6 | `gm0_cb_fill` | `gdsBoolean` union, CB layer 4 | `baseHeight=0.036`, `height=0.10`, `applyToAll=false` |

Fin and Gate are mapped from the GDS stack; CB and M0 are used as native boolean
operands from the same asset. The boolean steps are geometric pattern transfer:
vertical extrusion, no resist, OPC/bias, sidewall angle, selectivity or mask
consumption model. The validation warnings state that explicitly.

The ILD is cut to a flat `0.136 µm` top before the M0 boolean. The M0 `baseHeight`
is set to that same plane, so the W bottom at `0.136 µm` is the metal underside
on the ILD interface, not a sheet inside ILD. Contact fill is a separate CB union;
the `z=0.036` W bottom exists only inside CB openings. With an 8 nm tolerance the
final mesh has **zero** `z=0.036` W bottom triangles outside CB.

Final W bounds: x `-0.200…0.200`, y `-0.147…0.147`, z `0.036…0.1660` µm.
`check_w_geometry.py` records 0 `z=0.036` W bottom triangles outside CB at an
8 nm tolerance; the `z=0.136` W bottom triangles are the M0 underside on the
flat ILD top. The manifest W `upFacingArea` is `0.04916 µm²`; this is a surface-area metric,
not the planar M0 footprint (`4 × 0.400 × 0.024 = 0.0384 µm²`). The two must not
be used interchangeably.

## Scope

The flow contains only deposit and geometry operations. It has no lithography,
implant, anneal, silicide, stress, CMP model, doping or electrical step. The
`gm0_ild_cut` step is a height cutoff with no slurry, removal-rate or lateral
selectivity model. All six steps report `actualEngine = not-applicable`; no GPU
flux engine is used.

Screenshots and the complete project/run record are in
`/root/spx_examples/finfet22/workbench/geometry_m0/`; the tarball mirrors that
directory and includes the final snapshot, final mesh, manifest and replay summary.

## Reproduction

Use the project JSON and GDS from the external record:

```bash
curl -X POST 'http://127.0.0.1:8790/api/gds-assets?file_name=finfet22_workbench.gds' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @gds/finfet22_workbench.gds
curl -X POST http://127.0.0.1:8790/api/projects/validate \
  -H 'Content-Type: application/json' --data-binary @geometry_m0_project.json
curl -X POST http://127.0.0.1:8790/api/runs \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json; print(json.dumps({"kind":"simulate","project":json.load(open("geometry_m0_project.json"))}))')"
```

The `assetId` in the project is the GDS SHA-256; re-upload the same GDS whenever
the Workbench data directory changes.
