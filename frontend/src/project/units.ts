/**
 * Project-level length-unit conversion for the browser editors.
 *
 * The project document stores every length and rate in its declared native unit.
 * ViennaPS is unit-agnostic, so switching units is a lossless document
 * transformation: scale every known length/rate field by 1000 (um→nm) or 0.001
 * (nm→um), leave durations/angles/dimensionless factors alone, and update
 * ``units``.
 *
 * Some documents cannot be converted safely because their payload embeds a
 * micrometre-frame asset or a model whose length/rate fields are not declared in
 * the catalog. Those cases return a blocker instead of silently corrupting the
 * request.
 */

export type LengthUnit = 'um' | 'nm';

type JsonRecord = Record<string, any>;

export function lengthUnitOf(project: JsonRecord | null | undefined): LengthUnit {
  return project?.units?.length === 'nm' ? 'nm' : 'um';
}

export function lengthUnitsPerUm(unit: LengthUnit): number {
  return unit === 'nm' ? 1000 : 1;
}

export function lengthUnitLabel(unit: LengthUnit): string {
  return unit === 'nm' ? 'nm' : 'µm';
}

export function rateUnitLabel(unit: LengthUnit): string {
  return unit === 'nm' ? 'nm/s' : 'µm/s';
}

export function lengthMaxUmToUnit(maxUm: number, unit: LengthUnit): number {
  return maxUm * lengthUnitsPerUm(unit);
}

function scaleValue(value: unknown, factor: number): unknown {
  return typeof value === 'number' && Number.isFinite(value) ? value * factor : value;
}

function scaleArray(value: unknown, factor: number): unknown {
  return Array.isArray(value) ? value.map(item => scaleValue(item, factor)) : value;
}

function scalePairRates(value: unknown, factor: number): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const row = { ...(value as JsonRecord) };
  for (const key of ['directional', 'isotropic']) {
    if (typeof row[key] === 'number') row[key] = row[key] * factor;
  }
  return row;
}

function scaleMaterialRates(value: unknown, factor: number): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const table = value as JsonRecord;
  const next: JsonRecord = {};
  for (const [name, row] of Object.entries(table)) {
    next[name] = typeof row === 'number'
      ? row * factor
      : scalePairRates(row, factor);
  }
  return next;
}

function scaleStep(step: JsonRecord, factor: number): JsonRecord {
  const next: JsonRecord = { ...step };
  for (const key of ['radius', 'cutoffHeight', 'overPolish', 'width', 'height', 'depth',
    'rateMagnitude', 'directionalRateMagnitude', 'isotropicRateMagnitude',
    'isotropicComponent', 'directionalComponent']) {
    if (typeof next[key] === 'number') next[key] = next[key] * factor;
  }
  for (const key of ['position', 'min', 'max', 'halfAxes', 'offset']) {
    if (Array.isArray(next[key])) next[key] = scaleArray(next[key], factor);
  }
  if (next.cellSet && typeof next.cellSet === 'object' && !Array.isArray(next.cellSet)) {
    const cellSet = { ...(next.cellSet as JsonRecord) };
    for (const key of ['height', 'substrateDepth']) {
      if (typeof cellSet[key] === 'number') cellSet[key] = cellSet[key] * factor;
    }
    next.cellSet = cellSet;
  }
  if (next.gdsMask && typeof next.gdsMask === 'object' && !Array.isArray(next.gdsMask)) {
    const mask = { ...(next.gdsMask as JsonRecord) };
    if (Array.isArray(mask.position)) mask.position = scaleArray(mask.position, factor);
    for (const key of ['baseHeight', 'height']) {
      if (typeof mask[key] === 'number') mask[key] = mask[key] * factor;
    }
    next.gdsMask = mask;
  }
  if (next.materialRates !== undefined) next.materialRates = scaleMaterialRates(next.materialRates, factor);
  if (next.defaultMaterialRate !== undefined) next.defaultMaterialRate = scalePairRates(next.defaultMaterialRate, factor);
  if (Array.isArray(next.rateSets)) {
    next.rateSets = next.rateSets.map(row => {
      if (!row || typeof row !== 'object' || Array.isArray(row)) return row;
      const rate = { ...(row as JsonRecord) };
      for (const key of ['directionalVelocity', 'isotropicVelocity']) {
        if (typeof rate[key] === 'number') rate[key] = rate[key] * factor;
      }
      return rate;
    });
  }
  if (next.nativeParameters && typeof next.nativeParameters === 'object' && !Array.isArray(next.nativeParameters)) {
    const native = { ...(next.nativeParameters as JsonRecord) };
    if (native.materialRates !== undefined) native.materialRates = scaleMaterialRates(native.materialRates, factor);
    next.nativeParameters = native;
  }
  return next;
}

/**
 * Convert a project to ``target`` and return the transformed copy.
 *
 * ``blockedReason`` is non-empty when the document embeds data that this
 * editor cannot rescale losslessly; callers must keep the original project.
 */
export function convertProjectUnits<T extends JsonRecord>(
  project: T,
  target: LengthUnit,
): { project: T; blockedReason: string } {
  const source = lengthUnitOf(project);
  if (source === target) return { project, blockedReason: '' };
  const factor = lengthUnitsPerUm(target) / lengthUnitsPerUm(source);
  const geometry = (project.geometry ?? {}) as JsonRecord;
  const kind = String(geometry.kind ?? '');
  if (kind === 'gds') {
    return { project, blockedReason: 'GDS 几何的坐标来自版图资产，暂不支持切换长度单位；请保持 µm 或重新导入。' };
  }
  if (kind === 'stack' && (geometry.gdsMask || (geometry.pattern as JsonRecord | undefined)?.kind === 'gds')) {
    return { project, blockedReason: '带 GDS 掩膜的堆栈几何暂不支持切换长度单位。' };
  }
  if (project.continuationState) {
    return { project, blockedReason: '带 continuationState 的工程暂不支持切换长度单位；状态文件按原单位保存。' };
  }
  const steps = Array.isArray(project.steps) ? project.steps as JsonRecord[] : [];
  for (const step of steps) {
    const model = String(step.model ?? '');
    if (['csvProcess', 'gdsBoolean', 'implant', 'anneal', 'analysis', 'multiParticle'].includes(model)) {
      return { project, blockedReason: `步骤 ${String(step.name ?? step.id ?? model)}（${model}）含资产/能量/网格字段，暂不支持单位切换。` };
    }
    if (model === 'specialized' && String(step.nativeModel ?? '') !== 'selectiveEpitaxy') {
      return { project, blockedReason: `specialized:${String(step.nativeModel ?? '?')} 的 nativeParameters 单位未被编辑器完整声明，暂不支持切换。` };
    }
  }

  const next = structuredClone(project) as JsonRecord;
  next.units = { length: target, time: 's', rate: target === 'nm' ? 'nm/s' : 'um/s', angle: 'deg' };
  const domain = (next.domain ?? {}) as JsonRecord;
  for (const key of ['gridDelta', 'xExtent', 'yExtent']) {
    if (typeof domain[key] === 'number') domain[key] = domain[key] * factor;
  }
  if (Array.isArray(domain.bounds)) domain.bounds = scaleArray(domain.bounds, factor);
  next.domain = domain;

  const nextGeometry = (next.geometry ?? {}) as JsonRecord;
  for (const key of ['height', 'width', 'depth', 'radius', 'maskHeight', 'baseHeight']) {
    if (typeof nextGeometry[key] === 'number') nextGeometry[key] = nextGeometry[key] * factor;
  }
  if (Array.isArray(nextGeometry.layers)) {
    nextGeometry.layers = (nextGeometry.layers as JsonRecord[]).map(layer => ({
      ...layer,
      ...(typeof layer.thickness === 'number' ? { thickness: layer.thickness * factor } : {}),
    }));
  }
  if (nextGeometry.pattern && typeof nextGeometry.pattern === 'object' && !Array.isArray(nextGeometry.pattern)) {
    const pattern = { ...(nextGeometry.pattern as JsonRecord) };
    for (const key of ['width', 'radius']) {
      if (typeof pattern[key] === 'number') pattern[key] = pattern[key] * factor;
    }
    nextGeometry.pattern = pattern;
  }
  if (nextGeometry.extrusion && typeof nextGeometry.extrusion === 'object' && !Array.isArray(nextGeometry.extrusion)) {
    const extrusion = { ...(nextGeometry.extrusion as JsonRecord) };
    if (Array.isArray(extrusion.extent)) extrusion.extent = scaleArray(extrusion.extent, factor);
    nextGeometry.extrusion = extrusion;
  }
  next.geometry = nextGeometry;

  next.steps = steps.map(step => scaleStep(step, factor));

  const solver = (next.solver ?? {}) as JsonRecord;
  if (solver.rayTracing && typeof solver.rayTracing === 'object' && !Array.isArray(solver.rayTracing)) {
    const rays = { ...(solver.rayTracing as JsonRecord) };
    if (typeof rays.diskRadius === 'number') rays.diskRadius = rays.diskRadius * factor;
    solver.rayTracing = rays;
  }
  next.solver = solver;

  if (next.outputSettings && typeof next.outputSettings === 'object' && !Array.isArray(next.outputSettings)) {
    const output = { ...(next.outputSettings as JsonRecord) };
    if (typeof output.volumeMeshDelta === 'number') output.volumeMeshDelta = output.volumeMeshDelta * factor;
    if (output.cellSet && typeof output.cellSet === 'object' && !Array.isArray(output.cellSet)) {
      const cellSet = { ...(output.cellSet as JsonRecord) };
      for (const key of ['height', 'substrateDepth']) {
        if (typeof cellSet[key] === 'number') cellSet[key] = cellSet[key] * factor;
      }
      output.cellSet = cellSet;
    }
    next.outputSettings = output;
  }

  if (next.view && typeof next.view === 'object' && !Array.isArray(next.view)) {
    const view = { ...(next.view as JsonRecord) };
    for (const key of ['panX', 'panH']) {
      if (typeof view[key] === 'number') view[key] = view[key] * factor;
    }
    next.view = view;
  }
  if (next.view3D && typeof next.view3D === 'object' && !Array.isArray(next.view3D)) {
    const view = { ...(next.view3D as JsonRecord) };
    if (view.camera && typeof view.camera === 'object' && !Array.isArray(view.camera)) {
      const camera = { ...(view.camera as JsonRecord) };
      if (Array.isArray(camera.position)) camera.position = scaleArray(camera.position, factor);
      if (Array.isArray(camera.focalPoint)) camera.focalPoint = scaleArray(camera.focalPoint, factor);
      if (typeof camera.parallelScale === 'number') camera.parallelScale = camera.parallelScale * factor;
      view.camera = camera;
    }
    for (const key of ['clip', 'section']) {
      if (view[key] && typeof view[key] === 'object' && !Array.isArray(view[key])) {
        const settings = { ...(view[key] as JsonRecord) };
        if (typeof settings.position === 'number') settings.position = settings.position * factor;
        view[key] = settings;
      }
    }
    next.view3D = view;
  }

  return { project: next as T, blockedReason: '' };
}
