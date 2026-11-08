import type { Snapshot } from '../api/types';

// Group snapshot line segments into one SVG path per material id so the
// viewer draws one <path> element per material instead of one per segment.
export function linePathsByMaterial(snapshot: Snapshot | null): Map<number, string> {
  const segments = new Map<number, string[]>();
  if (!snapshot) return new Map();
  for (let index = 0; index < (snapshot.lines?.length ?? 0); index++) {
    const materialId = snapshot.cellData.materialIds[index];
    const line = snapshot.lines![index];
    const a = snapshot.points[line[0]];
    const b = snapshot.points[line[1]];
    if (!a || !b) continue;
    const paths = segments.get(materialId) ?? [];
    paths.push(`M${Number(a[0].toPrecision(9))},${Number(a[1].toPrecision(9))}L${Number(b[0].toPrecision(9))},${Number(b[1].toPrecision(9))}`);
    segments.set(materialId, paths);
  }
  return new Map([...segments].map(([materialId, paths]) => [materialId, paths.join('')]));
}
