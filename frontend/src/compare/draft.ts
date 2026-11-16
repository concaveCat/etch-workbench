/** Shared draft model for the three comparison pages. */

import type { Comparability, DomainSpec, InitialStructure, JobSpec, StepIR } from './api';

export type Draft = {
  caseId: string;
  view: 'iso' | 'iso90' | 'iso180' | 'iso270' | 'xz' | 'yz' | 'xy';
  cameraMode: 'preset' | 'free';
  camera: { azimuth: number; elevation: number; zoom: number };
  domain: DomainSpec;
  initial: InitialStructure;
  steps: StepIR[];
};

export const MATERIALS = ['Si', 'SiO2', 'Si3N4', 'PolySi', 'SiGe', 'Al2O3', 'HfO2', 'TiN', 'W', 'Mask'];
export const MATERIAL_COLORS: Record<string, string> = {
  Si: '#78909C', SiO2: '#00e5ff', Si3N4: '#dce775', PolySi: '#ff3d00', SiGe: '#00bfa5',
  Al2O3: '#72abab', HfO2: '#ff1744', TiN: '#9e9e9e', W: '#607d8b', Mask: '#fff59d',
};

/** Mirrors engines/comparability.json: grades for the viennaps|spx pair. */
const GRADES: Record<string, Comparability> = {
  initial: 'exact',
  'etch/isotropic': 'exact',
  'deposit/isotropic': 'exact',
  'etch/anisotropic': 'exact',
  'deposit/anisotropic': 'exact',
  'planarize/cmp': 'approximate',
};

const SPIKE_LAYOUT = { path: '/root/SPX/spx/V-2024.03/linux64/examples/Logic1_Layout.oasis', window: [0.035, 0.0, 0.175, 0.288] };

export function defaultDraft(): Draft {
  return {
    caseId: 'plane-iso-etch',
    view: 'iso',
    cameraMode: 'free',
    camera: { azimuth: 45, elevation: 30, zoom: 1 },
    domain: { width: 0.14, depth: 0.288, height: 0.4, grid_delta: 0.002, dimensions: 3 },
    initial: {
      kind: 'plane',
      substrate: 'Si',
      substrate_thickness: 0.3,
      films: [{ material: 'SiO2', thickness: 0.1 }],
    },
    steps: [
      { step_id: 's0_init', kind: 'initial', mechanism: 'initial', duration: 0, rates: {}, comparability: 'exact' },
      {
        step_id: 's1_etch', kind: 'etch', mechanism: 'isotropic', duration: 0.05,
        rates: { SiO2: -1.0, Si: -0.02 }, comparability: 'exact',
      },
    ],
  };
}

export function gradeOf(step: StepIR): Comparability {
  if (step.mechanism === 'initial') return 'exact';
  return GRADES[`${step.kind}/${step.mechanism}`] ?? 'no_counterpart';
}

export function toJob(draft: Draft): JobSpec {
  return {
    schema_version: 1,
    case_id: draft.caseId,
    engine: 'viennaps',
    domain: draft.domain,
    initial: draft.initial,
    steps: draft.steps.map((step) => ({ ...step, comparability: gradeOf(step) })),
    render: draft.cameraMode === 'free'
      ? { width: 700, height: 560, view: draft.view, camera: draft.camera }
      : { width: 700, height: 560, view: draft.view },
  };
}

/** SPX clips its structure from a layout window; keep the job consistent. */
export function withLayout(job: JobSpec): JobSpec & { layout: typeof SPIKE_LAYOUT } {
  return { ...job, layout: SPIKE_LAYOUT };
}
