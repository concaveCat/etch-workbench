"""Small HTTP client used by the P2 real-restart acceptance script."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys


def call(base: str, path: str, method: str = "GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}", method=method, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def create(base: str, project_path: str) -> str:
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    adopted = call(base, "/api/v1/workflow/flows/from-project", "POST", {"project": project})
    stamp = str(int(time.time() * 1000))
    metric_id = f"metric_rec_{stamp}"
    recipe_id = f"recipe_rec_{stamp}"
    call(base, "/api/v1/workflow/metrics", "POST", {
        "schemaVersion": 1, "metricId": metric_id, "name": "recovery depth", "unit": "um",
    })
    call(base, "/api/v1/workflow/metrology-recipes", "POST", {
        "schemaVersion": 1, "recipeId": recipe_id, "version": 1, "metricId": metric_id,
        "name": "recovery depth", "dimension": 2, "method": "etch_depth",
        "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0}, "material": "Si",
    })
    steps = [step["id"] for step in project["steps"] if step.get("enabled")]
    document = call(base, "/api/v1/workflow/experiments/doe", "POST", {
        "name": "restart recovery", "project": project, "flowId": adopted["flowId"],
        "compileId": adopted["compileId"], "recipeId": recipe_id, "recipeVersion": 1,
        "factors": [
            {"stepId": steps[0], "parameter": "rateMagnitude", "values": [0.01, 0.02, 0.03]},
            {"stepId": steps[1], "parameter": "duration", "values": [0.01, 0.02]},
            {"stepId": steps[2], "parameter": "duration", "values": [0.01, 0.02]},
        ],
        "replicates": 1, "seed": 8800, "maxAttempts": 2,
    })
    call(base, f"/api/v1/workflow/experiments/{document['experimentId']}/run", "POST", {})
    deadline = time.monotonic() + 60
    current = document
    while time.monotonic() < deadline:
        current = call(base, f"/api/v1/workflow/experiments/{document['experimentId']}")
        if current.get("queue", {}).get("running", 0) >= 1 or current.get("queue", {}).get("succeeded", 0) >= 1:
            break
        time.sleep(0.1)
    print(document["experimentId"])
    return document["experimentId"]


def verify(base: str, experiment_id: str) -> dict:
    deadline = time.monotonic() + 180
    document = {}
    while time.monotonic() < deadline:
        document = call(base, f"/api/v1/workflow/experiments/{experiment_id}")
        if document.get("status") in {"completed", "failed"}:
            break
        time.sleep(0.5)
    if document.get("status") != "completed":
        raise SystemExit(f"experiment did not complete after restart: {json.dumps(document)[:800]}")
    expected = int(document["design"]["logicalRunCount"])
    replicates = [
        replicate
        for variant in document["variants"]
        for replicate in variant["replicates"]
    ]
    if document["queue"]["succeeded"] != expected:
        raise SystemExit(f"expected {expected} succeeded runs, got {document['queue']['succeeded']}")
    if sum(1 for replicate in replicates if replicate.get("statisticalSample")) != expected:
        raise SystemExit("statistical sample count changed after restart")
    if sum(row["n"] for row in document["aggregation"]["rows"]) != expected:
        raise SystemExit("aggregation counted a retry as a new sample")
    if any(replicate.get("status") != "succeeded" for replicate in replicates):
        raise SystemExit("not every replicate succeeded")
    summary = {
        "experimentId": experiment_id,
        "status": document["status"],
        "conditions": document["design"]["conditionCount"],
        "logicalRuns": expected,
        "succeeded": document["queue"]["succeeded"],
        "retries": document["queue"]["retries"],
        "executionAttempts": document["queue"]["executionAttempts"],
        "attemptCounts": sorted({replicate["attemptCount"] for replicate in replicates}),
        "aggregateSamples": sum(row["n"] for row in document["aggregation"]["rows"]),
    }
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: workflow_p2_recovery_client.py create|verify BASE EXPERIMENT|PROJECT")
    command, base, argument = sys.argv[1], sys.argv[2], sys.argv[3]
    if command == "create":
        create(base, argument)
    elif command == "verify":
        verify(base, argument)
    else:
        raise SystemExit(f"unknown command {command!r}")
