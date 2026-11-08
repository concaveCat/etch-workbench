/**
 * Types shared by the 3D process-structure viewer, its parse worker and the
 * benchmark harness.
 *
 * The wire contract is the snapshot document returned by
 * `GET /api/runs/{runId}/snapshots/{stepId}`. Everything below that is the
 * parsed, transferable form produced by `snapshotParse.worker.ts`.
 */

/** One entry of `snapshot.materials`; colour is optional in older schemas. */
export type MaterialInfo = { name?: string | null; color?: string | null };

/** A material that is actually referenced by the mesh, with a resolved colour. */
export type MaterialEntry = { id: number; name: string; color: string };

/**
 * A snapshot document, or any structurally compatible payload.
 *
 * `points`/`triangles`/`lines` may be row arrays (`number[][]`, the API form)
 * or flat arrays (`Float32Array`, `number[]`). Flat typed arrays are cloned
 * cheaply and are what the benchmark harness feeds in.
 */
export type SnapshotDoc = {
  dimension?: number | null;
  points?: ArrayLike<number>[] | ArrayLike<number> | null;
  triangles?: ArrayLike<number>[] | ArrayLike<number> | null;
  lines?: ArrayLike<number>[] | ArrayLike<number> | null;
  cellData?: { materialIds?: ArrayLike<number> | null } | null;
  materials?: Record<string, MaterialInfo> | null;
  axisMap?: { x?: number; y?: number; z?: number } | null;
  coordinateUnit?: string | null;
  stepName?: string | null;
  stepIndex?: number | null;
  outputs?: ReadonlyArray<{ kind?: string; format?: string; url?: string }> | null;
};

/** Parsed geometry handed to the renderer. All arrays are transferable. */
export type ParsedMesh = {
  runId: string;
  stepId: string;
  dimension: 2 | 3;
  /** xyz triples, always 3 components per point. */
  positions: Float32Array;
  /** VTK cell array (`[3, i, j, k, ...]`) indexing `positions`, empty for 2D. */
  polys: Uint32Array;
  /**
   * Colour index into `materials`, one per *vertex*. Vertices are split by
   * material so every triangle has three identical scalars: the mesh stays
   * indexed (shared vertices, one draw call) and material edges stay crisp.
   */
  pointColor: Float32Array;
  /** Point index pairs, one line per 2 cells, empty for 3D. */
  lines: Uint32Array;
  /** Colour index into `materials`, one per line, empty for 3D. */
  lineColor: Float32Array;
  /**
   * Per-vertex normals (xyz), computed once in the parse worker. Lighting is
   * Gouraud/Phong, so a curved surface shows real shading instead of a flat
   * material fill. Empty for 2D meshes.
   */
  normals: Float32Array;
  pointCount: number;
  triangleCount: number;
  lineCount: number;
  /** [minX, minY, minZ, maxX, maxY, maxZ] over `positions`. */
  bounds: [number, number, number, number, number, number];
  /** Only materials referenced by this mesh, sorted by id. */
  materials: MaterialEntry[];
  axisMap: { x: number; y: number; z: number };
  coordinateUnit: string;
  /** Triangle count before vertex-clustering simplification. */
  sourceTriangles: number;
  /** True when vertex clustering ran. */
  simplified: boolean;
  /** Parse + simplify wall time measured inside the worker. */
  parseMs: number;
  /** True when the mesh came straight from the LRU cache. */
  fromCache: boolean;
  stepName: string | null;
  stepIndex: number | null;
};

export type ParseOptions = {
  /** Simplify above this triangle count. */
  maxTriangles?: number;
  /** Simplification target. */
  targetTriangles?: number;
};

export type ParseRequest = {
  id: number;
  runId: string;
  stepId: string;
  /** Snapshot URL; the worker fetches and JSON-parses it off the main thread. */
  url?: string;
  /** Pre-fetched snapshot; cloned into the worker (prefer `url` in production). */
  snapshot?: SnapshotDoc | null;
  options?: ParseOptions;
};

export type ParseResponse =
  | { id: number; ok: true; mesh: ParsedMesh }
  | { id: number; ok: false; error: string; status: number };

export type Viewer3DStats = {
  triangles: number;
  points: number;
  fps: number;
  parseMs: number;
  /** Extra hint so a parent can tell which step the numbers belong to. */
  stepId?: string;
  simplified?: boolean;
  sourceTriangles?: number;
};

/**
 * A red measurement line drawn over the structure.
 *
 * `axis` is the axis whose coordinate is fixed at `value`; the line runs along
 * the other axis of the section: a fixed X (etch depth) draws a vertical line
 * along Z, a fixed height (CD) draws a horizontal line along X. `y` is always
 * the mid-plane of the current geometry, so the line sits in the section.
 */
export type Viewer3DMarker = { axis: 'x' | 'y' | 'z'; value: number; label: string };

/** Imperative handle exposed through `onSceneReady` (toolbar / tests). */
export type Viewer3DHandle = {
  /** Fit the visible geometry, keeping the current camera direction. */
  resetView(): void;
  setStandardView(view: 'iso' | 'xy' | 'xz' | 'yz'): void;
  /** Rotate the camera and render synchronously (used for frame timing). */
  rotate(azimuthDeg: number, elevationDeg: number): void;
  renderNow(): void;
  /** Raw RGBA framebuffer readback for pixel assertions. */
  readPixels(): { width: number; height: number; pixels: Uint8Array } | null;
  getFps(): number;
  getTriangleCount(): number;
  getPointCount(): number;
  /** Bounds of the geometry currently on screen (hidden/clipped removed). */
  getVisibleBounds(): [number, number, number, number, number, number] | null;
};

export type ClipState = {
  enabled: boolean;
  axis: 'x' | 'y' | 'z';
  position: number;
  /** Keep the half space the plane normal points into. */
  keep: 'positive' | 'negative';
};

export type DisplayMode = 'surface' | 'wireframe';

export type WorkflowViewer3DProps = {
  runId: string;
  stepId: string;
  dimension: 2 | 3;
  /** Optional pre-fetched snapshot. Omit it to let the viewer fetch. */
  snapshot?: SnapshotDoc | null;
  height?: number;
  /** Optional ordered step list; neighbours are prefetched while idle. */
  stepIds?: readonly string[];
  /** Optional measurement line (3D only); nothing is drawn without it. */
  marker?: Viewer3DMarker | null;
  onStepUnavailable?: (reason: string) => void;
  onStats?: (stats: Viewer3DStats) => void;
  onSceneReady?: (handle: Viewer3DHandle) => void;
  className?: string;
};
