"""Structured evidence for Measurement -> execution-segment binding."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..models import StrictModel

Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Id = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
VerificationStatus = Literal["verified", "failed"]


class ExecutionEvidenceV1(StrictModel):
    status: VerificationStatus
    segmentId: Id | None = None
    runId: Id | None = None
    sourceCompileId: str | None = None
    segmentProjectHash: Hash64 | None = None
    submittedProjectHash: Hash64 | None = None
    manifestRunId: str | None = None
    reasons: list[str] = Field(default_factory=list)


class TargetStepEvidenceV1(StrictModel):
    status: VerificationStatus
    stepId: Id | None = None
    stepIndex: int | None = None
    completed: bool = False
    snapshotFile: str | None = None
    snapshotHash: Hash64 | None = None
    stateId: Hash64 | None = None
    stateHash: Hash64 | None = None
    reasons: list[str] = Field(default_factory=list)


class SourceIdentityEvidenceV1(StrictModel):
    status: VerificationStatus
    measurementStateId: Hash64 | None = None
    manifestStateId: Hash64 | None = None
    recomputedSnapshotHash: Hash64 | None = None
    measurementSnapshotHash: Hash64 | None = None
    reasons: list[str] = Field(default_factory=list)


class EquivalenceEvidenceV1(StrictModel):
    status: Literal["not_evaluated", "established", "failed", "inconclusive"] = "not_evaluated"
    comparisonProtocolId: str | None = None
    metricScope: str | None = None
    resultDigest: Hash64 | None = None
    reasons: list[str] = Field(default_factory=list)


class ProvenanceVerificationV1(StrictModel):
    methodVersion: Annotated[str, Field(min_length=1, max_length=32)]
    verifiedAt: Annotated[str, Field(min_length=1, max_length=40)]
    execution: ExecutionEvidenceV1
    targetStep: TargetStepEvidenceV1
    sourceIdentity: SourceIdentityEvidenceV1
    equivalence: EquivalenceEvidenceV1 = Field(default_factory=EquivalenceEvidenceV1)

    @property
    def binding_verified(self) -> bool:
        return (
            self.execution.status == "verified"
            and self.targetStep.status == "verified"
            and self.sourceIdentity.status == "verified"
        )


class VerificationRecordV1(StrictModel):
    verificationId: Id
    measurementId: Id
    status: VerificationStatus
    methodVersion: str
    createdAt: str
    evidence: ProvenanceVerificationV1
