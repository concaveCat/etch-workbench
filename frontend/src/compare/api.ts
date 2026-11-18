/** Types and fetchers for the multi-engine comparison & benchmark surface. */

export type StepKind = 'initial' | 'deposit' | 'etch' | 'planarize' | 'mask' | 'strip';
export type Mechanism =
  | 'initial'
  | 'isotropic'
  | 'anisotropic'
  | 'cmp'
  | 'gds_mask'
  | 'strip'
  | 'fill'
  | 'ellipsoidal'
  | 'litho'
  | 'custom';
export type Comparability = 'exact' | 'approximate' | 'no_counterpart';

export type EngineDescriptor = {
  engineId: string;
  label: string;
  role?: string;
  adapter: string;
  solveMode?: string;
  probeMethod?: string;
  mechanisms: string[];
  frameKinds: string[];
  rationale: string;
};

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
  parameters?: Record<string, unknown>;
  model?: string | null;
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
  render: {
    width: number;
    height: number;
    view: string;
    camera?: { azimuth: number; elevation: number; zoom: number };
  };
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
  solveMs?: number | null;
  frameMs?: number | null;
  probeMs?: number | null;
  sectionMs?: number | null;
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
  leftEngine?: string;
  rightEngine?: string;
  agreement?: number;
  activePoints?: number;
  activeAgreement?: number;
  featurePoints?: number;
  featureAgreement?: number;
  mismatchCount?: number;
  points?: number;
  iou?: Record<string, number>;
  topConfusions?: { count: number; [key: string]: unknown }[];
  topHeightUm?: Record<string, Record<string, number | null>>;
  mismatchExamples?: { point: number[]; [key: string]: unknown }[];
  reason?: string;
};

export type SectionMaterial = {
  status: 'measured' | 'not_resolved' | 'length_mismatch';
  columns?: number;
  resolved?: number;
  missingColumns?: number;
  meanAbsUm?: number;
  maxAbsUm?: number;
  l2Um?: number;
};

export type SectionMetrics = {
  status: 'measured' | 'not_resolved' | 'incomplete' | 'disabled' | 'single_engine' | 'empty';
  columns?: number;
  worstAbsUm?: number;
  materials?: Record<string, SectionMaterial>;
  heights?: Record<string, Record<string, (number | null)[]>>;
  reason?: string;
};

export type PairComparison = {
  probe?: ProbeMetrics;
  section?: SectionMetrics;
};

export type CompareStep = {
  stepId: string;
  kind: StepKind;
  mechanism: Mechanism;
  model?: string | null;
  duration: number;
  rates: Record<string, number>;
  comparability: Comparability;
  aligned: boolean;
  engines: Record<string, EngineRow>;
  comparisons?: Record<string, PairComparison>;
  probe?: ProbeMetrics;
  section?: SectionMetrics;
};

export type EngineSummaryInfo = {
  label?: string;
  role?: string;
  version: string | null;
  solveMode?: string;
  probeMethod?: string;
  solveWallMs?: number | null;
  frameWallMs?: number;
  probeWallMs?: number;
  sectionWallMs?: number;
  processWallMs: number;
  returncode: number;
  error: string | null;
};

export type ComparePayload = {
  schemaVersion: number;
  caseId: string;
  referenceEngine?: string;
  executionMode?: string;
  jobDigest: string;
  view?: string;
  camera?: { azimuth?: number; elevation?: number; zoom?: number } | null;
  domain?: { width: number; depth: number; height: number; origin: number[] | null };
  probePoints?: number;
  probeShape?: number[] | null;
  probeMethod?: Record<string, string>;
  sectionColumns?: number[][];
  sectionZ?: number[] | null;
  engines: Record<string, EngineSummaryInfo>;
  steps: CompareStep[];
  summary: {
    engineCount: number;
    referenceEngine?: string;
    stepCount: number;
    fullyAlignedSteps: number;
    totalSolveMs?: Record<string, number | null>;
    speedupVsReference?: Record<string, number | null>;
    totalWallMs: Record<string, number>;
    accuracy?: Record<string, {
      meanAgreement: number;
      medianAgreement: number;
      minAgreement: number;
      meanActiveAgreement: number;
      meanFeatureAgreement: number;
      exactSteps: number;
      measuredSteps: number;
    }>;
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

export async function fetchEngines(): Promise<EngineDescriptor[]> {
  const payload = await readJson<{ engines: EngineDescriptor[] }>(await fetch('/api/compare/engines'));
  return payload.engines;
}

export async function submitComparison(
  job: JobSpec,
  engines: string[],
  options?: { sequential?: boolean; reference?: string },
): Promise<{ runId: string }> {
  const response = await fetch('/api/compare/runs', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ job, engines, ...options }),
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
