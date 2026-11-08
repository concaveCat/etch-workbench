/**
 * Snapshot loading: one module-level parse worker, an LRU(8) cache keyed by
 * `runId:stepId`, in-flight de-duplication and idle-time prefetching of the
 * neighbouring steps.
 */
import type { ParsedMesh, ParseRequest, ParseResponse, ParseOptions, SnapshotDoc } from './types';

export const CACHE_LIMIT = 8;

const cache = new Map<string, ParsedMesh>();
const inflight = new Map<string, Promise<ParsedMesh>>();
const pending = new Map<number, { resolve: (mesh: ParsedMesh) => void; reject: (error: Error) => void }>();
let worker: Worker | null = null;
let nextRequestId = 1;

export class SnapshotUnavailableError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = 'SnapshotUnavailableError';
    this.status = status;
  }
}

export const cacheKey = (runId: string, stepId: string, options?: ParseOptions): string =>
  `${runId}:${stepId}:${options?.maxTriangles ?? 'default'}`;

export const snapshotUrl = (runId: string, stepId: string): string =>
  `/api/runs/${encodeURIComponent(runId)}/snapshots/${encodeURIComponent(stepId)}`;

function rejectAll(error: Error): void {
  for (const { reject } of pending.values()) reject(error);
  pending.clear();
}

function getWorker(): Worker {
  if (worker) return worker;
  const created = new Worker(new URL('./snapshotParse.worker.ts', import.meta.url), {
    type: 'module',
    name: 'viewer3d-snapshot-parse',
  });
  created.onmessage = (event: MessageEvent<ParseResponse>) => {
    const response = event.data;
    const entry = pending.get(response.id);
    if (!entry) return;
    pending.delete(response.id);
    if (response.ok) entry.resolve(response.mesh);
    else entry.reject(new SnapshotUnavailableError(response.error, response.status));
  };
  created.onerror = (event) => {
    const message = (event as ErrorEvent).message || '快照解析 worker 异常退出';
    rejectAll(new Error(message));
    created.terminate();
    if (worker === created) worker = null;
  };
  created.onmessageerror = () => rejectAll(new Error('快照解析 worker 消息无法反序列化'));
  worker = created;
  return created;
}

export type LoadRequest = {
  runId: string;
  stepId: string;
  /** Pre-fetched document; omit to let the worker fetch `snapshotUrl`. */
  snapshot?: SnapshotDoc | null;
  /** Explicit URL (tests / alternative endpoints). */
  url?: string;
  options?: ParseOptions;
};

function parseInWorker(request: LoadRequest): Promise<ParsedMesh> {
  return new Promise<ParsedMesh>((resolve, reject) => {
    const id = nextRequestId++;
    pending.set(id, { resolve, reject });
    const message: ParseRequest = {
      id,
      runId: request.runId,
      stepId: request.stepId,
      url: request.url ?? (request.snapshot ? undefined : snapshotUrl(request.runId, request.stepId)),
      snapshot: request.snapshot ?? null,
      options: request.options,
    };
    try {
      getWorker().postMessage(message);
    } catch (error) {
      pending.delete(id);
      reject(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

function remember(key: string, mesh: ParsedMesh): void {
  cache.delete(key);
  cache.set(key, mesh);
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next();
    if (oldest.done) break;
    cache.delete(oldest.value);
  }
}

/** Parse (or reuse) the geometry for one step. */
export function loadMesh(request: LoadRequest): Promise<ParsedMesh> {
  const key = cacheKey(request.runId, request.stepId, request.options);
  const cached = cache.get(key);
  if (cached) {
    // Refresh LRU order.
    cache.delete(key);
    cache.set(key, cached);
    return Promise.resolve({ ...cached, fromCache: true, parseMs: 0 });
  }
  const running = inflight.get(key);
  if (running) return running;
  const promise = parseInWorker(request);
  inflight.set(key, promise);
  promise
    .then((mesh) => remember(key, mesh))
    .catch(() => undefined)
    .finally(() => {
      if (inflight.get(key) === promise) inflight.delete(key);
    });
  return promise;
}

export function getCached(runId: string, stepId: string, options?: ParseOptions): ParsedMesh | undefined {
  return cache.get(cacheKey(runId, stepId, options));
}

const runSteps = new Map<string, Promise<string[]>>();

/**
 * Step order for a run, used to prefetch the neighbours. Cached per run; a
 * failure resolves to an empty list so callers never need a try/catch.
 */
export function loadRunStepIds(runId: string): Promise<string[]> {
  const cached = runSteps.get(runId);
  if (cached) return cached;
  const promise = fetch(`/api/runs/${encodeURIComponent(runId)}`, { headers: { accept: 'application/json' } })
    .then((response) => (response.ok ? response.json() : null))
    .then((body: unknown) => {
      const snapshots = (body as { snapshots?: Array<{ stepId?: unknown }> } | null)?.snapshots;
      if (!Array.isArray(snapshots)) return [];
      return snapshots
        .map((item) => (typeof item?.stepId === 'string' ? item.stepId : null))
        .filter((value): value is string => value !== null);
    })
    .catch(() => []);
  runSteps.set(runId, promise);
  return promise;
}

export function clearCache(): void {
  cache.clear();
  inflight.clear();
}

type IdleScheduler = (callback: () => void, options?: { timeout?: number }) => void;

const scheduleIdle: IdleScheduler = (callback, options) => {
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number })
    .requestIdleCallback;
  if (typeof idle === 'function') idle.call(globalThis, callback, options);
  else setTimeout(callback, 1200);
};

/**
 * Warm the cache for a neighbouring step without blocking the current frame.
 * Failures are swallowed: prefetching is an optimisation, never a requirement.
 */
export function prefetch(runId: string, stepId: string, url?: string): void {
  const key = cacheKey(runId, stepId);
  if (cache.has(key) || inflight.has(key)) return;
  scheduleIdle(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    void loadMesh({ runId, stepId, url }).catch(() => undefined);
  }, { timeout: 4000 });
}
