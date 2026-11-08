export type Vec3 = [number, number, number];
export type Axis3D = 'x' | 'y' | 'z';
export type Camera3D = { position: Vec3; focalPoint: Vec3; viewUp: Vec3; parallelScale: number };
export type View3D = {
  projection: 'parallel' | 'perspective'; camera: Camera3D | null;
  displayMode: 'surface' | 'wireframe' | 'surfaceEdges';
  visibleMaterialIds: number[]; opacityByMaterial: Record<string, number>;
  clip: { enabled: boolean; axis: Axis3D; position: number; keep: 'positive' | 'negative' };
  section: { enabled: boolean; axis: Axis3D; position: number };
};
export type Surface3D = {
  schemaVersion: number; dimension?: number; runId: string; stepId: string;
  points: number[][]; triangles?: number[][]; cellData: { materialIds: number[] };
  materials?: Record<string, { name: string; color?: string }>;
  surfaceBounds?: number[]; gridDelta?: number; substrateMaterialId?: number; materialIdsByLevelSetIndex?: number[];
  materialSurfaceStatus?: 'closed' | 'unavailable'; materialSurfaceError?: string;
  outputs?: { kind: string; url: string; format: string; sizeBytes?: number }[];
};
export const defaultView3D: View3D = {
  projection: 'parallel', camera: null, displayMode: 'surface', visibleMaterialIds: [],
  opacityByMaterial: {},
  clip: { enabled: false, axis: 'x', position: 0, keep: 'positive' },
  section: { enabled: false, axis: 'y', position: 0 },
};
export function snapshotDimension(s: { schemaVersion: number; dimension?: number }): 2 | 3 {
  if (s.schemaVersion === 1 && (s.dimension === undefined || s.dimension === 2)) return 2;
  if (s.schemaVersion === 2 && s.dimension === 3) return 3;
  if (s.schemaVersion === 3 && (s.dimension === 2 || s.dimension === 3)) return s.dimension;
  if (s.schemaVersion === 4 && (s.dimension === 2 || s.dimension === 3)) return s.dimension;
  if (s.schemaVersion === 5 && (s.dimension === 2 || s.dimension === 3)) return s.dimension;
  if (s.schemaVersion === 6 && (s.dimension === 2 || s.dimension === 3)) return s.dimension;
  if (s.schemaVersion === 7 && (s.dimension === 2 || s.dimension === 3)) return s.dimension;
  throw new Error('不支持的快照版本或维度');
}
