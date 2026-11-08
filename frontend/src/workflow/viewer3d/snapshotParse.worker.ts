/**
 * Snapshot parse worker.
 *
 * Everything expensive happens here so the main thread never walks a snapshot
 * array:
 *  - `fetch` + `JSON.parse` of the snapshot document (or accept a clone),
 *  - flattening row arrays into `Float32Array`/`Uint32Array`,
 *  - O(n) vertex-clustering simplification above the triangle budget,
 *  - building the VTK cell array and the per-cell material colour index.
 *
 * The result is posted back with transferable buffers.
 */
import { materialColor, materialName } from './colors';
import type {
  MaterialEntry,
  MaterialInfo,
  ParsedMesh,
  ParseOptions,
  ParseRequest,
  ParseResponse,
  SnapshotDoc,
} from './types';

type WorkerScope = {
  onmessage: ((event: MessageEvent<ParseRequest>) => void) | null;
  postMessage: (message: ParseResponse, transfer?: Transferable[]) => void;
};

const scope = globalThis as unknown as WorkerScope;

const DEFAULT_MAX_TRIANGLES = 500_000;
const DEFAULT_TARGET_TRIANGLES = 300_000;

class ParseFailure extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = 'ParseFailure';
    this.status = status;
  }
}

const isFlat = (values: ArrayLike<number>[] | ArrayLike<number>): boolean =>
  values.length > 0 && typeof (values as ArrayLike<number>)[0] === 'number';

/** Copy any point container into a flat xyz `Float32Array`. */
function readPoints(source: SnapshotDoc['points']): Float32Array {
  if (!source) return new Float32Array(0);
  if (isFlat(source)) {
    const flat = source as ArrayLike<number>;
    const out = new Float32Array(flat.length - (flat.length % 3));
    for (let i = 0; i < out.length; i++) out[i] = Number(flat[i]) || 0;
    return out;
  }
  const rows = source as ArrayLike<number>[];
  const out = new Float32Array(rows.length * 3);
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    out[i * 3] = Number(row?.[0]) || 0;
    out[i * 3 + 1] = Number(row?.[1]) || 0;
    out[i * 3 + 2] = Number(row?.[2]) || 0;
  }
  return out;
}

/** Copy a triangle/line connectivity container into a flat index array. */
function readConnectivity(source: SnapshotDoc['triangles'], stride: 2 | 3, label: string): Uint32Array {
  if (!source) return new Uint32Array(0);
  if (isFlat(source)) {
    const flat = source as ArrayLike<number>;
    const count = Math.floor(flat.length / stride);
    const out = new Uint32Array(count * stride);
    for (let i = 0; i < out.length; i++) {
      const value = Number(flat[i]);
      if (!Number.isInteger(value) || value < 0) throw new ParseFailure(`${label}索引非法：${String(flat[i])}`);
      out[i] = value;
    }
    return out;
  }
  const rows = source as ArrayLike<number>[];
  const out = new Uint32Array(rows.length * stride);
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (!row || row.length < stride) throw new ParseFailure(`${label}第 ${i} 行少于 ${stride} 个索引`);
    for (let k = 0; k < stride; k++) {
      const value = Number(row[k]);
      if (!Number.isInteger(value) || value < 0) throw new ParseFailure(`${label}第 ${i} 行索引非法：${String(row[k])}`);
      out[i * stride + k] = value;
    }
  }
  return out;
}

function readScalars(source: ArrayLike<number> | null | undefined): Float32Array {
  if (!source) return new Float32Array(0);
  const out = new Float32Array(source.length);
  for (let i = 0; i < source.length; i++) {
    const value = Number(source[i]);
    out[i] = Number.isFinite(value) ? value : -1;
  }
  return out;
}

function readMaterials(raw: SnapshotDoc['materials']): Record<string, MaterialInfo> {
  const out: Record<string, MaterialInfo> = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [key, value] of Object.entries(raw)) {
    if (!value || typeof value !== 'object') {
      out[key] = {};
      continue;
    }
    out[key] = { name: value.name ?? null, color: value.color ?? null };
  }
  return out;
}

function computeBounds(positions: Float32Array): [number, number, number, number, number, number] {
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;
  for (let i = 0; i < positions.length; i += 3) {
    const x = positions[i];
    const y = positions[i + 1];
    const z = positions[i + 2];
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (z < minZ) minZ = z;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
    if (z > maxZ) maxZ = z;
  }
  if (!Number.isFinite(minX)) return [0, 0, 0, 0, 0, 0];
  return [minX, minY, minZ, maxX, maxY, maxZ];
}

type ClusterInput = {
  positions: Float32Array;
  indices: Uint32Array;
  cellIds: Float32Array;
  bounds: [number, number, number, number, number, number];
};

/**
 * One O(n) vertex-clustering pass: snap vertices to a uniform grid, merge the
 * vertices of every grid cell, drop collapsed and duplicated triangles and keep
 * the material id of the surviving original triangle.
 */
function clusterPass(input: ClusterInput, resolution: number): ClusterInput {
  const { positions, indices, cellIds, bounds } = input;
  const pointCount = positions.length / 3;
  const extentX = Math.max(bounds[3] - bounds[0], 0);
  const extentY = Math.max(bounds[4] - bounds[1], 0);
  const extentZ = Math.max(bounds[5] - bounds[2], 0);
  const cellSize = Math.max(extentX, extentY, extentZ, 1e-9) / resolution;
  const nx = Math.max(1, Math.ceil(extentX / cellSize) || 1);
  const ny = Math.max(1, Math.ceil(extentY / cellSize) || 1);
  const nz = Math.max(1, Math.ceil(extentZ / cellSize) || 1);

  const cellToCluster = new Map<number, number>();
  const vertexToCluster = new Uint32Array(pointCount);
  const sums: number[] = [];
  const counts: number[] = [];

  for (let p = 0; p < pointCount; p++) {
    const x = positions[p * 3];
    const y = positions[p * 3 + 1];
    const z = positions[p * 3 + 2];
    const ix = Math.min(nx - 1, Math.max(0, Math.floor((x - bounds[0]) / cellSize)));
    const iy = Math.min(ny - 1, Math.max(0, Math.floor((y - bounds[1]) / cellSize)));
    const iz = Math.min(nz - 1, Math.max(0, Math.floor((z - bounds[2]) / cellSize)));
    const key = (ix * ny + iy) * nz + iz;
    let cluster = cellToCluster.get(key);
    if (cluster === undefined) {
      cluster = counts.length;
      cellToCluster.set(key, cluster);
      sums.push(0, 0, 0);
      counts.push(0);
    }
    vertexToCluster[p] = cluster;
    sums[cluster * 3] += x;
    sums[cluster * 3 + 1] += y;
    sums[cluster * 3 + 2] += z;
    counts[cluster] += 1;
  }

  const clusterCount = counts.length;
  const outPositions = new Float32Array(clusterCount * 3);
  for (let c = 0; c < clusterCount; c++) {
    const n = counts[c] || 1;
    outPositions[c * 3] = sums[c * 3] / n;
    outPositions[c * 3 + 1] = sums[c * 3 + 1] / n;
    outPositions[c * 3 + 2] = sums[c * 3 + 2] / n;
  }

  const triangleCount = indices.length / 3;
  const outIndices = new Uint32Array(triangleCount * 3);
  const outCellIds = new Float32Array(triangleCount);
  // Numeric key is safe while clusterCount^3 < 2^53; fall back to strings then.
  const numericKey = clusterCount <= 200_000;
  const seen = new Set<number | string>();
  let written = 0;
  for (let t = 0; t < triangleCount; t++) {
    const a = vertexToCluster[indices[t * 3]];
    const b = vertexToCluster[indices[t * 3 + 1]];
    const c = vertexToCluster[indices[t * 3 + 2]];
    if (a === b || b === c || a === c) continue;
    let x = a;
    let y = b;
    let z = c;
    if (x > y) { const tmp = x; x = y; y = tmp; }
    if (y > z) { const tmp = y; y = z; z = tmp; }
    if (x > y) { const tmp = x; x = y; y = tmp; }
    const key: number | string = numericKey ? (x * clusterCount + y) * clusterCount + z : `${x},${y},${z}`;
    if (seen.has(key)) continue;
    seen.add(key);
    outIndices[written * 3] = a;
    outIndices[written * 3 + 1] = b;
    outIndices[written * 3 + 2] = c;
    outCellIds[written] = cellIds[t];
    written += 1;
  }

  return {
    positions: outPositions,
    indices: written === triangleCount ? outIndices : outIndices.subarray(0, written * 3),
    cellIds: written === triangleCount ? outCellIds : outCellIds.subarray(0, written),
    bounds,
  };
}

/**
 * Cluster until the triangle budget is met.
 *
 * A surface mesh with `v` vertices spread over a box of extent `L` collapses to
 * roughly `resolution²` clusters, so `sqrt(target)` is a good first guess; a
 * short binary search then picks the *finest* grid that still fits the budget.
 * Each pass is O(n); at most 5 passes run.
 */
function simplify(input: ClusterInput, targetTriangles: number): ClusterInput {
  const sourceTriangles = input.indices.length / 3;
  if (sourceTriangles <= targetTriangles) return input;
  let low = 4;
  let high = 512;
  let resolution = Math.min(high, Math.max(low, Math.round(Math.sqrt(targetTriangles))));
  let best: ClusterInput | null = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    const pass = clusterPass(input, resolution);
    const produced = pass.indices.length / 3;
    if (produced <= targetTriangles) {
      if (!best || produced > best.indices.length / 3) best = pass;
      if (produced >= targetTriangles * 0.6) return pass;
      low = resolution;
    } else {
      high = resolution;
    }
    const next = Math.round((low + high) / 2);
    if (next <= low || next >= high) break;
    resolution = next;
  }
  return best ?? clusterPass(input, low);
}

/** Dense colour table for the materials actually referenced by the mesh. */
function buildMaterialTable(
  rawMaterials: Record<string, MaterialInfo>,
  ids: Float32Array,
): { materials: MaterialEntry[]; indexOf: Map<number, number> } {
  const unique = new Set<number>();
  for (let i = 0; i < ids.length; i++) unique.add(ids[i]);
  const sorted = [...unique].sort((a, b) => a - b);
  const materials: MaterialEntry[] = [];
  const indexOf = new Map<number, number>();
  for (const id of sorted) {
    const info = rawMaterials[String(id)];
    indexOf.set(id, materials.length);
    materials.push({ id, name: materialName(id, info), color: materialColor(info, materials.length) });
  }
  return { materials, indexOf };
}

function mapIds(ids: Float32Array, indexOf: Map<number, number>): Float32Array {
  const out = new Float32Array(ids.length);
  for (let i = 0; i < ids.length; i++) out[i] = indexOf.get(ids[i]) ?? 0;
  return out;
}

/**
 * Split vertices by material and build an indexed cell array.
 *
 * vtk.js flattens the vertex buffer (3 vertices per triangle, no index buffer)
 * as soon as cell scalars are present, which triples the vertex shader work.
 * Duplicating only the vertices that sit on a material boundary lets us colour
 * by *point* scalars while keeping shared vertices: every triangle still has
 * three identical scalars, so the material edges stay crisp.
 */
function buildSplitGeometry(
  positions: Float32Array,
  indices: Uint32Array,
  triangleMaterial: Float32Array,
  materialCount: number,
): { positions: Float32Array; polys: Uint32Array; pointColor: Float32Array } {
  const pointCount = positions.length / 3;
  const triangleCount = indices.length / 3;
  const owner = new Int32Array(pointCount).fill(-1);
  const shared = new Uint8Array(pointCount);
  for (let t = 0; t < triangleCount; t++) {
    const material = triangleMaterial[t];
    for (let k = 0; k < 3; k++) {
      const vertex = indices[t * 3 + k];
      const current = owner[vertex];
      if (current === -1) owner[vertex] = material;
      else if (current !== material) shared[vertex] = 1;
    }
  }

  const base = new Uint32Array(pointCount);
  let next = 0;
  for (let vertex = 0; vertex < pointCount; vertex++) base[vertex] = next++;

  const extras = new Map<number, number>();
  const extraPositions: number[] = [];
  const extraColors: number[] = [];
  const polys = new Uint32Array(triangleCount * 4);
  for (let t = 0; t < triangleCount; t++) {
    const material = triangleMaterial[t];
    polys[t * 4] = 3;
    for (let k = 0; k < 3; k++) {
      const vertex = indices[t * 3 + k];
      if (shared[vertex] === 0 || owner[vertex] === material) {
        polys[t * 4 + 1 + k] = base[vertex];
        continue;
      }
      const key = vertex * materialCount + material;
      let index = extras.get(key);
      if (index === undefined) {
        index = next++;
        extras.set(key, index);
        extraPositions.push(positions[vertex * 3], positions[vertex * 3 + 1], positions[vertex * 3 + 2]);
        extraColors.push(material);
      }
      polys[t * 4 + 1 + k] = index;
    }
  }

  const outPositions = new Float32Array(next * 3);
  outPositions.set(positions);
  outPositions.set(extraPositions, pointCount * 3);
  const pointColor = new Float32Array(next);
  for (let vertex = 0; vertex < pointCount; vertex++) pointColor[vertex] = owner[vertex] === -1 ? 0 : owner[vertex];
  pointColor.set(extraColors, pointCount);
  return { positions: outPositions, polys, pointColor };
}

/**
 * Area-weighted vertex normals for the split geometry, computed once here (in
 * the worker) so the render loop never walks the mesh. Vertices are split by
 * material, so the accumulation stays inside one material and shading cannot
 * bleed across a material boundary.
 */
function computeNormals(positions: Float32Array, polys: Uint32Array): Float32Array {
  const normals = new Float32Array(positions.length);
  for (let t = 0; t + 3 < polys.length; t += 4) {
    const i0 = polys[t + 1] * 3;
    const i1 = polys[t + 2] * 3;
    const i2 = polys[t + 3] * 3;
    const ax = positions[i1] - positions[i0];
    const ay = positions[i1 + 1] - positions[i0 + 1];
    const az = positions[i1 + 2] - positions[i0 + 2];
    const bx = positions[i2] - positions[i0];
    const by = positions[i2 + 1] - positions[i0 + 1];
    const bz = positions[i2 + 2] - positions[i0 + 2];
    const nx = ay * bz - az * by;
    const ny = az * bx - ax * bz;
    const nz = ax * by - ay * bx;
    normals[i0] += nx; normals[i0 + 1] += ny; normals[i0 + 2] += nz;
    normals[i1] += nx; normals[i1 + 1] += ny; normals[i1 + 2] += nz;
    normals[i2] += nx; normals[i2 + 1] += ny; normals[i2 + 2] += nz;
  }
  for (let i = 0; i < normals.length; i += 3) {
    const length = Math.hypot(normals[i], normals[i + 1], normals[i + 2]);
    if (length > 1e-12) {
      normals[i] /= length;
      normals[i + 1] /= length;
      normals[i + 2] /= length;
    } else {
      normals[i + 2] = 1;
    }
  }
  return normals;
}

function validateIndices(indices: Uint32Array, pointCount: number, label: string): void {  for (let i = 0; i < indices.length; i++) {
    if (indices[i] >= pointCount) {
      throw new ParseFailure(`${label}索引 ${indices[i]} 超出点数量 ${pointCount}，快照几何不一致`);
    }
  }
}

async function loadDocument(request: ParseRequest): Promise<SnapshotDoc> {
  if (request.snapshot) return request.snapshot;
  if (!request.url) throw new ParseFailure('没有可解析的快照数据');
  const response = await fetch(request.url, { headers: { accept: 'application/json' } });
  if (!response.ok) {
    const reason = response.status === 404
      ? '该步骤没有可用快照（后端返回 404）'
      : `快照读取失败（HTTP ${response.status}）`;
    throw new ParseFailure(reason, response.status);
  }
  const text = await response.text();
  if (!text) throw new ParseFailure('快照内容为空');
  try {
    return JSON.parse(text) as SnapshotDoc;
  } catch {
    throw new ParseFailure('快照 JSON 解析失败');
  }
}

function parseDocument(request: ParseRequest, doc: SnapshotDoc, options: ParseOptions): ParsedMesh {
  const started = performance.now();
  const dimension: 2 | 3 = doc.dimension === 2 ? 2 : 3;
  const positions = readPoints(doc.points);
  const pointCount = positions.length / 3;
  if (pointCount === 0) throw new ParseFailure('快照没有点数据');
  const bounds = computeBounds(positions);
  const rawMaterials = readMaterials(doc.materials);
  const axisMap = {
    x: typeof doc.axisMap?.x === 'number' ? doc.axisMap.x : 0,
    y: typeof doc.axisMap?.y === 'number' ? doc.axisMap.y : 1,
    z: typeof doc.axisMap?.z === 'number' ? doc.axisMap.z : 2,
  };

  if (dimension === 3) {
    const indices = readConnectivity(doc.triangles, 3, '三角形');
    const sourceTriangles = indices.length / 3;
    if (sourceTriangles === 0) {
      return {
        runId: request.runId,
        stepId: request.stepId,
        dimension,
        positions,
        polys: new Uint32Array(0),
        pointColor: new Float32Array(0),
        lines: new Uint32Array(0),
        lineColor: new Float32Array(0),
        normals: new Float32Array(0),
        pointCount,
        triangleCount: 0,
        lineCount: 0,
        bounds,
        materials: [],
        axisMap,
        coordinateUnit: doc.coordinateUnit || 'um',
        sourceTriangles: 0,
        simplified: false,
        parseMs: performance.now() - started,
        fromCache: false,
        stepName: doc.stepName ?? null,
        stepIndex: typeof doc.stepIndex === 'number' ? doc.stepIndex : null,
      };
    }
    validateIndices(indices, pointCount, '三角形');
    const rawCellIds = readScalars(doc.cellData?.materialIds);
    const cellIds = new Float32Array(sourceTriangles);
    for (let t = 0; t < sourceTriangles; t++) cellIds[t] = t < rawCellIds.length ? rawCellIds[t] : -1;

    const maxTriangles = options.maxTriangles ?? DEFAULT_MAX_TRIANGLES;
    const targetTriangles = Math.min(options.targetTriangles ?? DEFAULT_TARGET_TRIANGLES, maxTriangles);
    let working: ClusterInput = { positions, indices, cellIds, bounds };
    let simplified = false;
    if (sourceTriangles > maxTriangles) {
      working = simplify(working, targetTriangles);
      simplified = true;
    }
    const triangleCount = working.indices.length / 3;
    const { materials, indexOf } = buildMaterialTable(rawMaterials, working.cellIds);
    const triangleMaterial = mapIds(working.cellIds, indexOf);
    const materialCount = Math.max(1, materials.length);
    const split = buildSplitGeometry(working.positions, working.indices, triangleMaterial, materialCount);
    return {
      runId: request.runId,
      stepId: request.stepId,
      dimension,
      positions: split.positions,
      polys: split.polys,
      pointColor: split.pointColor,
      lines: new Uint32Array(0),
      lineColor: new Float32Array(0),
      normals: computeNormals(split.positions, split.polys),
      pointCount: split.positions.length / 3,
      triangleCount,
      lineCount: 0,
      bounds: simplified ? computeBounds(working.positions) : bounds,
      materials,
      axisMap,
      coordinateUnit: doc.coordinateUnit || 'um',
      sourceTriangles,
      simplified,
      parseMs: performance.now() - started,
      fromCache: false,
      stepName: doc.stepName ?? null,
      stepIndex: typeof doc.stepIndex === 'number' ? doc.stepIndex : null,
    };
  }

  const lines = readConnectivity(doc.lines, 2, '线段');
  validateIndices(lines, pointCount, '线段');
  const lineCount = lines.length / 2;
  const rawLineIds = readScalars(doc.cellData?.materialIds);
  const lineIds = new Float32Array(lineCount);
  for (let i = 0; i < lineCount; i++) lineIds[i] = i < rawLineIds.length ? rawLineIds[i] : -1;
  const { materials, indexOf } = buildMaterialTable(rawMaterials, lineIds);
  return {
    runId: request.runId,
    stepId: request.stepId,
    dimension,
    positions,
    polys: new Uint32Array(0),
    pointColor: new Float32Array(0),
    lines,
    lineColor: mapIds(lineIds, indexOf),
    normals: new Float32Array(0),
    pointCount,
    triangleCount: 0,
    lineCount,
    bounds,
    materials,
    axisMap,
    coordinateUnit: doc.coordinateUnit || 'um',
    sourceTriangles: 0,
    simplified: false,
    parseMs: performance.now() - started,
    fromCache: false,
    stepName: doc.stepName ?? null,
    stepIndex: typeof doc.stepIndex === 'number' ? doc.stepIndex : null,
  };
}

function transferables(mesh: ParsedMesh): Transferable[] {
  return [
    mesh.positions.buffer,
    mesh.polys.buffer,
    mesh.pointColor.buffer,
    mesh.lines.buffer,
    mesh.lineColor.buffer,
    mesh.normals.buffer,
  ];
}

scope.onmessage = (event: MessageEvent<ParseRequest>) => {
  const request = event.data;
  void (async () => {
    try {
      const doc = await loadDocument(request);
      const mesh = parseDocument(request, doc, request.options ?? {});
      scope.postMessage({ id: request.id, ok: true, mesh }, transferables(mesh));
    } catch (error) {
      const failure = error instanceof ParseFailure
        ? error
        : new ParseFailure(error instanceof Error ? error.message : String(error));
      scope.postMessage({ id: request.id, ok: false, error: failure.message, status: failure.status });
    }
  })();
};
