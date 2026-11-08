import assert from 'node:assert/strict';

/** Verify the pinned CUDA device after ViennaPS returned; nvidia-smi PID visibility is advisory. */
export function assertGpuReadback(readback, expectedUuid, label = 'GPU run') {
  assert.equal(readback?.readbackStatus, 'verified', `${label}: pre-run CUDA readback`);
  assert.equal(readback?.uuid, expectedUuid, `${label}: selected CUDA UUID`);
  assert.equal(readback?.synchronizeStatus, 0, `${label}: pre-run CUDA synchronization`);
  const after = readback?.afterProcessApply ?? readback?.afterCalculateFlux;
  assert.ok(after, `${label}: post-run CUDA readback missing`);
  assert.equal(after.readbackStatus, 'verified', `${label}: post-run CUDA readback`);
  assert.equal(after.uuid, expectedUuid, `${label}: CUDA UUID after ViennaPS returned`);
  assert.equal(after.synchronizeStatus, 0, `${label}: CUDA synchronize after ViennaPS returned`);
  const process = readback?.process;
  assert.ok(process, `${label}: process visibility record missing`);
  assert.equal(process.processId > 0, true, `${label}: worker PID missing`);
  assert.equal(process.processListed, process.processListStatus === 'verified',
    `${label}: process listing state is internally inconsistent`);
  assert.ok(['verified', 'not-observed', 'unavailable'].includes(process.processListStatus),
    `${label}: unsupported nvidia-smi visibility status ${process.processListStatus}`);
  if (process.processListStatus === 'verified') {
    assert.equal(process.gpuUuid, expectedUuid, `${label}: nvidia-smi GPU UUID`);
  } else {
    assert.equal(process.expectedGpuUuid, expectedUuid, `${label}: expected GPU UUID in visibility record`);
  }
}
