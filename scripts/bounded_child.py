#!/usr/bin/env python3
"""Run one child process with timeout, process-group, log and RSS supervision.

RSS polling is a best-effort termination threshold, not a kernel hard memory
limit. This module intentionally imports only the Python standard library.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLL_SECONDS = 0.2
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def _process_group_members(pgid: int) -> list[dict[str, int]]:
    members: list[dict[str, int]] = []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return members
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, "stat").read_text(encoding="ascii")
                right = stat.rfind(")")
                fields = stat[right + 2 :].split()
                if len(fields) < 22 or int(fields[2]) != pgid:
                    continue
                rss_pages = max(0, int(fields[21]))
                members.append(
                    {
                        "pid": int(entry.name),
                        "startTicks": int(fields[19]),
                        "rssBytes": rss_pages * PAGE_SIZE,
                    }
                )
            except (OSError, ValueError, IndexError):
                continue
    return members


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class LogCollector:
    def __init__(self, log_path: Path, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.total = 0
        self.discarded = 0
        self.by_stream = {"stdout": 0, "stderr": 0}
        self.exceeded = threading.Event()
        self.lock = threading.Lock()
        self.log_path = log_path

    def pump(self, stream: Any, source: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            chunk = stream.read1(64 * 1024) if hasattr(stream, "read1") else stream.read(64 * 1024)
            if not chunk:
                break
            with self.lock:
                allowance = max(0, self.max_bytes - self.total)
                accepted = chunk[:allowance]
                self.total += len(accepted)
                self.by_stream[source] += len(accepted)
                self.discarded += len(chunk) - len(accepted)
                if len(chunk) > allowance:
                    self.exceeded.set()
                if accepted:
                    with self.log_path.open("ab") as output:
                        output.write(accepted)


def _set_parent_death_signal(expected_parent: int) -> None:
    """Terminate the engine process if its supervising wrapper disappears."""
    libc = ctypes.CDLL(None, use_errno=True)
    # Linux PR_SET_PDEATHSIG = 1. This does not alter system configuration.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "prctl(PR_SET_PDEATHSIG)")
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGTERM)


def _terminate_group(
    pgid: int, grace_seconds: float, leader: subprocess.Popen[bytes] | None = None
) -> list[dict[str, int]]:
    members = _process_group_members(pgid)
    if members:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _process_group_members(pgid):
        if leader is not None:
            leader.poll()  # reap the group leader so it does not look like an orphaned zombie
        time.sleep(0.05)
    members = _process_group_members(pgid)
    if members:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if leader is not None:
            leader.poll()
        members = _process_group_members(pgid)
        if not members:
            break
        time.sleep(0.05)
    return _process_group_members(pgid)


def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-rss-mib", type=int, default=2048)
    parser.add_argument("--max-log-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--terminate-grace-seconds", type=float, default=1.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    cwd = args.cwd.resolve()
    record_path = args.record.resolve()
    log_path = args.log_path.resolve() if args.log_path else record_path.with_suffix(".run.log")
    ready_path = args.ready_file.resolve() if args.ready_file else None
    if not command:
        parser.error("缺少被监督的命令；命令前请使用 --")
    if (not _inside_root(cwd) or not _inside_root(record_path.parent) or not _inside_root(log_path.parent)
            or (ready_path is not None and not _inside_root(ready_path.parent))):
        parser.error("cwd、record 和 log 必须位于正式工作台目录内")
    if args.timeout_seconds < 0 or args.max_rss_mib <= 0 or args.max_log_bytes <= 0:
        parser.error("超时时间不能为负，资源限制必须为正数")
    if not cwd.is_dir():
        parser.error("cwd 不存在或不是目录")

    terminate_requested = threading.Event()

    def request_termination(_signum: int, _frame: Any) -> None:
        terminate_requested.set()

    previous_term = signal.signal(signal.SIGTERM, request_termination)
    previous_int = signal.signal(signal.SIGINT, request_termination)
    # Establish parent-death handling in this single-threaded guard process.
    # The PID check also closes the race where the API exits before this
    # process starts and Linux has already reparented it.
    try:
        _set_parent_death_signal(args.parent_pid if args.parent_pid is not None else os.getppid())
        if terminate_requested.is_set():
            return 130
    except BaseException:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        raise

    record_path.parent.mkdir(parents=True, exist_ok=True)
    log = LogCollector(log_path, args.max_log_bytes)
    parent_pid = os.getpid()
    started_wall = time.time()
    started = time.monotonic()
    try:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            preexec_fn=lambda: _set_parent_death_signal(parent_pid),
        )
    except BaseException:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        raise
    pgid = child.pid
    initial_members = _process_group_members(pgid)
    pumpers = [
        threading.Thread(target=log.pump, args=(child.stdout, "stdout"), daemon=True),
        threading.Thread(target=log.pump, args=(child.stderr, "stderr"), daemon=True),
    ]
    for thread in pumpers:
        thread.start()
    if ready_path is not None:
        _atomic_json(ready_path, {"pid": parent_pid, "childPid": child.pid, "processGroupId": pgid})

    reason = "exited"
    max_rss_bytes = 0
    last_members = initial_members
    timed_out = False
    rss_limited = False
    log_limited = False
    try:
        while child.poll() is None:
            last_members = _process_group_members(pgid)
            current_rss = sum(item["rssBytes"] for item in last_members)
            max_rss_bytes = max(max_rss_bytes, current_rss)
            elapsed = time.monotonic() - started
            if terminate_requested.is_set():
                reason = "supervisor_cancelled"
                break
            if log.exceeded.is_set():
                reason = "log_limit"
                log_limited = True
                break
            if current_rss > args.max_rss_mib * 1024 * 1024:
                reason = "rss_limit"
                rss_limited = True
                break
            if args.timeout_seconds > 0 and elapsed > args.timeout_seconds:
                reason = "timeout"
                timed_out = True
                break
            time.sleep(POLL_SECONDS)
        if reason != "exited":
            last_members = _terminate_group(pgid, args.terminate_grace_seconds, child)
        try:
            return_code = child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            last_members = _terminate_group(pgid, 0.1, child)
            return_code = child.wait(timeout=2.0)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        for thread in pumpers:
            thread.join(timeout=2.0)

    if reason == "exited" and log.exceeded.is_set():
        reason = "log_limit"
        log_limited = True

    elapsed_seconds = time.monotonic() - started
    final_members = _process_group_members(pgid)
    if final_members and reason == "exited":
        # The leader exited but left children in its owned group. Reap the group.
        reason = "orphaned_child"
        final_members = _terminate_group(pgid, args.terminate_grace_seconds, child)
    record = {
        "schemaVersion": 1,
        "command": command,
        "cwd": str(cwd),
        "supervisorPid": parent_pid,
        "pid": child.pid,
        "pidStartTicks": next((m["startTicks"] for m in initial_members if m["pid"] == child.pid), None),
        "processGroupId": pgid,
        "startedAtUnix": started_wall,
        "elapsedSeconds": round(elapsed_seconds, 6),
        "timeoutSeconds": args.timeout_seconds,
        "timeoutDisabled": args.timeout_seconds <= 0,
        "rssLimitMiB": args.max_rss_mib,
        "peakObservedRssBytes": max_rss_bytes,
        "rssLimitIsPollingTerminationNotHardLimit": True,
        "maxLogBytes": args.max_log_bytes,
        "logBytesRetained": log.total,
        "logBytesDiscarded": log.discarded,
        "logBytesByStream": log.by_stream,
        "terminationReason": reason,
        "timedOut": timed_out,
        "rssLimited": rss_limited,
        "logLimited": log_limited,
        "returnCode": return_code,
        "remainingOwnedGroupMembers": final_members,
    }
    _atomic_json(record_path, record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    if reason == "exited" and return_code == 0 and not final_members:
        return 0
    if reason == "timeout":
        return 124
    if reason == "rss_limit":
        return 125
    if reason == "log_limit":
        return 126
    if reason == "supervisor_cancelled":
        return 130
    return return_code if return_code else 1


if __name__ == "__main__":
    raise SystemExit(main())
