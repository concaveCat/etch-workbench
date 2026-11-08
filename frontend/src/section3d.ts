import type { Axis3D, Surface3D, Vec3 } from './types3d';
export type SectionSegment = { a: Vec3; b: Vec3; materialId: number };
export const axisIndex = (axis: Axis3D) => ({ x: 0, y: 1, z: 2 })[axis];
/** Surface intersection only. Coplanar faces are skipped; no inferred solid fill. */
export function cutTriangles(mesh: Surface3D, axis: Axis3D, position: number): SectionSegment[] {
  const k = axisIndex(axis), eps = 1e-9;
  const segments = new Map<string, SectionSegment>();
  for (let cell = 0; cell < (mesh.triangles?.length ?? 0); cell++) {
    const t = mesh.triangles![cell].map(i => mesh.points[i]);
    const d = t.map(p => p[k] - position);
    if (d.every(x => Math.abs(x) <= eps) || d.every(x => x > eps) || d.every(x => x < -eps)) continue;
    const hit: Vec3[] = [];
    const add = (p: Vec3) => { if (!hit.some(q => Math.hypot(...p.map((v, i) => v - q[i])) <= eps)) hit.push(p); };
    for (const [a, b] of [[0, 1], [1, 2], [2, 0]]) {
      if (Math.abs(d[a]) <= eps) add([...t[a]] as Vec3);
      if (d[a] * d[b] < -eps * eps) {
        const u = d[a] / (d[a] - d[b]); add(t[a].map((x, i) => x + (t[b][i] - x) * u) as Vec3);
      }
    }
    if (hit.length === 2 && Math.hypot(...hit[0].map((x, i) => x - hit[1][i])) > eps) {
      const materialId = mesh.cellData.materialIds[cell];
      const ends = hit.map(p => p.map(v => Math.round(v / eps)).join(',')).sort();
      segments.set(`${materialId}:${ends.join('|')}`, { a: hit[0], b: hit[1], materialId });
    }
  }
  return [...segments.values()];
}
