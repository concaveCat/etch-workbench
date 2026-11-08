export type Activity = 'structure' | 'recipe' | 'materials' | 'results' | 'settings';

export type Selection =
  | { kind: 'none' }
  | { kind: 'step'; id: string }
  | { kind: 'material'; name: string }
  | { kind: 'run'; id: string }
  | { kind: 'artifact'; key: string };

export type DockTab = 'log' | 'flux' | 'intermediate' | 'native' | 'artifacts';

// Additive response field from backend/preflight.py. `risk` is advisory:
// `warning` starts at 75 % of the configured run-directory cap and `reject`
// above 100 % (POST /api/runs answers 422 before the worker starts). The cap
// is deployment-configurable (WORKBENCH_MAX_ARTIFACT_MIB, default 4096 MiB), so
// read `capBytes`/`capMiB` from the estimate instead of assuming 512 MiB. A
// low-confidence estimate is downgraded to `warning` instead of `reject`.
export type ArtifactEstimateRisk = 'ok' | 'warning' | 'reject';
export type ArtifactEstimateContributor = { kind: string; bytes: number; detail: string };
export type ArtifactEstimate = {
  schemaVersion?: number;
  estimatedBytes: number;
  estimatedMiB: number;
  archiveEstimateBytes: number;
  archiveEstimateMiB: number;
  capBytes: number;
  capMiB: number;
  utilization: number;
  risk: ArtifactEstimateRisk;
  archiveRisk: ArtifactEstimateRisk;
  confidence: 'high' | 'medium' | 'low';
  rejectionDowngraded?: boolean;
  lazySnapshots?: boolean;
  kind?: string;
  safetyFactor?: number;
  warningFraction?: number;
  rejectFraction?: number;
  contributors?: ArtifactEstimateContributor[];
  suggestions?: string[];
};
