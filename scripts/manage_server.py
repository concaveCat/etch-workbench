"""Start/stop/status helper; it only signals a process matching our saved identity."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAR = Path(os.environ.get("VIENNAPS_WORKBENCH_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()
IDENTITY_FILE = VAR / "server.json"
SERVE_SCRIPT = ROOT / "backend" / "serve.py"


def process_identity(pid: int) -> dict[str, int | str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return {"pid": pid, "state": fields[0], "pgid": int(fields[2]), "startTicks": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def load_owned() -> dict[str, object] | None:
    try:
        saved = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
        pid = int(saved["pid"])
        current = process_identity(pid)
        if not current or current["state"] == "Z" or current["startTicks"] != int(saved["startTicks"]):
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        if str(SERVE_SCRIPT) not in command or saved.get("script") != str(SERVE_SCRIPT):
            return None
        return saved
    except (OSError, ValueError, KeyError, TypeError):
        return None


def health_probe(port: int) -> bool:
    deadline = time.monotonic() + 3
    connection = socket.create_connection(("127.0.0.1", port), timeout=1)
    try:
        connection.settimeout(max(0.1, deadline - time.monotonic()))
        connection.sendall(f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode("ascii"))
        response = connection.recv(512)
        return b" 200 " in response and b"application/json" in response.lower()
    finally:
        connection.close()


def start() -> int:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("frontend build is missing; install the locked npm dependencies and run scripts/build-frontend.sh first", file=sys.stderr)
        return 1
    existing = load_owned()
    if existing:
        print(f"Etch Workbench is already running at http://127.0.0.1:{existing['port']}/")
        return 0
    VAR.mkdir(parents=True, exist_ok=True)
    log_path = VAR / "server.log"
    with log_path.open("ab", buffering=0) as log:
        proc = subprocess.Popen([str(ROOT / ".venv" / "bin" / "python"), str(SERVE_SCRIPT)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(f"server exited with status {proc.returncode}; see {log_path}", file=sys.stderr)
            return 1
        identity = load_owned()
        if identity:
            try:
                if health_probe(int(identity["port"])):
                    print(f"Etch Workbench is running at http://127.0.0.1:{identity['port']}/")
                    return 0
            except OSError:
                pass
        time.sleep(0.25)
    print(f"server did not become healthy within 30 seconds; see {log_path}", file=sys.stderr)
    return 1


def stop() -> int:
    identity = load_owned()
    if not identity:
        print("Etch Workbench is not running (or its saved process identity no longer matches).")
        return 0
    pid = int(identity["pid"])
    current = process_identity(pid)
    if not current or current["state"] == "Z" or current["startTicks"] != int(identity["startTicks"]):
        print("server process identity changed; no signal sent", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        current = process_identity(pid)
        if current is None or current["state"] == "Z":
            print("Etch Workbench stopped.")
            return 0
        time.sleep(0.1)
    current = process_identity(pid)
    if current and current["state"] != "Z" and current["startTicks"] == int(identity["startTicks"]):
        os.kill(pid, signal.SIGKILL)
        print("Etch Workbench did not stop after SIGTERM; the verified application PID was stopped with SIGKILL.")
        return 1
    print("Etch Workbench stopped.")
    return 0


def status() -> int:
    identity = load_owned()
    if not identity:
        print("Etch Workbench is not running.")
        return 1
    try:
        healthy = health_probe(int(identity["port"]))
    except OSError:
        healthy = False
    state = "healthy" if healthy else "process exists but health check is not responding"
    print(f"Etch Workbench {state}: http://127.0.0.1:{identity['port']}/ (PID {identity['pid']})")
    return 0 if healthy else 1


if __name__ == "__main__":
    actions = {"start": start, "stop": stop, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit("usage: manage_server.py start|stop|status")
    raise SystemExit(actions[sys.argv[1]]())
