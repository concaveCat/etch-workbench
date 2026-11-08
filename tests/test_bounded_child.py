from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "bounded_child.py"


class BoundedChildTests(unittest.TestCase):
    def run_guard(self, directory: Path, code: str, *, timeout: float = 10, rss: int = 2048, log_cap: int = 1024 * 1024) -> tuple[subprocess.CompletedProcess[str], dict[str, object], bytes]:
        record = directory / "record.json"
        log = directory / "run.log"
        command = [sys.executable, str(GUARD), "--cwd", str(ROOT), "--record", str(record), "--log-path", str(log),
                   "--timeout-seconds", str(timeout), "--max-rss-mib", str(rss), "--max-log-bytes", str(log_cap),
                   "--terminate-grace-seconds", "0.2", "--", sys.executable, "-c", code]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
        return result, json.loads(record.read_text(encoding="utf-8")), log.read_bytes() if log.exists() else b""

    def test_success_captures_both_streams(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            result, record, log = self.run_guard(Path(directory), "import sys; print('out'); print('err', file=sys.stderr)")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(record["terminationReason"], "exited")
            self.assertIn(b"out", log)
            self.assertIn(b"err", log)

    def test_timeout_has_a_distinct_terminal_reason(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            result, record, _ = self.run_guard(Path(directory), "import time; time.sleep(10)", timeout=0.3)
            self.assertEqual(result.returncode, 124)
            self.assertEqual(record["terminationReason"], "timeout")
            self.assertEqual(record["remainingOwnedGroupMembers"], [])

    def test_zero_timeout_disables_the_watchdog(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            result, record, _ = self.run_guard(Path(directory), "import time; time.sleep(0.4)", timeout=0)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(record["terminationReason"], "exited")
            self.assertTrue(record["timeoutDisabled"])
            self.assertFalse(record["timedOut"])

    def test_rss_poll_threshold_terminates_the_owned_group(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            result, record, _ = self.run_guard(Path(directory), "import time; time.sleep(10)", rss=1)
            self.assertEqual(result.returncode, 125)
            self.assertEqual(record["terminationReason"], "rss_limit")
            self.assertTrue(record["rssLimitIsPollingTerminationNotHardLimit"])

    def test_log_cap_keeps_only_the_allowed_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            result, record, log = self.run_guard(Path(directory), "import os; os.write(1, b'x' * 100000)", log_cap=1024)
            self.assertEqual(result.returncode, 126)
            self.assertEqual(record["terminationReason"], "log_limit")
            self.assertLessEqual(len(log), 1024)
            self.assertGreater(record["logBytesDiscarded"], 0)

    def test_supervisor_signal_is_reaped_before_recording_cancel(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            root = Path(directory)
            record = root / "record.json"
            log = root / "run.log"
            command = [sys.executable, str(GUARD), "--cwd", str(ROOT), "--record", str(record), "--log-path", str(log),
                       "--timeout-seconds", "10", "--max-rss-mib", "2048", "--max-log-bytes", "1024", "--",
                       sys.executable, "-c", "import time; time.sleep(10)"]
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.3)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=8), 130)
            result = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(result["terminationReason"], "supervisor_cancelled")
            self.assertEqual(result["remainingOwnedGroupMembers"], [])

    def test_parent_process_death_reaps_guarded_worker(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as directory:
            root = Path(directory)
            worker_pid_path = root / "worker.pid"
            guard_pid_path = root / "guard.pid"
            record = root / "record.json"
            log = root / "run.log"
            worker_code = "import pathlib,os,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            helper_code = textwrap.dedent("""
                import os, pathlib, subprocess, sys, time
                root, guard, record, log, worker_pid = map(pathlib.Path, sys.argv[1:6])
                command = [
                    sys.executable, str(guard), "--cwd", str(root), "--record", str(record),
                    "--log-path", str(log), "--parent-pid", str(os.getpid()),
                    "--timeout-seconds", "60", "--max-rss-mib", "2048",
                    "--max-log-bytes", "1024", "--terminate-grace-seconds", "0.2", "--",
                    sys.executable, "-c", sys.argv[6], str(worker_pid),
                ]
                child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True, close_fds=True)
                pathlib.Path(sys.argv[7]).write_text(str(child.pid))
                time.sleep(60)
            """)
            parent = subprocess.Popen(
                [sys.executable, "-c", helper_code, str(ROOT), str(GUARD), str(record), str(log),
                 str(worker_pid_path), worker_code, str(guard_pid_path)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not (guard_pid_path.exists() and worker_pid_path.exists()):
                if parent.poll() is not None:
                    self.fail("parent helper exited before the guarded worker started")
                time.sleep(0.05)
            self.assertTrue(guard_pid_path.exists(), "guard did not start")
            self.assertTrue(worker_pid_path.exists(), "worker did not start")
            guard_pid = int(guard_pid_path.read_text())
            worker_pid = int(worker_pid_path.read_text())

            parent.kill()
            parent.wait(timeout=3)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                active = []
                for pid in (guard_pid, worker_pid):
                    try:
                        fields = Path(f"/proc/{pid}/stat").read_text().split()
                        if fields[2] != "Z":
                            active.append(pid)
                    except FileNotFoundError:
                        pass
                if not active:
                    break
                time.sleep(0.05)
            self.assertEqual(active, [], f"parent death left active child processes: {active}")
            if record.exists():
                result = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual(result["terminationReason"], "supervisor_cancelled")
                self.assertEqual(result["remainingOwnedGroupMembers"], [])


if __name__ == "__main__":
    unittest.main()
