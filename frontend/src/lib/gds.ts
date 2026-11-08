import type { GDSAsset, Project } from '../api/types';

// GDS cell traversal: collect layer/datatype pairs reachable from a structure,
// following SREF references the same way the backend flattens them.
export function gdsLayerIds(asset: GDSAsset, name: string): number[] {
  const byName = new Map(asset.structures.map(item => [item.name, item]));
  const layers = new Set<number>(); const seen = new Set<string>(); const pending = [name];
  while (pending.length) {
    const current = pending.pop()!;
    if (seen.has(current)) continue;
    seen.add(current);
    const cell = byName.get(current);
    if (!cell) continue;
    cell.layers.forEach(layer => layers.add(layer));
    pending.push(...cell.references);
  }
  return [...layers].sort((a, b) => a - b);
}
export function gdsPairs(asset: GDSAsset, name: string): [number, number][] {
  const byName = new Map(asset.structures.map(item => [item.name, item]));
  const pairs = new Set<string>(); const seen = new Set<string>(); const pending = [name];
  while (pending.length) {
    const current = pending.pop()!;
    if (seen.has(current)) continue;
    seen.add(current);
    const cell = byName.get(current);
    if (!cell) continue;
    (cell.layerDatatypes ?? cell.layers.map(layer => [layer, 0] as [number, number])).forEach(([layer, datatype]) => pairs.add(`${layer}/${datatype}`));
    pending.push(...cell.references);
  }
  return [...pairs].map(value => value.split('/').map(Number) as [number, number]).sort((a,b) => a[0]-b[0] || a[1]-b[1]);
}
export function gdsMaskPairs(asset: GDSAsset, name: string): [number, number][] {
  const byName = new Map(asset.structures.map(item => [item.name, item]));
  const pairs = new Set<string>(); const seen = new Set<string>(); const pending = [name];
  while (pending.length) {
    const current = pending.pop()!;
    if (seen.has(current)) continue;
    seen.add(current);
    const cell = byName.get(current);
    if (!cell) continue;
    (cell.maskLayerDatatypes ?? []).forEach(([layer, datatype]) => pairs.add(`${layer}/${datatype}`));
    pending.push(...cell.references);
  }
  return [...pairs].map(value => value.split('/').map(Number) as [number, number]).sort((a,b) => a[0]-b[0] || a[1]-b[1]);
}
export function gdsAssetIdFromProject(value: Project): string | null {
  if (value.geometry.kind === 'gds') return value.geometry.assetId;
  if (value.geometry.kind === 'stack') return value.geometry.gdsMask?.assetId ?? null;
  return null;
}
