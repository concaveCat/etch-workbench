import type { Geometry, IonSpecies, MaterialCatalogItem, NeutralSpecies, Project, StackGeometry, Step } from '../api/types';
import { isGeometryStep } from '../lib/projectRules';

// Project defaults and migrations. `defaultProject` is the new-project
// template; the promote/normalize helpers upgrade projects loaded from disk
// without touching the rest of their content.

export const defaultProject: Project = {
  schemaVersion: 7,
  name: '未命名项目',
  dimension: 2,
  units: { length: 'um', time: 's', rate: 'um/s', angle: 'deg' },
  domain: { gridDelta: 0.1, xExtent: 5, xBoundary: 'reflective' },
  geometry: { kind: 'stack', substrateMaterial: 'Si', baseHeight: 0, layers: [
    { id: 'layer_sio2', name: 'SiO₂ 薄膜', material: 'SiO2', thickness: 0.6, role: 'film' },
    { id: 'layer_si3n4', name: 'Si₃N₄ 掩膜', material: 'Si3N4', thickness: 0.6, role: 'mask' },
  ], pattern: { kind: 'trench', width: 2, taperAngle: 0 } },
  steps: [],
  outputSettings: { levelSets: false, levelSetMeshes: false, surfaceMeshVtk: false, volumeMesh: false, nativeMetadata: false },
  solver: { timeStepRatio: 0.25, raysPerPoint: 256, cpuThreads: 1, random: { mode: 'fixed', seed: 314159 }, enginePreference: 'auto', timeoutSeconds: 0,
    advection: { spatialScheme: 'ENGQUIST_OSHER_1ST_ORDER', temporalScheme: 'FORWARD_EULER', adaptiveTimeStepping: false, adaptiveTimeStepSubdivisions: 20, calculateIntermediateVelocities: false, checkDissipation: true, dissipationAlpha: 1, ignoreVoids: false, velocityOutput: false },
    rayTracing: { diskRadius: 0, ignoreFluxBoundaries: false, maxBoundaryHits: 1000, maxReflections: 4294967295, minNodeDistanceFactor: 0.05, normalizationType: 'SOURCE', smoothingNeighbors: 1 } },
  view: { panX: 0, panH: 0, zoom: 1, showGrid: true },
};

export function initialVisibleMaterialIds(catalog: MaterialCatalogItem[], geometry: Geometry, preferred: number[] = []): number[] {
  const usedNames = geometry.kind === 'stack'
    ? [geometry.substrateMaterial, ...geometry.layers.map(layer => layer.material), ...(geometry.gdsMask ? [geometry.gdsMask.material] : [])]
    : geometry.kind === 'gds'
      ? geometry.layers.map(layer => layer.material)
      : [geometry.substrateMaterial, ...(geometry.kind === 'fin' ? [geometry.maskMaterial] : geometry.kind === 'trench' || geometry.kind === 'hole' ? [geometry.maskMaterial] : [])];
  const catalogIds = catalog.map(material => material.id);
  const usedIds = catalog.filter(material => usedNames.includes(material.name)).map(material => material.id);
  return [...new Set([...usedIds, ...preferred, ...catalogIds])].filter(id => catalogIds.includes(id)).slice(0, 16);
}

export function promoteProjectToV5(source: Project): Project {
  return {
    ...source,
    schemaVersion: source.schemaVersion >= 7 ? 7 : source.schemaVersion === 6 ? 6 : 5,
    steps: source.steps.map(step => step.model === 'directional' || step.model === 'multiParticle' || step.model === 'specialized' || step.model === 'csvProcess' || isGeometryStep(step) ? step : { ...step, rateMode: step.rateMode ?? 'uniform' }),
    solver: { ...source.solver, enginePreference: 'cpu' },
  };
}
export function promoteProjectToV7(source: Project): Project {
  const defaults = defaultProject.solver;
  return {
    ...source,
    schemaVersion: 7,
    steps: source.steps.map(step => step.model === 'directional' || step.model === 'multiParticle' || step.model === 'specialized' || step.model === 'csvProcess' || isGeometryStep(step) ? step : { ...step, rateMode: step.rateMode ?? 'uniform' }),
    solver: { ...source.solver, enginePreference: 'auto', advection: { ...defaults.advection!, ...source.solver.advection }, rayTracing: { ...defaults.rayTracing!, ...source.solver.rayTracing } },
    outputSettings: source.outputSettings ?? { ...structuredClone(defaultProject.outputSettings!), volumeMesh: source.dimension === 3 },
  };
}
export function normalizeGdsMaskLayer(source: Project): Project {
  if (source.geometry.kind !== 'stack' || !source.geometry.gdsMask || source.geometry.layers.some(layer => layer.source === 'gds-mask')) return source;
  const mask = source.geometry.gdsMask;
  const id = `gds_mask_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`;
  const geometry: StackGeometry = {
    ...source.geometry,
    layers: [...source.geometry.layers, { id, name: mask.name, material: mask.material, thickness: mask.height, role: 'mask', source: 'gds-mask' }],
    pattern: { kind: 'gds' },
    gdsMask: { ...mask, baseHeight: source.geometry.baseHeight + source.geometry.layers.reduce((sum, layer) => sum + layer.thickness, 0) },
  };
  return { ...source, schemaVersion: 7, dimension: 3, domain: { ...source.domain, yExtent: source.domain.yExtent ?? source.domain.xExtent, boundaryXY: source.domain.boundaryXY ?? 'reflective' }, geometry };
}
export function defaultDirection(dimension: 2 | 3): [number, number, number] {
  return dimension === 2 ? [0, -1, 0] : [0, 0, -1];
}
export function defaultIonSpecies(label = 'argonIon'): IonSpecies {
  return { kind: 'ion', label, sourcePower: 1, thetaRMin: 0, thetaRMax: 50, minAngle: 60, B_sp: -1,
    meanEnergy: 120, sigmaEnergy: 15, thresholdEnergy: 10, inflectAngle: 25, n: 1 };
}
export function defaultNeutralSpecies(label = 'fluorineNeutral'): NeutralSpecies {
  return { kind: 'neutral', label, defaultStickingProbability: 0.4, stickingProbabilityByMaterial: {} };
}
export function defaultMultiParticleFields(): Pick<Step, 'species' | 'rateCoefficients'> {
  return { species: [defaultIonSpecies(), defaultNeutralSpecies()],
    rateCoefficients: { defaultBySpecies: { argonIon: 0.025, fluorineNeutral: 0.025 }, byMaterial: {} } };
}
