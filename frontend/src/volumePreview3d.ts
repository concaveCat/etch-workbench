import type { Vec3 } from './types3d';
import type { NativeVolume3D } from './parseNativeVtu';

type OrientedFace = { materialId: number; triangle: [number, number, number] };

const cross = (a: Vec3, b: Vec3): Vec3 => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const subtract = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: Vec3, b: Vec3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function outwardFace(points: Vec3[], nodes: [number, number, number, number], opposite: number, materialId: number): OrientedFace {
  let triangle: [number, number, number] = [nodes[0], nodes[1], nodes[2]];
  const a = points[triangle[0]], b = points[triangle[1]], c = points[triangle[2]], inside = points[opposite];
  const normal = cross(subtract(b, a), subtract(c, a));
  const center: Vec3 = [(a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3];
  if (dot(normal, subtract(inside, center)) > 0) triangle = [triangle[0], triangle[2], triangle[1]];
  return { materialId, triangle };
}

/** Extracts only the native tetrahedral boundary and cross-material interfaces. */
export function volumeMaterialSurfaces(volume: NativeVolume3D): Map<number, number[][]> {
  const faces = new Map<string, OrientedFace[]>();
  const localFaces: [number, number, number, number][] = [
    [0, 1, 2, 3], [0, 3, 1, 2], [1, 3, 2, 0], [2, 3, 0, 1],
  ];
  for (let cell = 0; cell < volume.tetrahedra.length; cell++) {
    const tet = volume.tetrahedra[cell];
    const materialId = volume.materialIds[cell];
    for (const [a, b, c, opposite] of localFaces) {
      const face = outwardFace(volume.points, [tet[a], tet[b], tet[c], tet[opposite]], tet[opposite], materialId);
      const key = [...face.triangle].sort((left, right) => left - right).join(':');
      const siblings = faces.get(key);
      if (siblings) siblings.push(face);
      else faces.set(key, [face]);
    }
  }

  const surfaces = new Map<number, number[][]>();
  const append = (face: OrientedFace) => {
    const triangles = surfaces.get(face.materialId) ?? [];
    triangles.push(face.triangle);
    surfaces.set(face.materialId, triangles);
  };
  for (const siblings of faces.values()) {
    const materialIds = new Set(siblings.map(face => face.materialId));
    if (siblings.length === 1) append(siblings[0]);
    else if (materialIds.size > 1) {
      for (const materialId of materialIds) append(siblings.find(face => face.materialId === materialId)!);
    }
  }
  return surfaces;
}

export type VolumeSlicePolygon = { points: Vec3[]; materialId: number };

/** Intersects the native tetrahedra so volume sections show filled materials. */
export function cutVolumeTetrahedra(volume: NativeVolume3D, axisIndex: number, position: number): VolumeSlicePolygon[] {
  const eps = 1e-9;
  const axes = axisIndex === 0 ? [1, 2] : axisIndex === 1 ? [0, 2] : [0, 1];
  const polygons: VolumeSlicePolygon[] = [];
  const polygonKeys = new Set<string>();
  for (let cell = 0; cell < volume.tetrahedra.length; cell++) {
    const tetrahedron = volume.tetrahedra[cell].map(index => volume.points[index]);
    const distances = tetrahedron.map(point => point[axisIndex] - position);
    const minDistance = Math.min(...distances), maxDistance = Math.max(...distances);
    if (minDistance > eps || maxDistance < -eps || maxDistance - minDistance <= eps) continue;
    const hits: Vec3[] = [];
    const add = (point: Vec3) => {
      if (!hits.some(previous => Math.hypot(...point.map((value, index) => value - previous[index])) <= eps)) hits.push(point);
    };
    for (const [a, b] of [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]]) {
      if (Math.abs(distances[a]) <= eps) add([...tetrahedron[a]] as Vec3);
      if (distances[a] * distances[b] < -eps * eps) {
        const factor = distances[a] / (distances[a] - distances[b]);
        add(tetrahedron[a].map((value, index) => value + (tetrahedron[b][index] - value) * factor) as Vec3);
      }
    }
    if (hits.length < 3) continue;
    const center: Vec3 = [0, 0, 0];
    for (const point of hits) point.forEach((value, index) => { center[index] += value / hits.length; });
    hits.sort((left, right) => Math.atan2(left[axes[1]] - center[axes[1]], left[axes[0]] - center[axes[0]])
      - Math.atan2(right[axes[1]] - center[axes[1]], right[axes[0]] - center[axes[0]]));
    const key = `${volume.materialIds[cell]}:${hits.map(point => point.map(value => Math.round(value / eps)).join(',')).sort().join('|')}`;
    if (polygonKeys.has(key)) continue;
    polygonKeys.add(key);
    polygons.push({ points: hits, materialId: volume.materialIds[cell] });
  }
  return polygons;
}
