"""SQLite persistence for P0-2 route plans and execution branches."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .binding import verify_binding
from .hashing import full_project_hash
from .ids import sha256_hex
from .metrology import (
    MeasurementResultV1,
    MetrologyRecipeV1,
    MetricDefinitionV1,
    measure_snapshot,
)
from ..models import ProjectV7, parse_project
from .models import CompiledWorkflowV1
from .project_state import (
    MAX_STATE_JSON_BYTES,
    ProjectWorkflowStateV1,
)
from .modules import FlowDefinitionV2, ModuleV1, flow_definition_pins, module_step_pins
from .provenance import VerificationRecordV1
from .routes import (
    AttemptV1,
    CheckpointRefV1,
    RoutePlanV1,
    SegmentRefV1,
    SegmentReuseV1,
    SegmentV1,
    materialize_attempt,
)
from .verification import prefix_compatibility, verify_route_plan_compiles


SCHEMA_VERSION = "13"

# Reports are HTML documents, not build artifacts: they are small enough to keep
# in SQLite, but a single report may not grow into a blob and one project may not
# accumulate an unbounded history.
MAX_REPORT_HTML_BYTES = 2 * 1024 * 1024
MAX_REPORTS_PER_PROJECT = 50


def new_project_id() -> str:
    """Return a server-generated project identity."""

    return f"proj_{uuid.uuid4().hex[:24]}"


class WorkflowError(Exception):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowConflict(WorkflowError):
    pass


class WorkflowValidation(WorkflowError):
    pass


class WorkflowStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self, *, seed: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Read-only preflight: an unknown future schema must be rejected
            # before this process creates any tables in the database.
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as preflight:
                table = preflight.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                ).fetchone()
                if table is not None:
                    row = preflight.execute(
                        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    if row is not None and row[0] not in {
                        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", SCHEMA_VERSION,
                    }:
                        raise RuntimeError(
                            f"unsupported workflow schema version {row[0]!r}; "
                            f"expected {SCHEMA_VERSION!r}"
                        )
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS compiles (
                    compile_id TEXT PRIMARY KEY,
                    envelope_json TEXT NOT NULL,
                    envelope_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    head_attempt_id TEXT NOT NULL,
                    step_order_json TEXT NOT NULL,
                    legacy_unverified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
                    parent_attempt_id TEXT,
                    compile_id TEXT NOT NULL,
                    submission_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    base_checkpoint_json TEXT,
                    start_after_step_id TEXT,
                    through_step_id TEXT NOT NULL,
                    changed_step_ids_json TEXT NOT NULL,
                    prefix_compatibility_json TEXT,
                    seed_policy TEXT NOT NULL,
                    run_seed INTEGER NOT NULL,
                    flow_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    legacy_compatibility_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS segments (
                    segment_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                    parent_segment_id TEXT,
                    source_compile_id TEXT NOT NULL,
                    segment_project_hash TEXT NOT NULL,
                    start_after_step_id TEXT,
                    through_step_id TEXT NOT NULL,
                    start_checkpoint_json TEXT,
                    end_checkpoint_json TEXT,
                    run_id TEXT NOT NULL,
                    provenance_input_hash TEXT NOT NULL,
                    flow_digest TEXT NOT NULL,
                    execution_hash TEXT NOT NULL,
                    result_digest TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reuse (
                    reuse_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                    reused_from_segment_id TEXT NOT NULL,
                    from_step_id TEXT NOT NULL,
                    to_step_id TEXT NOT NULL,
                    compatibility_json TEXT NOT NULL,
                    legacy_compatibility_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    metric_id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metrology_recipes (
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    recipe_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (recipe_id, version)
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    measurement_id TEXT PRIMARY KEY,
                    metric_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    recipe_version INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    source_run_id TEXT,
                    source_step_id TEXT,
                    source_state_id TEXT,
                    source_snapshot_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value REAL,
                    unit TEXT NOT NULL,
                    reason TEXT,
                    intersections_json TEXT NOT NULL,
                    material TEXT NOT NULL,
                    region_json TEXT,
                    coordinate_system TEXT NOT NULL,
                    uncertainty REAL,
                    uncertainty_status TEXT NOT NULL,
                    binding_status TEXT NOT NULL DEFAULT 'unbound',
                    binding_evidence_json TEXT,
                    equivalence_status TEXT NOT NULL DEFAULT 'not_evaluated',
                    equivalence_evidence_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_route_idx ON attempts(route_id);
                CREATE INDEX IF NOT EXISTS segments_attempt_idx ON segments(attempt_id);
                CREATE INDEX IF NOT EXISTS reuse_attempt_idx ON reuse(attempt_id);
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calibration_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calibration_fits (
                    fit_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS layout_assets (
                    layout_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clips (
                    clip_id TEXT PRIMARY KEY,
                    layout_id TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_templates (
                    template_id TEXT PRIMARY KEY,
                    template_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS modules (
                    module_id TEXT PRIMARY KEY,
                    module_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS module_revisions (
                    module_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    module_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (module_id, revision)
                );
                CREATE TABLE IF NOT EXISTS library_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_definitions (
                    definition_id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_items (
                    item_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_workflow_state (
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_reports (
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    report_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    html TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, report_id)
                );
                CREATE INDEX IF NOT EXISTS workflow_reports_project_idx
                    ON workflow_reports(project_id, created_at);
                CREATE TABLE IF NOT EXISTS geometry_bindings (
                    binding_id TEXT PRIMARY KEY,
                    binding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comparison_protocols (
                    protocol_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    protocol_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (protocol_id, version)
                );
                CREATE TABLE IF NOT EXISTS equivalence_checks (
                    check_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    left_hash TEXT NOT NULL,
                    right_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_refs (
                    artifact_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    required_for TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, owner_type, owner_id, required_for)
                );
                CREATE TABLE IF NOT EXISTS deletion_records (
                    record_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_pins (
                    artifact_id TEXT PRIMARY KEY,
                    pinned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurement_verifications (
                    verification_id TEXT PRIMARY KEY,
                    measurement_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    method_version TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measurement_decisions (
                    decision_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS measurement_decisions_route_idx ON measurement_decisions(route_id);
                CREATE INDEX IF NOT EXISTS measurements_run_idx ON measurements(source_run_id);
                CREATE INDEX IF NOT EXISTS measurements_recipe_idx ON measurements(recipe_id, recipe_version);
                CREATE INDEX IF NOT EXISTS measurement_verifications_idx ON measurement_verifications(measurement_id);
                CREATE INDEX IF NOT EXISTS library_items_category_idx ON library_items(category);
                CREATE UNIQUE INDEX IF NOT EXISTS projects_name_idx ON projects(name);
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Databases written by schema 5 stored only the current module. Seed
            # the retained history from it so pins and expansion have a baseline.
            connection.execute(
                "INSERT OR IGNORE INTO module_revisions"
                " (module_id, revision, module_json, content_hash, created_at)"
                " SELECT module_id, revision, module_json, content_hash, created_at FROM modules"
            )
            route_columns = {row[1] for row in connection.execute("PRAGMA table_info(routes)")}
            if "legacy_unverified" not in route_columns:
                connection.execute("ALTER TABLE routes ADD COLUMN legacy_unverified INTEGER NOT NULL DEFAULT 0")
            attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            if "request_hash" not in attempt_columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
            if "legacy_compatibility_json" not in attempt_columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN legacy_compatibility_json TEXT")
            reuse_columns = {row[1] for row in connection.execute("PRAGMA table_info(reuse)")}
            if "legacy_compatibility_json" not in reuse_columns:
                connection.execute("ALTER TABLE reuse ADD COLUMN legacy_compatibility_json TEXT")
            measurement_columns = {row[1] for row in connection.execute("PRAGMA table_info(measurements)")}
            if "binding_status" not in measurement_columns:
                connection.execute("ALTER TABLE measurements ADD COLUMN binding_status TEXT NOT NULL DEFAULT 'unbound'")
            if "binding_evidence_json" not in measurement_columns:
                connection.execute("ALTER TABLE measurements ADD COLUMN binding_evidence_json TEXT")
            if "equivalence_status" not in measurement_columns:
                connection.execute("ALTER TABLE measurements ADD COLUMN equivalence_status TEXT NOT NULL DEFAULT 'not_evaluated'")
            if "equivalence_evidence_json" not in measurement_columns:
                connection.execute("ALTER TABLE measurements ADD COLUMN equivalence_evidence_json TEXT")

            deletion_columns = {row[1] for row in connection.execute("PRAGMA table_info(deletion_records)")}
            if "owner_type" not in deletion_columns:
                connection.execute("ALTER TABLE deletion_records ADD COLUMN owner_type TEXT NOT NULL DEFAULT ''")
            if "owner_id" not in deletion_columns:
                connection.execute("ALTER TABLE deletion_records ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            version_row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version_row is None:
                connection.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            elif version_row["value"] in {"1", "2"}:
                self._migrate_legacy_compatibility(connection)
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            elif version_row["value"] in {"3", "4", "5", "6", "7", "8", "9", "10", "11", "12"}:
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            elif version_row["value"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported workflow schema version {version_row['value']!r}; "
                    f"expected {SCHEMA_VERSION!r}"
                )
        if seed:
            # A brand-new install should not open the Library on four empty
            # categories. Seeding is idempotent and never touches a category
            # that already holds user entries.
            self.seed_library_defaults()

    def _migrate_legacy_compatibility(self, connection: sqlite3.Connection) -> None:
        """Repair v2 prefix summaries only after a full cross-compile check.

        A source and target can each be rebuilt from their own compile and still
        be incompatible with each other. The migrated RoutePlan is therefore
        assembled in memory and validated before any row is updated. If that
        fails, the original summaries are preserved and the whole route is
        marked legacy/unverified.
        """

        route_rows = connection.execute("SELECT * FROM routes").fetchall()
        for route in route_rows:
            route_id = route["route_id"]
            attempts = connection.execute(
                "SELECT * FROM attempts WHERE route_id = ?", (route_id,)
            ).fetchall()
            segments = connection.execute(
                "SELECT * FROM segments WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE route_id = ?)",
                (route_id,),
            ).fetchall()
            reuse = connection.execute(
                "SELECT * FROM reuse WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE route_id = ?)",
                (route_id,),
            ).fetchall()
            segments_by_id = {row["segment_id"]: row for row in segments}
            attempts_by_id = {row["attempt_id"]: row for row in attempts}
            compiles: dict[str, CompiledWorkflowV1] = {}
            for compile_id in {row["compile_id"] for row in attempts} | {row["source_compile_id"] for row in segments}:
                compile_row = connection.execute(
                    "SELECT envelope_json FROM compiles WHERE compile_id = ?",
                    (compile_id,),
                ).fetchone()
                if compile_row is None:
                    continue
                try:
                    compiles[compile_id] = CompiledWorkflowV1.model_validate(json.loads(compile_row["envelope_json"]))
                except (ValueError, TypeError, KeyError):
                    continue

            legacy = False

            def repair_attempt(row: sqlite3.Row) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                nonlocal legacy
                raw = row["prefix_compatibility_json"]
                if raw is None:
                    return None, None
                old = json.loads(raw)
                if "solverDigest" in old:
                    return old, old
                compiled = compiles.get(row["compile_id"])
                if compiled is not None:
                    try:
                        candidate = prefix_compatibility(compiled, old["stepIds"], row["run_seed"]).model_dump(mode="json", exclude_none=True)
                        if all(
                            old.get(key) == candidate.get(key)
                            for key in ("geometryBindingHash", "seedPolicy", "runSeed", "stepIds", "resolvedStepDigest")
                        ):
                            return candidate, old
                    except (ValueError, KeyError, TypeError):
                        pass
                legacy = True
                placeholder = dict(old)
                placeholder.setdefault("prefixDigest", "0" * 64)
                placeholder["solverDigest"] = "0" * 64
                return placeholder, old

            def repair_reuse(item: sqlite3.Row) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                nonlocal legacy
                raw = item["compatibility_json"]
                if raw is None:
                    return None, None
                old = json.loads(raw)
                if "solverDigest" in old:
                    return old, old
                source = segments_by_id.get(item["reused_from_segment_id"])
                source_attempt = None if source is None else attempts_by_id.get(source["attempt_id"])
                if source is not None and source_attempt is not None:
                    compiled = compiles.get(source["source_compile_id"])
                    if compiled is not None:
                        try:
                            candidate = prefix_compatibility(compiled, old["stepIds"], source_attempt["run_seed"]).model_dump(mode="json", exclude_none=True)
                            if all(
                                old.get(key) == candidate.get(key)
                                for key in ("geometryBindingHash", "seedPolicy", "runSeed", "stepIds", "resolvedStepDigest")
                            ):
                                return candidate, old
                        except (ValueError, KeyError, TypeError):
                            pass
                legacy = True
                placeholder = dict(old)
                placeholder.setdefault("prefixDigest", "0" * 64)
                placeholder["solverDigest"] = "0" * 64
                return placeholder, old

            attempt_candidates = {row["attempt_id"]: repair_attempt(row) for row in attempts}
            reuse_candidates = {item["reuse_id"]: repair_reuse(item) for item in reuse}

            def payload(*, legacy_flag: bool) -> dict[str, Any]:
                attempt_payloads = []
                for row in attempts:
                    item = self._attempt_payload(row)
                    candidate, original = attempt_candidates[row["attempt_id"]]
                    item["prefixCompatibility"] = candidate
                    item["legacyCompatibility"] = original if legacy_flag else None
                    attempt_payloads.append(item)
                reuse_payloads = []
                for item_row in reuse:
                    item = self._reuse_payload(item_row)
                    candidate, original = reuse_candidates[item_row["reuse_id"]]
                    item["compatibility"] = candidate
                    item["legacyCompatibility"] = original if legacy_flag else None
                    reuse_payloads.append(item)
                return {
                    "route": {
                        "routeId": route["route_id"],
                        "flowId": route["flow_id"],
                        "name": route["name"],
                        "headAttemptId": route["head_attempt_id"],
                        "legacyUnverified": legacy_flag,
                    },
                    "stepOrder": json.loads(route["step_order_json"]),
                    "attempts": attempt_payloads,
                    "segments": [self._segment_payload(row) for row in segments],
                    "reuse": reuse_payloads,
                }

            verified_payload = payload(legacy_flag=False)
            try:
                migrated = RoutePlanV1.model_validate(verified_payload)
                verify_route_plan_compiles(migrated, compiles)
            except (ValueError, KeyError, TypeError):
                legacy = True
            final_payload = payload(legacy_flag=legacy)
            try:
                RoutePlanV1.model_validate(final_payload)
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(f"legacy route {route_id!r} cannot be reconstructed safely: {exc}") from exc

            for row in attempts:
                candidate, original = attempt_candidates[row["attempt_id"]]
                connection.execute(
                    "UPDATE attempts SET prefix_compatibility_json = ?, legacy_compatibility_json = ? WHERE attempt_id = ?",
                    (
                        None if candidate is None else self._dump(candidate),
                        None if not legacy or original is None else self._dump(original),
                        row["attempt_id"],
                    ),
                )
            for item in reuse:
                candidate, original = reuse_candidates[item["reuse_id"]]
                connection.execute(
                    "UPDATE reuse SET compatibility_json = ?, legacy_compatibility_json = ? WHERE reuse_id = ?",
                    (
                        None if candidate is None else self._dump(candidate),
                        None if not legacy or original is None else self._dump(original),
                        item["reuse_id"],
                    ),
                )
            connection.execute(
                "UPDATE routes SET legacy_unverified = ? WHERE route_id = ?",
                (1 if legacy else 0, route_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _load(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    @staticmethod
    def _attempt_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attemptId": row["attempt_id"],
            "routeId": row["route_id"],
            "parentAttemptId": row["parent_attempt_id"],
            "compileId": row["compile_id"],
            "submissionKey": row["submission_key"],
            "baseCheckpointRef": WorkflowStore._load(row["base_checkpoint_json"]),
            "startAfterStepId": row["start_after_step_id"],
            "throughStepId": row["through_step_id"],
            "changedStepIds": WorkflowStore._load(row["changed_step_ids_json"]),
            "prefixCompatibility": WorkflowStore._load(row["prefix_compatibility_json"]),
            "seedPolicy": row["seed_policy"],
            "runSeed": row["run_seed"],
            "flowDigest": row["flow_digest"],
            "status": row["status"],
            "legacyCompatibility": WorkflowStore._load(row["legacy_compatibility_json"]),
        }

    @staticmethod
    def _segment_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "segmentId": row["segment_id"],
            "attemptId": row["attempt_id"],
            "parentSegmentId": row["parent_segment_id"],
            "sourceCompileId": row["source_compile_id"],
            "segmentProjectHash": row["segment_project_hash"],
            "startAfterStepId": row["start_after_step_id"],
            "throughStepId": row["through_step_id"],
            "startCheckpointRef": WorkflowStore._load(row["start_checkpoint_json"]),
            "endCheckpointRef": WorkflowStore._load(row["end_checkpoint_json"]),
            "runId": row["run_id"],
            "provenanceInputHash": row["provenance_input_hash"],
            "flowDigest": row["flow_digest"],
            "executionHash": row["execution_hash"],
            "resultDigest": row["result_digest"],
            "status": row["status"],
        }

    @staticmethod
    def _reuse_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reuseId": row["reuse_id"],
            "attemptId": row["attempt_id"],
            "reusedFromSegmentId": row["reused_from_segment_id"],
            "fromStepId": row["from_step_id"],
            "toStepId": row["to_step_id"],
            "compatibility": WorkflowStore._load(row["compatibility_json"]),
            "legacyCompatibility": WorkflowStore._load(row["legacy_compatibility_json"]),
        }

    def register_compile(self, compiled: CompiledWorkflowV1) -> CompiledWorkflowV1:
        compiled = compiled.verify()
        payload = compiled.model_dump(mode="json", exclude_none=True)
        envelope_hash = sha256_hex(payload)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT envelope_hash FROM compiles WHERE compile_id = ?",
                    (compiled.compileRecord.compileId,),
                ).fetchone()
                if existing is not None:
                    if existing["envelope_hash"] != envelope_hash:
                        raise WorkflowConflict("compileId already exists with different content")
                    return compiled
                connection.execute(
                    "INSERT INTO compiles (compile_id, envelope_json, envelope_hash, created_at) VALUES (?, ?, ?, ?)",
                    (
                        compiled.compileRecord.compileId,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                        envelope_hash,
                        self._now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict(f"compile record conflicts with an existing object: {exc}") from exc
        return compiled

    def get_compile(self, compile_id: str) -> CompiledWorkflowV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM compiles WHERE compile_id = ?",
                (compile_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"compile {compile_id!r} does not exist")
        try:
            return CompiledWorkflowV1.model_validate(json.loads(row["envelope_json"]))
        except (ValueError, TypeError) as exc:
            raise WorkflowValidation(f"stored compile {compile_id!r} is invalid") from exc

    def list_compiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT compile_id, envelope_json, created_at FROM compiles ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        result = []
        for row in rows:
            try:
                compiled = CompiledWorkflowV1.model_validate(json.loads(row["envelope_json"]))
            except (ValueError, TypeError):
                continue
            result.append(
                {
                    "compileId": compiled.compileRecord.compileId,
                    "flowRevisionId": compiled.compileRecord.flowRevisionId,
                    "fullProjectHash": compiled.compileRecord.fullProjectHash,
                    "geometryBindingHash": compiled.compileRecord.geometryBindingHash,
                    "stepCount": len(compiled.project.steps),
                    "createdAt": row["created_at"],
                }
            )
        return result

    def _compile_map(self, plan: RoutePlanV1) -> dict[str, CompiledWorkflowV1]:
        ids = {attempt.compileId for attempt in plan.attempts}
        ids.update(segment.sourceCompileId for segment in plan.segments)
        return {compile_id: self.get_compile(compile_id) for compile_id in ids}

    def _verify_plan_compiles(self, plan: RoutePlanV1) -> None:
        try:
            verify_route_plan_compiles(plan, self._compile_map(plan))
        except WorkflowNotFound as exc:
            raise WorkflowValidation(str(exc)) from exc
        except WorkflowError:
            raise
        except (ValueError, KeyError) as exc:
            raise WorkflowValidation(str(exc)) from exc

    def schema_version(self) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return "" if row is None else str(row["value"])

    def register_metric(self, metric: MetricDefinitionV1) -> MetricDefinitionV1:
        payload = metric.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT definition_json FROM metrics WHERE metric_id = ?",
                (metric.metricId,),
            ).fetchone()
            if existing is not None:
                if sha256_hex(json.loads(existing["definition_json"])) != digest:
                    raise WorkflowConflict("metricId already exists with different content")
                return metric
            connection.execute(
                "INSERT INTO metrics (metric_id, definition_json, created_at) VALUES (?, ?, ?)",
                (metric.metricId, self._dump(payload), self._now()),
            )
        return metric

    def get_metric(self, metric_id: str) -> MetricDefinitionV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM metrics WHERE metric_id = ?",
                (metric_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"metric {metric_id!r} does not exist")
        return MetricDefinitionV1.model_validate(json.loads(row["definition_json"]))

    def register_recipe(self, recipe: MetrologyRecipeV1) -> MetrologyRecipeV1:
        payload = recipe.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT recipe_json FROM metrology_recipes WHERE recipe_id = ? AND version = ?",
                (recipe.recipeId, recipe.version),
            ).fetchone()
            if existing is not None:
                if sha256_hex(json.loads(existing["recipe_json"])) != digest:
                    raise WorkflowConflict("recipeId/version already exists with different content")
                return recipe
            connection.execute(
                "INSERT INTO metrology_recipes (recipe_id, version, recipe_json, created_at) VALUES (?, ?, ?, ?)",
                (recipe.recipeId, recipe.version, self._dump(payload), self._now()),
            )
        return recipe

    def get_recipe(self, recipe_id: str, version: int | None = None) -> MetrologyRecipeV1:
        with self._connect() as connection:
            if version is None:
                row = connection.execute(
                    "SELECT recipe_json FROM metrology_recipes WHERE recipe_id = ? ORDER BY version DESC LIMIT 1",
                    (recipe_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT recipe_json FROM metrology_recipes WHERE recipe_id = ? AND version = ?",
                    (recipe_id, version),
                ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"metrology recipe {recipe_id!r} does not exist")
        return MetrologyRecipeV1.model_validate(json.loads(row["recipe_json"]))

    def list_recipes(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT recipe_id, version, recipe_json, created_at FROM metrology_recipes ORDER BY recipe_id, version DESC"
            ).fetchall()
        result = []
        for row in rows:
            recipe = MetrologyRecipeV1.model_validate(json.loads(row["recipe_json"]))
            result.append(
                {
                    "recipeId": recipe.recipeId,
                    "version": recipe.version,
                    "metricId": recipe.metricId,
                    "method": recipe.method,
                    "name": recipe.name,
                    "createdAt": row["created_at"],
                }
            )
        return result

    @staticmethod
    def _measurement_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "measurementId": row["measurement_id"],
            "metricId": row["metric_id"],
            "recipeId": row["recipe_id"],
            "recipeVersion": row["recipe_version"],
            "method": row["method"],
            "algorithmVersion": row["algorithm_version"],
            "sourceRunId": row["source_run_id"],
            "sourceStepId": row["source_step_id"],
            "sourceStateId": row["source_state_id"],
            "sourceSnapshotHash": row["source_snapshot_hash"],
            "status": row["status"],
            "value": row["value"],
            "unit": row["unit"],
            "reason": row["reason"],
            "intersections": json.loads(row["intersections_json"]),
            "material": row["material"],
            "region": None if row["region_json"] is None else json.loads(row["region_json"]),
            "coordinateSystem": row["coordinate_system"],
            "uncertainty": row["uncertainty"],
            "uncertaintyStatus": row["uncertainty_status"],
            "bindingStatus": row["binding_status"],
            "bindingEvidence": None if row["binding_evidence_json"] is None else json.loads(row["binding_evidence_json"]),
            "equivalenceStatus": row["equivalence_status"],
            "equivalenceEvidence": None if row["equivalence_evidence_json"] is None else json.loads(row["equivalence_evidence_json"]),
            "createdAt": row["created_at"],
        }

    def record_measurement(self, result: MeasurementResultV1) -> MeasurementResultV1:
        payload = result.model_dump(mode="json", exclude_none=True)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO measurements (
                        measurement_id, metric_id, recipe_id, recipe_version, method,
                        algorithm_version, source_run_id, source_step_id, source_state_id,
                        source_snapshot_hash, status, value, unit, reason,
                        intersections_json, material, region_json, coordinate_system,
                        uncertainty, uncertainty_status, binding_status, binding_evidence_json,
                        equivalence_status, equivalence_evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.measurementId, result.metricId, result.recipeId, result.recipeVersion,
                        result.method, result.algorithmVersion, result.sourceRunId, result.sourceStepId,
                        result.sourceStateId, result.sourceSnapshotHash, result.status, result.value,
                        result.unit, result.reason, self._dump(result.intersections), result.material,
                        None if result.region is None else self._dump(result.region.model_dump(mode="json")),
                        result.coordinateSystem, result.uncertainty, result.uncertaintyStatus,
                        result.bindingStatus,
                        None if result.bindingEvidence is None else self._dump(result.bindingEvidence.model_dump(mode="json", exclude_none=True)),
                        result.equivalenceStatus,
                        None if result.equivalenceEvidence is None else self._dump(result.equivalenceEvidence.model_dump(mode="json", exclude_none=True)),
                        result.createdAt or self._now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict(f"measurement conflicts with an existing record: {exc}") from exc
        return self.get_measurement(result.measurementId)

    def get_measurement(self, measurement_id: str) -> MeasurementResultV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM measurements WHERE measurement_id = ?",
                (measurement_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"measurement {measurement_id!r} does not exist")
        return MeasurementResultV1.model_validate(self._measurement_payload(row))

    def list_measurements(
        self,
        *,
        run_id: str | None = None,
        recipe_id: str | None = None,
        binding_status: str | None = None,
    ) -> list[MeasurementResultV1]:
        clauses = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("source_run_id = ?")
            params.append(run_id)
        if recipe_id is not None:
            clauses.append("recipe_id = ?")
            params.append(recipe_id)
        if binding_status is not None:
            clauses.append("binding_status = ?")
            params.append(binding_status)
        query = "SELECT * FROM measurements"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, rowid DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [MeasurementResultV1.model_validate(self._measurement_payload(row)) for row in rows]

    def measure_and_record(
        self,
        recipe: MetrologyRecipeV1,
        metric: MetricDefinitionV1,
        snapshot: dict[str, Any],
        *,
        source_run_id: str | None = None,
        source_step_id: str | None = None,
        source_state_id: str | None = None,
    ) -> MeasurementResultV1:
        result = measure_snapshot(
            recipe,
            metric,
            snapshot,
            source_run_id=source_run_id,
            source_step_id=source_step_id,
            source_state_id=source_state_id,
        )
        return self.record_measurement(result)

    def measure_run(self, recipe: MetrologyRecipeV1, metric: MetricDefinitionV1, run_id: str, step_id: str) -> MeasurementResultV1:
        from .. import snapshots, storage

        try:
            run_dir = storage.run_path(run_id)
            manifest = storage.read_json(run_dir / "manifest.json")
            row = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == step_id), None)
            if row is None:
                raise WorkflowNotFound(f"snapshot {step_id!r} does not exist in run {run_id!r}")
            snapshot_path = snapshots.ensure_materialized(run_dir, step_id)
            snapshot = storage.read_json(snapshot_path)
            state_id = (row.get("nativeState") or {}).get("stateId")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise WorkflowNotFound(f"historical snapshot is unavailable: {exc}") from exc
        return self.measure_and_record(
            recipe,
            metric,
            snapshot,
            source_run_id=run_id,
            source_step_id=step_id,
            source_state_id=state_id,
        )

    @staticmethod
    def _verification_payload(row: sqlite3.Row) -> dict[str, Any]:
        evidence = json.loads(row["evidence_json"])
        return {
            "verificationId": row["verification_id"],
            "measurementId": row["measurement_id"],
            "status": row["status"],
            "methodVersion": row["method_version"],
            "createdAt": row["created_at"],
            "evidence": evidence,
        }

    def list_verifications(self, measurement_id: str) -> list[VerificationRecordV1]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM measurement_verifications WHERE measurement_id = ? ORDER BY rowid",
                (measurement_id,),
            ).fetchall()
        return [VerificationRecordV1.model_validate(self._verification_payload(row)) for row in rows]

    def _record_verification(self, measurement_id: str, evidence, now: str) -> VerificationRecordV1:
        verification_id = f"v_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO measurement_verifications (
                    verification_id, measurement_id, status, method_version, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    verification_id,
                    measurement_id,
                    "verified" if evidence.binding_verified else "failed",
                    evidence.methodVersion,
                    self._dump(evidence.model_dump(mode="json", exclude_none=True)),
                    now,
                ),
            )
        return VerificationRecordV1(
            verificationId=verification_id,
            measurementId=measurement_id,
            status="verified" if evidence.binding_verified else "failed",
            methodVersion=evidence.methodVersion,
            createdAt=now,
            evidence=evidence,
        )

    def bind_measurement(self, measurement_id: str, segment_id: str, run_id: str, step_id: str) -> MeasurementResultV1:
        """Recompute execution/step/source evidence; keep failures explicit."""

        from .. import snapshots, storage

        measurement = self.get_measurement(measurement_id)
        segment = self.get_segment(segment_id)
        attempt = self.get_attempt(segment.attemptId)
        plan = self.get_plan(attempt.routeId)
        compile_record = self.get_compile(segment.sourceCompileId)

        manifest: dict[str, Any] | None = None
        snapshot_row: dict[str, Any] | None = None
        snapshot: dict[str, Any] | None = None
        state_hash: str | None = None
        submitted_project_hash: str | None = None
        submitted_project = None
        try:
            run_dir = storage.run_path(run_id)
            manifest = storage.read_json(run_dir / "manifest.json")
            snapshot_row = next(
                (item for item in manifest.get("snapshots", []) if item.get("stepId") == step_id),
                None,
            )
            if snapshot_row is not None:
                snapshot_path = snapshots.ensure_materialized(run_dir, step_id)
                snapshot = storage.read_json(snapshot_path)
                state = snapshot_row.get("nativeState") or {}
                if state:
                    state_path = storage.resolve_domain_state(state)
                    state_hash = storage.file_sha256(state_path)
            try:
                submitted_project = parse_project(manifest["normalizedProject"])
                submitted_project_hash = full_project_hash(submitted_project)
            except (KeyError, ValueError, TypeError):
                submitted_project = None
                submitted_project_hash = None
        except (FileNotFoundError, OSError, ValueError):
            pass

        evidence = verify_binding(
            measurement=measurement,
            plan=plan,
            attempt=attempt,
            segment=segment,
            compile_record=compile_record,
            manifest=manifest,
            snapshot_row=snapshot_row,
            snapshot=snapshot,
            state_hash=state_hash,
            submitted_project=submitted_project,
            submitted_project_hash=submitted_project_hash,
            run_id=run_id,
            step_id=step_id,
        )
        now = self._now()
        self._record_verification(measurement_id, evidence, now)
        status = "verified" if evidence.binding_verified else "failed"
        update_cache = True
        if status == "failed" and measurement.bindingStatus == "verified":
            current = measurement.bindingEvidence
            same_binding = (
                current is not None
                and current.execution.segmentId == segment_id
                and current.execution.runId == run_id
                and current.targetStep.stepId == step_id
            )
            update_cache = same_binding
        if update_cache:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE measurements SET binding_status = ?, binding_evidence_json = ? WHERE measurement_id = ?",
                    (status, self._dump(evidence.model_dump(mode="json", exclude_none=True)), measurement_id),
                )
        return self.get_measurement(measurement_id)

    def create_plan(self, plan: RoutePlanV1) -> RoutePlanV1:
        self._verify_plan_compiles(plan)
        now = self._now()
        payload = plan.model_dump(mode="json", exclude_none=True)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO routes (route_id, flow_id, name, head_attempt_id, step_order_json, legacy_unverified, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (payload["route"]["routeId"], payload["route"]["flowId"], payload["route"]["name"],
                     payload["route"]["headAttemptId"], self._dump(payload["stepOrder"]),
                     1 if payload["route"].get("legacyUnverified", False) else 0, now, now),
                )
                self._insert_attempts(connection, payload["attempts"], now)
                self._insert_segments(connection, payload["segments"], now)
                self._insert_reuse(connection, payload["reuse"], now)
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict(f"route plan conflicts with an existing workflow object: {exc}") from exc
        return self.get_plan(payload["route"]["routeId"])

    def _insert_attempts(
        self,
        connection: sqlite3.Connection,
        attempts: list[dict[str, Any]],
        now: str,
        request_hashes: dict[str, str] | None = None,
    ) -> None:
        request_hashes = request_hashes or {}
        for attempt in attempts:
            request_hash = request_hashes.get(attempt["attemptId"], sha256_hex(attempt))
            connection.execute(
                """
                INSERT INTO attempts (
                    attempt_id, route_id, parent_attempt_id, compile_id, submission_key,
                    request_hash, base_checkpoint_json, start_after_step_id, through_step_id,
                    changed_step_ids_json, prefix_compatibility_json, seed_policy,
                    run_seed, flow_digest, status, legacy_compatibility_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt["attemptId"], attempt["routeId"], attempt.get("parentAttemptId"),
                    attempt["compileId"], attempt["submissionKey"], request_hash,
                    self._checkpoint_value(attempt.get("baseCheckpointRef")),
                    attempt.get("startAfterStepId"), attempt["throughStepId"],
                    self._dump(attempt.get("changedStepIds", [])),
                    self._dump(attempt["prefixCompatibility"]) if attempt.get("prefixCompatibility") is not None else None,
                    attempt["seedPolicy"], attempt["runSeed"], attempt["flowDigest"],
                    attempt["status"], self._dump(attempt["legacyCompatibility"]) if attempt.get("legacyCompatibility") is not None else None,
                    now, now,
                ),
            )

    def _insert_segments(self, connection: sqlite3.Connection, segments: list[dict[str, Any]], now: str) -> None:
        for segment in segments:
            connection.execute(
                """
                INSERT INTO segments (
                    segment_id, attempt_id, parent_segment_id, source_compile_id,
                    segment_project_hash, start_after_step_id, through_step_id,
                    start_checkpoint_json, end_checkpoint_json, run_id,
                    provenance_input_hash, flow_digest, execution_hash,
                    result_digest, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment["segmentId"], segment["attemptId"], segment.get("parentSegmentId"),
                    segment["sourceCompileId"], segment["segmentProjectHash"],
                    segment.get("startAfterStepId"), segment["throughStepId"],
                    self._checkpoint_value(segment.get("startCheckpointRef")),
                    self._checkpoint_value(segment.get("endCheckpointRef")),
                    segment["runId"], segment["provenanceInputHash"],
                    segment["flowDigest"], segment["executionHash"],
                    segment.get("resultDigest"), segment["status"], now, now,
                ),
            )

    def _insert_reuse(self, connection: sqlite3.Connection, reuse: list[dict[str, Any]], now: str) -> None:
        for item in reuse:
            connection.execute(
                """
                INSERT INTO reuse (
                    reuse_id, attempt_id, reused_from_segment_id,
                    from_step_id, to_step_id, compatibility_json, legacy_compatibility_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["reuseId"], item["attemptId"], item["reusedFromSegmentId"],
                    item["fromStepId"], item["toStepId"],
                    self._dump(item["compatibility"]),
                    self._dump(item["legacyCompatibility"]) if item.get("legacyCompatibility") is not None else None,
                    now,
                ),
            )

    @staticmethod
    def _checkpoint_value(value: Any) -> str | None:
        return None if value is None else WorkflowStore._dump(value)

    def list_plans(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.route_id, r.flow_id, r.name, r.head_attempt_id,
                       r.step_order_json, r.legacy_unverified, r.updated_at,
                       (SELECT COUNT(*) FROM attempts a WHERE a.route_id = r.route_id) AS attempt_count
                FROM routes r ORDER BY r.updated_at DESC, r.rowid DESC
                """
            ).fetchall()
        return [
            {
                "routeId": row["route_id"],
                "flowId": row["flow_id"],
                "name": row["name"],
                "headAttemptId": row["head_attempt_id"],
                "stepCount": len(self._load(row["step_order_json"])),
                "attemptCount": row["attempt_count"],
                "legacyUnverified": bool(row["legacy_unverified"]),
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def get_plan(self, route_id: str) -> RoutePlanV1:
        with self._connect() as connection:
            route = connection.execute("SELECT * FROM routes WHERE route_id = ?", (route_id,)).fetchone()
            if route is None:
                raise WorkflowNotFound(f"route {route_id!r} does not exist")
            attempts = connection.execute(
                "SELECT * FROM attempts WHERE route_id = ? ORDER BY created_at, attempt_id",
                (route_id,),
            ).fetchall()
            segments = connection.execute(
                "SELECT * FROM segments WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE route_id = ?) ORDER BY created_at, segment_id",
                (route_id,),
            ).fetchall()
            reuse = connection.execute(
                "SELECT * FROM reuse WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE route_id = ?) ORDER BY created_at, reuse_id",
                (route_id,),
            ).fetchall()
        return RoutePlanV1.model_validate(
            {
                "route": {
                    "routeId": route["route_id"],
                    "flowId": route["flow_id"],
                    "name": route["name"],
                    "headAttemptId": route["head_attempt_id"],
                    "legacyUnverified": bool(route["legacy_unverified"]),
                },
                "stepOrder": self._load(route["step_order_json"]),
                "attempts": [self._attempt_payload(row) for row in attempts],
                "segments": [self._segment_payload(row) for row in segments],
                "reuse": [self._reuse_payload(row) for row in reuse],
            }
        )

    def find_attempt_by_submission_key(self, submission_key: str) -> AttemptV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE submission_key = ?",
                (submission_key,),
            ).fetchone()
        return None if row is None else AttemptV1.model_validate(self._attempt_payload(row))

    def get_attempt_request_hash(self, attempt_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"attempt {attempt_id!r} does not exist")
        return str(row["request_hash"])

    def append_attempt(
        self,
        route_id: str,
        attempt: AttemptV1,
        segments: list[SegmentV1],
        reuse: list[SegmentReuseV1],
    ) -> RoutePlanV1:
        request_hash = sha256_hex(
            {
                "attempt": attempt.model_dump(mode="json", exclude_none=True),
                "segments": [segment.model_dump(mode="json", exclude_none=True) for segment in segments],
                "reuse": [item.model_dump(mode="json", exclude_none=True) for item in reuse],
            }
        )
        existing = self.find_attempt_by_submission_key(attempt.submissionKey)
        if existing is not None:
            if existing.routeId != route_id or existing.attemptId != attempt.attemptId:
                raise WorkflowConflict("submissionKey already belongs to a different attempt")
            if self.get_attempt_request_hash(existing.attemptId) != request_hash:
                raise WorkflowConflict("submissionKey already exists with different request content")
            return self.get_plan(route_id)

        if any(segment.attemptId != attempt.attemptId for segment in segments):
            raise WorkflowConflict("new segments must belong to the new attempt")
        if any(item.attemptId != attempt.attemptId for item in reuse):
            raise WorkflowConflict("reuse entries must belong to the new attempt")

        plan = self.get_plan(route_id)
        payload = plan.model_dump(mode="json", exclude_none=True)
        payload["route"]["headAttemptId"] = attempt.attemptId
        payload["attempts"].append(attempt.model_dump(mode="json", exclude_none=True))
        payload["segments"].extend(segment.model_dump(mode="json", exclude_none=True) for segment in segments)
        payload["reuse"].extend(item.model_dump(mode="json", exclude_none=True) for item in reuse)
        updated = RoutePlanV1.model_validate(payload)
        self._verify_plan_compiles(updated)

        now = self._now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE routes SET head_attempt_id = ?, updated_at = ? WHERE route_id = ?",
                    (attempt.attemptId, now, route_id),
                )
                self._insert_attempts(
                    connection,
                    [attempt.model_dump(mode="json", exclude_none=True)],
                    now,
                    request_hashes={attempt.attemptId: request_hash},
                )
                self._insert_segments(connection, [segment.model_dump(mode="json", exclude_none=True) for segment in segments], now)
                self._insert_reuse(connection, [item.model_dump(mode="json", exclude_none=True) for item in reuse], now)
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict(f"attempt conflicts with an existing workflow object: {exc}") from exc
        return updated

    def get_attempt(self, attempt_id: str) -> AttemptV1:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"attempt {attempt_id!r} does not exist")
        return AttemptV1.model_validate(self._attempt_payload(row))

    def get_attempt_detail(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.get_attempt(attempt_id)
        with self._connect() as connection:
            segments = connection.execute(
                "SELECT * FROM segments WHERE attempt_id = ? ORDER BY created_at, segment_id",
                (attempt_id,),
            ).fetchall()
            reuse = connection.execute(
                "SELECT * FROM reuse WHERE attempt_id = ? ORDER BY created_at, reuse_id",
                (attempt_id,),
            ).fetchall()
        return {
            "attempt": attempt,
            "segments": [SegmentV1.model_validate(self._segment_payload(row)) for row in segments],
            "reuse": [SegmentReuseV1.model_validate(self._reuse_payload(row)) for row in reuse],
        }

    def _reconcile_segment(self, segment_id: str) -> None:
        from .. import snapshots, storage

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE segment_id = ?", (segment_id,)
            ).fetchone()
        if row is None or row["status"] != "running" or not row["run_id"]:
            return
        try:
            run_dir = storage.run_path(row["run_id"])
            manifest = storage.read_json(run_dir / "manifest.json")
        except (FileNotFoundError, OSError, ValueError):
            return
        status = manifest.get("status")
        if status not in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
            return
        end_checkpoint = None
        snapshot = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == row["through_step_id"]), None)
        state = None if snapshot is None else snapshot.get("nativeState")
        if isinstance(state, dict) and state.get("stateId"):
            end_checkpoint = CheckpointRefV1(
                checkpointId=f"cp_{row['run_id']}_{row['through_step_id']}",
                runId=row["run_id"],
                stepId=row["through_step_id"],
                stateHash=state["stateId"],
            )
        terminal = "failed" if status == "timed_out" else status
        if terminal == "succeeded" and end_checkpoint is None:
            terminal = "failed"
        from .metrology import snapshot_content_hash

        result_digest = None
        try:
            snapshot_path = snapshots.ensure_materialized(run_dir, row["through_step_id"])
            result_digest = snapshot_content_hash(storage.read_json(snapshot_path))
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            pass
        self.finish_segment(
            segment_id,
            terminal,
            end_checkpoint,
            provenance_input_hash=manifest.get("requestedInputHash"),
            execution_hash=manifest.get("executionHash"),
            result_digest=result_digest,
        )

    def get_segment(self, segment_id: str) -> SegmentV1:
        self._reconcile_segment(segment_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM segments WHERE segment_id = ?", (segment_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"segment {segment_id!r} does not exist")
        return SegmentV1.model_validate(self._segment_payload(row))

    def get_segment_by_run_id(self, run_id: str) -> SegmentV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE run_id = ? ORDER BY rowid DESC LIMIT 1", (run_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"segment for run {run_id!r} does not exist")
        return SegmentV1.model_validate(self._segment_payload(row))

    def update_attempt_status(self, attempt_id: str, status: str) -> AttemptV1:
        attempt = self.get_attempt(attempt_id)
        allowed = {
            "planned": {"running", "cancelled", "failed", "interrupted"},
            "running": {"succeeded", "failed", "cancelled", "interrupted"},
        }
        if status not in allowed.get(attempt.status, set()):
            raise WorkflowConflict(f"attempt {attempt_id!r} cannot move from {attempt.status!r} to {status!r}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE attempts SET status = ?, updated_at = ? WHERE attempt_id = ?",
                (status, self._now(), attempt_id),
            )
        return self.get_attempt(attempt_id)

    def update_segment_status(self, segment_id: str, status: str) -> SegmentV1:
        segment = self.get_segment(segment_id)
        allowed = {
            "planned": {"running", "cancelled", "failed", "interrupted"},
            "running": {"succeeded", "failed", "cancelled", "interrupted"},
        }
        if status not in allowed.get(segment.status, set()):
            raise WorkflowConflict(f"segment {segment_id!r} cannot move from {segment.status!r} to {status!r}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE segments SET status = ?, updated_at = ? WHERE segment_id = ?",
                (status, self._now(), segment_id),
            )
        return self.get_segment(segment_id)

    def materialize(self, route_id: str, attempt_id: str) -> list[SegmentRefV1]:
        plan = self.get_plan(route_id)
        return materialize_attempt(plan, attempt_id)

    def save_measurement_decision(self, decision: Any) -> Any:
        """Persist one immutable measurement decision record.

        The document is stored as JSON next to its route/created-at columns so a
        future schema can still read the row and report why it cannot decode it.
        """

        from .decisions import (
            MeasurementDecisionV1,
            seal_measurement_decision,
            verify_measurement_decision,
        )

        record = (
            decision
            if isinstance(decision, MeasurementDecisionV1)
            else MeasurementDecisionV1.model_validate(decision)
        )
        if record.recordHash is None:
            record = seal_measurement_decision(record)
        else:
            verify_measurement_decision(record)
        self.get_plan(record.routeId)
        payload = record.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT document_json FROM measurement_decisions WHERE decision_id = ?",
                    (record.decisionId,),
                ).fetchone()
                if existing is not None:
                    if sha256_hex(json.loads(existing["document_json"])) != digest:
                        raise WorkflowConflict(
                            "decisionId already exists with different content"
                        )
                    return record
                connection.execute(
                    "INSERT INTO measurement_decisions (decision_id, route_id, document_json, created_at) VALUES (?, ?, ?, ?)",
                    (record.decisionId, record.routeId, self._dump(payload), record.createdAt),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowConflict(
                f"measurement decision conflicts with an existing object: {exc}"
            ) from exc
        return record

    def get_measurement_decision(
        self, decision_id: str, *, route_id: str | None = None, verify: bool = True
    ) -> Any:
        from .decisions import MeasurementDecisionV1, verify_measurement_decision

        with self._connect() as connection:
            row = connection.execute(
                "SELECT route_id, document_json FROM measurement_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"measurement decision {decision_id!r} does not exist")
        if route_id is not None and row["route_id"] != route_id:
            raise WorkflowNotFound(
                f"measurement decision {decision_id!r} does not belong to route {route_id!r}"
            )
        record = MeasurementDecisionV1.model_validate(json.loads(row["document_json"]))
        if verify and record.recordHash is not None:
            verify_measurement_decision(record)
        return record

    def list_measurement_decisions(self, route_id: str) -> list[Any]:
        from .decisions import MeasurementDecisionV1, verify_measurement_decision

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_json FROM measurement_decisions WHERE route_id = ? "
                "ORDER BY created_at, rowid",
                (route_id,),
            ).fetchall()
        decisions: list[Any] = []
        for row in rows:
            record = MeasurementDecisionV1.model_validate(
                json.loads(row["document_json"])
            )
            if record.recordHash is not None:
                verify_measurement_decision(record)
            decisions.append(record)
        return decisions

    def workflow_run_route_ids(self, workflow_run_id: str) -> set[str]:
        """Return the routes that already recorded this workflow run id.

        Workflow runs are a grouping of decision records, so the check scans the
        small decision table instead of introducing a second identity store.
        """

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT route_id, document_json FROM measurement_decisions"
            ).fetchall()
        routes: set[str] = set()
        for row in rows:
            try:
                document = json.loads(row["document_json"])
            except ValueError:
                continue
            if document.get("workflowRunId") == workflow_run_id:
                routes.add(row["route_id"])
        return routes

    def save_experiment(self, experiment_id: str, document: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO experiments (experiment_id, document_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(experiment_id) DO UPDATE SET document_json = excluded.document_json, updated_at = excluded.updated_at",
                (experiment_id, self._dump(document), self._now()),
            )
        return document

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT document_json FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"experiment {experiment_id!r} does not exist")
        return json.loads(row["document_json"])

    def delete_experiment(self, experiment_id: str) -> None:
        """Delete one experiment document, never its runs or measurements.

        A running/queued experiment is refused: deleting its document while the
        worker still updates it would resurrect a half-written record.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound(f"experiment {experiment_id!r} does not exist")
            document = json.loads(row["document_json"])
            status = str(document.get("status") or "")
            running = any(
                str(replicate.get("status") or "") in {"running", "queued"}
                for variant in document.get("variants") or []
                for replicate in variant.get("replicates") or []
            )
            if status in {"running", "queued"} or running:
                raise WorkflowConflict("实验运行中，先取消")
            connection.execute(
                "DELETE FROM experiments WHERE experiment_id = ?", (experiment_id,)
            )

    def list_experiment_documents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_json FROM experiments ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [json.loads(row["document_json"]) for row in rows]

    def list_experiments(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT document_json, updated_at FROM experiments ORDER BY updated_at DESC, rowid DESC").fetchall()
        return [
            {"experimentId": json.loads(row["document_json"])["experimentId"],
             "name": json.loads(row["document_json"]).get("name"),
             "updatedAt": row["updated_at"]}
            for row in rows
        ]

    def save_calibration_dataset(self, document: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO calibration_datasets (dataset_id, document_json, created_at) VALUES (?, ?, ?)",
                (document["datasetId"], self._dump(document), document.get("createdAt", self._now())),
            )
        return document

    def get_calibration_dataset(self, dataset_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM calibration_datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"calibration dataset {dataset_id!r} does not exist")
        return json.loads(row["document_json"])

    def save_calibration_fit(self, document: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO calibration_fits (fit_id, dataset_id, result_json, created_at) VALUES (?, ?, ?, ?)",
                (document["fitId"], document["datasetId"], self._dump(document), document.get("createdAt", self._now())),
            )
        return document

    def get_calibration_fit(self, fit_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM calibration_fits WHERE fit_id = ?", (fit_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"calibration fit {fit_id!r} does not exist")
        return json.loads(row["result_json"])

    def register_layout(self, layout_id: str, asset_id: str, file_name: str, document: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT document_json FROM layout_assets WHERE layout_id = ?", (layout_id,)
            ).fetchone()
            if existing is not None:
                stored = json.loads(existing["document_json"])
                if stored.get("assetId") == asset_id and stored.get("fileName") == file_name:
                    return stored
                raise WorkflowConflict("layoutId already exists with different content")
            connection.execute(
                "INSERT INTO layout_assets (layout_id, asset_id, file_name, document_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (layout_id, asset_id, file_name, self._dump(document), self._now()),
            )
        return document

    def get_layout(self, layout_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT document_json FROM layout_assets WHERE layout_id = ?", (layout_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"layout {layout_id!r} does not exist")
        return json.loads(row["document_json"])

    def list_layouts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_json FROM layout_assets ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [
            {
                "layoutId": json.loads(row["document_json"])["layoutId"],
                "fileName": json.loads(row["document_json"])["fileName"],
                "structureCount": len(json.loads(row["document_json"]).get("structures") or []),
                "createdAt": json.loads(row["document_json"])["createdAt"],
            }
            for row in rows
        ]

    def save_clip(self, document: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute("SELECT created_at FROM clips WHERE clip_id = ?", (document["clipId"],)).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO clips (clip_id, layout_id, document_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (document["clipId"], document["layoutId"], self._dump(document), document.get("createdAt", now), now),
                )
            else:
                document["createdAt"] = existing["created_at"]
                connection.execute(
                    "UPDATE clips SET document_json = ?, updated_at = ? WHERE clip_id = ?",
                    (self._dump(document), now, document["clipId"]),
                )
        return document

    def get_clip(self, clip_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT document_json FROM clips WHERE clip_id = ?", (clip_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"clip {clip_id!r} does not exist")
        return json.loads(row["document_json"])

    def list_clips(self, layout_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if layout_id is None:
                rows = connection.execute("SELECT document_json FROM clips ORDER BY created_at DESC, rowid DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT document_json FROM clips WHERE layout_id = ? ORDER BY created_at DESC, rowid DESC",
                    (layout_id,),
                ).fetchall()
        return [json.loads(row["document_json"]) for row in rows]

    def register_template(self, template) -> Any:
        payload = template.model_dump(mode="json", exclude_none=True)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT template_json FROM flow_templates WHERE template_id = ?",
                (template.templateId,),
            ).fetchone()
            if existing is not None:
                if sha256_hex(json.loads(existing["template_json"])) != sha256_hex(payload):
                    raise WorkflowConflict("templateId already exists with different content")
                return template
            connection.execute(
                "INSERT INTO flow_templates (template_id, template_json, created_at) VALUES (?, ?, ?)",
                (template.templateId, self._dump(payload), self._now()),
            )
        return template

    def get_template(self, template_id: str) -> Any:
        from .templates import FlowTemplateV1

        with self._connect() as connection:
            row = connection.execute(
                "SELECT template_json FROM flow_templates WHERE template_id = ?",
                (template_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"flow template {template_id!r} does not exist")
        return FlowTemplateV1.model_validate(json.loads(row["template_json"]))

    def list_templates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT template_id, template_json, created_at FROM flow_templates ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [
            {
                "templateId": row["template_id"],
                "name": json.loads(row["template_json"]).get("name"),
                "stepCount": len(json.loads(row["template_json"]).get("steps") or []),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def save_module(self, module: ModuleV1) -> dict[str, Any]:
        """Create or update one module after the caller validated its content.

        ``moduleId`` is the stable identity. Re-posting identical content is
        idempotent; a validated change appends a new retained revision and makes
        it current. Older revisions remain resolvable so pinned calls and old
        compile provenance keep referring to the exact content they expanded.
        """

        payload = module.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        now = self._now()
        with self._connect() as connection:
            for module_id, revision in module_step_pins(module):
                if self._module_revision_row(connection, module_id, revision) is None:
                    raise WorkflowValidation(
                        f"module {module.moduleId!r} pins {module_id!r} revision {revision}, "
                        "which is not retained"
                    )
            row = connection.execute(
                "SELECT * FROM modules WHERE module_id = ?", (module.moduleId,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO modules (module_id, module_json, content_hash, revision, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (module.moduleId, self._dump(payload), digest, 1, now, now),
                )
                self._insert_module_revision(connection, module.moduleId, 1, payload, digest, now)
                return self._module_summary(module, revision=1, updated_at=now)
            if row["content_hash"] == digest:
                return self._module_summary(module, revision=row["revision"], updated_at=row["updated_at"])
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE modules SET module_json = ?, content_hash = ?, revision = ?, updated_at = ?"
                " WHERE module_id = ?",
                (self._dump(payload), digest, revision, now, module.moduleId),
            )
            self._insert_module_revision(connection, module.moduleId, revision, payload, digest, now)
        return self._module_summary(module, revision=revision, updated_at=now)

    @staticmethod
    def _module_revision_row(connection: sqlite3.Connection, module_id: str, revision: int):
        return connection.execute(
            "SELECT module_json FROM module_revisions WHERE module_id = ? AND revision = ?",
            (module_id, revision),
        ).fetchone()

    def _insert_module_revision(
        self,
        connection: sqlite3.Connection,
        module_id: str,
        revision: int,
        payload: dict[str, Any],
        digest: str,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO module_revisions"
            " (module_id, revision, module_json, content_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (module_id, revision, self._dump(payload), digest, created_at),
        )

    @staticmethod
    def _module_summary(module: ModuleV1, *, revision: int, updated_at: str) -> dict[str, Any]:
        return {
            "moduleId": module.moduleId,
            "name": module.name,
            "revision": revision,
            "parameterCount": len(module.parameters),
            "stepCount": len(module.steps),
            "updatedAt": updated_at,
        }

    def get_module(self, module_id: str, revision: int | None = None) -> ModuleV1:
        with self._connect() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT module_json FROM modules WHERE module_id = ?", (module_id,)
                ).fetchone()
            else:
                row = self._module_revision_row(connection, module_id, revision)
        if row is None:
            if revision is None:
                raise WorkflowNotFound(f"module {module_id!r} does not exist")
            raise WorkflowNotFound(f"module {module_id!r} revision {revision} does not exist")
        try:
            return ModuleV1.model_validate(json.loads(row["module_json"]))
        except (ValueError, TypeError) as exc:
            raise WorkflowValidation(f"stored module {module_id!r} is invalid") from exc

    def module_history(self) -> dict[str, dict[int, ModuleV1]]:
        """Return every retained module revision grouped by module id."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT module_id, revision, module_json FROM module_revisions"
                " ORDER BY module_id, revision"
            ).fetchall()
        history: dict[str, dict[int, ModuleV1]] = {}
        for row in rows:
            try:
                module = ModuleV1.model_validate(json.loads(row["module_json"]))
            except (ValueError, TypeError):
                continue
            history.setdefault(row["module_id"], {})[int(row["revision"])] = module
        return history

    def module_documents(self) -> list[ModuleV1]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT module_json FROM modules ORDER BY module_id"
            ).fetchall()
        documents: list[ModuleV1] = []
        for row in rows:
            try:
                documents.append(ModuleV1.model_validate(json.loads(row["module_json"])))
            except (ValueError, TypeError):
                continue
        return documents

    def list_modules(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT module_json, revision, updated_at FROM modules ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        summaries = []
        for row in rows:
            try:
                module = ModuleV1.model_validate(json.loads(row["module_json"]))
            except (ValueError, TypeError):
                continue
            summaries.append(
                self._module_summary(module, revision=row["revision"], updated_at=row["updated_at"])
            )
        return summaries

    def current_module_revision(self, module_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision FROM modules WHERE module_id = ?", (module_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"module {module_id!r} does not exist")
        return int(row["revision"])

    def module_revision_exists(self, module_id: str, revision: int) -> bool:
        with self._connect() as connection:
            return self._module_revision_row(connection, module_id, revision) is not None

    def import_module_revision(
        self, module: ModuleV1, revision: int, *, make_current: bool = False
    ) -> str:
        """Write one exact retained revision without renumbering it.

        Content is immutable per ``(moduleId, revision)``. The current pointer is
        advanced only when requested and never downgraded, so importing an old
        bundle cannot hide newer local revisions.
        """

        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkflowValidation("imported module revision must be a positive integer")
        payload = module.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        now = self._now()
        with self._connect() as connection:
            existing = self._module_revision_row(connection, module.moduleId, revision)
            if existing is not None:
                if sha256_hex(json.loads(existing["module_json"])) != digest:
                    raise WorkflowConflict(
                        f"module {module.moduleId!r} revision {revision} already exists "
                        "with different content"
                    )
                status = "existing"
            else:
                self._insert_module_revision(
                    connection, module.moduleId, revision, payload, digest, now
                )
                status = "imported"
            if make_current:
                row = connection.execute(
                    "SELECT * FROM modules WHERE module_id = ?", (module.moduleId,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO modules"
                        " (module_id, module_json, content_hash, revision, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (module.moduleId, self._dump(payload), digest, revision, now, now),
                    )
                elif revision > int(row["revision"]):
                    connection.execute(
                        "UPDATE modules SET module_json = ?, content_hash = ?, revision = ?,"
                        " updated_at = ? WHERE module_id = ?",
                        (self._dump(payload), digest, revision, now, module.moduleId),
                    )
                elif revision == int(row["revision"]) and row["content_hash"] != digest:
                    raise WorkflowConflict(
                        f"module {module.moduleId!r} current revision {revision} "
                        "already exists with different content"
                    )
        return status

    def save_library_bundle(self, bundle: Any) -> dict[str, Any]:
        """Register one content-addressed bundle, idempotently.

        ``bundleId`` is the hash of the bundle content and deliberately excludes
        ``createdAt`` and list order. Re-exporting the same content therefore has
        to reuse the stored copy instead of failing as a content conflict; a
        different content set always yields a different ``bundleId``.
        """

        payload = bundle.model_dump(mode="json", exclude_none=True) if hasattr(bundle, "model_dump") else dict(bundle)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT document_json FROM library_bundles WHERE bundle_id = ?",
                (payload["bundleId"],),
            ).fetchone()
            if existing is not None:
                return json.loads(existing["document_json"])
            connection.execute(
                "INSERT INTO library_bundles (bundle_id, document_json, created_at) VALUES (?, ?, ?)",
                (
                    payload["bundleId"],
                    self._dump(payload),
                    payload.get("createdAt") or self._now(),
                ),
            )
        return payload

    def get_library_bundle(self, bundle_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM library_bundles WHERE bundle_id = ?", (bundle_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"library bundle {bundle_id!r} does not exist")
        return json.loads(row["document_json"])

    def list_library_bundles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT bundle_id, document_json, created_at FROM library_bundles"
                " ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        summaries = []
        for row in rows:
            try:
                document = json.loads(row["document_json"])
            except ValueError:
                continue
            summaries.append(
                {
                    "bundleId": row["bundle_id"],
                    "name": document.get("name"),
                    "description": document.get("description") or "",
                    "createdAt": row["created_at"],
                    "moduleCount": len(document.get("modules") or []),
                    "definitionCount": len(document.get("definitions") or []),
                    "templateCount": len(document.get("templates") or []),
                    "experimentTemplateCount": len(document.get("experimentTemplates") or []),
                }
            )
        return summaries

    def save_flow_definition(self, definition: FlowDefinitionV2) -> dict[str, Any]:
        """Create or revise one flow definition.

        A definition id is immutable identity; revision must increase when the
        content changes. Re-posting identical content is idempotent.
        """

        payload = definition.model_dump(mode="json", exclude_none=True)
        digest = sha256_hex(payload)
        now = self._now()
        with self._connect() as connection:
            for module_id, revision in flow_definition_pins(definition):
                if self._module_revision_row(connection, module_id, revision) is None:
                    raise WorkflowValidation(
                        f"flow definition {definition.definitionId!r} pins module "
                        f"{module_id!r} revision {revision}, which is not retained"
                    )
            row = connection.execute(
                "SELECT * FROM flow_definitions WHERE definition_id = ?",
                (definition.definitionId,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO flow_definitions"
                    " (definition_id, flow_id, revision, document_json, content_hash, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        definition.definitionId,
                        definition.flowId,
                        definition.revision,
                        self._dump(payload),
                        digest,
                        now,
                        now,
                    ),
                )
                return self._definition_summary(definition, updated_at=now)
            if row["content_hash"] == digest:
                return self._definition_summary(definition, updated_at=row["updated_at"])
            if definition.revision <= int(row["revision"]):
                raise WorkflowConflict(
                    f"flow definition {definition.definitionId!r} already has revision "
                    f"{row['revision']}; a changed document needs a higher revision"
                )
            connection.execute(
                "UPDATE flow_definitions SET flow_id = ?, revision = ?, document_json = ?,"
                " content_hash = ?, updated_at = ? WHERE definition_id = ?",
                (
                    definition.flowId,
                    definition.revision,
                    self._dump(payload),
                    digest,
                    now,
                    definition.definitionId,
                ),
            )
        return self._definition_summary(definition, updated_at=now)

    @staticmethod
    def _definition_summary(definition: FlowDefinitionV2, *, updated_at: str) -> dict[str, Any]:
        return {
            "definitionId": definition.definitionId,
            "flowId": definition.flowId,
            "revision": definition.revision,
            "name": definition.name,
            "nodeCount": len(definition.nodes),
            "variableCount": len(definition.variables),
            "updatedAt": updated_at,
        }

    def get_flow_definition(self, definition_id: str) -> FlowDefinitionV2:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM flow_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"flow definition {definition_id!r} does not exist")
        try:
            return FlowDefinitionV2.model_validate(json.loads(row["document_json"]))
        except (ValueError, TypeError) as exc:
            raise WorkflowValidation(f"stored flow definition {definition_id!r} is invalid") from exc

    def list_flow_definitions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_json, updated_at FROM flow_definitions ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        summaries = []
        for row in rows:
            try:
                definition = FlowDefinitionV2.model_validate(json.loads(row["document_json"]))
            except (ValueError, TypeError):
                continue
            summaries.append(self._definition_summary(definition, updated_at=row["updated_at"]))
        return summaries

    # ------------------------------------------------------------------
    # Reusable library objects (schema 9)
    # ------------------------------------------------------------------

    @staticmethod
    def _library_item_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "itemId": row["item_id"],
            "category": row["category"],
            "name": row["name"],
            "description": row["description"],
            "payload": json.loads(row["payload_json"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _library_item_content(document: Mapping[str, Any]) -> tuple[Any, ...]:
        """Content identity: timestamps never make two items different."""

        return (
            document.get("category"),
            document.get("name"),
            document.get("description") or "",
            document.get("payload"),
        )

    def save_library_item(
        self,
        document: Mapping[str, Any],
        *,
        max_per_category: int | None = None,
    ) -> dict[str, Any]:
        """Create one library item, idempotently per ``itemId``.

        Re-saving identical content returns the stored row unchanged. The same
        id with different content is a conflict instead of a silent overwrite.
        """

        item_id = str(document.get("itemId") or "")
        category = str(document.get("category") or "")
        if not item_id or not category:
            raise WorkflowValidation("library item requires itemId and category")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise WorkflowValidation("library item payload must be an object")
        content = self._library_item_content(document)
        timestamp = str(document.get("createdAt") or self._now())
        updated = str(document.get("updatedAt") or timestamp)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if existing is not None:
                stored = self._library_item_document(existing)
                if self._library_item_content(stored) != content:
                    raise WorkflowConflict(
                        f"library item {item_id!r} already exists with different content"
                    )
                return stored
            if max_per_category is not None:
                (count,) = connection.execute(
                    "SELECT COUNT(*) FROM library_items WHERE category = ?", (category,)
                ).fetchone()
                if count >= max_per_category:
                    raise WorkflowValidation(
                        f"library category {category!r} already holds {count} items; "
                        f"the limit is {max_per_category}"
                    )
            connection.execute(
                "INSERT INTO library_items"
                " (item_id, category, name, description, payload_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    category,
                    str(document.get("name") or ""),
                    str(document.get("description") or ""),
                    self._dump(payload),
                    timestamp,
                    updated,
                ),
            )
            row = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._library_item_document(row)

    def update_library_item(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Replace the editable fields of an existing item.

        ``itemId``, ``category`` and ``createdAt`` are immutable. Re-sending the
        stored content is a no-op: the original ``updatedAt`` is preserved so an
        editor retry does not look like an edit.
        """

        item_id = str(document.get("itemId") or "")
        if not item_id:
            raise WorkflowValidation("library item requires itemId")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise WorkflowValidation("library item payload must be an object")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if existing is None:
                raise WorkflowNotFound(f"library item {item_id!r} does not exist")
            stored = self._library_item_document(existing)
            if existing["category"] != str(document.get("category") or existing["category"]):
                raise WorkflowConflict(
                    f"library item {item_id!r} category is immutable"
                )
            if self._library_item_content(stored) == self._library_item_content(document):
                return stored
            connection.execute(
                "UPDATE library_items SET name = ?, description = ?, payload_json = ?,"
                " updated_at = ? WHERE item_id = ?",
                (
                    str(document.get("name") or ""),
                    str(document.get("description") or ""),
                    self._dump(payload),
                    str(document.get("updatedAt") or self._now()),
                    item_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._library_item_document(row)

    def get_library_item(self, item_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM library_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"library item {item_id!r} does not exist")
        return self._library_item_document(row)

    def list_library_items(self, category: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM library_items"
        parameters: tuple[Any, ...] = ()
        if category is not None:
            query += " WHERE category = ?"
            parameters = (category,)
        query += " ORDER BY updated_at DESC, rowid DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._library_item_document(row) for row in rows]

    def count_library_items(self, category: str) -> int:
        with self._connect() as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM library_items WHERE category = ?", (category,)
            ).fetchone()
        return int(count)

    def delete_library_item(self, item_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM library_items WHERE item_id = ?", (item_id,)
            )
            if cursor.rowcount == 0:
                raise WorkflowNotFound(f"library item {item_id!r} does not exist")

    def seed_library_items(self, documents: Sequence[Mapping[str, Any]]) -> int:
        """Insert built-in presets into library categories that are still empty.

        The emptiness test is taken once per category, before any preset of
        that category is written, so the first run writes every preset while a
        later run writes nothing. A category that already holds user entries is
        never touched, and the deterministic preset ids make a re-run unable to
        duplicate a row even if an earlier seed was interrupted.
        """

        inserted = 0
        with self._connect() as connection:
            categories = {str(document.get("category") or "") for document in documents}
            empty = {
                category: connection.execute(
                    "SELECT COUNT(*) FROM library_items WHERE category = ?", (category,)
                ).fetchone()[0]
                == 0
                for category in categories
                if category
            }
            # The library lists newest first. Inserting the presets in reverse
            # makes the declared order (etch, deposit, single particle) the
            # order a user sees, while user-created items still land on top.
            for document in reversed(list(documents)):
                category = str(document.get("category") or "")
                item_id = str(document.get("itemId") or "")
                payload = document.get("payload")
                if not empty.get(category) or not item_id or not isinstance(payload, dict):
                    continue
                timestamp = str(document.get("createdAt") or self._now())
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO library_items"
                    " (item_id, category, name, description, payload_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        category,
                        str(document.get("name") or ""),
                        str(document.get("description") or ""),
                        self._dump(payload),
                        timestamp,
                        str(document.get("updatedAt") or timestamp),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def seed_library_defaults(self) -> int:
        """Seed the built-in material and process presets (idempotent)."""

        from .library_presets import seed_library_defaults

        return seed_library_defaults(self, now=self._now())

    # ------------------------------------------------------------------
    # Persisted projects (schema 10)
    # ------------------------------------------------------------------

    @staticmethod
    def _project_document(document: Mapping[str, Any]) -> dict[str, Any]:
        """Validate one ProjectV7 document and return its canonical JSON form."""

        try:
            project = ProjectV7.model_validate(dict(document))
        except ValidationError as exc:
            first = exc.errors(include_url=False, include_context=False)[0]
            location = ".".join(
                str(part) for part in first.get("loc", ()) if part != "__root__"
            )
            detail = first.get("msg") or "invalid project"
            where = f" at {location}" if location else ""
            raise WorkflowValidation(
                f"project document is not a valid ProjectV7{where}: {detail}"
            ) from exc
        if not str(project.name).strip():
            raise WorkflowValidation("project name must not be blank")
        return project.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _project_record(row: sqlite3.Row) -> dict[str, Any]:
        document = json.loads(row["document_json"])
        steps = document.get("steps")
        return {
            "schemaVersion": 1,
            "projectId": row["project_id"],
            "name": row["name"],
            "dimension": document.get("dimension"),
            "stepCount": len(steps) if isinstance(steps, list) else 0,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def create_project(self, document: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Store one project; return ``(record, created)``.

        Creating the same name with identical content is idempotent and returns
        the stored record with ``created`` false. The same name with different
        content is a conflict: project names are the human identity in the
        project list, so two silently diverging rows would be ambiguous.
        """

        validated = self._project_document(document)
        name = str(validated["name"])
        canonical = self._dump(validated)
        timestamp = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None:
                if existing["document_json"] != canonical:
                    raise WorkflowConflict(
                        f"project name {name!r} already exists with different content"
                    )
                return self._project_record(existing), False
            project_id = new_project_id()
            try:
                connection.execute(
                    "INSERT INTO projects"
                    " (project_id, name, document_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (project_id, name, canonical, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkflowConflict(
                    f"project name {name!r} already exists with different content"
                ) from exc
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_record(row), True

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"project {project_id!r} does not exist")
        try:
            return json.loads(row["document_json"])
        except ValueError as exc:
            raise WorkflowValidation(f"stored project {project_id!r} is invalid") from exc

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return [self._project_record(row) for row in rows]

    def update_project(self, project_id: str, document: Mapping[str, Any]) -> dict[str, Any]:
        """Replace one stored project document.

        A PUT always replaces the whole document, including ``steps``: there is
        no partial step merge, so a removed step stays removed. Re-sending the
        stored content is a no-op and keeps the original ``updatedAt``.
        """

        validated = self._project_document(document)
        name = str(validated["name"])
        canonical = self._dump(validated)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if existing is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            collision = connection.execute(
                "SELECT project_id FROM projects WHERE name = ? AND project_id <> ?",
                (name, project_id),
            ).fetchone()
            if collision is not None:
                raise WorkflowConflict(
                    f"project name {name!r} already exists with different content"
                )
            if existing["document_json"] == canonical:
                return self._project_record(existing)
            connection.execute(
                "UPDATE projects SET name = ?, document_json = ?, updated_at = ?"
                " WHERE project_id = ?",
                (name, canonical, self._now(), project_id),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_record(row)

    def delete_project(self, project_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )
            if cursor.rowcount == 0:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")

    # ------------------------------------------------------------------
    # Per-project workflow state (schema 11; compileBasis added by schema 13)
    # ------------------------------------------------------------------

    def get_project_workflow_state(self, project_id: str) -> ProjectWorkflowStateV1:
        """Return the stored 03-page state, or an all-empty state when unset.

        Reads never re-validate references: a compile/route/segment that GC
        removed after the state was saved must still return the saved document
        so the UI can decide what to do with the now-missing target.
        """

        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            row = connection.execute(
                "SELECT state_json FROM project_workflow_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return ProjectWorkflowStateV1()
        try:
            return ProjectWorkflowStateV1.model_validate(json.loads(row["state_json"]))
        except (ValueError, TypeError) as exc:
            raise WorkflowValidation(
                f"stored workflow state for project {project_id!r} is invalid"
            ) from exc

    def save_project_workflow_state(
        self,
        project_id: str,
        state: ProjectWorkflowStateV1 | Mapping[str, Any],
    ) -> ProjectWorkflowStateV1:
        """Replace the whole state document after checking every reference.

        ``updatedAt`` is server-owned: the incoming value is ignored and the
        stamp only moves when the content actually changes, so a repeated
        autosave PUT is idempotent.
        """

        record = (
            state
            if isinstance(state, ProjectWorkflowStateV1)
            else ProjectWorkflowStateV1.model_validate(dict(state))
        )
        content = record.content()
        encoded = self._dump({**content, "updatedAt": None})
        if len(encoded.encode("utf-8")) > MAX_STATE_JSON_BYTES:
            raise WorkflowValidation(
                f"workflow state JSON exceeds the {MAX_STATE_JSON_BYTES} byte limit"
            )
        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            self._validate_workflow_state_references(connection, record)
            existing = connection.execute(
                "SELECT state_json FROM project_workflow_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                stored = json.loads(existing["state_json"])
                if stored.get("updatedAt") is not None:
                    stored_content = {
                        key: value for key, value in stored.items() if key != "updatedAt"
                    }
                    if self._dump(stored_content) == self._dump(content):
                        return ProjectWorkflowStateV1.model_validate(stored)
            timestamp = self._now()
            document = {**content, "updatedAt": timestamp}
            connection.execute(
                "INSERT INTO project_workflow_state (project_id, state_json, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(project_id) DO UPDATE SET"
                " state_json = excluded.state_json, updated_at = excluded.updated_at",
                (project_id, self._dump(document), timestamp),
            )
        return ProjectWorkflowStateV1.model_validate(document)

    def delete_project_workflow_state(self, project_id: str) -> None:
        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            connection.execute(
                "DELETE FROM project_workflow_state WHERE project_id = ?", (project_id,)
            )

    # ------------------------------------------------------------------
    # Persisted reports (schema 12)
    # ------------------------------------------------------------------

    @staticmethod
    def _report_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reportId": row["report_id"],
            "title": row["title"],
            "html": row["html"],
            "sha256": row["sha256"],
            "createdAt": row["created_at"],
        }

    def save_report(
        self,
        project_id: str,
        report_id: str,
        title: str,
        html: str,
        sha256: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Store one report for a project.

        Two explicit limits keep a project's report history bounded: the HTML
        body may not exceed 2 MiB and only the newest 50 reports are kept. When
        a 51st distinct report arrives the oldest one is replaced instead of
        rejecting the new report, because the report the user just generated is
        the one they expect to see in 06.
        """

        encoded = html.encode("utf-8")
        if len(encoded) > MAX_REPORT_HTML_BYTES:
            raise WorkflowValidation(
                f"报告 HTML 超过 {MAX_REPORT_HTML_BYTES} 字节上限（当前 {len(encoded)}）"
            )
        timestamp = created_at or self._now()
        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            existing = connection.execute(
                "SELECT 1 FROM workflow_reports WHERE project_id = ? AND report_id = ?",
                (project_id, report_id),
            ).fetchone()
            if existing is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM workflow_reports WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
                overflow = count - (MAX_REPORTS_PER_PROJECT - 1)
                if overflow > 0:
                    connection.execute(
                        "DELETE FROM workflow_reports WHERE rowid IN ("
                        " SELECT rowid FROM workflow_reports WHERE project_id = ?"
                        " ORDER BY created_at ASC, rowid ASC LIMIT ?)",
                        (project_id, overflow),
                    )
            connection.execute(
                "INSERT INTO workflow_reports"
                " (project_id, report_id, title, html, sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(project_id, report_id) DO UPDATE SET"
                " title = excluded.title, html = excluded.html,"
                " sha256 = excluded.sha256, created_at = excluded.created_at",
                (project_id, report_id, title, html, sha256, timestamp),
            )
        return {
            "reportId": report_id,
            "title": title,
            "html": html,
            "sha256": sha256,
            "createdAt": timestamp,
        }

    def get_latest_report(self, project_id: str) -> dict[str, Any] | None:
        """Return the newest report, or ``None`` when the project has none."""

        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            row = connection.execute(
                "SELECT * FROM workflow_reports WHERE project_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return None if row is None else self._report_payload(row)

    def get_report(self, project_id: str, report_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkflowNotFound(f"project {project_id!r} does not exist")
            row = connection.execute(
                "SELECT * FROM workflow_reports WHERE project_id = ? AND report_id = ?",
                (project_id, report_id),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(
                f"report {report_id!r} does not exist for project {project_id!r}"
            )
        return self._report_payload(row)

    def delete_report(self, project_id: str, report_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM workflow_reports WHERE project_id = ? AND report_id = ?",
                (project_id, report_id),
            )
            if cursor.rowcount == 0:
                raise WorkflowNotFound(
                    f"report {report_id!r} does not exist for project {project_id!r}"
                )

    @staticmethod
    def _validate_workflow_state_references(
        connection: sqlite3.Connection, state: ProjectWorkflowStateV1
    ) -> None:
        """Reject dangling references instead of persisting them."""

        compile_id = state.compileId
        route_id = state.routeId
        if compile_id is not None:
            row = connection.execute(
                "SELECT 1 FROM compiles WHERE compile_id = ?", (compile_id,)
            ).fetchone()
            if row is None:
                raise WorkflowValidation(f"compile {compile_id!r} does not exist")
        if route_id is not None:
            row = connection.execute(
                "SELECT 1 FROM routes WHERE route_id = ?", (route_id,)
            ).fetchone()
            if row is None:
                raise WorkflowValidation(f"route {route_id!r} does not exist")
            if compile_id is not None:
                match = connection.execute(
                    "SELECT 1 FROM attempts WHERE route_id = ? AND compile_id = ? LIMIT 1",
                    (route_id, compile_id),
                ).fetchone()
                if match is None:
                    raise WorkflowValidation(
                        f"route {route_id!r} does not belong to compile {compile_id!r}"
                    )
        if state.segmentRuns and route_id is None:
            raise WorkflowValidation(
                "routeId is required when segmentRuns is not empty"
            )
        for entry in state.segmentRuns:
            segment = connection.execute(
                "SELECT 1 FROM segments WHERE segment_id = ?", (entry.segmentId,)
            ).fetchone()
            if segment is None:
                raise WorkflowValidation(f"segment {entry.segmentId!r} does not exist")
            if route_id is not None:
                belongs = connection.execute(
                    "SELECT 1 FROM segments s"
                    " JOIN attempts a ON a.attempt_id = s.attempt_id"
                    " WHERE s.segment_id = ? AND a.route_id = ? LIMIT 1",
                    (entry.segmentId, route_id),
                ).fetchone()
                if belongs is None:
                    raise WorkflowValidation(
                        f"segment {entry.segmentId!r} does not belong to route {route_id!r}"
                    )
            if entry.measurementId is not None:
                measurement = connection.execute(
                    "SELECT 1 FROM measurements WHERE measurement_id = ?",
                    (entry.measurementId,),
                ).fetchone()
                if measurement is None:
                    raise WorkflowValidation(
                        f"measurement {entry.measurementId!r} does not exist"
                    )
        for measurement_id in state.measurementIds:
            row = connection.execute(
                "SELECT 1 FROM measurements WHERE measurement_id = ?",
                (measurement_id,),
            ).fetchone()
            if row is None:
                raise WorkflowValidation(
                    f"measurement {measurement_id!r} does not exist"
                )
        for experiment_id in state.experimentIds:
            row = connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise WorkflowValidation(f"experiment {experiment_id!r} does not exist")

    def register_binding(self, binding) -> Any:
        payload = binding.model_dump(mode="json", exclude_none=True)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT binding_json FROM geometry_bindings WHERE binding_id = ?",
                (binding.bindingId,),
            ).fetchone()
            if existing is not None:
                if sha256_hex(json.loads(existing["binding_json"])) != sha256_hex(payload):
                    raise WorkflowConflict("geometry binding already exists with different content")
                return binding
            connection.execute(
                "INSERT INTO geometry_bindings (binding_id, binding_json, created_at) VALUES (?, ?, ?)",
                (binding.bindingId, self._dump(payload), self._now()),
            )
        return binding

    def get_binding(self, binding_id: str):
        from .models import GeometryBindingV1

        with self._connect() as connection:
            row = connection.execute(
                "SELECT binding_json FROM geometry_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"geometry binding {binding_id!r} does not exist")
        return GeometryBindingV1.model_validate(json.loads(row["binding_json"]))

    def register_comparison_protocol(self, protocol) -> Any:
        payload = protocol.model_dump(mode="json", exclude_none=True)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT protocol_json FROM comparison_protocols WHERE protocol_id = ? AND version = ?",
                (protocol.protocolId, protocol.version),
            ).fetchone()
            if existing is not None:
                if sha256_hex(json.loads(existing["protocol_json"])) != sha256_hex(payload):
                    raise WorkflowConflict("comparison protocol already exists with different content")
                return protocol
            connection.execute(
                "INSERT INTO comparison_protocols (protocol_id, version, protocol_json, created_at) VALUES (?, ?, ?, ?)",
                (protocol.protocolId, protocol.version, self._dump(payload), self._now()),
            )
        return protocol

    def get_comparison_protocol(self, protocol_id: str, version: int | None = None) -> Any:
        from .equivalence import ComparisonProtocolV1

        with self._connect() as connection:
            if version is None:
                row = connection.execute(
                    "SELECT protocol_json FROM comparison_protocols WHERE protocol_id = ? ORDER BY version DESC LIMIT 1",
                    (protocol_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT protocol_json FROM comparison_protocols WHERE protocol_id = ? AND version = ?",
                    (protocol_id, version),
                ).fetchone()
        if row is None:
            raise WorkflowNotFound(f"comparison protocol {protocol_id!r} does not exist")
        return ComparisonProtocolV1.model_validate(json.loads(row["protocol_json"]))

    def list_comparison_protocols(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT protocol_id, version, protocol_json, created_at FROM comparison_protocols ORDER BY protocol_id, version DESC"
            ).fetchall()
        return [
            {
                "protocolId": row["protocol_id"],
                "version": row["version"],
                "methodVersion": json.loads(row["protocol_json"]).get("methodVersion"),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def record_equivalence(self, result) -> Any:
        check_id = f"eq_{uuid.uuid4().hex}"
        stored = result.model_copy(update={"checkId": check_id})
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO equivalence_checks (check_id, protocol_id, protocol_version, left_hash, right_hash, status, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (check_id, stored.protocolId, stored.protocolVersion, stored.leftHash, stored.rightHash, stored.status, self._dump(stored.model_dump(mode="json", exclude_none=True)), self._now()),
            )
        return stored

    def get_equivalence(self, check_id: str) -> Any:
        from .equivalence import EquivalenceResultV1

        with self._connect() as connection:
            row = connection.execute("SELECT result_json FROM equivalence_checks WHERE check_id = ?", (check_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"equivalence check {check_id!r} does not exist")
        return EquivalenceResultV1.model_validate(json.loads(row["result_json"]))

    def register_artifact_ref(self, artifact_id: str, owner_type: str, owner_id: str, required_for: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO artifact_refs (artifact_id, owner_type, owner_id, required_for, created_at) VALUES (?, ?, ?, ?, ?)",
                (artifact_id, owner_type, owner_id, required_for, self._now()),
            )

    def list_artifact_refs(self, artifact_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if artifact_id is None:
                rows = connection.execute("SELECT * FROM artifact_refs ORDER BY artifact_id, owner_type, owner_id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM artifact_refs WHERE artifact_id = ? ORDER BY owner_type, owner_id", (artifact_id,)).fetchall()
        return [
            {"artifactId": row["artifact_id"], "ownerType": row["owner_type"], "ownerId": row["owner_id"], "requiredFor": row["required_for"]}
            for row in rows
        ]

    def abandon_capability(
        self,
        artifact_id: str,
        owner_type: str,
        owner_id: str,
        capability: str,
        reason: str,
    ) -> dict[str, Any]:
        record_id = f"del_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM artifact_refs WHERE artifact_id = ? AND owner_type = ? AND owner_id = ? AND required_for = ?",
                (artifact_id, owner_type, owner_id, capability),
            )
            connection.execute(
                "INSERT INTO deletion_records (record_id, artifact_id, owner_type, owner_id, capability, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_id, artifact_id, owner_type, owner_id, capability, reason, self._now()),
            )
        return {"recordId": record_id, "artifactId": artifact_id, "ownerType": owner_type, "ownerId": owner_id, "capability": capability}

    def artifact_dependencies(self, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            refs = connection.execute(
                "SELECT owner_type, owner_id, required_for FROM artifact_refs WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchall()
            measurements = connection.execute(
                "SELECT measurement_id FROM measurements WHERE source_run_id = ?", (artifact_id,)
            ).fetchall()
            segment_owners = connection.execute(
                "SELECT segment_id FROM segments WHERE run_id = ?", (artifact_id,)
            ).fetchall()
            segments = connection.execute("SELECT segment_id, start_checkpoint_json, end_checkpoint_json FROM segments").fetchall()
            pins = connection.execute(
                "SELECT 1 FROM artifact_pins WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        checkpoint_owners = []
        for row in segments:
            for raw in (row["start_checkpoint_json"], row["end_checkpoint_json"]):
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if payload.get("runId") == artifact_id:
                    checkpoint_owners.append(row["segment_id"])
        return {
            "refs": [{"ownerType": row["owner_type"], "ownerId": row["owner_id"], "requiredFor": row["required_for"]} for row in refs],
            "measurements": [row["measurement_id"] for row in measurements],
            "segmentOwners": [row["segment_id"] for row in segment_owners],
            "checkpointOwners": sorted(set(checkpoint_owners)),
            "pinned": pins is not None,
        }

    def gc_dry_run(self) -> dict[str, Any]:
        from .. import storage

        deps: set[tuple[str, str, str, str]] = set()
        with self._connect() as connection:
            for row in connection.execute("SELECT segment_id, run_id FROM segments WHERE run_id IS NOT NULL"):
                deps.add((row["run_id"], "segment", row["segment_id"], "report"))
            for row in connection.execute("SELECT measurement_id, source_run_id FROM measurements WHERE source_run_id IS NOT NULL"):
                deps.add((row["source_run_id"], "measurement", row["measurement_id"], "remeasure"))
            for row in connection.execute("SELECT segment_id, start_checkpoint_json, end_checkpoint_json FROM segments"):
                for raw in (row["start_checkpoint_json"], row["end_checkpoint_json"]):
                    if raw:
                        try:
                            run_id = json.loads(raw).get("runId")
                            if isinstance(run_id, str):
                                deps.add((run_id, "segment", row["segment_id"], "continue"))
                        except (ValueError, TypeError):
                            pass
            for row in connection.execute("SELECT attempt_id, base_checkpoint_json FROM attempts WHERE base_checkpoint_json IS NOT NULL"):
                try:
                    run_id = json.loads(row["base_checkpoint_json"]).get("runId")
                    if isinstance(run_id, str):
                        deps.add((run_id, "attempt", row["attempt_id"], "continue"))
                except (ValueError, TypeError):
                    pass
            explicit_refs = {row["artifact_id"] for row in connection.execute("SELECT artifact_id FROM artifact_refs")}
            pins = {row["artifact_id"] for row in connection.execute("SELECT artifact_id FROM artifact_pins")}
            abandoned = {
                (row["artifact_id"], row["owner_type"], row["owner_id"], row["capability"])
                for row in connection.execute("SELECT artifact_id, owner_type, owner_id, capability FROM deletion_records")
            }
        active = deps - abandoned
        referenced = {item[0] for item in active} | explicit_refs | pins
        candidates = []
        for path in sorted(storage.RUNS.iterdir()) if storage.RUNS.is_dir() else []:
            if not path.is_dir() or path.name in referenced:
                continue
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            candidates.append({"artifactId": path.name, "bytes": size})
        return {"referenced": sorted(referenced), "candidates": candidates, "bytesReclaimable": sum(item["bytes"] for item in candidates)}

    def pin_artifact(self, artifact_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO artifact_pins (artifact_id, pinned_at) VALUES (?, ?)",
                (artifact_id, self._now()),
            )

    def unpin_artifact(self, artifact_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM artifact_pins WHERE artifact_id = ?", (artifact_id,))

    def resolve_segment_start(self, segment_id: str) -> None:
        """Fill a successor's start checkpoint from its verified predecessor."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id, start_after_step_id, start_checkpoint_json FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound(f"segment {segment_id!r} does not exist")
            if row["start_after_step_id"] is None or row["start_checkpoint_json"] is not None:
                return
            predecessor = connection.execute(
                "SELECT end_checkpoint_json FROM segments WHERE attempt_id = ? AND through_step_id = ? AND end_checkpoint_json IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                (row["attempt_id"], row["start_after_step_id"]),
            ).fetchone()
            if predecessor is None:
                raise WorkflowConflict("continued Segment has no successful predecessor checkpoint")
            connection.execute(
                "UPDATE segments SET start_checkpoint_json = ? WHERE segment_id = ?",
                (predecessor["end_checkpoint_json"], segment_id),
            )

    def mark_segment_started(self, segment_id: str, run_id: str, segment_project_hash: str) -> None:
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id, status FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound(f"segment {segment_id!r} does not exist")
            if row["status"] not in {"planned"}:
                raise WorkflowConflict(f"segment {segment_id!r} is not planned")
            connection.execute(
                "UPDATE segments SET run_id = ?, segment_project_hash = ?, status = 'running', updated_at = ? WHERE segment_id = ?",
                (run_id, segment_project_hash, now, segment_id),
            )
            connection.execute(
                "UPDATE attempts SET status = 'running', updated_at = ? WHERE attempt_id = ?",
                (now, row["attempt_id"]),
            )

    def finish_segment(
        self,
        segment_id: str,
        status: str,
        end_checkpoint: CheckpointRefV1 | None,
        *,
        provenance_input_hash: str | None = None,
        execution_hash: str | None = None,
        result_digest: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise WorkflowConflict(f"invalid terminal segment status {status!r}")
        now = self._now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if row is None:
                raise WorkflowNotFound(f"segment {segment_id!r} does not exist")
            connection.execute(
                "UPDATE segments SET status = ?, end_checkpoint_json = ?, provenance_input_hash = COALESCE(?, provenance_input_hash), execution_hash = COALESCE(?, execution_hash), result_digest = COALESCE(?, result_digest), updated_at = ? WHERE segment_id = ?",
                (status, self._checkpoint_value(None if end_checkpoint is None else end_checkpoint.model_dump(mode="json")), provenance_input_hash, execution_hash, result_digest, now, segment_id),
            )
            if end_checkpoint is not None:
                connection.execute(
                    "UPDATE segments SET start_checkpoint_json = ? WHERE attempt_id = ? AND start_after_step_id = ? AND start_checkpoint_json IS NULL AND segment_id != ?",
                    (self._checkpoint_value(end_checkpoint.model_dump(mode="json")), row["attempt_id"], end_checkpoint.stepId, segment_id),
                )
            attempt_id = row["attempt_id"]
            statuses = [item["status"] for item in connection.execute(
                "SELECT status FROM segments WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()]
            if any(value in {"failed", "cancelled", "interrupted"} for value in statuses):
                attempt_status = "failed" if "failed" in statuses else "cancelled" if "cancelled" in statuses else "interrupted"
            elif statuses and all(value == "succeeded" for value in statuses):
                attempt_status = "succeeded"
            else:
                attempt_status = "running"
            connection.execute(
                "UPDATE attempts SET status = ?, updated_at = ? WHERE attempt_id = ?",
                (attempt_status, now, attempt_id),
            )
