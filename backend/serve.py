"""Bind the local API to one loopback port without workers or reloaders."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.storage import VAR, atomic_json  # noqa: E402


def _start_ticks(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = stat[stat.rfind(")") + 2 :].split()
    return int(fields[19])


def main() -> int:
    import uvicorn

    VAR.mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = None
    configured = 8767
    try:
        configured = int(json.loads((VAR / "listen-port.json").read_text())["port"])
    except FileNotFoundError:
        pass
    if not 1024 <= configured <= 65535:
        raise ValueError("configured local port must be 1024..65535")
    for candidate in [int(os.environ.get("WORKBENCH_PORT", str(configured)))]:
        try:
            listener.bind(("127.0.0.1", candidate))
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        raise RuntimeError(f"cannot bind configured loopback port {os.environ.get('WORKBENCH_PORT', configured)}; no other listener was changed")
    listener.listen(128)
    os.environ["WORKBENCH_PORT"] = str(port)
    identity = {
        "pid": os.getpid(),
        "startTicks": _start_ticks(os.getpid()),
        "processGroupId": os.getpgrp(),
        "host": "127.0.0.1",
        "port": port,
        "script": str(ROOT / "backend" / "serve.py"),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    identity_path = VAR / "server.json"
    atomic_json(identity_path, identity)
    config = uvicorn.Config("backend.main:app", host="127.0.0.1", port=port, workers=1, reload=False,
                            access_log=False, log_level="info", server_header=False, date_header=True)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        try:
            current = json.loads(identity_path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid() and current.get("startTicks") == identity["startTicks"]:
                identity_path.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
