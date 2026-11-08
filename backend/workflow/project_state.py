"""Per-project workflow UI state (schema 11, extended by schema 13).

The document only remembers *which* compile, route, segment runs, measurements
and experiments the project's 03 page is currently showing. It deliberately
copies no compile/route content: those records stay the single source of truth
and the state must not become a second, stale copy of them.

``compileBasis`` is the one deliberate exception: an opaque, client-generated
fingerprint of the authored project as it was when the saved Compile was made.
It lets a restore decide whether the Compile still describes the project
without guessing from the Compile's expanded steps. The server stores it
verbatim and never interprets it; older states simply have no basis (``null``),
which the UI treats as "recompile required".

All identifiers are format-checked here and existence-checked by the store on
write. Reads never re-validate references, so a state whose target was removed
by GC still loads and the UI can turn the resulting 404 into an empty panel.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..models import StrictModel

MAX_SEGMENT_RUNS = 128
MAX_MEASUREMENT_IDS = 256
MAX_EXPERIMENT_IDS = 128
MAX_COMPILE_BASIS_CHARS = 128
MAX_STATE_JSON_BYTES = 64 * 1024

CompileId = Annotated[str, Field(pattern=r"^c_[a-f0-9]{24}$")]
RouteId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
SegmentId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
RunId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
MeasurementId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^m_[A-Za-z0-9_-]+$")]
ExperimentId = Annotated[str, Field(min_length=4, max_length=64, pattern=r"^exp_[A-Za-z0-9_-]+$")]
CompileBasis = Annotated[str, Field(max_length=MAX_COMPILE_BASIS_CHARS)]


class ProjectWorkflowSegmentRunV1(StrictModel):
    """One executed segment and the run/measurement it produced."""

    segmentId: SegmentId
    runId: RunId
    measurementId: MeasurementId | None = None


class ProjectWorkflowStateV1(StrictModel):
    """The 03 page state of one persisted project."""

    schemaVersion: Literal[1] = 1
    compileId: CompileId | None = None
    compileBasis: CompileBasis | None = None
    routeId: RouteId | None = None
    segmentRuns: list[ProjectWorkflowSegmentRunV1] = Field(
        default_factory=list, max_length=MAX_SEGMENT_RUNS
    )
    measurementIds: list[MeasurementId] = Field(
        default_factory=list, max_length=MAX_MEASUREMENT_IDS
    )
    experimentIds: list[ExperimentId] = Field(
        default_factory=list, max_length=MAX_EXPERIMENT_IDS
    )
    updatedAt: str | None = None

    def content(self) -> dict:
        """Return the state without the server-owned ``updatedAt`` stamp."""

        return self.model_dump(mode="json", exclude={"updatedAt"})
