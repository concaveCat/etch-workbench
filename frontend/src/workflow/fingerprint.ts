/**
 * Execution fingerprint of an authored project.
 *
 * This is the stable "compile basis" the 03 page stores next to its Compile:
 * only the fields that change what a Compile/Run actually does are hashed, so
 * a fresh Compile of the same project compares equal while a changed duration,
 * step list, domain, solver or geometry does not. The function lives in its own
 * module because the live acceptance tests import it directly (Node strips the
 * types) instead of duplicating the normalization rules.
 */

/** The subset of a project document the fingerprint looks at. */
export type FingerprintDocument = {
  dimension?: unknown;
  domain?: unknown;
  geometry?: unknown;
  steps?: unknown;
  solver?: unknown;
  [key: string]: unknown;
};

/**
 * Solver fields the backend fills in when a project is saved or compiled
 * (`SolverV7` defaults). A raw imported project omits them, so both sides of a
 * fingerprint comparison are padded before hashing; otherwise an unchanged
 * project would look edited right after its first save.
 */
const SOLVER_EXECUTION_DEFAULTS = {
  timeoutSeconds: 0,
  advection: {
    spatialScheme: 'ENGQUIST_OSHER_1ST_ORDER',
    temporalScheme: 'FORWARD_EULER',
    adaptiveTimeStepping: false,
    adaptiveTimeStepSubdivisions: 20,
    calculateIntermediateVelocities: false,
    checkDissipation: true,
    dissipationAlpha: 1.0,
    ignoreVoids: false,
    velocityOutput: false,
  },
  rayTracing: {
    diskRadius: 0.0,
    ignoreFluxBoundaries: false,
    maxBoundaryHits: 1000,
    maxReflections: 4294967295,
    minNodeDistanceFactor: 0.05,
    normalizationType: 'SOURCE',
    smoothingNeighbors: 1,
  },
};

function normalizeSolverForFingerprint(solver: unknown): Record<string, unknown> {
  const raw = (solver && typeof solver === 'object' ? solver : {}) as Record<string, unknown>;
  const advection = (raw.advection && typeof raw.advection === 'object' ? raw.advection : {}) as Record<string, unknown>;
  const rayTracing = (raw.rayTracing && typeof raw.rayTracing === 'object' ? raw.rayTracing : {}) as Record<string, unknown>;
  return {
    ...SOLVER_EXECUTION_DEFAULTS,
    ...raw,
    advection: { ...SOLVER_EXECUTION_DEFAULTS.advection, ...advection },
    rayTracing: { ...SOLVER_EXECUTION_DEFAULTS.rayTracing, ...rayTracing },
  };
}

/** FNV-1a over UTF-16 code units; two seeds give a 128-bit digest. */
function fnv1a64(text: string, seed: bigint): bigint {
  let hash = seed;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= BigInt(text.charCodeAt(index));
    hash = (hash * 0x100000001b3n) & 0xffffffffffffffffn;
  }
  return hash;
}

/**
 * The compact, stable string stored as `compileBasis` (≤ 128 chars). It hashes
 * the canonical fingerprint so the server can keep a short bounded field; two
 * independent FNV-1a passes make an accidental collision between two edited
 * projects implausible. Deterministic across reloads and browsers.
 */
export function executionCompileBasis(
  document: FingerprintDocument | null | undefined,
  sourceMap?: Array<{ stepId: string; authoredStepId: string }>,
): string {
  const canonical = executionProjectFingerprint(document, sourceMap);
  const low = fnv1a64(canonical, 0xcbf29ce484222325n);
  const high = fnv1a64(canonical, 0x84222325cbf29ce4n);
  return `fp1_${low.toString(16).padStart(16, '0')}${high.toString(16).padStart(16, '0')}`;
}

/**
 * Fingerprint of a project as it will be executed. Labels (name/view/
 * timestamps) are ignored and null defaults are dropped. A compiled envelope's
 * generated step ids are mapped back to their authored ids through the source
 * map, so the same authored project fingerprints identically before and after
 * a Compile.
 */
export function executionProjectFingerprint(
  document: FingerprintDocument | null | undefined,
  sourceMap?: Array<{ stepId: string; authoredStepId: string }>,
): string {
  if (!document) return '';
  const authoredByCompiled = new Map((sourceMap ?? []).map(entry => [entry.stepId, entry.authoredStepId]));
  const normalize = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === 'object') {
      const result: Record<string, unknown> = {};
      for (const key of Object.keys(value as Record<string, unknown>).sort()) {
        const item = (value as Record<string, unknown>)[key];
        if (item === null || item === undefined) continue;
        result[key] = normalize(item);
      }
      return result;
    }
    return value;
  };
  const raw = document as Record<string, unknown>;
  const steps = Array.isArray(raw.steps) ? raw.steps as Array<Record<string, unknown>> : [];
  const normalizedSteps = steps.map(step => {
    const copy: Record<string, unknown> = { ...step };
    if (typeof copy.id === 'string') copy.id = authoredByCompiled.get(copy.id) ?? copy.id;
    delete copy.name;
    return normalize(copy);
  });
  return JSON.stringify(normalize({
    dimension: raw.dimension,
    domain: raw.domain,
    geometry: raw.geometry,
    steps: normalizedSteps,
    solver: normalizeSolverForFingerprint(raw.solver),
  }));
}
