import type { DirectionalMaterialRate, Geometry, Project, SpecializedModel, Step } from '../api/types';

// Project/recipe rules: what a step actually does, material roles and the
// defaults a specialized ViennaPS model needs. Pure functions, no state.

// Isotropic steps hold one scalar rate per material; directional steps hold a
// directional/isotropic pair. Both live in `materialRates`, so read them through
// these helpers instead of casting at each use site.
export function scalarMaterialRates(step: Step): Record<string, number> {
  const table = step.materialRates ?? {};
  return Object.fromEntries(Object.entries(table).filter(entry => typeof entry[1] === 'number')) as Record<string, number>;
}
export function directionalMaterialRates(step: Step): Record<string, DirectionalMaterialRate> {
  const table = step.materialRates ?? {};
  return Object.fromEntries(Object.entries(table).filter(entry => typeof entry[1] === 'object' && entry[1] !== null)) as Record<string, DirectionalMaterialRate>;
}
export function isDirectionalMaterialMode(step: Step): boolean {
  return step.model === 'directional' && (step.materialRates !== undefined || step.defaultMaterialRate !== undefined);
}
export function materialRateModeAvailable(step: Step): boolean { return step.model === 'isotropic' || step.operation === 'etch'; }
export function materialRateModeActive(step: Step): boolean { return materialRateModeAvailable(step) && step.rateMode === 'material'; }
export function stripIsotropicMethod(step: Step): Step {
  const next = { ...step }; delete next.isotropicMethod; delete next.isotropicTimeStepRatio; return next;
}
export function stripDirectionalRateTable(step: Step): Step {
  const next: Step & { rateSets?: unknown } = { ...step };
  delete next.materialRates; delete next.defaultMaterialRate; delete next.rateSets;
  return next;
}
export function stripDirectionalScalarRates(step: Step): Step {
  const next: Step & { rateSets?: unknown } = { ...step };
  delete next.directionalRateMagnitude; delete next.isotropicRateMagnitude; delete next.calculateVisibility; delete next.rateSets;
  return next;
}

export const MATERIAL_CATEGORY_OPTIONS = [
  ['Generic', '通用'], ['Silicon', '硅'], ['OxideNitride', '氧化物 / 氮化物'], ['Hardmask', '硬掩膜'],
  ['Metal', '金属'], ['Silicide', '硅化物'], ['Compound', '化合物'], ['TwoD', '二维材料'],
  ['TCO', '透明导电氧化物'], ['Misc', '其他'],
] as const;
export function materialCategoryLabel(value?: string): string {
  return MATERIAL_CATEGORY_OPTIONS.find(([key]) => key === value)?.[1] ?? value ?? '通用';
}

const SPECIALIZED_FLUX_MODELS = new Set(['sf6o2', 'cf4o2', 'hbro2', 'sf6c4f8', 'fluorocarbon', 'ionBeam', 'faradayCage', 'teosDeposition', 'teospecvd', 'ald', 'neutralTransport']);

export function hasMotion(step: Step): boolean {
  if (isGeometryStep(step)) return false;
  if (step.model === 'specialized' || step.model === 'csvProcess') return true;
  if (step.model === 'directional') {
    if (isDirectionalMaterialMode(step)) {
      return Object.values(directionalMaterialRates(step)).some(row => (row.directional ?? 0) > 0 || (row.isotropic ?? 0) > 0)
        || (step.defaultMaterialRate?.directional ?? 0) > 0 || (step.defaultMaterialRate?.isotropic ?? 0) > 0;
    }
    return (step.directionalRateMagnitude ?? 0) > 0 || (step.isotropicRateMagnitude ?? 0) > 0;
  }
  if (step.model === 'multiParticle') {
    const table = step.rateCoefficients;
    return Object.values(table?.defaultBySpecies ?? {}).some(rate => rate > 0)
      || Object.values(table?.byMaterial ?? {}).some(row => Object.values(row).some(rate => rate > 0));
  }
  if (step.rateMode !== 'material') return (step.rateMagnitude ?? 0) > 0;
  return (step.rateMagnitude ?? 0) > 0 || Object.values(scalarMaterialRates(step)).some(rate => rate > 0);
}
export function usesFlux(step: Step): boolean { return step.enabled && ((step.model === 'specialized' && SPECIALIZED_FLUX_MODELS.has(step.nativeModel ?? '')) || (step.model === 'singleParticle' || step.model === 'multiParticle') && hasMotion(step)); }
export function isGeometryStep(step: Step): boolean { return step.model === 'removeMaterial' || step.model === 'planarize' || step.model === 'boolean' || step.model === 'gdsBoolean'; }

export function stepKindLabel(step: Step): string {
  if (step.model === 'removeMaterial') return `移除材料 · ${step.material ?? '未选择'}`;
  if (step.model === 'planarize') return step.stopMaterial ? `平坦化 · 停在 ${step.stopMaterial}` : '平坦化';
  if (step.model === 'boolean') return `布尔几何 · ${{ union: '并集', intersection: '交集', difference: '差集' }[step.booleanOperation ?? 'difference']}`;
  if (step.model === 'gdsBoolean') return `GDS 布尔 · layer ${step.gdsMask?.layer ?? '—'}/${step.gdsMask?.datatype ?? 0} · ${{ union: '并集', intersection: '交集', difference: '差集' }[step.booleanOperation ?? 'difference']}`;
  if (step.model === 'specialized') return `ViennaPS 专用模型 · ${step.nativeModel ?? '未选择'}`;
  if (step.model === 'csvProcess') return `CSV 速率表 · ${step.fileName ?? '未选择文件'} · ${step.interpolationMode ?? '未设置插值'}`;
  const directionalTableCount = step.model === 'directional' ? Object.keys(directionalMaterialRates(step)).length : 0;
  return `${step.operation === 'etch' ? '刻蚀' : '沉积'} · ${step.model === 'isotropic' ? '各向同性' : step.model === 'singleParticle' ? '单粒子' : step.model === 'multiParticle' ? '多粒子' : '定向过程'}${hasMotion(step) ? '' : ' · 零速率'}${directionalTableCount ? ` · 材料速率表 ${directionalTableCount} 项` : ''}`;
}
export function geometryMaterialNames(geometry: Geometry): string[] {
  const names = new Set<string>();
  if (geometry.kind === 'gds') geometry.layers.forEach(layer => names.add(layer.material));
  else {
    names.add(geometry.substrateMaterial);
    if (geometry.kind === 'stack') { geometry.layers.forEach(layer => names.add(layer.material)); if (geometry.gdsMask) names.add(geometry.gdsMask.material); }
    if ((geometry.kind === 'trench' || geometry.kind === 'hole' || geometry.kind === 'fin') && geometry.maskHeight > 0) names.add(geometry.maskMaterial ?? 'Mask');
  }
  return [...names].filter(Boolean).sort();
}
export function specializedModelAvailable(spec: SpecializedModel, dimension: number): boolean {
  return !spec.dimensions || spec.dimensions.includes(dimension);
}
export function specializedDimensionLabel(spec: SpecializedModel): string | null {
  if (!spec.dimensions || spec.dimensions.length === 0 || spec.dimensions.length >= 2) return null;
  return `仅支持 ${spec.dimensions[0]}D`;
}
export function specializedDefaults(spec: SpecializedModel, geometry: Geometry): Record<string, unknown> {
  const names = geometryMaterialNames(geometry);
  return Object.fromEntries(spec.fields.map(field => {
    if (field.kind === 'materialRates') {
      const required = spec.id === 'selectiveEpitaxy' ? ['Si', 'SiGe'] : spec.id === 'wetEtch' ? names : [];
      return [field.key, Object.fromEntries(required.map(name => [name, field.default]))];
    }
    if (field.kind === 'fluorocarbonMaterials') return [field.key, Object.fromEntries([...new Set([...names, 'Mask', 'Polymer'])].sort().map(name => [name, { ...(field.defaults ?? {}) }]))];
    return [field.key, structuredClone(field.default)];
  }));
}

export function configuredMaterialRates(project: Project, fallback: number): Record<string, number> {
  const roles = new Map<string, { seen: boolean; mask: boolean }>();
  const add = (name: string, mask: boolean) => {
    const current = roles.get(name) ?? { seen: false, mask: false };
    roles.set(name, { seen: true, mask: current.mask || mask });
  };
  if (project.geometry.kind === 'gds') project.geometry.layers.forEach(layer => add(layer.material, layer.mask ?? false));
  else {
    add(project.geometry.substrateMaterial, false);
    if (project.geometry.kind === 'stack') { project.geometry.layers.filter(layer => layer.source !== 'gds-mask').forEach(layer => add(layer.material, layer.role === 'mask')); if (project.geometry.gdsMask?.protectsEtch) add(project.geometry.gdsMask.material, true); }
    else if (project.geometry.kind === 'trench' || project.geometry.kind === 'hole' || project.geometry.kind === 'fin') add(project.geometry.maskMaterial ?? 'Mask', true);
  }
  return Object.fromEntries([...roles].map(([name, role]) => [name, role.mask ? 0 : fallback]));
}
