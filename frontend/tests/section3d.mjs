import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { cutTriangles } from '../src/section3d.ts';
const root = path.resolve(import.meta.dirname, '../..');
const rows = JSON.parse(fs.readFileSync(path.join(root, 'var/evidence/t02-t05.json')));
const evidence = [];
for (const row of rows.filter(r => r.kind === 'hole' || r.kind === 'trench')) {
  const mesh = JSON.parse(fs.readFileSync(path.join(root, `var/runs/${row.runId}/initial.json`)));
  for (const axis of ['x', 'y', 'z']) {
    const segments = cutTriangles(mesh, axis, axis === 'z' ? -1 : 0);
    assert(segments.length > 0);
    assert(segments.every(s => s.a.every(Number.isFinite) && s.b.every(Number.isFinite)));
    assert(segments.every(s => mesh.cellData.materialIds.includes(s.materialId)));
    evidence.push({ runId: row.runId, kind: row.kind, boundary: row.boundary, axis, segmentCount: segments.length });
  }
  const central = cutTriangles(mesh, 'y', 0).filter(s => s.materialId === 10).flatMap(s => [s.a, s.b]);
  const depth = -Math.min(...central.map(p => p[2]));
  assert(Math.abs(depth - 2) <= .4);
  const top = central.filter(p => Math.abs(p[2]) <= .01);
  const neg = top.filter(p => p[0] < 0).map(p => p[0]), pos = top.filter(p => p[0] > 0).map(p => p[0]);
  const opening = Math.min(...pos) - Math.max(...neg);
  assert(Math.abs(opening - 2) <= .4, `opening ${opening}`);
  if (row.kind === 'hole') {
    for (const z of [-1.8, -.2, .2, .4]) {
      const points = cutTriangles(mesh, 'z', z).flatMap(s => [s.a, s.b]);
      const radius = Math.min(...points.map(p => Math.hypot(p[0], p[1])));
      const expected = 1 + z * Math.tan(10 * Math.PI / 180);
      assert(Math.abs(radius - expected) <= .4);
      evidence.push({ runId: row.runId, z, measuredInnerRadius: radius, expectedInnerRadius: expected, tolerance: .4 });
    }
  }
  evidence.push({ runId: row.runId, centralSectionOpeningWidth: opening, centralSectionDepth: depth, tolerance: .4 });
}
assert.deepEqual(cutTriangles({ points: [[0,0,0],[1,0,0],[0,1,0]], triangles: [[0,1,2]], cellData: { materialIds: [10] } }, 'z', 0), []);
fs.writeFileSync(path.join(root, 'var/evidence/t11-sections.json'), JSON.stringify(evidence,null,2));
console.log(`PASS: real 3D mesh sections, ${evidence.length} checks`);
