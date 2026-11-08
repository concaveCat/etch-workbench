# Schema version 4: material dependent etch rates

Schema 4 keeps the schema 3 geometry, material catalog, dimensional view, and
snapshot shape. Steps add `rateMode`, which defaults to `uniform`.

- `uniform` keeps `rateMagnitude` as the process rate and preserves mask role
  protection. `materialRates` is forbidden.
- `material` is available for etch steps with either isotropic or single particle
  models. `rateMagnitude` is the fallback rate for materials omitted from the
  required `materialRates` map. Explicit entries override the fallback, including
  zero entries. Rates are magnitudes from 0 to 2 um/s and etch rates are sent to
  ViennaPS with a negative sign. Material roles do not override this map, so mask
  materials can have nonzero explicit rates. Material dependent deposition is
  unsupported.

An enabled step moves the surface when its fallback or any explicit rate is
nonzero. A zero fallback with a nonzero map entry still runs and, for a single
particle step, receives the run seed and a verified CPU flux engine. Only a zero
fallback and an all-zero map are treated as a no-op.

`selectivityReference` is saved with the project for display calculations. It is
excluded from physical request hashes and never passed to ViennaPS. The rate map
is hashed by material name, independent of its JSON key order. Schema 1, 2, and 3
hash and replay rules remain unchanged.
