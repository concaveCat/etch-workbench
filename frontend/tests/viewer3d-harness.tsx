/**
 * Benchmark harness for the workflow 3D viewer. Runs without the backend.
 *
 * `window.__viewer3dHarness` is the automation surface used by
 * `viewer3d-benchmark.mjs`: step switching with timing, frame-time spinning and
 * framebuffer analysis.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import WorkflowViewer3D from '../src/workflow/viewer3d/WorkflowViewer3D';
import type { SnapshotDoc, Viewer3DHandle, Viewer3DStats } from '../src/workflow/viewer3d/types';
import { BENCH_MATERIALS, profileSnapshot, surfaceSnapshot } from './viewer3d-synthetic';
import { webglBaseline } from './viewer3d-baseline';
import '../src/workflow/viewer3d/viewer3d.css';
import './viewer3d-harness.css';

type StepSpec = {
  stepId: string;
  name: string;
  snapshot: SnapshotDoc;
};

type HarnessState = {
  ready: boolean;
  stepId: string;
  stats: Viewer3DStats | null;
  steps: Array<{ stepId: string; name: string; triangles: number }>;
  errors: string[];
  simplified: boolean;
};

type FrameStats = { count: number; avg: number; p50: number; p95: number; max: number; fps: number };

type PixelReport = {
  width: number;
  height: number;
  total: number;
  nonBackground: number;
  uniqueColors: number;
  /** Cheap content hash, used to prove that a control changed the image. */
  signature: number;
  materials: Array<{ color: string; pixels: number }>;
};

type HarnessApi = {
  getState(): HarnessState;
  switchStep(stepId: string): Promise<{ ms: number; stats: Viewer3DStats }>;
  spin(frames: number): Promise<FrameStats>;
  /** rAF-paced frames without camera motion (full detail). */
  staticFrames(frames: number): Promise<FrameStats>;
  analyze(): PixelReport | null;
  /** Same report for the 2D canvas renderer. */
  analyze2d(): PixelReport | null;
  resetView(): void;
  /** Visible geometry bounds, used to prove the clip plane is real. */
  visibleBounds(): [number, number, number, number, number, number] | null;
  /** Synchronous single frame, for isolating render cost from vsync. */
  renderFrame(): number;
  /** Raw WebGL2 floor for the same triangle count and canvas size. */
  webglBaseline(width: number, height: number, triangles: number): { triangles: number; width: number; height: number; avgMs: number };
};

declare global {
  interface Window {
    __viewer3dHarness?: HarnessApi;
  }
}

const params = new URLSearchParams(window.location.search);
const dimension = params.get('dimension') === '2' ? 2 : 3;
/** `source=api` exercises the production path: the worker fetches the snapshot. */
const source = params.get('source') === 'api' ? 'api' : 'prop';
const baseTriangles = Number(params.get('tris') ?? 240_000);
const heavyTriangles = Number(params.get('heavyTris') ?? 700_000);

function rgbToHue(r: number, g: number, b: number): { hue: number; saturation: number; value: number } {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  if (delta === 0) return { hue: -1, saturation: 0, value: max };
  let hue: number;
  if (max === r) hue = ((g - b) / delta) % 6;
  else if (max === g) hue = (b - r) / delta + 2;
  else hue = (r - g) / delta + 4;
  hue *= 60;
  if (hue < 0) hue += 360;
  return { hue, saturation: delta / max, value: max };
}

function summarizePixels(width: number, height: number, pixels: Uint8Array, background: number[]): PixelReport {
  const hues = BENCH_MATERIALS.map((material) => hueOf(material.color));
  const counts = hues.map(() => 0);
  let nonBackground = 0;
  let signature = 2166136261;
  const quantized = new Set<number>();
  for (let i = 0; i < pixels.length; i += 4) {
    const r = pixels[i];
    const g = pixels[i + 1];
    const b = pixels[i + 2];
    quantized.add(((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3));
    if (Math.abs(r - background[0]) <= 6 && Math.abs(g - background[1]) <= 6 && Math.abs(b - background[2]) <= 6) continue;
    nonBackground += 1;
    if ((i & 0x3ff) === 0) signature = ((signature ^ r) * 16777619) >>> 0;
    const { hue, saturation, value } = rgbToHue(r / 255, g / 255, b / 255);
    if (hue < 0 || saturation < 0.18 || value < 0.1) continue;
    let best = -1;
    let bestDistance = 16;
    hues.forEach((candidate, index) => {
      const distance = hueDistance(hue, candidate);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = index;
      }
    });
    if (best >= 0) counts[best] += 1;
  }
  return {
    width,
    height,
    total: width * height,
    nonBackground,
    uniqueColors: quantized.size,
    signature,
    materials: BENCH_MATERIALS.map((material, index) => ({ color: material.color, pixels: counts[index] })),
  };
}

function summarize(deltas: number[], fps: number): FrameStats {
  const sorted = [...deltas].sort((a, b) => a - b);
  const sum = deltas.reduce((total, value) => total + value, 0);
  return {
    count: deltas.length,
    avg: sum / Math.max(1, deltas.length),
    p50: sorted[Math.floor(sorted.length / 2)] ?? 0,
    p95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))] ?? 0,
    max: sorted[sorted.length - 1] ?? 0,
    fps,
  };
}

function hueOf(hex: string): number {
  const value = Number.parseInt(hex.slice(1), 16);
  const { hue } = rgbToHue(((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255);
  return hue;
}

function hueDistance(a: number, b: number): number {
  const diff = Math.abs(a - b) % 360;
  return diff > 180 ? 360 - diff : diff;
}

function Harness() {
  const [height] = useState(() => Math.max(360, window.innerHeight - 96));
  const [stepIndex, setStepIndex] = useState(0);
  const [stats, setStats] = useState<Viewer3DStats | null>(null);
  const [errors] = useState<string[]>([]);
  const [sceneReady, setSceneReady] = useState(false);
  const handleRef = useRef<Viewer3DHandle | null>(null);
  const statsRef = useRef<Viewer3DStats | null>(null);
  const pendingRef = useRef<{ stepId: string; started: number; resolve: (value: { ms: number; stats: Viewer3DStats }) => void } | null>(null);
  const stepIdRef = useRef('');
  const readyRef = useRef(false);

  const steps = useMemo<StepSpec[]>(() => {
    if (source === 'api') {
      return [
        { stepId: 'api-1', name: '接口步骤 1', snapshot: null as unknown as SnapshotDoc },
        { stepId: 'api-2', name: '接口步骤 2', snapshot: null as unknown as SnapshotDoc },
      ];
    }
    if (dimension === 2) {
      return [{ stepId: 'profile-2d', name: '二维剖面', snapshot: profileSnapshot() }];
    }
    return [
      {
        stepId: 'step-1',
        name: '沉积后',
        snapshot: surfaceSnapshot({ triangles: baseTriangles, amplitude: 0.55, phase: 0.0, seed: 11, stepId: 'step-1', stepName: '沉积后', stepIndex: 0 }),
      },
      {
        stepId: 'step-2',
        name: '刻蚀后',
        snapshot: surfaceSnapshot({ triangles: baseTriangles, amplitude: 0.9, phase: 1.7, seed: 29, stepId: 'step-2', stepName: '刻蚀后', stepIndex: 1 }),
      },
      {
        stepId: 'step-3',
        name: '重载网格',
        snapshot: surfaceSnapshot({ triangles: heavyTriangles, amplitude: 0.7, phase: 0.9, seed: 47, stepId: 'step-3', stepName: '重载网格', stepIndex: 2 }),
      },
    ];
  }, []);

  const current = steps[Math.min(stepIndex, steps.length - 1)];
  stepIdRef.current = current.stepId;

  const onStats = useCallback((next: Viewer3DStats) => {
    statsRef.current = next;
    setStats(next);
    if (next.triangles > 0 || next.points > 0) readyRef.current = true;
    const pending = pendingRef.current;
    if (pending && next.stepId === pending.stepId) {
      pendingRef.current = null;
      pending.resolve({ ms: performance.now() - pending.started, stats: next });
    }
  }, []);

  const onSceneReady = useCallback((handle: Viewer3DHandle) => {
    handleRef.current = handle;
    setSceneReady(true);
  }, []);

  useEffect(() => {
    const api: HarnessApi = {
      getState: () => ({
        ready: readyRef.current && (dimension === 2 || sceneReady),
        stepId: stepIdRef.current,
        stats: statsRef.current,
        steps: steps.map((step) => ({
          stepId: step.stepId,
          name: step.name,
          triangles: Number((step.snapshot?.triangles as ArrayLike<number> | undefined)?.length ?? 0) / 3,
        })),
        errors,
        simplified: statsRef.current?.simplified ?? false,
      }),
      switchStep: (stepId: string) => {
        const target = steps.findIndex((step) => step.stepId === stepId);
        if (target < 0) return Promise.reject(new Error(`未知步骤 ${stepId}`));
        const started = performance.now();
        return new Promise((resolve) => {
          pendingRef.current = { stepId, started, resolve };
          setStepIndex(target);
        });
      },
      spin: async (frames: number) => {
        const handle = handleRef.current;
        if (!handle) throw new Error('三维场景尚未就绪');
        const deltas: number[] = [];
        await new Promise<void>((resolve) => {
          let previous = performance.now();
          let count = 0;
          const tick = (): void => {
            const now = performance.now();
            if (count > 0) deltas.push(now - previous);
            previous = now;
            handle.rotate(1.3, 0.4);
            count += 1;
            if (count >= frames) resolve();
            else requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        });
        return summarize(deltas, handle.getFps());
      },
      staticFrames: async (frames: number) => {
        const handle = handleRef.current;
        if (!handle) throw new Error('三维场景尚未就绪');
        const deltas: number[] = [];
        await new Promise<void>((resolve) => {
          let previous = performance.now();
          let count = 0;
          const tick = (): void => {
            const now = performance.now();
            if (count > 0) deltas.push(now - previous);
            previous = now;
            handle.renderNow();
            count += 1;
            if (count >= frames) resolve();
            else requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        });
        return summarize(deltas, handle.getFps());
      },
      analyze: () => {
        const handle = handleRef.current;
        if (!handle) return null;
        const read = handle.readPixels();
        if (!read) return null;
        return summarizePixels(read.width, read.height, read.pixels, [246, 247, 248]);
      },
      analyze2d: () => {
        const canvas = document.querySelector<HTMLCanvasElement>('.wb-viewer3d__canvas2d');
        if (!canvas) return null;
        const context = canvas.getContext('2d');
        if (!context) return null;
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        return summarizePixels(canvas.width, canvas.height, image.data, [247, 249, 251]);
      },
      resetView: () => handleRef.current?.resetView(),
      visibleBounds: () => handleRef.current?.getVisibleBounds() ?? null,
      webglBaseline: (width: number, height: number, triangles: number) => webglBaseline(width, height, triangles),
      renderFrame: () => {
        const handle = handleRef.current;
        if (!handle) return 0;
        const started = performance.now();
        handle.rotate(0.5, 0.1);
        return performance.now() - started;
      },
    };
    window.__viewer3dHarness = api;
    return () => {
      delete window.__viewer3dHarness;
    };
  }, [steps, errors, sceneReady]);

  return (
    <div className="harness">
      <header className="harness__bar">
        <strong>viewer3d benchmark harness</strong>
        <span className="harness__meta">
          {dimension}D{source === 'api' ? ' · API' : ''} · {stats ? `${stats.triangles.toLocaleString()} 三角形` : '解析中'}
          {stats?.simplified ? ` · 已简化自 ${stats.sourceTriangles?.toLocaleString()}` : ''}
          {stats ? ` · ${Math.round(stats.parseMs)} ms` : ''}
        </span>
        <nav className="harness__steps">
          {steps.map((step, index) => (
            <button
              key={step.stepId}
              type="button"
              className={index === stepIndex ? 'is-active' : undefined}
              onClick={() => setStepIndex(index)}
            >
              {step.name}
            </button>
          ))}
        </nav>
      </header>
      <main className="harness__stage">
        <WorkflowViewer3D
          runId="benchmark-run"
          stepId={current.stepId}
          dimension={dimension as 2 | 3}
          snapshot={source === 'api' ? undefined : current.snapshot}
          height={height}
          onStats={onStats}
          onSceneReady={onSceneReady}
        />
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Harness />);
