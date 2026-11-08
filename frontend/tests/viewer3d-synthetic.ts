/**
 * Synthetic snapshots for the viewer3d benchmark harness.
 *
 * No backend, no fixtures: the meshes are generated in the page as flat typed
 * arrays so the harness exercises the exact same parse path as the API (only
 * the fetch is replaced by a prop).
 */
import type { SnapshotDoc } from '../src/workflow/viewer3d/types';

export type MaterialSpec = { id: number; name: string; color: string };

/** Five well-separated hues so a pixel test can identify every material. */
export const BENCH_MATERIALS: MaterialSpec[] = [
  { id: 0, name: 'Mask', color: '#3b82f6' },
  { id: 10, name: 'Si', color: '#f59e0b' },
  { id: 30, name: 'SiO2', color: '#22c55e' },
  { id: 16, name: 'Si3N4', color: '#ef4444' },
  { id: 11, name: 'PolySi', color: '#a855f7' },
];

function lcg(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

function heightAt(x: number, y: number, amplitude: number, phase: number): number {
  const base =
    amplitude * (0.55 * Math.sin(x * 1.4 + phase) * Math.cos(y * 1.1 - phase * 0.5) +
      0.3 * Math.sin((x + y) * 1.9 + phase));
  const trench =
    -amplitude * 1.6 * Math.exp(-((x - 0.6) ** 2) / 0.35) * Math.exp(-((y - 0.2) ** 2) / 6);
  const bump = amplitude * 0.8 * Math.exp(-(((x + 2.1) ** 2) + ((y + 1.7) ** 2)) / 1.2);
  return base + trench + bump;
}

function materialBand(x: number, y: number, half: number): number {
  const t = (x + half) / (2 * half) + 0.05 * Math.sin(y * 2.1);
  return Math.min(BENCH_MATERIALS.length - 1, Math.max(0, Math.floor(t * BENCH_MATERIALS.length)));
}

export type SurfaceOptions = {
  /** Requested triangle count; the grid is rounded to the nearest match. */
  triangles: number;
  amplitude: number;
  phase: number;
  seed: number;
  stepId: string;
  stepName: string;
  stepIndex: number;
  half?: number;
};

/** Undulating 5-material surface mesh, ~`triangles` triangles. */
export function surfaceSnapshot(options: SurfaceOptions): SnapshotDoc {
  const half = options.half ?? 4;
  const grid = Math.max(8, Math.round(Math.sqrt(options.triangles / 2)));
  const side = grid + 1;
  const random = lcg(options.seed);
  const cell = (2 * half) / grid;
  const points = new Float32Array(side * side * 3);
  for (let j = 0; j < side; j++) {
    for (let i = 0; i < side; i++) {
      const x = -half + i * cell + (random() - 0.5) * cell * 0.22;
      const y = -half + j * cell + (random() - 0.5) * cell * 0.22;
      const z = heightAt(x, y, options.amplitude, options.phase);
      const index = (j * side + i) * 3;
      points[index] = x;
      points[index + 1] = y;
      points[index + 2] = z;
    }
  }
  const triangles = new Uint32Array(grid * grid * 2 * 3);
  const materialIds = new Uint8Array(grid * grid * 2);
  let write = 0;
  for (let j = 0; j < grid; j++) {
    for (let i = 0; i < grid; i++) {
      const a = j * side + i;
      const b = a + 1;
      const c = a + side;
      const d = c + 1;
      triangles[write * 3] = a;
      triangles[write * 3 + 1] = c;
      triangles[write * 3 + 2] = b;
      materialIds[write] = BENCH_MATERIALS[materialBand(points[a * 3], points[a * 3 + 1], half)].id;
      write += 1;
      triangles[write * 3] = b;
      triangles[write * 3 + 1] = c;
      triangles[write * 3 + 2] = d;
      materialIds[write] = BENCH_MATERIALS[materialBand(points[d * 3], points[d * 3 + 1], half)].id;
      write += 1;
    }
  }
  return {
    dimension: 3,
    points,
    triangles,
    cellData: { materialIds },
    materials: Object.fromEntries(BENCH_MATERIALS.map((material) => [String(material.id), {
      name: material.name,
      color: material.color,
    }])),
    axisMap: { x: 0, y: 1, z: 2 },
    coordinateUnit: 'um',
    stepId: options.stepId,
    stepName: options.stepName,
    stepIndex: options.stepIndex,
  };
}

/** 2D trench profile: one polyline, three materials along x. */
export function profileSnapshot(): SnapshotDoc {
  const samples = 720;
  const half = 4;
  const points = new Float32Array(samples * 3);
  for (let i = 0; i < samples; i++) {
    const x = -half + (2 * half * i) / (samples - 1);
    const depth = -1.4 * Math.exp(-((x - 0.4) ** 2) / 0.5) + 0.25 * Math.sin(x * 3.1);
    points[i * 3] = x;
    points[i * 3 + 1] = 0;
    points[i * 3 + 2] = depth;
  }
  const lines = new Uint32Array((samples - 1) * 2);
  const materialIds = new Uint8Array(samples - 1);
  for (let i = 0; i < samples - 1; i++) {
    lines[i * 2] = i;
    lines[i * 2 + 1] = i + 1;
    const x = points[i * 3];
    materialIds[i] = x < -1 ? 1 : x > 1.4 ? 2 : 0;
  }
  return {
    dimension: 2,
    points,
    lines,
    cellData: { materialIds },
    materials: {
      '0': { name: 'Si', color: '#f59e0b' },
      '1': { name: 'Mask', color: '#3b82f6' },
      '2': { name: 'SiO2', color: '#22c55e' },
    },
    axisMap: { x: 0, y: 1, z: 2 },
    coordinateUnit: 'um',
    stepName: '二维剖面',
    stepIndex: 0,
  };
}
