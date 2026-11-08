# Workflow provenance and measurement binding contract

**Status**: Binding v1 implemented; equivalence remains separate and unimplemented.
**Scope**: Measurement → Route/Attempt/Segment binding  
**Non-goal**: claiming that a measurement is a verified result of a real run until every required evidence item below is present.

## 1. Three independent status axes

A measurement has three separate facts. They must never be collapsed into one boolean.

| Axis | Values | Meaning |
|---|---|---|
| `measurementStatus` | `measured`, `no_intersection`, `ambiguous`, `material_not_found`, `region_empty`, `unsupported_dimension`, `unsupported_units`, `invalid_geometry` | Did the measurement algorithm produce a value? |
| `bindingStatus` | `unbound`, `verified`, `failed` | Is the measurement tied to a server-verified execution segment and step? |
| `equivalenceStatus` | `not_evaluated`, `established`, `failed`, `inconclusive` | Has numerical/semantic equivalence to a segment result been established? |

Rules:

- `measurementStatus=measured` does not imply `bindingStatus=verified`.
- `bindingStatus=verified` does not imply `equivalenceStatus=established`.
- Historical measurements may remain `unbound` forever without becoming invalid.
- A failed binding must preserve the original Measurement and record the reason. Do not overwrite or delete it.
- `Segment.runId` supplied by a user is not evidence. Only a server-established or server-verified link to a real run can grant `verified`.

## 2. Minimum evidence for `bindingStatus=verified`

| Evidence object | Minimum requirement |
|---|---|
| **Execution ownership** | Server establishes or verifies `Segment → actual Run`. The actual submitted project must match `segmentProjectHash` and the source `CompileRecord`. |
| **Segment input derivation** | From the full Compile, step range, starting checkpoint and actual seed, derive the expected Segment input; compare it with the real Run's `normalizedProject` (not only its hash). |
| **Target step** | The target step belongs to that segment; the run manifest contains its snapshot; the step is recorded as completed. |
| **Source identity** | Measurement `sourceStateId` equals the native state of that step; `sourceSnapshotHash` is recomputed from the snapshot actually read. |
| **Verification record** | Persist method version, evidence references, result, and failure reason. |

Additional rules:

- If any item is missing or mismatched, `bindingStatus=failed` (with reason) or remains `unbound`; never `verified`.
- The actual submitted project hash is compared to `segmentProjectHash`; the segment's `sourceCompileId` must resolve to an immutable CompileRecord.
- The expected Segment input is derived from the full Compile slice (steps, geometry/domain/material binding, solver settings, starting checkpoint and seed) and compared with the real Run `normalizedProject`. A matching hash alone is insufficient if the project cannot be derived from the Compile.
- If Measurement already has `sourceRunId` or `sourceStepId`, the binding request must match them. Equal geometry hashes do not permit reattributing the Measurement to another run.
- The run manifest must record the step as completed and reference the snapshot/state. A step that merely appears in a string field is not sufficient.
- If the snapshot is re-materialized or its content changes, `sourceSnapshotHash` must be recomputed and the binding re-checked.

## 3. Hash roles

| Hash | Role |
|---|---|
| `sourceSnapshotHash` | Identifies the measurement input snapshot content. |
| `segmentProjectHash` | Identifies the actual project submitted for the segment. |
| `CompileRecord.fullProjectHash` | Identifies the complete compiled flow. |
| `resultDigest` | Semantic result comparison for the segment's **final** state. |

Do not use `resultDigest` to validate an intermediate-step measurement. A CD/depth measured at step 5 cannot be compared with the segment's final-state digest. Numerical equivalence, when implemented, uses a separate `ComparisonProtocol` and explicitly stated scope.

## 4. Verification record

Proposed `ProvenanceVerificationV1`:

```text
ProvenanceVerificationV1 {
  methodVersion,
  verifiedAt,
  execution: {
    status: verified | failed,
    segmentId, runId,
    sourceCompileId, segmentProjectHash,
    submittedProjectHash, manifestRunId,
    reasons[]
  },
  targetStep: {
    status: verified | failed,
    stepId, stepIndex, completed,
    snapshotFile, snapshotHash, stateId, stateHash,
    reasons[]
  },
  sourceIdentity: {
    status: verified | failed,
    measurementStateId, manifestStateId,
    recomputedSnapshotHash, measurementSnapshotHash,
    reasons[]
  },
  equivalence: {
    status: not_evaluated | established | failed | inconclusive,
    comparisonProtocolId?, metricScope?, resultDigest?, reasons[]
  }
}
```

`bindingStatus=verified` requires `execution.status=verified`, `targetStep.status=verified`, and `sourceIdentity.status=verified`. It does not require `equivalence.status=established`.

## 5. State transitions

```text
unbound ──verify success──► verified
unbound ──verify failure──► failed (reason preserved)
failed  ──new evidence────► verified
verified ──same-binding re-verification mismatch──► failed (revoked)
verified ──different candidate fails──► verified (failure recorded separately)
```

- A measurement created with `POST /measurements` from a raw snapshot is `unbound` unless the caller also supplies a server-verifiable segment/run/step identity.
- `POST /measurements/from-run` may verify execution ownership only when the run/step/state exists and matches. Otherwise it returns the Measurement as `unbound` with a reason.
- Binding is idempotent for the same evidence; a new verification method version produces a new verification record, not a mutated old one.
- Verification records are append-only and carry their own ID. Measurement caches only the current status/evidence.
- A failed attempt to bind a different Segment/run/step must be recorded but must not automatically revoke an existing verified binding.
- The same failed binding may revoke `verified` only when it re-verifies the identical Segment/run/step and the current evidence no longer matches.

## 6. Persistence and API proposal

`measurements` table additions:

```text
binding_status TEXT NOT NULL DEFAULT 'unbound',
binding_evidence_json TEXT,
equivalence_status TEXT NOT NULL DEFAULT 'not_evaluated',
equivalence_evidence_json TEXT
```

Verification records are stored separately:

```text
measurement_verifications {
  verificationId, measurementId, status,
  methodVersion, evidenceJson, createdAt
}
```

Implemented endpoints for binding v1:

```text
POST /api/v1/workflow/measurements/{measurementId}/bind
GET  /api/v1/workflow/measurements/{measurementId}/provenance
GET  /api/v1/workflow/measurements?binding_status=unbound
```

Equivalence remains separate and is not part of `/bind`:

```text
POST /api/v1/workflow/measurements/{measurementId}/equivalence   # not implemented
```

`POST /bind` accepts an explicit `segmentId`, `runId`, `stepId` and evidence references; it must not trust arbitrary strings without manifest/state/snapshot checks. `GET /provenance` returns the current cached binding plus the ordered verification records.

## 7. Acceptance tests for the next stage

1. A user-supplied `Segment.runId` that does not match a real run leaves the Measurement `unbound`.
2. A step that exists in the manifest but is not completed cannot be bound.
3. A snapshot whose recomputed hash differs from `sourceSnapshotHash` cannot be bound.
4. A `sourceStateId` that differs from the manifest native state cannot be bound.
5. A submitted project whose hash differs from `segmentProjectHash` cannot be bound.
6. A valid run/step/state/snapshot/compile chain produces `bindingStatus=verified` and a verification record.
7. An intermediate-step measurement with no final `resultDigest` can still be `verified` for binding but remains `equivalenceStatus=not_evaluated`.
8. Revoking a verified binding after a hash mismatch sets `bindingStatus=failed` and preserves the original Measurement.
9. Historical measurements with no execution identity remain readable and `unbound`.

## 8. Invariants

- Measurement success and execution binding are independent.
- Binding requires evidence, not labels.
- `sourceSnapshotHash` is recalculated from the actual snapshot.
- `resultDigest` is never a substitute for intermediate-step binding.
- Failed or absent verification is explicit, never silent.

## 9. P4-2 measurement-driven workflow runs

P4-2 does **not** add control flow inside a native engine process. A *workflow
run* is a composition in the workflow control layer:

```text
Route
  └─ Attempt A (prefix) → Segment A (real run, succeeded, end checkpoint)
       └─ measurement at Segment A.throughStepId
            └─ MeasurementDecisionV1 (measurement, condition, resolved parameters)
                 └─ Attempt B (branch) → Segment B (planned, reuses checkpoint of A)
```

The `MeasurementDecisionV1` record is the only place where the branch condition
is decided. It stores the source Segment/Attempt/Compile/Run, the measurement
status axes (binding, equivalence), the recipe/metric, operator/threshold, the
measured value, the boolean result, the chosen branch, **both** candidate
parameter maps, the authored-step resolved parameters, the branch end step, the
target compile, the source prefix digest/seed/checkpoint hash, and the new
branch Attempt/Segment.

Decisions on one Route form a sequence under one `workflowRunId` (`wfr_...`).
The first decision creates it, later decisions continue it, and an explicit
`workflowRunId` may either continue an existing run or start a new one. A run id
may never be shared by two routes. `GET /api/v1/workflow/routes/{id}/workflow-runs`
and `/workflow-runs/{workflowRunId}` return the run as decisions plus the
Attempt/Segment lineage they reference.

A decision measures the **end step of the measured Segment** and requires
`bindingStatus=verified`; a failed or unbound measurement is recorded and
preserved but cannot pick a branch. `equivalenceStatus` stays an independent
axis: it is copied onto the decision and is not a gate.

`branchThroughStepId` may stop the branch before the last route step. The branch
Segment then ends at a checkpoint that the next decision can measure, so several
measurement-driven branches can chain inside one workflow run. Resolved
parameters may only touch authored steps strictly after `startAfterStepId` and no
later than the branch end. Envelope fields (geometry binding, solver, seed
policy, run seed) of every reuse entry must match the attempt prefix, while each
entry's step identity covers only its own contiguous range; this is what lets a
chained attempt reuse an earlier attempt's prefix plus its own segment.

Replay is record-driven:

- The original Measurement is reused; no new Measurement is created and the
  source snapshot is not measured again.
- `chosenBranch`, `resolvedParameters` and `measurementId` are copied unchanged.
- The record is checked before anything is appended: operator/threshold/result
  and the chosen branch must agree, `resolvedParameters` must equal the recorded
  candidate branch, the Measurement value/recipe/status/unit must be unchanged
  and still verified, and the source Attempt/seed/checkpoint/prefix digest must
  match. A record that used an unverified Measurement is rejected.
- The target compile is rebuilt from `resolvedParameters` using the source
  compile's compiler version, engine versions, placements, module revisions,
  variables and overrides. It must reproduce the recorded `targetCompileId` and
  `targetCompileHash`; otherwise replay is rejected.
- A new branch Attempt/Segment is appended with `replayOf=<originalDecisionId>`.

Carrying the source compile's `variableSet`/`overrides` and identity inputs into
the target compile keeps two different base compiles (for example a
variable-driven flow and an equivalent literal flow) from collapsing into one
target compile id, and keeps earlier decision overrides auditable through a
chain.

This preserves Route → Attempt → Segment → Run → Compile provenance. It makes
no claim that the branch was executed inside one native process; the branch
Segment is a normal bounded run that starts from the reused prefix checkpoint.
