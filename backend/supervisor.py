"""Single-run process supervisor; the API process never imports ViennaPS."""

from __future__ import annotations

import calendar
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import storage
from .gpu_devices import cuda_devices
from .hashing import execution_hash
from .native_build import configured_runtime

# Re-exported for the API layer and the acceptance tests, which import
# ``ReplayMismatch``/``now_iso`` from ``backend.supervisor``.
from .planning import ReplayMismatch, now_iso, plan_run, requested_input_hash_from_dict  # noqa: F401
from .runtime_limits import max_log_bytes, max_log_mib, rss_limit_mib
from .v2_records import read_v2_steps

ROOT = storage.ROOT
PYTHON = ROOT / ".venv" / "bin" / "python"
GUARD = ROOT / "scripts" / "bounded_child.py"
WORKER = ROOT / "worker" / "simulate.py"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
ACTIVE = {"pending", "running", "cancelling"}


def child_command(run_dir: Path, timeout: int) -> list[str]:
    """Build the bounded-child command for one supervised worker run."""
    return [
        str(PYTHON),
        str(GUARD),
        "--cwd",
        str(ROOT),
        "--record",
        str(run_dir / "child-record.json"),
        "--log-path",
        str(run_dir / "run.log"),
        "--ready-file",
        str(run_dir / ".guard-ready"),
        "--parent-pid",
        str(os.getpid()),
        "--timeout-seconds",
        str(timeout),
        "--max-rss-mib",
        str(rss_limit_mib()),
        "--max-log-bytes",
        str(max_log_bytes()),
        "--terminate-grace-seconds",
        "5",
        "--",
        str(PYTHON),
        str(WORKER),
        str(run_dir / "input.json"),
        str(run_dir),
    ]


def _proc_identity(pid: int) -> dict[str, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return {"pid": pid, "pgid": int(fields[2]), "startTicks": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def _group_members(pgid: int) -> list[int]:
    result = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return result
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            identity = _proc_identity(int(entry.name))
            if identity and identity["pgid"] == pgid:
                result.append(identity["pid"])
    return result


def _terminate_recorded(pid: int, start_ticks: int, pgid: int) -> bool:
    identity = _proc_identity(pid)
    if identity != {"pid": pid, "pgid": pgid, "startTicks": start_ticks}:
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _group_members(pgid):
            return True
        time.sleep(0.05)
    if _group_members(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def _tail_text(path: Path, byte_limit: int = 8192) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - byte_limit))
            return stream.read(byte_limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


@dataclass
class RunHandle:
    run_id: str
    run_dir: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen[bytes] | None = None
    thread: threading.Thread | None = None
    external_failure: str | None = None


class ActiveRunError(RuntimeError):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__("a workbench run is already active")


class Supervisor:
    def __init__(self, lock_fd: int):
        self.lock_fd = lock_fd
        self.lock = threading.RLock()
        self.active: RunHandle | None = None
        self._recover_interrupted()

    def close(self) -> None:
        with self.lock:
            handle = self.active
            if handle and self._status(handle.run_id) in ACTIVE:
                handle.cancel_event.set()
                proc = handle.process
                if proc and proc.poll() is None:
                    self._set_status(handle, "cancelling", cancelRequested=True)
                    self._send_owned_signal(handle, signal.SIGTERM)
        if handle and handle.thread:
            handle.thread.join(timeout=10)
        if handle and handle.process and handle.process.poll() is None:
            self._send_owned_signal(handle, signal.SIGKILL)
            handle.thread.join(timeout=3)
        os.close(self.lock_fd)

    def _recover_interrupted(self) -> None:
        for run_dir in storage.RUNS.iterdir():
            if not run_dir.is_dir():
                continue
            manifest_path = run_dir / "manifest.json"
            try:
                manifest = storage.read_json(manifest_path)
            except (OSError, ValueError):
                continue
            if manifest.get("status") not in ACTIVE:
                continue
            managed = manifest.get("supervisorProcess") or {}
            pid = managed.get("pid")
            start_ticks = managed.get("startTicks")
            pgid = managed.get("processGroupId")
            if pgid is None:
                pgid = managed.get("pgid")  # legacy development records
            if all(isinstance(item, int) for item in (pid, start_ticks, pgid)):
                _terminate_recorded(pid, start_ticks, pgid)
            manifest.update(
                {
                    "status": "interrupted",
                    "finishedAt": now_iso(),
                    "failureReason": "server restarted before the run reached a terminal state",
                }
            )
            storage.atomic_json(manifest_path, manifest)

    def start(
        self,
        kind: str,
        project: dict[str, Any],
        *,
        replay: dict[str, Any] | None = None,
        analysis_step_id: str | None = None,
        slice_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.active and self._status(self.active.run_id) in ACTIVE:
                raise ActiveRunError(self.active.run_id)
            # ``cuda_devices`` is resolved here so patching the supervisor
            # module global still supplies the plan with a fake inventory.
            plan = plan_run(
                kind,
                project,
                replay=replay,
                analysis_step_id=analysis_step_id,
                slice_request=slice_request,
                inventory_provider=cuda_devices,
            )
            handle = RunHandle(plan.run_id, plan.run_dir)
            self.active = handle
            handle.thread = threading.Thread(
                target=self._run, args=(handle,), name=f"viennaps-{plan.run_id}", daemon=True
            )
            handle.thread.start()
            return {"runId": plan.run_id, "status": "pending", "requestedInputHash": plan.request_hash}

    def _run(self, handle: RunHandle) -> None:
        run_dir = handle.run_dir
        try:
            if handle.cancel_event.is_set():
                self._finish(handle, "cancelled", failure="cancelled before worker start")
                return
            manifest = storage.read_json(run_dir / "manifest.json")
            project = manifest["normalizedProject"]
            timeout = int(project["solver"].get("timeoutSeconds") or 0)
            cmd = child_command(run_dir, timeout)
            ready_path = run_dir / ".guard-ready"
            worker_env = os.environ.copy()
            expected_fingerprint = manifest.get("nativeEngineFingerprint")
            if expected_fingerprint:
                runtime_dir, actual_fingerprint = configured_runtime(expected_fingerprint)
                if actual_fingerprint != expected_fingerprint:
                    raise ReplayMismatch("patched ViennaPS engine changed before worker start")
                worker_env["PYTHONPATH"] = os.pathsep.join(
                    [str(runtime_dir), str(ROOT), worker_env.get("PYTHONPATH", "")]
                )
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env=worker_env,
            )
            handle.process = proc
            ready_deadline = time.monotonic() + 10.0
            while not ready_path.is_file() and proc.poll() is None and time.monotonic() < ready_deadline:
                time.sleep(0.02)
            if not ready_path.is_file():
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                if handle.cancel_event.is_set():
                    self._finish(handle, "cancelled", failure="run cancelled before worker startup")
                else:
                    self._finish(handle, "failed", failure="worker guard did not become ready within 10 seconds")
                return
            identity = _proc_identity(proc.pid)
            with self.lock:
                self._set_status(
                    handle,
                    "running",
                    startedAt=now_iso(),
                    supervisorProcess={
                        "pid": proc.pid,
                        "processGroupId": identity["pgid"] if identity else proc.pid,
                        "startTicks": identity["startTicks"] if identity else None,
                    },
                )
            last_storage_check = time.monotonic()
            while proc.poll() is None:
                self._read_events(handle)
                if time.monotonic() - last_storage_check >= 5:
                    last_storage_check = time.monotonic()
                    try:
                        storage.check_disk_preflight()
                        storage.check_artifact_budget(run_dir)
                    except OSError as exc:
                        handle.external_failure = str(exc)
                        self._send_owned_signal(handle, signal.SIGTERM)
                        break
                if handle.cancel_event.is_set() and proc.poll() is None:
                    self._send_owned_signal(handle, signal.SIGTERM)
                time.sleep(0.2)
            proc.wait()
            self._read_events(handle)
            self._complete_from_record(handle, proc.returncode)
        except BaseException as exc:
            try:
                self._finish(handle, "failed", failure=f"supervisor failure: {type(exc).__name__}: {exc}")
            except BaseException:
                pass

    def _read_events(self, handle: RunHandle) -> None:
        path = handle.run_dir / "events.ndjson"
        offset = int(getattr(handle, "event_offset", 0))
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                for raw in stream:
                    offset += len(raw)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._apply_event(handle, item)
        except FileNotFoundError:
            pass
        handle.event_offset = offset

    def _apply_event(self, handle: RunHandle, item: dict[str, Any]) -> None:
        with self.lock:
            manifest = storage.read_json(handle.run_dir / "manifest.json")
            event_type = item.get("type")
            if event_type == "step-started":
                manifest["currentStepId"] = item.get("stepId")
                manifest["currentStepName"] = item.get("name")
            elif event_type == "snapshot":
                entry = {
                    key: item[key]
                    for key in (
                        "stepId",
                        "name",
                        "stepIndex",
                        "snapshot",
                        "vtk",
                        "pointCount",
                        "lineCount",
                        "triangleCount",
                        "topology",
                        "dimension",
                        "materialIds",
                        "materialIdsByLevelSetIndex",
                        "noOp",
                        "measuredGeometry",
                        "geometryReadback",
                        "sliceReadback",
                        "nativeState",
                        "oxidation",
                        "outputs",
                    )
                    if key in item
                }
                if not any(existing.get("stepId") == entry.get("stepId") for existing in manifest["snapshots"]):
                    manifest["snapshots"].append(entry)
                if entry.get("stepId") != "initial" and entry.get("stepId") not in manifest["completedStepIds"]:
                    manifest["completedStepIds"].append(entry["stepId"])
                if entry.get("noOp"):
                    manifest["noOpSteps"].append(
                        {"stepId": entry["stepId"], "reason": "zero-rate step; process was not called"}
                    )
                manifest["currentStepId"] = None
                manifest["currentStepName"] = None
            elif event_type == "worker-failed":
                manifest["workerError"] = item.get("error")
            elif event_type == "step-configured":
                record = {
                    key: item[key]
                    for key in (
                        "stepId",
                        "operation",
                        "model",
                        "noOp",
                        "actualEngine",
                        "engineSelectionReason",
                        "requestedEnginePreference",
                        "actualSeed",
                        "actualRate",
                        "duration",
                        "actualParameters",
                    )
                    if key in item
                }
                existing = manifest.setdefault("actualExecution", [])
                existing[:] = [old for old in existing if old.get("stepId") != record.get("stepId")]
                existing.append(record)
            elif event_type == "flux-result":
                manifest["fluxResult"] = {
                    key: item[key]
                    for key in (
                        "fileName",
                        "vtk",
                        "meshIdentity",
                        "pointCount",
                        "fieldCount",
                        "fieldNames",
                        "actualEngine",
                        "actualSeed",
                    )
                    if key in item
                }
                manifest["currentStepId"] = None
                manifest["currentStepName"] = None
            elif event_type == "intermediate-output":
                output = item.get("output")
                if isinstance(output, dict) and isinstance(output.get("fileName"), str):
                    outputs = manifest.setdefault("intermediateOutputs", [])
                    outputs[:] = [old for old in outputs if old.get("fileName") != output["fileName"]]
                    outputs.append(output)
            storage.atomic_json(handle.run_dir / "manifest.json", manifest)

    def _complete_from_record(self, handle: RunHandle, return_code: int | None) -> None:
        run_dir = handle.run_dir
        try:
            child_record = storage.read_json(run_dir / "child-record.json")
        except (OSError, ValueError):
            child_record = {}
        try:
            worker_result = storage.read_json(run_dir / "worker-result.json")
        except (OSError, ValueError):
            worker_result = None
        if handle.external_failure:
            self._finish(handle, "failed", failure=handle.external_failure, childRecord=child_record)
            return
        if return_code == 0 and worker_result and not child_record.get("remainingOwnedGroupMembers"):
            try:
                storage.check_artifact_budget(run_dir)
                storage.check_disk_preflight()
            except OSError as exc:
                self._finish(handle, "failed", failure=str(exc), childRecord=child_record)
                return
            manifest = storage.read_json(run_dir / "manifest.json")
            if worker_result.get("nativeEngineFingerprint") != manifest.get("nativeEngineFingerprint"):
                self._finish(
                    handle,
                    "failed",
                    failure="worker native engine build identity differs from the planned run",
                    childRecord=child_record,
                )
                return
            actual = worker_result.get("actualSteps", [])
            request_hash = manifest["requestedInputHash"]
            exec_hash = execution_hash(
                request_hash,
                run_seed=worker_result["actualSeed"],
                cpu_threads=worker_result["cpuThreads"],
                step_engines=actual,
                viennaps_version=worker_result["versions"]["viennaps"],
                viennals_version=worker_result["versions"]["viennals"],
                native_engine_fingerprint=manifest.get("nativeEngineFingerprint"),
            )
            self._finish(
                handle,
                "succeeded",
                workerResult=worker_result,
                childRecord=child_record,
                actualExecution=worker_result.get("stepRecords", []),
                executionHash=exec_hash,
            )
            return
        reason = child_record.get("terminationReason")
        if reason == "timeout":
            self._finish(
                handle, "timed_out", failure="run exceeded its configured wall-clock timeout", childRecord=child_record
            )
        elif reason == "supervisor_cancelled" or handle.cancel_event.is_set():
            self._finish(
                handle, "cancelled", failure="run cancelled by the user or server shutdown", childRecord=child_record
            )
        elif reason == "rss_limit":
            self._finish(
                handle,
                "failed",
                failure=f"observed process-group RSS exceeded the {rss_limit_mib()} MiB termination threshold",
                childRecord=child_record,
            )
        elif reason == "log_limit":
            self._finish(
                handle,
                "failed",
                failure=f"combined stdout/stderr exceeded the {max_log_mib()} MiB log cap",
                childRecord=child_record,
            )
        elif reason == "orphaned_child":
            self._finish(
                handle, "failed", failure="worker left a process in its owned process group", childRecord=child_record
            )
        else:
            current = storage.read_json(run_dir / "manifest.json")
            detail = (
                current.get("workerError")
                or _tail_text(run_dir / "run.log")
                or f"worker exited with return code {return_code}"
            )
            self._finish(handle, "failed", failure=detail[-8192:], childRecord=child_record)

    def _finish(self, handle: RunHandle, status: str, *, failure: str | None = None, **updates: Any) -> None:
        with self.lock:
            manifest = storage.read_json(handle.run_dir / "manifest.json")
            if manifest.get("status") in TERMINAL:
                return
            start_epoch = None
            if manifest.get("startedAt"):
                try:
                    start_epoch = calendar.timegm(time.strptime(manifest["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))
                except ValueError:
                    pass
            manifest.update(updates)
            manifest["status"] = status
            manifest["finishedAt"] = now_iso()
            if start_epoch is not None:
                manifest["elapsedSeconds"] = round(max(0.0, time.time() - start_epoch), 3)
            if failure:
                manifest["failureReason"] = failure
            storage.atomic_json(handle.run_dir / "manifest.json", manifest)

    def _set_status(self, handle: RunHandle, status: str, **updates: Any) -> None:
        manifest = storage.read_json(handle.run_dir / "manifest.json")
        if manifest.get("status") in TERMINAL:
            return
        if manifest.get("status") == "cancelling" and status == "running":
            status = "cancelling"
        manifest.update(updates)
        manifest["status"] = status
        storage.atomic_json(handle.run_dir / "manifest.json", manifest)

    def _send_owned_signal(self, handle: RunHandle, signum: int) -> None:
        proc = handle.process
        if not proc:
            return
        try:
            if not (handle.run_dir / ".guard-ready").is_file():
                return
            manifest = storage.read_json(handle.run_dir / "manifest.json")
            identity = manifest.get("supervisorProcess") or {}
            current = _proc_identity(proc.pid)
            expected_pgid = identity.get("processGroupId", identity.get("pgid"))
            if (
                current
                and current.get("startTicks") == identity.get("startTicks")
                and current.get("pgid") == expected_pgid
            ):
                os.kill(proc.pid, signum)
        except (OSError, ValueError):
            pass

    def cancel(self, run_id: str) -> dict[str, Any]:
        manifest = self._manifest(run_id)
        with self.lock:
            status = manifest.get("status")
            if status in TERMINAL:
                return {"runId": run_id, "status": status}
            handle = self.active if self.active and self.active.run_id == run_id else None
            if not handle:
                return {"runId": run_id, "status": status}
            if handle.process and handle.process.poll() is not None:
                return {"runId": run_id, "status": status}
            handle.cancel_event.set()
            self._set_status(handle, "cancelling", cancelRequested=True)
            self._send_owned_signal(handle, signal.SIGTERM)
            return {"runId": run_id, "status": "cancelling"}

    def _manifest(self, run_id: str) -> dict[str, Any]:
        return storage.read_json(storage.run_path(run_id) / "manifest.json")

    def _status(self, run_id: str) -> str:
        try:
            return str(self._manifest(run_id).get("status"))
        except (OSError, ValueError):
            return "unknown"

    def get_run(self, run_id: str, *, cursor: int = 0) -> dict[str, Any]:
        run_dir = storage.run_path(run_id)
        manifest = storage.read_json(run_dir / "manifest.json")
        log_path = run_dir / "run.log"
        log_size = log_path.stat().st_size if log_path.exists() else 0
        if cursor < 0 or cursor > log_size:
            cursor = max(0, min(cursor, log_size))
        with log_path.open("rb") if log_path.exists() else open(os.devnull, "rb") as stream:
            stream.seek(cursor)
            data = stream.read(64 * 1024)
        next_cursor = cursor + len(data)
        try:
            child_record = storage.read_json(run_dir / "child-record.json")
            was_truncated = child_record.get("logBytesDiscarded", 0) > 0
            termination = {
                "reason": child_record.get("terminationReason"),
                "returnCode": child_record.get("returnCode"),
                "rssLimitMiB": child_record.get("rssLimitMiB"),
                "peakObservedRssBytes": child_record.get("peakObservedRssBytes"),
            }
        except (OSError, ValueError):
            was_truncated = log_size >= max_log_bytes()
            termination = None
        return {
            "runId": run_id,
            "kind": manifest.get("kind"),
            "status": manifest.get("status"),
            "currentStepId": manifest.get("currentStepId"),
            "completedStepIds": manifest.get("completedStepIds", []),
            "stepTotal": sum(
                1 for step in manifest.get("normalizedProject", {}).get("steps", []) if step.get("enabled")
            )
            if manifest.get("kind") == "simulate"
            else 0,
            "startedAt": manifest.get("startedAt"),
            "elapsedSeconds": round(
                max(0.0, time.time() - calendar.timegm(time.strptime(manifest["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))), 3
            )
            if manifest.get("status") in ACTIVE and manifest.get("startedAt")
            else manifest.get("elapsedSeconds", 0),
            "requestedInputHash": manifest.get("requestedInputHash"),
            "executionHash": manifest.get("executionHash"),
            "runMode": manifest.get("runMode", "from-initial"),
            "sliceRequest": manifest.get("sliceRequest"),
            "initialState": manifest.get("initialState"),
            "fluxResult": manifest.get("fluxResult"),
            "intermediateOutputs": manifest.get("intermediateOutputs", []),
            "failureReason": manifest.get("failureReason"),
            "termination": termination,
            "log": {
                "cursor": cursor,
                "nextCursor": next_cursor,
                "truncated": was_truncated,
                "text": data.decode("utf-8", errors="replace"),
            },
            "snapshots": manifest.get("snapshots", []),
            "actualExecution": manifest.get("actualExecution", []),
            "v2Steps": read_v2_steps(run_dir),
            "completedAt": manifest.get("finishedAt"),
        }

    def get_manifest(self, run_id: str) -> dict[str, Any]:
        return self._manifest(run_id)
