/** Types and fetchers for the engine comparison surface.
 *
 * Kept deliberately independent from `../api/types`: the comparison contract is
 * the engine job protocol (`engines/protocol.py`), not the legacy project schema.
 */

export type StepKind = 'initial' | 'deposit' | 'etch' | 'planarize' | 'mask';
export type Mechanism = 'initial' | 'isotropic' | 'anisotropic' | 'cmp' | 'gds_mask';
export type Comparability = 'exact' | 'approximate' | 'no_counterpart';

export type Film = { material: string; thickness: number };

export type InitialStructure = {
  kind: 'plane' | 'trench';
  substrate: string;
  substrate_thickness: number;
  films: Film[];
};

export type StepIR = {
  step_id: string;
  kind: StepKind;
  mechanism: Mechanism;
  duration: number;
  rates: Record<string, number>;
  comparability: Comparability;
};

export type DomainSpec = {
  width: number;
  depth: number;
  height: number;
  grid_delta: number;
  dimensions: number;
};

export type JobSpec = {
  schema_version: number;
  case_id: string;
  engine: string;
  domain: DomainSpec;
  initial: InitialStructure;
  steps: StepIR[];
  render: { width: number; height: number; view: string };
};

export type MeshFrame = {
  stepId: string;
  kind: 'mesh';
  dimension: number;
  points: number[][];
  triangles: number[][];
  materialIds: number[];
  materials: { id: number; name: string; color: string }[];
  bbox: number[];
};

export type EngineRow = {
  status: 'ok' | 'missing' | 'unsupported' | 'failed' | 'out_of_domain' | 'skipped';
  wallMs: number | null;
  frame: null | {
    kind: 'mesh' | 'raster';
    path: string;
    width: number;
    height: number;
    camera: Record<string, unknown>;
  };
  reason: string | null;
  metrics: Record<string, unknown>;
};

export type ProbeMetrics = {
  status: 'measured' | 'incomplete' | 'disabled' | 'single_engine' | 'invalid' | 'empty';
  agreement?: number;
  mismatchCount?: number;
  iou?: Record<string, number>;
  topHeightUm?: Record<string, Record<string, number | null>>;
  mismatchExamples?: { point: number[]; [key: string]: unknown }[];
  reason?: string;
};

export type CompareStep = {
  stepId: string;
  kind: StepKind;
  mechanism: Mechanism;
  duration: number;
  rates: Record<string, number>;
  comparability: Comparability;
  aligned: boolean;
  engines: Record<string, EngineRow>;
  probe?: ProbeMetrics;
};

export type ComparePayload = {
  schemaVersion: number;
  caseId: string;
  jobDigest: string;
  probePoints?: number;
  probeShape?: number[] | null;
  probeMethod?: Record<string, string>;
  engines: Record<string, { version: string | null; processWallMs: number; returncode: number; error: string | null }>;
  steps: CompareStep[];
  summary: {
    engineCount: number;
    stepCount: number;
    fullyAlignedSteps: number;
    totalWallMs: Record<string, number>;
  };
};

export type CompareRun = {
  runId: string;
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'rejected';
  caseId?: string;
  detail?: string;
  compare?: ComparePayload;
};

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text.slice(0, 400)}`);
  }
  return (await response.json()) as T;
}

export async function submitComparison(job: JobSpec, engines: string[]): Promise<{ runId: string }> {
  const response = await fetch('/api/compare/runs', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ job, engines }),
  });
  return readJson<{ runId: string }>(response);
}

export async function fetchComparison(runId: string): Promise<CompareRun> {
  return readJson<CompareRun>(await fetch(`/api/compare/runs/${runId}`));
}

export async function fetchRecentRuns(): Promise<CompareRun[]> {
  const payload = await readJson<{ runs: CompareRun[] }>(await fetch('/api/compare/runs'));
  return payload.runs;
}

export function frameUrl(runId: string, path: string): string {
  return `/api/compare/runs/${runId}/frames/${path}`;
}

export async function fetchMesh(runId: string, path: string): Promise<MeshFrame> {
  return readJson<MeshFrame>(await fetch(frameUrl(runId, path)));
}
