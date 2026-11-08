import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

const base = process.env.WORKBENCH_URL ?? 'http://127.0.0.1:8809';
const dataRoot = process.env.VIENNAPS_WORKBENCH_DATA_DIR;
const sourceRunId = process.env.SOURCE_RUN_ID;
const expectedFingerprint = process.env.EXPECTED_FINGERPRINT;
assert.ok(dataRoot && sourceRunId && expectedFingerprint);
const replayPath = path.join(dataRoot, 'runs', sourceRunId, 'replay-project.json');
const replay = JSON.parse(await fs.readFile(replayPath, 'utf8'));
assert.equal(replay.executionRequirements.nativeEngineFingerprint, expectedFingerprint);

let runId = process.env.REPLAY_RUN_ID;
if (!runId) {
  const started = await fetch(`${base}/api/runs/replay`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(replay),
  });
  const payload = await started.json();
  assert.equal(started.status, 202, JSON.stringify(payload));
  runId = payload.runId;
}
const deadline = Date.now() + 180_000;
let result;
while (Date.now() < deadline) {
  const response = await fetch(`${base}/api/runs/${runId}`);
  assert.equal(response.status, 200);
  result = await response.json();
  if (['succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'].includes(result.status)) break;
  await new Promise(resolve => setTimeout(resolve, 300));
}
assert.equal(result?.status, 'succeeded', result?.failureReason ?? 'replay timed out');
const runDir = path.join(dataRoot, 'runs', runId);
const manifest = JSON.parse(await fs.readFile(path.join(runDir, 'manifest.json'), 'utf8'));
const worker = JSON.parse(await fs.readFile(path.join(runDir, 'worker-result.json'), 'utf8'));
const child = JSON.parse(await fs.readFile(path.join(runDir, 'child-record.json'), 'utf8'));
assert.equal(manifest.nativeEngineFingerprint, expectedFingerprint);
assert.equal(worker.nativeEngineFingerprint, expectedFingerprint);
assert.equal(child.returnCode, 0);
assert.deepEqual(child.remainingOwnedGroupMembers, []);
assert.equal(worker.actualSteps[0].engine, 'GPU_DISK');

const evidence = {
  status: 'passed', sourceRunId, replayRunId: runId, expectedFingerprint,
  loadedFingerprint: worker.nativeEngineFingerprint,
  engine: worker.actualSteps[0].engine,
  gpuUuid: worker.selectedGpuDevice?.uuid,
  childReturnCode: child.returnCode,
  remainingOwnedGroupMembers: child.remainingOwnedGroupMembers,
};
const evidencePath = process.env.EVIDENCE_PATH ?? path.join(
  process.cwd(), 'docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/retained-runtime-replay-acceptance.json');
await fs.writeFile(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`);
console.log(JSON.stringify(evidence, null, 2));
