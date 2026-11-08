# Plasma advanced native parameters (B1)

Status: backend/API contract for the four built-in plasma models. No frontend
field is added by this change; the catalog exposes the schema so a client can
render it later.

## 1. Why this exists

`worker/specialized.py` historically called the flat float overloads. ViennaPS
4.6.2 also binds the parameter-object overloads:

| model | parameter class | constructor |
|---|---|---|
| `sf6o2` | `PlasmaEtchingParameters` | `ps.SF6O2Etching(parameters)` |
| `hbro2` | `PlasmaEtchingParameters` | `ps.HBrO2Etching(parameters)` |
| `sf6c4f8` | `PlasmaEtchingParameters` | `ps.SF6C4F8Etching(parameters)` |
| `cf4o2` | `CF4O2Parameters` | `ps.CF4O2Etching(parameters)` |

The nested parameter families are exposed through an optional, strict
`nativeParameters.advanced` object. The allowlist is the subset the installed
CPU models actually read, not every member of the native structs.

## 2. Payload shape

`advanced` sits beside the existing flat fields, never inside them:

```json
{
  "nativeModel": "sf6o2",
  "nativeParameters": {
    "ionFlux": 1,
    "etchantFlux": 1,
    "oxygenFlux": 0.2,
    "meanIonEnergy": 100,
    "sigmaIonEnergy": 10,
    "ionExponent": 100,
    "oxySputterYield": 3,
    "etchStopDepth": -1000000,
    "advanced": {
      "ions": {"meanEnergy": 60, "n_l": 8},
      "substrate": {"A_ie": 4},
      "beta_E": {"default": 1, "byMaterial": {"Si": 0.7}}
    }
  }
}
```

Rules:

* `advanced` is optional. Omitting it keeps the historical flat overload and
  the historical request hash (pinned by tests).
* Presence of `advanced` (even `{}`) selects the parameter-object overload and
  changes the request hash, because it is part of the submitted input.
* `advanced` is accepted only for `sf6o2`, `cf4o2`, `hbro2`, and `sf6c4f8`.
  Other specialized models reject the key.
* Unknown top-level blocks, unknown nested fields, wrong JSON types, non-finite
  numbers, astronomically large integers, and out-of-range numbers are
  rejected; nothing is silently dropped.
* Nested numbers use native units. Angles are **radians** (the installed
  `Ions.minAngle/inflectAngle/thetaRMin/thetaRMax` are radians), not degrees.
* `beta_*` maps are sticking probabilities in `[0, 1]`; `gamma_*` maps are also
  in `[0, 1]`. `rateFactors` is a non-negative multiplier in `[0, 1e6]`.

## 3. Exact exposed schema

Each advanced field carries `defaultSource`:

* `"flat"` — the field has a flat equivalent and the published default equals
  the flat catalog default; `flatKey` names that flat field. Materialising the
  catalog into `advanced` therefore cannot change behaviour.
* `"nativeParameterClass"` — no flat equivalent; the default is the installed
  parameter-class/model default.
* `"nativeMaterialMap"` — a `MaterialValueMap`; `default` is the map fallback,
  and per-material native defaults are model-specific and recorded at run time.

### 3.1 `sf6o2` — `PlasmaEtchingParameters`

```text
advanced.ions
  meanEnergy        0 .. 1e6        default 100    flat: meanIonEnergy
  sigmaEnergy       0 .. 1e6        default 10     flat: sigmaIonEnergy
  exponent          0 .. 1e6        default 100    flat: ionExponent
  n_l               0 .. 1e6        default 10     native
  minAngle          0 .. pi rad     default 1.3962634       native
  inflectAngle      0 .. pi rad     default 1.55334303      native
  thetaRMin         0 .. pi rad     default 1.2217304763960306 native
  thetaRMax         0 .. pi rad     default 1.5707963267948966 native

advanced.mask
  A_sp              0 .. 1e6        default 0.0139 native
  B_sp              0 .. 1e6        default 9.3    native
  Eth_sp            0 .. 1e6        default 20     native
  rho               1e-6 .. 1e6     default 500    native

advanced.passivation
  A_ie              0 .. 1e6        default 3      flat: oxySputterYield
  Eth_ie            0 .. 1e6        default 10     native

advanced.polymer
  A_sp              0 .. 1e6        default 0.02   native
  B_sp              0 .. 1e6        default 8.5    native
  Eth_sp            0 .. 1e6        default 15     native
  rho               1e-6 .. 1e6     default 5      native
  a1                -1e6 .. 1e6     default -0.26  native
  a2                -1e6 .. 1e6     default 2.72   native
  a3                -1e6 .. 1e6     default -4.3   native
  a4                -1e6 .. 1e6     default 1.95   native
  usePolyCosThetaYield  boolean    default false  native

advanced.substrate
  A_ie              0 .. 1e6        default 7      native
  A_sp              0 .. 1e6        default 0.0337 native
  B_sp              0 .. 1e6        default 9.3    native
  Eth_ie            0 .. 1e6        default 15     native
  Eth_sp            0 .. 1e6        default 20     native
  beta_sigma        0 .. 1e6        default 0.04   native
  k_sigma           0 .. 1e6        default 300    native
  rho               1e-6 .. 1e6     default 5.02   native
  # Substrate.B_ie exists in the native struct but is unused by the CPU model

advanced.passivationFlux   0 .. 1e6   default 0.2   flat: oxygenFlux

advanced.beta_E      map {default 1, byMaterial {material: value}}  0 .. 1
advanced.beta_P      map {default 1, byMaterial {material: value}}  0 .. 1
advanced.rateFactors map {default 1, byMaterial {material: value}}  0 .. 1e6
```

### 3.2 `hbro2` — `PlasmaEtchingParameters`

Same block/field allowlist and flat mappings as `sf6o2`. Model-specific native
defaults differ only in the substrate block:

```text
advanced.substrate.A_ie      default 5    (sf6o2: 7)
advanced.substrate.A_sp      default 0.5  (sf6o2: 0.0337)
advanced.substrate.Eth_sp    default 30   (sf6o2: 20)
```

`ions.meanEnergy=100`, `ions.sigmaEnergy=10`, `ions.exponent=100`,
`passivation.A_ie=3`, and `passivationFlux=0.2` match the workbench flat
defaults. `beta_E` per-material native defaults are 0.1 for Si/Mask in HBrO2.

### 3.3 `sf6c4f8` — `PlasmaEtchingParameters`

```text
advanced.ions
  meanEnergy        0 .. 1e6        default 100    flat: meanEnergy
  sigmaEnergy       0 .. 1e6        default 10     flat: sigmaEnergy
  exponent          0 .. 1e6        default 300    flat: ionExponent
  n_l               0 .. 1e6        default 10     native
  minAngle          0 .. pi rad     default 1.3962634       native
  inflectAngle      0 .. pi rad     default 1.55334303      native
  thetaRMin         0 .. pi rad     default 1.2217304763960306 native
  thetaRMax         0 .. pi rad     default 1.5707963267948966 native

advanced.mask      (same as sf6o2: A_sp, B_sp, Eth_sp, rho)
advanced.polymer   (same as sf6o2)
advanced.substrate (same fields/values as sf6o2, no B_ie)

advanced.beta_E      map {default 1, byMaterial {material: value}}  0 .. 1
advanced.rateFactors map {default 1, byMaterial {material: value}}  0 .. 1e6
```

**No passivation family is exposed.** `SF6C4F8Etching` creates no passivation
particle. Its native surface model still tests `params.passivationFlux > 0`
and then dereferences the missing `"passivationFlux"` flux scalar, which
SIGSEGVs. Therefore:

* `advanced.passivationFlux` is rejected with an explicit error;
* `advanced.passivation` is not in the allowlist;
* `advanced.beta_P` is not in the allowlist.

The `polymer` block remains exposed because the native surface model has a
real `Material::Polymer` sputter branch, even though this model does not
deposit polymer itself.

### 3.4 `cf4o2` — `CF4O2Parameters`

```text
advanced.ions
  meanEnergy        0 .. 1e6        default 100    flat: meanIonEnergy
  sigmaEnergy       0 .. 1e6        default 10     flat: sigmaIonEnergy
  exponent          0 .. 1e6        default 100    flat: ionExponent
  n_l               0 .. 1e6        default 10     native
  minAngle          0 .. pi rad     default 1.3962634  native
  inflectAngle      0 .. pi rad     default 1.55334303 native
  # no thetaRMin/thetaRMax in CF4O2ParametersIons

advanced.mask
  A_sp              0 .. 1e6        default 0.0139 native
  Eth_sp            0 .. 1e6        default 20     native
  rho               1e-6 .. 1e6     default 500    native
  # no B_sp in CF4O2ParametersMask

advanced.passivation
  A_O_ie            0 .. 1e6        default 3      flat: oxySputterYield
  A_C_ie            0 .. 1e6        default 3      flat: polySputterYield
  Eth_O_ie          0 .. 1e6        default 10     native
  Eth_C_ie          0 .. 1e6        default 10     native

advanced.si
  A_ie              0 .. 1e6        default 7      native
  A_sp              0 .. 1e6        default 0.0337 native
  Eth_ie            0 .. 1e6        default 15     native
  Eth_sp            0 .. 1e6        default 20     native
  beta_sigma        0 .. 1e6        default 0.04   native
  k_sigma           0 .. 1e6        default 300    native
  rho               1e-6 .. 1e6     default 5.02   native

advanced.siGe
  beta_sigma        0 .. 1e6        default 0.04   native
  k_sigma           0 .. 1e6        default 345.168420976798 native
  rho               1e-6 .. 1e6     default 4.84   native
  # A_ie/A_sp/Eth_ie/Eth_sp are ignored by the native SiGe branch;
  # x only seeds the initial k_sigma default and does not recompute k_sigma.

advanced.fluxIncludeSticking   boolean   default false   native

advanced.gamma_C             map {default 1, byMaterial {material: value}}  0 .. 1
advanced.gamma_C_oxidized    map {default 1, byMaterial {material: value}}  0 .. 1
advanced.gamma_F             map {default 1, byMaterial {material: value}}  0 .. 1
advanced.gamma_F_oxidized    map {default 1, byMaterial {material: value}}  0 .. 1
advanced.gamma_O             map {default 1, byMaterial {material: value}}  0 .. 1
advanced.gamma_O_passivated  map {default 1, byMaterial {material: value}}  0 .. 1
```

`gamma_*` maps are read by the native etchant/oxygen/polymer
`surfaceReflection` callbacks on every run; they change the reflected fraction
even when `fluxIncludeSticking` is false. `fluxIncludeSticking=true`
additionally applies the coverage-dependent sticking factor in
`surfaceCollision`. `CF4O2ParametersSiGe.k_sigma_SiGe(x)` is a method, not a
field, so it is not exposed.

## 4. How defaults are derived

For fields shared with a flat field, the catalog default is the **flat catalog
default** the worker already sends, not the C++ parameter-class default:

| shared field | flat source | advanced default |
|---|---|---|
| `ions.meanEnergy` | `meanIonEnergy` | 100 |
| `ions.sigmaEnergy` | `sigmaIonEnergy` | 10 |
| `ions.exponent` | `ionExponent` | 100 (`sf6o2`/`hbro2`/`cf4o2`), 300 (`sf6c4f8`) |
| `passivation.A_ie` | `oxySputterYield` | 3 (`sf6o2`/`hbro2`) |
| `passivation.A_O_ie` | `oxySputterYield` | 3 (`cf4o2`) |
| `passivation.A_C_ie` | `polySputterYield` | 3 (`cf4o2`) |
| `passivationFlux` | `oxygenFlux` | 0.2 (`sf6o2`/`hbro2`; removed for `sf6c4f8`) |

Thus `sf6o2`/`hbro2` expose `ions.exponent=100` and
`passivationFlux=0.2`, not the raw class defaults 500/100. `cf4o2` exposes
`ions.exponent=100`, not 500. Native-only fields keep the model's installed
default; HBrO2's substrate overrides and the per-material map defaults are
recorded in run metadata.

## 5. Native construction path

Without `advanced`:

```text
nativeParameters.advanced absent
  -> ps.<Model>(*flat_values)          # unchanged historical path
  -> no nativeAdvancedParameters metadata
```

With `advanced`:

```text
nativeParameters.advanced present
  -> flat_model = ps.<Model>(*flat_values)      # same flat overload
  -> params = flat_model.getParameters()        # model-owned parameter object
  -> re-apply every flat field through flatMapping
  -> snapshot defaults                         # before advanced overrides
  -> apply validated advanced blocks/scalars/maps
  -> model = ps.<Model>(params)                 # parameter-object overload
  -> snapshot effective
  -> discard flat_model (kept alive until the final constructor copied params)
```

The flat model is used as the base instead of constructing a fresh
`PlasmaEtchingParameters()`/`CF4O2Parameters()` because the flat overloads
install model-specific values and per-material map values. Using a fresh
parameter class would silently change physics for HBrO2 and SF6C4F8.

Flat-to-native mapping (also recorded in metadata as `flatMapping`):

| model | flat field | native path |
|---|---|---|
| `sf6o2`, `hbro2` | `ionFlux` | `ionFlux` |
| | `etchantFlux` | `etchantFlux` |
| | `oxygenFlux` | `passivationFlux` |
| | `meanIonEnergy` | `Ions.meanEnergy` |
| | `sigmaIonEnergy` | `Ions.sigmaEnergy` |
| | `ionExponent` | `Ions.exponent` |
| | `oxySputterYield` | `Passivation.A_ie` |
| | `etchStopDepth` | `etchStopDepth` |
| `sf6c4f8` | `ionFlux` | `ionFlux` |
| | `etchantFlux` | `etchantFlux` |
| | `meanEnergy` | `Ions.meanEnergy` |
| | `sigmaEnergy` | `Ions.sigmaEnergy` |
| | `ionExponent` | `Ions.exponent` |
| | `etchStopDepth` | `etchStopDepth` |
| `cf4o2` | `ionFlux` | `ionFlux` |
| | `etchantFlux` | `etchantFlux` |
| | `oxygenFlux` | `oxygenFlux` |
| | `polymerFlux` | `polymerFlux` |
| | `meanIonEnergy` | `Ions.meanEnergy` |
| | `sigmaIonEnergy` | `Ions.sigmaEnergy` |
| | `ionExponent` | `Ions.exponent` |
| | `oxySputterYield` | `Passivation.A_O_ie` |
| | `polySputterYield` | `Passivation.A_C_ie` |
| | `etchStopDepth` | `etchStopDepth` |

The installed binding returns `getParameters()` as a reference into the model;
the flat model must stay alive until the final constructor has consumed it.
A controlled probe also showed that passing
`ps.<Model>.defaultParameters()` directly into the parameter-object constructor
can leave `Ions.thetaRMax` uninitialized, so that overload is not used.

## 6. What each model consumes

| model | consumed advanced families | deliberately absent |
|---|---|---|
| `sf6o2` | ions, mask, passivation, polymer, substrate, `passivationFlux`, `beta_E`, `beta_P`, `rateFactors` | `Substrate.B_ie` (native struct field unused by CPU model) |
| `hbro2` | same as `sf6o2` | `Substrate.B_ie` |
| `sf6c4f8` | ions, mask, polymer, substrate, `beta_E`, `rateFactors` | `passivation`, `passivationFlux`, `beta_P` (no passivation particle; positive flux SIGSEGVs), `Substrate.B_ie` |
| `cf4o2` | ions, mask, passivation, Si, SiGe (`beta_sigma`/`k_sigma`/`rho` only), `fluxIncludeSticking`, `gamma_*` maps | SiGe `A_ie`/`A_sp`/`Eth_ie`/`Eth_sp`/`x` (ignored by native SiGe branch), `k_sigma_SiGe` method |

`sf6o2`/`hbro2` create the passivation particle only when
`passivationFlux > 0`; the `passivation` block and `beta_P` affect physics in
that mode. `polymer` affects only geometry containing `Material::Polymer`.
`gamma_*` affects the reflected fraction unconditionally; `fluxIncludeSticking`
adds the coverage-dependent sticking factor in `surfaceCollision`.

## 7. Run metadata

For every run that contains `advanced`, `execution.nativeModel` (and the
per-step `actualParameters.nativeModel`) contains:

```json
"nativeAdvancedParameters": {
  "parameterClass": "PlasmaEtchingParameters",
  "constructor": "SF6O2Etching",
  "constructorOverload": "parameter-object; flat values applied first, then advanced overrides",
  "flatMapping": {"ionFlux": "ionFlux", "...": "..."},
  "requested": {"ions": {"meanEnergy": 60}},
  "defaults": {"blocks": {"...": {"...": 0}}, "scalars": {"...": 0}, "maps": {"...": {"default": 1, "byMaterial": {"Si": 0.7}}}},
  "effective": {"blocks": {"...": {"...": 0}}, "scalars": {"...": 0}, "maps": {"...": {"default": 1, "byMaterial": {"Si": 0.6}}}},
  "defaultsSource": "installed flat overload parameters before advanced overrides"
}
```

`defaults` is the flat-path value before any advanced override; `effective` is
what the constructed model received. JSON booleans stay booleans in metadata
(for example `polymer.usePolyCosThetaYield` and
`cf4o2.fluxIncludeSticking`); block booleans are not coerced to `1.0/0.0`.
Maps record `default` and per-material values; unresolved native registry names
are listed in `unresolvedMaterialNames`.

## 8. Validation

`backend/validation.py` enforces the same schema used by the worker:

* `advanced` must be a JSON object for one of the four plasma models;
* unknown top-level keys, unknown block keys, and unknown map keys are errors;
* explicitly rejected keys produce a model-specific message
  (`sf6c4f8.advanced.passivationFlux`);
* numbers must be finite JSON numbers inside the published range; booleans must
  be JSON booleans (no `0`/`1` coercion); huge integers that overflow
  `float()` are rejected as non-finite;
* `thetaRMin <= thetaRMax` and `minAngle <= inflectAngle` are enforced for the
  `PlasmaEtchingParameters` ion block;
* `byMaterial` keys must name installed catalog materials (custom materials and
  the `Polymer` pseudo-material are not accepted here);
* existing flat-field validation is untouched and still runs first;
* the worker re-checks the allowlist/rejected keys and raises if it is asked to
  set a member that the schema does not describe (no silent fallback).

## 9. Evidence and limitations

`tests/test_plasma_advanced.py` pins:

* the eight pre-B1 request hashes are unchanged when `advanced` is omitted;
* the catalog schema equals the native-consumed subset, including the rejected
  inert fields (`SiGe` extras, `Substrate.B_ie`, SF6C4F8 passivation family);
* advanced defaults equal the effective flat defaults for shared fields and the
  installed native defaults for native-only fields;
* empty `advanced` reconstructs the flat path with identical values;
* 2D **and** 3D golden runs for `sf6o2`, `hbro2`, `sf6c4f8`, and a SiGe
  `cf4o2` case match an independently constructed native reference within
  `1e-6 um`, while differing from the corresponding no-advanced run;
* run-level map/scalar/boolean overrides (`beta_E`, `rateFactors`, `gamma_F`,
  `fluxIncludeSticking`, `usePolyCosThetaYield`) and the CF4O2 SiGe
  `k_sigma`/`rho` path;
* unknown key, inert/cross-model block, type, range, angle, material, huge
  integer, and SF6C4F8 passivation-flux negatives (including the worker guard);
* save/reopen, run metadata, replay-project round-trip, and replay execution.

Limitations:

* No custom materials: the built-in plasma models have no verified parameter
  rows for custom materials, so existing validation still rejects them.
* The gamma/beta map per-material native defaults are not statically listed in
  the catalog; they are model-specific and recorded at run time.
* `docs/*-evidence/` is not modified by this task, so the B1 acceptance
  “archived evidence” step remains open even though the automated golden
  comparison is in the test suite.
* This is a backend/API contract only; no frontend field is exposed.
