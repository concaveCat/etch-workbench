import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import './workflow.css';
import { SAMPLE_EXAMPLES } from './samples';
import { executionCompileBasis } from './fingerprint';
import { convertProjectUnits, lengthUnitLabel, lengthUnitsPerUm, rateUnitLabel, type LengthUnit } from '../project/units';
import {
  coverageStepNames,
  enabledCoverageSteps,
  missingCompiledSteps,
  missingRouteSteps,
  skippedCoverageSteps,
} from './compileCoverage';
import WorkflowViewer3D from './viewer3d/WorkflowViewer3D';
import type { Viewer3DMarker } from './viewer3d/types';

type Health = { status: string; applicationVersion?: string; viennaps?: string; viennals?: string; activeRunId?: string | null };
/** Global settings contract: GET/PUT /api/settings. */
type SettingsLimitKey = 'maxDataGiB' | 'artifactLimitMiB' | 'rssLimitMiB' | 'maxLogMiB' | 'minFreeGiB';
type SettingsLimitSource = 'default' | 'env' | 'settings';
type SettingsLimit = {
  value: number;
  source: SettingsLimitSource;
  default: number;
  minimum: number;
  maximum: number | null;
  envVariable?: string | null;
  envValue?: number | null;
  unit?: string;
  label?: string;
};
type SettingsStorage = {
  dataDirectory: string;
  usedBytes: number;
  freeBytes: number;
  totalBytes: number;
  runs: number;
  projects: number;
  domainStates: number;
  gdsAssets: number;
  csvAssets: number;
};
type SettingsPayload = {
  schemaVersion: number;
  limits: Record<SettingsLimitKey, SettingsLimit>;
  defaults?: { solverTimeoutSeconds?: number };
  storage: SettingsStorage;
  versions: { viennaps?: string; viennals?: string };
};
type SettingsDraft = Record<SettingsLimitKey, string> & { solverTimeoutSeconds: string };

const SETTINGS_LIMIT_KEYS: SettingsLimitKey[] = ['maxDataGiB', 'artifactLimitMiB', 'rssLimitMiB', 'maxLogMiB', 'minFreeGiB'];
const SOLVER_TIMEOUT_DEFAULT = 600;
const SOLVER_TIMEOUT_MIN = 5;
const SOLVER_TIMEOUT_MAX = 86400;
const SETTINGS_LIMIT_FALLBACK: Record<SettingsLimitKey, { label: string; unit: string }> = {
  maxDataGiB: { label: '数据目录上限', unit: 'GiB' },
  artifactLimitMiB: { label: '单产物上限', unit: 'MiB' },
  rssLimitMiB: { label: '进程内存上限', unit: 'MiB' },
  maxLogMiB: { label: '日志上限', unit: 'MiB' },
  minFreeGiB: { label: '最小剩余空间', unit: 'GiB' },
};
const SETTINGS_SOURCE_LABEL: Record<SettingsLimitSource, string> = { settings: '设置', env: '环境变量', default: '默认' };
const EMPTY_SETTINGS_DRAFT: SettingsDraft = {
  maxDataGiB: '', artifactLimitMiB: '', rssLimitMiB: '', maxLogMiB: '', minFreeGiB: '',
  solverTimeoutSeconds: '',
};
type GdsAsset = { assetId: string; fileName: string };
type IsotropicMethod = 'auto' | 'levelSet' | 'geometric';
type DirectionalMaterialRate = { directional: number; isotropic: number };
/** Isotropic steps use a scalar table; directional steps use the rate pair. */
type StepMaterialRates = Record<string, number> | Record<string, DirectionalMaterialRate>;
type ProjectStep = {
  id: string; name: string; model: string; operation?: string; enabled: boolean;
  duration?: number; thickness?: number; rateMagnitude?: number;
  depositMaterial?: string; stickingProbability?: number; sourceExponent?: number;
  rateMode?: string;
  isotropicMethod?: IsotropicMethod;
  isotropicTimeStepRatio?: number;
  materialRates?: StepMaterialRates;
  defaultMaterialRate?: DirectionalMaterialRate;
  [key: string]: unknown;
};
/** One `/api/specialized-models` field hint; the editor only renders label/kind. */
type SpecializedFieldHint = { key: string; label?: string; kind?: string };
type SpecializedModelSpec = {
  id: string;
  label?: string;
  operation?: string;
  dimensions?: number[];
  depositMaterial?: string;
  fields?: SpecializedFieldHint[];
  optionalFields?: SpecializedFieldHint[];
};
type ProjectGeometrySource = {
  kind: 'parametric' | 'layout';
  clipId?: string;
  clipName?: string;
  layoutId?: string;
  clipHash?: string;
};
type ProjectDoc = {
  name?: string;
  dimension?: number;
  steps?: ProjectStep[];
  geometry?: Record<string, unknown>;
  domain?: Record<string, unknown>;
  solver?: Record<string, unknown>;
  [key: string]: unknown;
};
type ProjectRecord = {
  schemaVersion: number;
  projectId: string;
  name: string;
  dimension: number;
  stepCount: number;
  createdAt: string;
  updatedAt: string;
};
/** Per-project 03 page state, mirroring backend `ProjectWorkflowStateV1`. */
type ProjectWorkflowState = {
  schemaVersion: 1;
  compileId: string | null;
  /** Opaque fingerprint of the authored project when the saved Compile ran. */
  compileBasis: string | null;
  routeId: string | null;
  segmentRuns: Array<{ segmentId: string; runId: string; measurementId: string | null }>;
  measurementIds: string[];
  experimentIds: string[];
  updatedAt?: string | null;
};

type CompiledEnvelope = {
  project: { name: string; dimension: number; steps: ProjectStep[] };
  sourceMap: Array<{ stepId: string; authoredStepId: string }>;
  compileRecord: { compileId: string; flowId: string; geometryBindingId: string };
};

type LibraryCategory = 'material' | 'process' | 'flow' | 'experiment';
type LibraryItem = {
  schemaVersion: number;
  itemId: string;
  category: LibraryCategory;
  name: string;
  description: string;
  payload: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
};
type MaterialRow = { id?: number; name: string; color?: string; category?: string; isCustom?: boolean };
type FlowDefinitionSummary = {
  definitionId: string;
  flowId: string;
  revision: number;
  name: string;
  nodeCount: number;
  variableCount: number;
  updatedAt: string;
};
type RecipeSummary = { recipeId: string; version: number; metricId: string; method: string; name: string; createdAt: string };
type CompileInfo = {
  flowId: string;
  bindingId: string;
  compileId: string;
  projectName: string;
  dimension: number;
  stepCount: number;
  authoredSteps?: ProjectStep[];
  compile: { project: { name: string; dimension: number; steps: ProjectStep[]; solver?: { timeoutSeconds?: number; [key: string]: unknown } }; sourceMap?: Array<{ stepId: string; authoredStepId: string }> };
};
type RoutePlan = {
  route: { routeId: string; name: string; headAttemptId: string };
  stepOrder: string[];
  reuse: Array<{ reuseId: string; attemptId: string; reusedFromSegmentId: string; fromStepId: string; toStepId: string }>;
  attempts: Array<{ attemptId: string; status: string; compileId: string; runSeed: number }>;
  segments: Array<{
    segmentId: string;
    attemptId: string;
    status: string;
    runId: string | null;
    startAfterStepId: string | null;
    throughStepId: string;
    segmentProjectHash: string;
    endCheckpointRef: unknown;
  }>;
};
type Measurement = {
  measurementId: string;
  status: string;
  value: number | null;
  unit: string;
  reason: string | null;
  createdAt?: string;
  bindingStatus: string;
  equivalenceStatus: string;
  sourceRunId: string | null;
  sourceStepId: string | null;
  sourceStateId: string | null;
  recipeId: string;
  recipeVersion: number;
  method: string;
  material: string;
  sourceSnapshotHash: string;
  intersections: number[];
  uncertainty: number | null;
  uncertaintyStatus: string;
  bindingEvidence: { methodVersion: string; verifiedAt: string } | null;
};
type MeasurementDecision = {
  schemaVersion: number;
  decisionId: string;
  workflowRunId: string;
  routeId: string;
  sourceSegmentId: string;
  sourceAttemptId: string | null;
  sourceCompileId: string;
  sourceCompileHash: string | null;
  sourceRunId: string;
  startAfterStepId: string;
  sourceCheckpointHash: string | null;
  sourcePrefixDigest: string | null;
  seed: number | null;
  measurementId: string;
  measurementStatus: string;
  measurementBindingStatus: string;
  measurementEquivalenceStatus: string;
  recipeId: string;
  recipeVersion: number;
  metricId: string;
  method: string;
  measuredValue: number;
  unit: string;
  operator: string;
  threshold: number;
  result: boolean;
  chosenBranch: 'true' | 'false';
  candidateParameters: {
    true: { parameters: Record<string, Record<string, unknown>> };
    false: { parameters: Record<string, Record<string, unknown>> };
  } | null;
  resolvedParameters: Record<string, Record<string, unknown>>;
  targetCompileId: string;
  targetCompileHash: string | null;
  branchAttemptId: string;
  branchSegmentId: string;
  branchThroughStepId: string | null;
  replayOf: string | null;
  createdAt: string;
};
type MeasurementDecisionResponse = {
  decision: MeasurementDecision;
  plan: RoutePlan;
  workflowRunId: string;
  measurement: Measurement;
  resolvedParameters: Record<string, Record<string, unknown>>;
};
type Recipe = {
  recipeId: string;
  version: number;
  metricId: string;
  name: string;
  dimension: number;
  method: string;
  material: string;
  parameters: Record<string, unknown>;
};
type TemplateSummary = { templateId: string; name: string; stepCount: number };
type LibraryBundleSummary = {
  bundleId: string;
  name: string;
  description: string;
  createdAt: string;
  moduleCount: number;
  definitionCount: number;
  templateCount: number;
  experimentTemplateCount: number;
};
type LibraryBundle = {
  schemaVersion: number;
  bundleId: string;
  name: string;
  description: string;
  createdAt: string;
  modules: Array<{ module: { moduleId: string; name: string }; revision: number }>;
  definitions: Array<{ definitionId: string }>;
  templates: Array<{ templateId: string }>;
  experimentTemplates: Array<{ name: string }>;
};
type LibraryImportResult = {
  bundleId: string;
  policy: 'reject' | 'clone';
  modules: Array<{ moduleId: string; revision: number; status: string }>;
  definitionIds: string[];
  templateIds: string[];
  experimentIds: string[];
  skipped: string[];
  conflicts: string[];
  renames: Array<{ kind: string; previousId: string; importedId: string }>;
};
type LayoutStructure = {
  name: string;
  layers: number[];
  layerDatatypes: number[][];
  maskLayerDatatypes: number[][];
  references: string[];
  bounds: { x0: number; y0: number; x1: number; y1: number } | null;
};
type LayoutDetail = {
  layoutId: string;
  assetId: string;
  fileName: string;
  sizeBytes: number;
  structures: LayoutStructure[];
  createdAt: string;
};
type ClipPolygon = { layer: number; datatype: number; points: number[][] };
type ClipCanvas = {
  structure: string;
  bbox: { x0: number; y0: number; x1: number; y1: number };
  width: number;
  height: number;
  polygons: ClipPolygon[];
  polygonCount: number;
  pointCount: number;
  contentBounds: { x0: number; y0: number; x1: number; y1: number };
};
type Clip = ClipCanvas & {
  clipId: string;
  name: string;
  layoutId: string;
  layers: Array<{ layer: number; datatype: number | null; material: string; baseHeight?: number | null; height?: number | null }>;
  annotations: Array<{ annotationId: string; text: string; x: number; y: number }>;
  clipHash: string;
  gdsAssetId: string;
  gdsFileName: string;
  createdAt: string;
};
type ExperimentDoc = {
  experimentId: string;
  name: string;
  status: string;
  kind?: string;
  recipeVersion: number;
  unit?: string;
  design?: { conditionCount: number; logicalRunCount: number; replicates: number };
  queue?: { total: number; pending: number; running: number; succeeded: number; failed: number; retries: number; estimatedSecondsRemaining: number; estimatedCostUsd: number };
  aggregation?: { rows: Array<{ ordinal: number; n: number; mean: number | null; std: number | null; ci95: number | null; unit: string; statisticalStatus: string }>; ciMethod?: string };
  retention?: { mode: string };
  baseCompileId?: string | null;
  variants: Array<{
    ordinal: number;
    parameters?: Record<string, Record<string, number>>;
    replicates: Array<{
      status: string; value?: number | null; unit?: string; runId?: string; seed?: number;
      attemptCount?: number; statisticalSample?: boolean; measurementId?: string; reason?: string;
      measurementStatus?: string;
    }>;
  }>;
  project?: { steps: Array<{ id: string }> };
};
type ExperimentSummaryRow = { experimentId: string; name: string; updatedAt: string; status: string; variantCount: number };
type DoeFactorRow = { stepId: string; parameter: string; values: string };

/** Numeric step parameters the DOE endpoint accepts. */
const DOE_PARAMETERS: Array<{ id: string; label: string }> = [
  { id: 'rateMagnitude', label: 'rateMagnitude（µm/s）' },
  { id: 'duration', label: 'duration（s）' },
  { id: 'stickingProbability', label: 'stickingProbability' },
  { id: 'sourceExponent', label: 'sourceExponent' },
];

const DOE_DEFAULT_VALUES: Record<string, string> = {
  rateMagnitude: '0.01,0.02,0.03',
  duration: '0.02,0.04,0.06',
  stickingProbability: '0.01,0.1',
  sourceExponent: '0.5,1.0',
};
type SnapshotSummary = {
  runId: string;
  stepId: string;
  dimension: number;
  topology: string;
  digest: string;
  pointCount: number;
  lineCount: number;
  triangleCount: number;
  bounds: number[];
  materialMeasures: Record<string, number>;
  measureKind: string;
};
type CompareResult = {
  candidates: Array<{ runId: string; stepId: string; label: string | null; summary: SnapshotSummary }>;
  pairwise: Array<{
    left: string; right: string; status: string; leftDigest: string; rightDigest: string;
    maxPointDelta: number | null; boundsDelta: number; materialDelta: Record<string, number>;
    reasons: string[]; tolerance: number;
  }>;
};
type CandidateView = {
  dimension: number;
  topology: string;
  points: number[][];
  lines: number[][];
  triangles: number[][];
  materialIds: number[];
  lineMaterialIds: number[];
  triangleMaterialIds: number[];
  materials: Record<string, { name?: string; color?: string }>;
  materialNames?: Record<string, string>;
  pointCount: number;
  lineCount: number;
  triangleCount: number;
  bounds: number[];
  sampledPointCount: number;
  coordinateUnit?: string;
  /** Point indices for x / h / z; 2D snapshots keep the height in `h`. */
  axisMap?: { x?: number; h?: number; z?: number };
};
type ProcessWindowResult = {
  summary: { total: number; pass: number; fail: number; uncertain: number; notEvaluated: number };
  factorWindows: Array<{ stepId: string; parameter: string; min: number | null; max: number | null }>;
  candidates: Array<{ ordinal: number; mean: number | null; ci95: number | null; status: string; margin: number | null; parameters: Record<string, Record<string, number>> }>;
  rows: Array<{ ordinal: number; mean: number | null; ci95: number | null; status: string }>;
  lower: number;
  upper: number;
};
type CalibrationDataset = { datasetId: string; name: string; trainCount: number; validationCount: number; sha256: string };
type CalibrationFit = {
  fitId: string;
  model: string;
  method: string;
  parameterNames: string[];
  parameters: number[];
  iterations: number;
  validationStatus: string;
  trainResiduals: { n: number; rmse: number | null; mae: number | null; maxAbs: number | null };
  validationResiduals: { n: number; rmse: number | null; mae: number | null; maxAbs: number | null };
};
type ReportDocument = { reportId: string; title: string; html: string; sha256: string; createdAt: string };
type ExperimentTable = {
  experimentId: string;
  status: string;
  unit: string;
  queue?: ExperimentDoc['queue'];
  rows: Array<{ ordinal: number; n: number; mean: number | null; std: number | null; ci95: number | null; statisticalStatus: string; parameters: Record<string, Record<string, number>> }>;
};
type Provenance = {
  measurementId: string;
  bindingStatus: string;
  verifications: Array<{ verificationId: string; status: string; createdAt: string; evidence: { methodVersion: string } }>;
};
type ExpandedModuleStep = {
  authoredStepId: string;
  stepId: string;
  flowPath: string;
  moduleInstanceIdPath: string;
  iterationKey: string;
  moduleId: string | null;
  moduleInstanceId: string | null;
  step: Record<string, unknown> & { name?: string; model?: string };
  parameterProvenance: Record<string, { kind: string; source: string | null; resolvedValue: unknown }>;
};
type ModuleExpansion = {
  definitionId: string;
  flowId: string;
  revision: number;
  stepCount: number;
  maxDepth: number;
  steps: ExpandedModuleStep[];
  variables: Array<{ name: string; value: unknown; unit: string | null; source: string }>;
  iterations: Array<{ path: string; variable: string; index: number; value: unknown }>;
  moduleInstances: Array<{ instancePath: string; moduleId: string; parameters: Record<string, unknown> }>;
};

const DEFAULT_MODULE_JSON = JSON.stringify({
  schemaVersion: 1,
  moduleId: 'mod_ui_etch',
  name: 'UI etch module',
  description: '',
  parameters: [
    { name: 'rate', default: 0.1, unit: 'um/s' },
    { name: 'duration', default: 0.1, unit: 's' },
  ],
  steps: [{
    id: 'etch_ui_a', name: 'etch', enabled: true, operation: 'etch', model: 'isotropic',
    rateMagnitude: '${rate}', duration: '${duration}', rateMode: 'uniform',
  }],
}, null, 2);

const DEFAULT_DEFINITION_JSON = JSON.stringify({
  schemaVersion: 2,
  definitionId: 'def_ui_demo',
  flowId: 'flow_ui_demo',
  revision: 1,
  name: 'UI modular flow',
  description: '',
  variables: [{ name: 'rate', default: 0.1, unit: 'um/s' }],
  nodes: [
    { kind: 'module-call', instanceId: 'call_ui_a', moduleId: 'mod_ui_etch', parameters: { rate: '${rate}' } },
    {
      kind: 'repeat', repeatId: 'loop_ui', loopVariable: 'level', count: 2,
      body: [{ kind: 'module-call', instanceId: 'call_ui_b', moduleId: 'mod_ui_etch', parameters: { rate: '${level * 0.05}' } }],
    },
  ],
  solver: {
    timeStepRatio: 0.25, raysPerPoint: 32, cpuThreads: 1,
    random: { mode: 'fixed', seed: 7 }, enginePreference: 'cpu', timeoutSeconds: 30,
  },
}, null, 2);

/**
 * Output log verbs. Only the completed action is printed (never the internal
 * handler id), so the log stays a single Chinese result line per action.
 */
const ACTION_LABELS: Record<string, string> = {
  'compile-flow': '编译流程',
  'template': '保存模板',
  'diff': '比较编译版本',
  'route': '创建 Route',
  'execute': '执行 Segment',
  'measure': '测量',
  'decision': '生成测量决策',
  'decision-replay': '重放决策',
  'branch': '创建分支',
  'experiment': '运行实验',
  'experiment-create': '创建实验',
  'experiment-delete': '删除实验',
  'experiment-open': '打开实验',
  'layout': '注册版图',
  'preview': '预览 Clip',
  'clip': '保存 Clip',
  'annotation': '添加标注',
  'clip-use': '使用 Clip',
  'module': '保存模块',
  'definition': '保存流程定义',
  'expand': '展开流程定义',
  'definition-compile': '编译模块化流程',
  'library': '更新共享库',
  'library-save': '保存库条目',
  'library-delete': '删除库条目',
  'library-insert': '插入库条目',
  'doe': '设计 DOE',
  'retention': '更新保留策略',
  'table': '更新实验表格',
  'metric-detail': '载入测量证据',
  'compare': '比较候选',
  'window': '计算工艺窗口',
  'report': '生成报告',
  'calibration': '更新标定',
  'gds': '上传 GDS',
  'project': '更新工程',
  'project-delete': '删除工程',
  'project-open': '打开工程',
  'project-save': '保存工程',
};

function actionLabel(name: string): string {
  return ACTION_LABELS[name] ?? '操作';
}

const ZONES = [
  { id: 'setup', num: '01', label: '工程', title: '工程定义', desc: '定义名称、维度、几何来源、计算域与求解器。' },
  { id: 'flow', num: '02', label: '流程', title: '流程编辑', desc: '从库插入步骤，编辑参数并编译流程。' },
  { id: 'run', num: '03', label: '运行', title: '运行与测量', desc: '执行 Segment、测量并查看指标。' },
  { id: 'experiment', num: '04', label: '实验', title: '实验设计', desc: '手工实验、全因子 DOE、队列与聚合。' },
  { id: 'analyze', num: '05', label: '分析', title: '分析', desc: '候选对比、结构差异、工艺窗口与基础标定。' },
  { id: 'report', num: '06', label: '报告', title: '报告', desc: '生成包含来源证据、结构视图与实验结果的 HTML 报告。' },
] as const;

/** Read a persisted pane size, clamped to the layout's safe range. */
function readStoredSize(key: string, fallback: number, minimum: number, maximum: number): number {
  try {
    const raw = Number(window.localStorage.getItem(key));
    return Number.isFinite(raw) && raw >= minimum && raw <= maximum ? raw : fallback;
  } catch {
    return fallback;
  }
}

const STATUS_LABELS: Record<string, string> = {
  planned: '待执行',
  queued: '排队中',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
  timed_out: '超时',
  verified: '已核验',
  measured: '已测量',
  ambiguous: '测量有歧义',
  region_empty: '区域为空',
  no_intersection: '无交点',
  material_not_found: '材料缺失',
  unsupported_dimension: '维度不支持',
  unsupported_units: '单位不支持',
  invalid_geometry: '几何无效',
  stale: '待重跑',
  completed: '已完成',
  pending: '待处理',
  evaluated: '已评估',
  not_evaluated: '未评估',
  pass: '通过',
  fail: '失败',
  uncertain: '不确定',
  'not-applicable': '不适用',
  not_applicable: '不适用',
  'shared-run-seed': '共享运行种子',
};

/** Uncertainty uses its own wording: `ambiguous` here means "有歧义", not a status pill. */
const UNCERTAINTY_LABELS: Record<string, string> = {
  ambiguous: '有歧义',
  'not-applicable': '不适用',
  not_applicable: '不适用',
  measured: '已确定',
};

function uncertaintyLabel(value: string | null | undefined): string {
  if (!value) return '—';
  return UNCERTAINTY_LABELS[value] ?? STATUS_LABELS[value] ?? value;
}

const ENGINE_LABELS: Record<string, string> = { cpu: 'CPU', gpu: 'GPU', auto: 'Auto' };

const MATERIAL_ROLES: Array<[string, string]> = [
  ['substrate', '基底 (substrate)'],
  ['film', '薄膜 (film)'],
  ['mask', '掩膜 (mask)'],
  ['resist', '光刻胶 (resist)'],
];

const BINDING_LABELS: Record<string, string> = {
  verified: '已绑定',
  unverified: '未绑定',
  pending: '待绑定',
  not_applicable: '不适用',
  equivalent: '等价',
  not_equivalent: '不等价',
  not_evaluated: '未评估',
};

function bindingLabel(status: string): string {
  return BINDING_LABELS[status] ?? status;
}

function clockTime(date = new Date()): string {
  return date.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.toLocaleDateString('en-CA')} ${date.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit' })}`;
}

/** Compact × for one-shot banners. Confirmation banners keep 取消/确认 instead. */
function BannerClose({ onClose }: { onClose: () => void }) {
  return <button type="button" className="spx-banner-close" aria-label="关闭提示" onClick={onClose}>×</button>;
}

const PROJECT_STORAGE_KEY = 'viennaps.workflow.projectId';

async function projectRequest(path: string, method: 'POST' | 'PUT', document: unknown): Promise<ProjectRecord> {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(document),
  });
  const text = await response.text();
  let payload: unknown = text;
  try { payload = text ? JSON.parse(text) : {}; } catch { /* keep text */ }
  if (!response.ok) {
    const error = new Error(errorText(payload)) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return payload as ProjectRecord;
}

const LIBRARY_CATEGORY_LABELS: Record<LibraryCategory, string> = {
  material: '材料库',
  process: '工艺库',
  flow: '流程库',
  experiment: '实验库',
};

/** Empty `ProjectWorkflowStateV1`; unset server state reads exactly like this. */
function emptyWorkflowState(): ProjectWorkflowState {
  return {
    schemaVersion: 1, compileId: null, compileBasis: null, routeId: null,
    segmentRuns: [], measurementIds: [], experimentIds: [],
  };
}

/** Strip server-owned timestamps so a re-save of identical content stays a no-op. */
function workflowStatePayload(state: ProjectWorkflowState) {
  return {
    schemaVersion: 1 as const,
    compileId: state.compileId,
    compileBasis: state.compileBasis,
    routeId: state.routeId,
    segmentRuns: state.segmentRuns,
    measurementIds: state.measurementIds,
    experimentIds: state.experimentIds,
  };
}

function compileInfoFromEnvelope(envelope: CompiledEnvelope): CompileInfo {
  return {
    flowId: envelope.compileRecord.flowId,
    bindingId: envelope.compileRecord.geometryBindingId,
    compileId: envelope.compileRecord.compileId,
    projectName: envelope.project.name,
    dimension: envelope.project.dimension,
    stepCount: envelope.project.steps.length,
    compile: { project: envelope.project, sourceMap: envelope.sourceMap },
  };
}

/** ProjectV7 geometry binding for one saved Clip (same shape the server stores). */
function clipGeometryBinding(clip: Clip, dimension: number) {
  return {
    kind: 'gds', assetId: clip.gdsAssetId, fileName: clip.gdsFileName,
    structure: 'CLIP', unitScale: 1, position: [0, 0, 0],
    layers: clip.layers.map(item => dimension === 3
      // 3D GDS needs a z placement per layer; the backend rejects a missing one.
      ? {
        layer: item.layer, material: item.material || 'Si',
        baseHeight: item.baseHeight ?? 0, height: item.height ?? 0.2,
      }
      : { layer: item.layer, material: item.material || 'Si' }),
  };
}

/** Default parametric geometry for a dimension/kind switch inside 01. */
function defaultParametricGeometry(dimension: number, kind: string, previous: Record<string, unknown>): Record<string, unknown> {
  const substrateMaterial = typeof previous.substrateMaterial === 'string' ? previous.substrateMaterial : 'Si';
  const maskMaterial = typeof previous.maskMaterial === 'string' ? previous.maskMaterial : 'Mask';
  if (kind === 'trench') {
    return {
      kind: 'trench', width: Number(previous.width) || 1, depth: Number(previous.depth) || 2,
      taperAngle: Number(previous.taperAngle) || 0, maskHeight: Number(previous.maskHeight) || 0,
      maskTaperAngle: 0, substrateMaterial, maskMaterial,
    };
  }
  if (kind === 'hole') {
    return {
      kind: 'hole', radius: Number(previous.radius) || 1, depth: Number(previous.depth) || 2,
      taperAngle: Number(previous.taperAngle) || 0, maskHeight: Number(previous.maskHeight) || 0,
      maskTaperAngle: 0, substrateMaterial, maskMaterial,
    };
  }
  if (kind === 'fin') {
    return {
      kind: 'fin', width: Number(previous.width) || 1, height: Number(previous.finHeight ?? previous.height) || 1.2,
      taperAngle: Number(previous.taperAngle) || 0, maskHeight: Number(previous.maskHeight) || 0,
      maskTaperAngle: 0, substrateMaterial, maskMaterial, construction: 'native',
    };
  }
  return dimension === 2
    ? { kind: 'plane', height: Number(previous.height) || 0, substrateMaterial }
    : { kind: 'fin', width: 1, height: 1.2, taperAngle: 0, maskHeight: 0, maskTaperAngle: 0, substrateMaterial, maskMaterial, construction: 'native' };
}

function isHexColor(value: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(value.trim());
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

const TOLERANCE_LABELS: Record<string, string> = {
  identical: '一致',
  within_tolerance: '在公差内',
  WITHIN_TOLERANCE: '在公差内',
  different: '超出公差',
  OUTSIDE_TOLERANCE: '超出公差',
  not_evaluated: '未评估',
  NOT_EVALUATED: '未评估',
};

function toleranceLabel(status: string): string {
  return TOLERANCE_LABELS[status] ?? status;
}

const TOPOLOGY_LABELS: Record<string, string> = {
  lines: '线框',
  triangles: '三角面',
  points: '点云',
};

function topologyLabel(topology: string): string {
  return TOPOLOGY_LABELS[topology] ?? topology;
}

/** Micron values: three decimals (`0.020`, `2.000`) so the unit column aligns. */
function formatMicrons(value: number): string {
  if (!Number.isFinite(value)) return '—';
  return Math.abs(value) >= 1000 ? formatNumber(value) : value.toFixed(3);
}

/** One-line annotation drawn inside a 2D structure view. */
function structureSurfaceNote(view: CandidateView, axis: StructureAxis): string {
  const extent = structureExtent(view, axis);
  const unit = structureUnit(view);
  const depth = extent.maxV - extent.minV;
  if (!extent.flatV) return `深度 ${formatMicrons(depth)} ${unit}`;
  const surface = Math.abs(extent.maxV) < 5e-4 ? '0.000' : formatMicrons(extent.maxV);
  return `表面高度 ${surface} ${unit}`;
}

/** `{"Si": 0}` reads as "无材料差异"; non-zero entries use a full-width colon. */
function materialDeltaText(delta: Record<string, number>): string {
  const entries = Object.entries(delta ?? {});
  if (!entries.length) return '无材料差异';
  const changed = entries.filter(([, value]) => Math.abs(Number(value) || 0) > 1e-12);
  if (!changed.length) return '无材料差异';
  return changed.map(([key, value]) => `${key}：${formatNumber(value)}`).join(' · ');
}

const COMPARE_REASON_PATTERNS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^snapshot topology or element counts differ$/, () => '拓扑或元素数量不同'],
  [/^snapshot line\/triangle connectivity differs$/, () => '线/三角形连接关系不同'],
  [/^max point delta ([\d.eE+-]+) exceeds tolerance ([\d.eE+-]+)$/, match => `最大点偏差 ${formatNumber(Number(match[1]))} 超过公差 ${formatNumber(Number(match[2]))}`],
  [/^bounds delta ([\d.eE+-]+) exceeds tolerance ([\d.eE+-]+)$/, match => `边界偏差 ${formatNumber(Number(match[1]))} 超过公差 ${formatNumber(Number(match[2]))}`],
  [/^material (.+) measure delta ([\d.eE+-]+) exceeds material tolerance ([\d.eE+-]+)$/, match => `材料 ${match[1]} 偏差 ${formatNumber(Number(match[2]))} 超过材料公差 ${formatNumber(Number(match[3]))}`],
];

function compareReasonLabel(reason: string): string {
  for (const [pattern, render] of COMPARE_REASON_PATTERNS) {
    const match = reason.match(pattern);
    if (match) return render(match);
  }
  return reason;
}

const METRIC_METHOD_LABELS: Record<string, string> = {
  etch_depth: '刻蚀深度',
  cd_at_height: '指定高度 CD',
};

/** The compare table stays readable at 4 candidates; every view still loads. */
const COMPARE_CANDIDATE_LIMIT = 4;

function candidateKey(row: { runId: string; stepId: string }): string {
  return `${row.runId}:${row.stepId}`;
}

function metricMethodLabel(method: string): string {
  return METRIC_METHOD_LABELS[method] ?? method;
}

/** Backend stores `um`; the UI shows the micro sign. */
function displayUnit(unit: string | null | undefined): string {
  if (!unit) return 'µm';
  return unit === 'um' ? 'µm' : unit;
}

/** Compact number for tables: 4 significant digits, no float noise, no needless exponents. */
function formatNumber(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value === 0) return '0';
  const abs = Math.abs(value);
  if (abs >= 1e6 || abs < 1e-6) return value.toExponential(3);
  return String(Number(value.toPrecision(digits)));
}

/** Only a `measured` result carries a number; every other status is an ambiguity or a failure. */
function measurementHasValue(item: { status?: string | null; value?: number | null }): boolean {
  return item.status === 'measured' && typeof item.value === 'number' && Number.isFinite(item.value);
}

const MEASUREMENT_REASON_PATTERNS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^multiple selected-material surfaces cross the measurement x$/, () => '测量位置 X 处有多条材料界面相交，无法确定唯一深度；请移动 X 位置或更换测量材料。'],
  [/^no material surface found at the measurement x$/, () => '测量位置 X 处没有找到材料界面。'],
  [/^no selected-material segment crosses the measurement x$/, () => '测量位置 X 处没有所选材料的界面。'],
  [/^isotropic model does not use a ray flux engine$/, () => '各向同性模型不使用射线通量引擎。'],
  [/^a selected-material segment lies on the measurement height$/, () => '所选材料的界面正好落在测量高度上。'],
  [/^fewer than two selected-material crossings at the measurement height$/, () => '测量高度上少于两个所选材料交点。'],
  [/^etch-depth x coordinate is outside the measurement region$/, () => '深度测量的 X 坐标超出测量区域。'],
  [/^material '?([^']+)'? is not present in the snapshot$/, match => `快照中没有材料 ${match[1]}。`],
  [/^line and material-id counts differ$/, () => '快照的线与材料编号数量不一致。'],
  [/^snapshot requires points, lines and cellData\.materialIds$/, () => '快照缺少点、线或材料编号。'],
  [/^3D material top-height summary has no finite height$/, () => '3D 材料顶面高度不是有限值。'],
  [/^3D metrology v1 supports material top-height depth only$/, () => '3D 测量仅支持材料顶面高度深度。'],
  [/^P0-3 metrology supports 2D and 3D-depth snapshots only$/, () => '测量仅支持 2D 与 3D 深度快照。'],
  [/^P0-3 V1 requires snapshot coordinates in um; normalize before measuring$/, () => '快照坐标必须是 µm，请先归一化。'],
];

const MEASUREMENT_FALLBACK_REASON = '测量无有效值';

/** Chinese explanation for a metrology reason; unmapped English stays in the tooltip only. */
function measurementReasonText(reason: string | null | undefined): string {
  if (!reason || !reason.trim()) return MEASUREMENT_FALLBACK_REASON;
  for (const [pattern, render] of MEASUREMENT_REASON_PATTERNS) {
    const match = reason.match(pattern);
    if (match) return render(match);
  }
  return MEASUREMENT_FALLBACK_REASON;
}

/** Raw backend text, kept for title/technical details only when it has no Chinese mapping. */
function measurementReasonDetail(reason: string | null | undefined): string | undefined {
  if (!reason || !reason.trim()) return undefined;
  return MEASUREMENT_REASON_PATTERNS.some(([pattern]) => pattern.test(reason)) ? undefined : reason;
}

function measurementValueText(item: { status?: string | null; value?: number | null; unit?: string | null }): string {
  return measurementHasValue(item) ? `${formatNumber(item.value)} ${displayUnit(item.unit)}` : '—';
}

function measurementUnitText(item: { status?: string | null; value?: number | null; unit?: string | null }): string {
  return measurementHasValue(item) ? displayUnit(item.unit) : '';
}

/** `V1 · 0.02 s · seed 20261029` — condition label used by 04 results and 05 candidates. */
function variantConditionLabel(variant: ExperimentDoc['variants'][number], replicateSeed?: number): string {
  const parameters = variant.parameters ?? {};
  const parts: string[] = [`V${variant.ordinal}`];
  for (const [stepId, values] of Object.entries(parameters)) {
    for (const [parameter, value] of Object.entries(values ?? {})) {
      const unit = parameter === 'duration' ? ' s' : parameter === 'rateMagnitude' ? ' µm/s' : '';
      parts.push(`${parameter === 'duration' ? '' : `${parameter} `}${formatNumber(value)}${unit}`);
    }
    if (!Object.keys(values ?? {}).length) parts.push(stepId);
  }
  const seed = replicateSeed ?? variant.replicates.find(item => item.seed !== undefined)?.seed;
  if (seed !== undefined) parts.push(`seed ${seed}`);
  return parts.join(' · ');
}

function experimentVariantStatus(variant: ExperimentDoc['variants'][number]): string {
  const statuses = variant.replicates.map(item => item.status);
  if (!statuses.length) return 'planned';
  if (statuses.some(status => status === 'running')) return 'running';
  if (statuses.some(status => status === 'queued')) return 'pending';
  if (statuses.every(status => status === 'succeeded')) return 'succeeded';
  if (statuses.some(status => status === 'failed')) return 'failed';
  if (statuses.every(status => status === 'planned')) return 'planned';
  return statuses[0];
}

function newStepId(): string {
  return `step_${Math.random().toString(16).slice(2, 10)}${Math.random().toString(16).slice(2, 6)}`;
}

function substituteTemplate(value: unknown, variables: Record<string, unknown>): unknown {
  if (Array.isArray(value)) return value.map(item => substituteTemplate(item, variables));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => [key, substituteTemplate(item, variables)]));
  }
  if (typeof value === 'string') {
    const match = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(value.trim());
    if (match && match[1] in variables) return variables[match[1]];
  }
  return value;
}

function blankStep(): ProjectStep {
  return {
    id: newStepId(), name: '新步骤', enabled: true,
    operation: 'etch', model: 'isotropic', rateMagnitude: 0.02,
    duration: 0.02, rateMode: 'uniform',
  };
}

/** Models this step editor can create without extra model-specific fields. */
const EDITABLE_STEP_MODELS = ['isotropic', 'singleParticle'];
const OPERATION_STEP_MODELS = new Set(['isotropic', 'singleParticle', 'directional', 'multiParticle']);
const RATE_STEP_MODELS = new Set(['isotropic', 'singleParticle', 'directional']);
const RATE_MODE_STEP_MODELS = new Set(['isotropic', 'singleParticle']);
const DEPOSIT_STEP_MODELS = new Set(['isotropic', 'singleParticle', 'directional', 'multiParticle', 'specialized']);
// Only these step models declare a `duration` in backend/models.py. In
// particular gdsBoolean/planarize/removeMaterial/boolean are pure geometry
// operations, and particleShape/implant/anneal/analysis carry their own time
// fields — a stray `duration` makes the strict model reject the project.
const DURATION_STEP_MODELS = new Set(['isotropic', 'singleParticle', 'directional', 'multiParticle', 'specialized', 'csvProcess']);
const GEOMETRY_ONLY_STEP_MODELS = new Set(['gdsBoolean', 'planarize', 'removeMaterial', 'boolean', 'insertBox', 'removeTopLevelSet']);
// Backend ProjectV7 domain floors. Domain.xExtent uses the historical 1 um
// floor; Domain3D.x/yExtent use MIN_DOMAIN_EXTENT_3D (0.01) plus a two-cell
// minimum. The form mirrors them so a backend-valid project can always save.
const MIN_DOMAIN_EXTENT_2D = 1;
const MIN_DOMAIN_EXTENT_3D = 0.01;
const MAX_DOMAIN_EXTENT = 40;

/**
 * Hard masks and resists are pattern materials, not deposition sources. The
 * backend StepV7 accepts any material string, so the editor keeps this list
 * explicit instead of offering the whole catalog.
 */
const NON_DEPOSIT_MATERIAL_CATEGORIES = new Set(['Hardmask']);
function isDepositableMaterial(row: MaterialRow): boolean {
  if (NON_DEPOSIT_MATERIAL_CATEGORIES.has(String(row.category ?? ''))) return false;
  return !/(mask|resist|photoresist)/i.test(row.name);
}
function depositableMaterialNames(catalog: MaterialRow[]): string[] {
  const names = catalog.filter(isDepositableMaterial).map(row => row.name);
  // SiO2 is the least surprising default for a new deposition step.
  return names.sort((left, right) => (left === 'SiO2' ? -1 : right === 'SiO2' ? 1 : 0));
}

/**
 * Drop fields the selected operation/model does not accept, and fill the ones
 * the schema requires. Backend validators reject `depositMaterial` on etch and
 * particle fields on isotropic, so an operation/model switch must clean up
 * instead of carrying stale values into the next Compile.
 */
function sanitizeProjectStep(step: ProjectStep, materialNames: string[]): ProjectStep {
  const next: ProjectStep = { ...step };
  const model = String(next.model ?? 'isotropic');
  const operation = String(next.operation ?? 'etch');
  const geometryOnly = GEOMETRY_ONLY_STEP_MODELS.has(model);
  // Directional `rateSets` is a mutually exclusive alternative to the scalar
  // direction/rate triple. Keeping both (or injecting the scalar placeholder)
  // makes the strict DirectionalStepV5 reject an otherwise valid project.
  const hasRateSets = model === 'directional'
    && Array.isArray(next.rateSets) && next.rateSets.length > 0;
  // The directional material table is the third mode. Like the backend, the
  // presence of the table is the mode switch, so it wins over a stale array.
  // `rateMode` is an isotropic/singleParticle field only, so its `material`
  // value is the discriminator that keeps a scalar isotropic table from being
  // read as a directional one on a model switch.
  const hasDirectionalMaterialRates = model === 'directional'
    && step.rateMode !== 'material'
    && (step.materialRates != null || step.defaultMaterialRate != null);
  // Isotropic dispatch fields exist only for isotropic steps. `geometric` has
  // no level-set time step, and the schema rejects the pair with a material
  // table, so clean both stale combinations here.
  if (model !== 'isotropic') {
    delete next.isotropicMethod;
    delete next.isotropicTimeStepRatio;
  } else if (next.isotropicMethod === 'geometric') {
    delete next.isotropicTimeStepRatio;
  }
  if (OPERATION_STEP_MODELS.has(model)) {
    if (operation !== 'deposit' && operation !== 'etch') next.operation = 'etch';
  } else if (model !== 'specialized') {
    delete next.operation;
  }
  if (geometryOnly) {
    // Geometry-only models accept only id/name/enabled/model plus their own
    // mask/cutoff/material fields. A model switch must not carry any shared
    // process scalar into them: the strict backend rejects every extra key.
    delete next.operation;
    delete next.duration;
    delete next.rateMagnitude;
    delete next.directionalRateMagnitude;
    delete next.isotropicRateMagnitude;
    delete next.rateMode;
    delete next.materialRates;
    delete next.defaultMaterialRate;
    delete next.rateSets;
    delete next.depositMaterial;
    delete next.stickingProbability;
    delete next.sourceExponent;
    delete next.direction;
    delete next.calculateVisibility;
  } else if (!DURATION_STEP_MODELS.has(model)) {
    // Models whose time lives in their own fields (particleShape/implant/
    // anneal/analysis) do not declare `duration`.
    delete next.duration;
  }
  if (operation !== 'deposit' || !DEPOSIT_STEP_MODELS.has(model)) {
    delete next.depositMaterial;
  } else if (!next.depositMaterial) {
    next.depositMaterial = materialNames[0] ?? 'SiO2';
  }
  if (model !== 'singleParticle') {
    delete next.stickingProbability;
    delete next.sourceExponent;
  } else {
    if (typeof next.stickingProbability !== 'number') next.stickingProbability = 1;
    if (typeof next.sourceExponent !== 'number') next.sourceExponent = 1;
  }
  // Isotropic now accepts a material table for deposit as well as etch (the
  // native IsotropicProcess takes the operation sign itself). `geometric`
  // offsets the whole surface by one radius, so it stays uniform.
  const materialRateModeAllowed = model === 'isotropic'
    ? next.isotropicMethod !== 'geometric'
    : operation === 'etch';
  if (!RATE_MODE_STEP_MODELS.has(model)) {
    delete next.rateMode;
    if (model !== 'directional') delete next.materialRates;
  } else if (next.rateMode === 'material' && materialRateModeAllowed) {
    if (!next.materialRates || typeof next.materialRates !== 'object' || Array.isArray(next.materialRates)) {
      next.materialRates = {};
    }
  } else {
    next.rateMode = 'uniform';
    delete next.materialRates;
  }
  if (!RATE_STEP_MODELS.has(model)) {
    delete next.rateMagnitude;
    delete next.directionalRateMagnitude;
    delete next.isotropicRateMagnitude;
  } else if (model === 'directional' && hasDirectionalMaterialRates) {
    // Native RateMap material mode: scalar rates are forbidden, but the
    // direction vector is still required. The table falls back per material to
    // `defaultMaterialRate` (0 = protected).
    delete next.rateMagnitude;
    delete next.directionalRateMagnitude;
    delete next.isotropicRateMagnitude;
    if (!isUsableDirection(next.direction)) next.direction = [0, 0, -1];
    if (!next.materialRates || typeof next.materialRates !== 'object' || Array.isArray(next.materialRates)) {
      next.materialRates = {};
    }
    if (!next.defaultMaterialRate || typeof next.defaultMaterialRate !== 'object' || Array.isArray(next.defaultMaterialRate)) {
      next.defaultMaterialRate = { directional: 0, isotropic: 0 };
    }
    // The native RateMap overload has no visibility switch; the schema only
    // accepts the default, so a stale `false` must not survive the switch.
    if (next.calculateVisibility !== true) delete next.calculateVisibility;
  } else if (model === 'directional' && hasRateSets) {
    // Scalar-only fields must be absent, including `direction`.
    delete next.direction;
    delete next.rateMagnitude;
    delete next.directionalRateMagnitude;
    delete next.isotropicRateMagnitude;
  } else if (model === 'directional') {
    delete next.rateMagnitude;
    if (typeof next.directionalRateMagnitude !== 'number') next.directionalRateMagnitude = 0.02;
  } else {
    delete next.directionalRateMagnitude;
    delete next.isotropicRateMagnitude;
  }
  if (model !== 'directional') delete next.defaultMaterialRate;
  if (model === 'directional' && !hasDirectionalMaterialRates) delete next.materialRates;
  // rateSets belongs to directional only; an empty array is not a mode, so it
  // falls back to the scalar path instead of sending `rateSets: []`.
  if (model !== 'directional' || !hasRateSets || hasDirectionalMaterialRates) delete next.rateSets;
  // Model-specific payloads (specialized.nativeParameters, gdsBoolean.gdsMask,
  // planarize.cutoffHeight/stopMaterial/overPolish, removeMaterial.material)
  // are intentionally left untouched: only the shared scalar fields above are
  // normalized.
  return next;
}

/** A direction is usable when it is three finite numbers and not the zero vector. */
function isUsableDirection(value: unknown): value is [number, number, number] {
  if (!Array.isArray(value) || value.length !== 3) return false;
  if (!value.every(entry => typeof entry === 'number' && Number.isFinite(entry))) return false;
  return Math.hypot(value[0], value[1], value[2]) > 1e-12;
}

/**
 * Parse a specialized model's `nativeParameters` draft. The backend requires a
 * JSON object; invalid JSON or an array must never reach `updateSelectedStep`.
 */
function parseNativeParametersText(text: string): { value: Record<string, unknown> | null; error: string } {
  if (!text.trim()) return { value: null, error: '参数不能为空，请填写 JSON 对象' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const reason = error instanceof Error ? error.message : '无法解析';
    return { value: null, error: `JSON 语法错误：${reason}` };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { value: null, error: '必须是 JSON 对象，例如 {"rate111": 0.5}' };
  }
  return { value: parsed as Record<string, unknown>, error: '' };
}

/**
 * Parse the `rateSets` draft against the strict `DirectionalRateSetV5` shape.
 * Values are written verbatim, so the UI checks every field the model needs
 * (non-zero 3-component direction, non-negative velocities, unique materials).
 */
function parseRateSetsText(text: string): { value: Array<Record<string, unknown>> | null; error: string } {
  if (!text.trim()) return { value: null, error: 'rateSets 不能为空，请填写 JSON 数组' };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const reason = error instanceof Error ? error.message : '无法解析';
    return { value: null, error: `JSON 语法错误：${reason}` };
  }
  if (!Array.isArray(parsed)) return { value: null, error: '必须是 JSON 数组，例如 [{"direction":[0,0,-1],...}]' };
  if (parsed.length === 0) return { value: null, error: '至少需要一个 rateSet' };
  if (parsed.length > 8) return { value: null, error: '最多 8 个 rateSet' };
  for (let index = 0; index < parsed.length; index += 1) {
    const entry: unknown = parsed[index];
    const at = `第 ${index + 1} 项`;
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return { value: null, error: `${at}必须是 JSON 对象` };
    }
    const row = entry as Record<string, unknown>;
    const direction = row.direction;
    if (!Array.isArray(direction) || direction.length !== 3
      || direction.some(value => typeof value !== 'number' || !Number.isFinite(value))) {
      return { value: null, error: `${at} direction 必须是 3 个数字` };
    }
    if (direction.every(value => Math.abs(Number(value)) <= 1e-12)) {
      return { value: null, error: `${at} direction 不能是零向量` };
    }
    const velocityError = (name: string, value: unknown, required: boolean) => {
      if (value === undefined && !required) return '';
      if (typeof value !== 'number' || !Number.isFinite(value)) return `${at} ${name} 必须是数字`;
      if (value < 0 || value > 2) return `${at} ${name} 必须在 0–2 µm/s`;
      return '';
    };
    const directionalError = velocityError('directionalVelocity', row.directionalVelocity, true);
    if (directionalError) return { value: null, error: directionalError };
    const isotropicError = velocityError('isotropicVelocity', row.isotropicVelocity, false);
    if (isotropicError) return { value: null, error: isotropicError };
    if (row.maskMaterials !== undefined && row.maskMaterials !== null) {
      const maskMaterials = row.maskMaterials;
      if (!Array.isArray(maskMaterials)
        || maskMaterials.some(name => typeof name !== 'string' || !name.trim())) {
        return { value: null, error: `${at} maskMaterials 必须是材料名数组` };
      }
      if (new Set(maskMaterials).size !== maskMaterials.length) {
        return { value: null, error: `${at} maskMaterials 不能包含重复材料` };
      }
    }
    if (row.calculateVisibility !== undefined && typeof row.calculateVisibility !== 'boolean') {
      return { value: null, error: `${at} calculateVisibility 必须是布尔值` };
    }
  }
  return { value: parsed as Array<Record<string, unknown>>, error: '' };
}

/**
 * Parse a `materialRates` / `defaultMaterialRate` draft as a plain JSON object.
 * Only the outer shape is checked: material names and the 0–2 µm/s ranges stay
 * with the backend schema, so a copy-pasted table is still rejected loudly.
 */
function parseMaterialRateJsonText(
  text: string,
  label: string,
  example: string,
): { value: Record<string, unknown> | null; error: string } {
  if (!text.trim()) return { value: null, error: `${label} 不能为空，请填写 JSON 对象` };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const reason = error instanceof Error ? error.message : '无法解析';
    return { value: null, error: `JSON 语法错误：${reason}` };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { value: null, error: `必须是 JSON 对象，例如 ${example}` };
  }
  return { value: parsed as Record<string, unknown>, error: '' };
}

function stepFromLibrary(payload: Record<string, unknown>, name: string): ProjectStep {
  const step = { ...payload } as Record<string, unknown>;
  delete step.id;
  return {
    ...step,
    id: newStepId(),
    name: typeof step.name === 'string' && step.name.trim() ? String(step.name) : name,
    enabled: true,
  } as ProjectStep;
}

const STEP_LABELS: Record<string, string> = {
  isotropic: '各向同性',
  singleParticle: '单粒子',
  multiParticle: '多粒子',
  directional: '定向',
  specialized: '专用模型',
  planarize: '平坦化',
  removeMaterial: '移除材料',
  boolean: '布尔几何',
  gdsBoolean: 'GDS 布尔',
  csvProcess: 'CSV 速率表',
  particleShape: '粒子形状',
  implant: '注入',
  anneal: '退火',
  analysis: '分析',
};

const BOOLEAN_OPERATION_LABELS: Record<string, string> = {
  union: '并集',
  intersection: '交集',
  difference: '差集',
};

/**
 * Short, capability-aware description of a geometry-only step for the 02 step
 * table: the parameters the backend actually reads, not just the model name.
 */
function geometryStepSummary(step: ProjectStep, unitLabel = 'µm'): string {
  const model = String(step.model ?? '');
  const format = (value: unknown) => (typeof value === 'number' ? String(value) : String(value ?? ''));
  switch (model) {
    case 'gdsBoolean': {
      const mask = (step.gdsMask ?? {}) as Record<string, unknown>;
      const layer = mask.layer ?? '?';
      const datatype = mask.datatype ?? 0;
      const operation = BOOLEAN_OPERATION_LABELS[String(step.booleanOperation ?? '')] ?? String(step.booleanOperation ?? '—');
      const retained = mask.retainedRegion === 'outside' ? ' · 保留外侧' : '';
      return `${operation} · L${layer}/${datatype}${retained}`;
    }
    case 'planarize': {
      if (step.stopMaterial) {
        const overPolish = typeof step.overPolish === 'number' ? ` · 过抛 ${format(step.overPolish)} ${unitLabel}` : '';
        return `停在 ${String(step.stopMaterial)}${overPolish}`;
      }
      return step.cutoffHeight === undefined || step.cutoffHeight === null
        ? '未设置切削高度'
        : `切削 ${format(step.cutoffHeight)} ${unitLabel}`;
    }
    case 'removeMaterial':
      return step.material ? `移除 ${String(step.material)}` : '未选择材料';
    case 'specialized':
      return String(step.nativeModel ?? '未选择原生模型');
    case 'boolean': {
      const operation = BOOLEAN_OPERATION_LABELS[String(step.booleanOperation ?? '')] ?? String(step.booleanOperation ?? '—');
      return `${operation} · ${String(step.position ?? '')}`.trim();
    }
    case 'csvProcess':
      return [step.fileName, step.interpolationMode].filter(Boolean).map(String).join(' · ') || 'CSV 速率表';
    case 'particleShape':
      return step.shape === 'sphere'
        ? `球 · r=${format(step.radius)} ${unitLabel}`
        : `盒 · ${Array.isArray(step.halfAxes) ? (step.halfAxes as unknown[]).map(format).join(' × ') : '?'} ${unitLabel}`;
    default:
      return '';
  }
}

function errorText(payload: unknown): string {
  let text = '请求失败';
  if (typeof payload === 'string') {
    text = payload;
  } else if (payload && typeof payload === 'object') {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string') {
      text = detail;
    } else if (Array.isArray(detail)) {
      text = detail.map(item => {
        if (item && typeof item === 'object') {
          const record = item as { msg?: unknown; message?: unknown };
          return String(record.msg ?? record.message ?? JSON.stringify(item));
        }
        return String(item);
      }).join('; ');
    } else {
      text = JSON.stringify(payload);
    }
  }
  return translateBackendError(text);
}

/** 422 detail is a list of {path,message}; the dialog shows the first message. */
function firstValidationMessage(payload: unknown): string {
  const detail = payload && typeof payload === 'object' ? (payload as { detail?: unknown }).detail : null;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as unknown;
    if (first && typeof first === 'object') {
      const record = first as { message?: unknown; msg?: unknown };
      return String(record.message ?? record.msg ?? JSON.stringify(first));
    }
    return String(first);
  }
  return errorText(payload);
}

async function settingsRequest(init?: RequestInit): Promise<SettingsPayload> {
  const response = await fetch('/api/settings', init);
  const text = await response.text();
  let payload: unknown = text;
  try { payload = text ? JSON.parse(text) : {}; } catch { /* keep text */ }
  if (!response.ok) throw new Error(firstValidationMessage(payload));
  return payload as SettingsPayload;
}

function settingsDraftFrom(payload: SettingsPayload): SettingsDraft {
  const draft: SettingsDraft = { ...EMPTY_SETTINGS_DRAFT };
  for (const key of SETTINGS_LIMIT_KEYS) draft[key] = String(payload.limits[key]?.value ?? '');
  draft.solverTimeoutSeconds = String(payload.defaults?.solverTimeoutSeconds ?? SOLVER_TIMEOUT_DEFAULT);
  return draft;
}

/** Human-readable bytes for the read-only storage panel. */
function formatBytes(bytes: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const rounded = index === 0 || value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[index]}`;
}

/**
 * Backend conflicts are English by design (they are API codes). The UI never
 * shows them verbatim: known ones are translated, the rest get a Chinese prefix
 * and keep the original in the activity log for debugging.
 */
const BACKEND_ERROR_PATTERNS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^continued Segment has no successful predecessor checkpoint$/, () => '当前 Segment 需要前一个 Segment 成功后才能继续；请先执行前序 Segment，或点“执行全部 Segment”。'],
  [/^continued Segment requires a start checkpoint$/, () => '当前 Segment 缺少起始检查点；请先执行前序 Segment。'],
  [/^start checkpoint has no native Domain state$/, () => '前序 Segment 的检查点没有可续算的 Domain 状态；请重新执行前序 Segment。'],
  [/^start checkpoint state hash does not match the manifest$/, () => '前序检查点与运行清单不一致；请重新执行前序 Segment。'],
  [/^segment (.+) is not planned$/, () => '该 Segment 不在待执行状态，可能已运行或正在运行。'],
  [/^Segment range does not select a valid compile slice$/, () => 'Segment 的步骤范围无效；请重新创建 Route。'],
  [/^legacy unverified route cannot be executed$/, () => '旧版未验证 Route 不能执行；请重新创建 Route。'],
  [/^route compatibility verification failed: (.+)$/, match => `Route 与当前编译不兼容：${translateBackendError(match[1])}`],
  [/^Segment project is invalid: (.+)$/, match => `Segment 工程无效：${match[1]}`],
  [/^segment run could not start: a workbench run is already active$/, () => '已有运行中的任务；已尝试自动停止，请重试。'],
  [/^segment run could not start: (.+)$/, match => `Segment 无法启动：${match[1]}`],
  [/^a workbench run is already active$/, () => '已有运行中的任务；已尝试自动停止，请重试。'],
  [/^上一个运行停止超时：(.+)$/, match => `上一个运行停止超时（Run ${match[1]}）；请稍后重试。`],
  [/^上一个运行未能停止，请稍后重试$/, () => '上一个运行未能停止，请稍后重试。'],
  // Pydantic validation messages are English API text; the user gets a Chinese
  // sentence instead of "List should have at most 4 items after validation".
  [/^List should have at most (\d+) items after validation, not (\d+)$/, match => `数量超出上限：最多 ${match[1]} 个，当前 ${match[2]} 个。`],
  [/^List should have at least (\d+) items after validation, not (\d+)$/, match => `数量不足：至少 ${match[1]} 个，当前 ${match[2]} 个。`],
  [/^String should have at most (\d+) characters$/, match => `文本过长：最多 ${match[1]} 个字符。`],
  [/^String should have at least (\d+) characters$/, match => `文本过短：至少 ${match[1]} 个字符。`],
  [/^Field required$/, () => '缺少必填字段。'],
  [/^Input should be a valid (.+)$/, match => `输入格式无效：${match[1]}。`],
];

function translateBackendError(message: string): string {
  const text = (message ?? '').trim();
  for (const [pattern, render] of BACKEND_ERROR_PATTERNS) {
    const match = text.match(pattern);
    if (match) return render(match);
  }
  return text;
}

function stepOptionLabel(step: ProjectStep, index: number): string {
  const operation = step.operation === 'deposit' ? '沉积' : step.operation === 'etch' ? '刻蚀' : (step.operation ?? '');
  return `${step.name || `步骤 ${index + 1}`}${operation ? ` · ${operation}` : ''}`;
}

function parseAuthoredParameters(text: string, label: string): Record<string, Record<string, unknown>> {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(`${label} 参数必须是合法 JSON，例如 {"step_02":{"duration":0.02}}`);
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} 参数必须是 JSON 对象，例如 {"step_02":{"duration":0.02}}`);
  }
  return value as Record<string, Record<string, unknown>>;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const text = await response.text();
  let payload: unknown = text;
  try { payload = text ? JSON.parse(text) : {}; } catch { /* keep text */ }
  if (!response.ok) {
    const error = new Error(errorText(payload)) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return payload as T;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* ---------------------------------------------------------------------------
   Shared structure renderer. 03 (current run) and 05 (candidate compare) use
   the same component so a snapshot is drawn exactly once in the code base.
--------------------------------------------------------------------------- */

type StructureAxis = 'xy' | 'xz' | 'yz';
/** Point indices actually drawn on the horizontal / vertical axis. */
type StructureAxes = { horizontal: number; vertical: number; verticalLabel: string; is2D: boolean };
type StructureFrame = { minU: number; maxU: number; minV: number; maxV: number; unit: string; flatV: boolean };
type StructureMarker = { orientation: 'vertical' | 'horizontal'; value: number; label: string } | null;
type StructureStepRow = {
  runId: string;
  stepId: string;
  segmentId: string;
  segmentIndex: number;
  kind: 'initial' | 'step';
  label: string;
  /** False until the owning Segment succeeded and produced this snapshot. */
  executed: boolean;
  pendingReason?: string;
};

function structureUnit(view: CandidateView): string {
  return view.coordinateUnit === 'um' || !view.coordinateUnit ? 'µm' : view.coordinateUnit;
}

/**
 * Resolve the point indices drawn on each axis.
 *
 * 3D views follow the XY/XZ/YZ selector. 2D snapshots are authored on x with the
 * height in the `h` slot (`z` is a constant 0 for those writers), so the old
 * hard-coded `point[1]`/`point[2]` pair collapsed the profile into one flat
 * line. Prefer `axisMap.h`, then `axisMap.z`, then the first axis with a real
 * span, so both the current and older snapshots keep their depth.
 */
function structureAxes(view: CandidateView, axis: StructureAxis): StructureAxes {
  if (view.dimension !== 2) {
    if (axis === 'yz') return { horizontal: 1, vertical: 2, verticalLabel: 'Z', is2D: false };
    if (axis === 'xz') return { horizontal: 0, vertical: 2, verticalLabel: 'Z', is2D: false };
    return { horizontal: 0, vertical: 1, verticalLabel: 'Y', is2D: false };
  }
  const map = view.axisMap ?? {};
  const asIndex = (value: unknown): number | null =>
    typeof value === 'number' && Number.isInteger(value) && value >= 0 && value < 3 ? value : null;
  const horizontal = asIndex(map.x) ?? 0;
  const preferred = [asIndex(map.h), asIndex(map.z), 1, 2]
    .filter((value): value is number => value !== null);
  const candidates = [...new Set(preferred)].filter(value => value !== horizontal);
  const spanOf = (index: number): number => {
    let low = Infinity, high = -Infinity;
    for (const point of view.points) {
      const value = Number(point[index] ?? 0);
      if (value < low) low = value;
      if (value > high) high = value;
    }
    return Number.isFinite(low) ? high - low : 0;
  };
  const vertical = candidates.find(index => spanOf(index) > 1e-12)
    ?? candidates[0]
    ?? (horizontal === 1 ? 2 : 1);
  return { horizontal, vertical, verticalLabel: '高度', is2D: true };
}

function structurePoint(point: number[], axes: StructureAxes): [number, number] {
  return [Number(point[axes.horizontal] ?? 0), Number(point[axes.vertical] ?? 0)];
}

type StructureExtent = { minU: number; maxU: number; minV: number; maxV: number; flatV: boolean };

/** Raw data extent of the drawn axes (no padding). */
function structureExtent(view: CandidateView, axis: StructureAxis): StructureExtent {
  const axes = structureAxes(view, axis);
  let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
  for (const point of view.points) {
    const [u, v] = structurePoint(point, axes);
    minU = Math.min(minU, u); maxU = Math.max(maxU, u);
    minV = Math.min(minV, v); maxV = Math.max(maxV, v);
  }
  if (!Number.isFinite(minU) || !Number.isFinite(minV)) {
    const bounds = view.bounds ?? [];
    const pair = axes.horizontal === 0 && axes.vertical === 1
      ? [bounds[0], bounds[1], bounds[2], bounds[3]]
      : axes.horizontal === 0 && axes.vertical === 2
        ? [bounds[0], bounds[1], bounds[4], bounds[5]]
        : [bounds[2], bounds[3], bounds[4], bounds[5]];
    [minU, maxU, minV, maxV] = pair.map(value => Number(value) || 0) as [number, number, number, number];
  }
  if (maxU - minU < 1e-9) { minU -= 0.5; maxU += 0.5; }
  return { minU, maxU, minV, maxV, flatV: maxV - minV < 1e-9 };
}

function structureFrame(view: CandidateView, axis: StructureAxis): StructureFrame {
  const extent = structureExtent(view, axis);
  let { minU, maxU, minV, maxV } = extent;
  if (extent.flatV) {
    // A no-op / zero-rate step must still read as a substrate with thickness
    // instead of a single hairline at y = 0.
    const thickness = Math.max(maxU - minU, 1e-6) * 0.35;
    minV -= thickness;
    maxV += thickness * 0.15;
  }
  const paddingU = Math.max((maxU - minU) * 0.12, 1e-9);
  const paddingV = Math.max((maxV - minV) * 0.12, 1e-9);
  return {
    minU: minU - paddingU, maxU: maxU + paddingU,
    minV: minV - paddingV, maxV: maxV + paddingV,
    unit: structureUnit(view),
    flatV: extent.flatV,
  };
}

function mergeFrames(frames: StructureFrame[], zoom: number): StructureFrame {
  if (!frames.length) return { minU: 0, maxU: 1, minV: 0, maxV: 1, unit: 'µm', flatV: false };
  const minU = Math.min(...frames.map(frame => frame.minU));
  const maxU = Math.max(...frames.map(frame => frame.maxU));
  const minV = Math.min(...frames.map(frame => frame.minV));
  const maxV = Math.max(...frames.map(frame => frame.maxV));
  const factor = Math.max(0.25, zoom);
  const width = (maxU - minU) / factor;
  const height = (maxV - minV) / factor;
  return {
    minU: (minU + maxU) / 2 - width / 2, maxU: (minU + maxU) / 2 + width / 2,
    minV: (minV + maxV) / 2 - height / 2, maxV: (minV + maxV) / 2 + height / 2,
    unit: frames[0].unit,
    flatV: frames.every(frame => frame.flatV),
  };
}

function frameViewBox(frame: StructureFrame, margin: { left: number; right: number; top: number; bottom: number }): string {
  const width = (frame.maxU - frame.minU) + margin.left + margin.right;
  const height = (frame.maxV - frame.minV) + margin.top + margin.bottom;
  return `${frame.minU - margin.left} ${-frame.maxV - margin.top} ${width} ${height}`;
}

function axisTicks(min: number, max: number): number[] {
  const span = max - min;
  if (!Number.isFinite(span) || span <= 0) return [];
  const raw = span / 5;
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalized = raw / power;
  const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = factor * power;
  const values: number[] = [];
  for (let value = Math.ceil(min / step) * step; value <= max + step * 1e-6; value += step) {
    values.push(Number(value.toFixed(9)));
  }
  return values;
}

function formatTick(value: number): string {
  if (!Number.isFinite(value)) return '—';
  if (value === 0) return '0';
  return String(Number(value.toPrecision(3)));
}

/**
 * Split the per-material line set into polylines. A 2D cross section is a set
 * of open interface polylines (plus the occasional closed contour), so walking
 * shared endpoints is enough to recover the drawable chains.
 */
function structureChains(edges: Array<[number, number]>): number[][] {
  const used = new Array<boolean>(edges.length).fill(false);
  const adjacency = new Map<number, number[]>();
  edges.forEach(([a, b], index) => {
    adjacency.set(a, [...(adjacency.get(a) ?? []), index]);
    adjacency.set(b, [...(adjacency.get(b) ?? []), index]);
  });
  const other = (edgeIndex: number, node: number): number => (edges[edgeIndex][0] === node ? edges[edgeIndex][1] : edges[edgeIndex][0]);
  const chains: number[][] = [];
  for (let start = 0; start < edges.length; start++) {
    if (used[start]) continue;
    used[start] = true;
    const chain = [edges[start][0], edges[start][1]];
    for (let grew = true; grew;) {
      grew = false;
      const tail = chain[chain.length - 1];
      for (const edgeIndex of adjacency.get(tail) ?? []) {
        if (used[edgeIndex]) continue;
        used[edgeIndex] = true;
        chain.push(other(edgeIndex, tail));
        grew = true;
        break;
      }
    }
    for (let grew = true; grew;) {
      grew = false;
      const head = chain[0];
      for (const edgeIndex of adjacency.get(head) ?? []) {
        if (used[edgeIndex]) continue;
        used[edgeIndex] = true;
        chain.unshift(other(edgeIndex, head));
        grew = true;
        break;
      }
    }
    chains.push(chain);
  }
  return chains;
}

type StructureFill = { materialId: number; path: string };

/**
 * Fill the material below every interface line down to the bottom of the view.
 *
 * A material can own more than one chain: a conformal film that pinches off
 * leaves a closed loop around a void next to the open top surface. Filling each
 * chain as its own polygon paints that void as material (and overlaps the top
 * fill). One compound path per material with ``fill-rule: evenodd`` keeps the
 * closed loop as a hole instead. Overlaps between different materials still
 * stack their translucency; the outlines are drawn afterwards and stay crisp.
 */
function structureFillPolygons(view: CandidateView, points: Array<[number, number]>, fillBottom: number): StructureFill[] {
  if (view.dimension !== 2 || !view.lines.length) return [];
  const byMaterial = new Map<number, Array<[number, number]>>();
  view.lines.forEach((line, index) => {
    if (!line || line.length < 2) return;
    const a = Number(line[0]);
    const b = Number(line[1]);
    if (!Number.isFinite(a) || !Number.isFinite(b) || a === b) return;
    if (!points[a] || !points[b]) return;
    const materialId = Number(view.lineMaterialIds[index] ?? -1);
    const edges = byMaterial.get(materialId) ?? [];
    edges.push([a, b]);
    byMaterial.set(materialId, edges);
  });
  const fills: StructureFill[] = [];
  for (const [materialId, edges] of byMaterial) {
    const subpaths: string[] = [];
    for (const chain of structureChains(edges)) {
      const closed = chain.length > 2 && chain[0] === chain[chain.length - 1];
      const coords = chain.map(index => points[index]);
      if (!closed) {
        const xs = coords.map(([x]) => x);
        if (Math.max(...xs) - Math.min(...xs) < 1e-12) continue;
        coords.push([coords[coords.length - 1][0], fillBottom], [coords[0][0], fillBottom]);
      }
      subpaths.push(`${coords.map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x},${-y}`).join(' ')} Z`);
    }
    if (subpaths.length) fills.push({ materialId, path: subpaths.join(' ') });
  }
  return fills;
}

/** Rendered pixels per world unit for the 2D structure SVG.
 *
 * The SVG is stretched to its container, so a font size expressed in world
 * coordinates grows with the container. Text, ticks and strokes therefore have
 * to be solved from the *rendered* size: pick a font that measures `targetPx`
 * on screen, then let the margins follow the font. Since
 * `scale = min(width / (data + margins))` and the margins are proportional to
 * `1 / scale`, a few fixed-point iterations converge quickly.
 */
function structureCanvasScale(
  width: number, height: number, dataWidth: number, dataHeight: number,
  targetPx: number, marginX: number, marginY: number,
): number {
  if (!(width > 0) || !(height > 0) || !(dataWidth > 0) || !(dataHeight > 0)) return 0;
  let scale = Math.min(width / dataWidth, height / dataHeight);
  for (let step = 0; step < 6; step += 1) {
    if (!(scale > 0) || !Number.isFinite(scale)) return 0;
    const font = targetPx / scale;
    const next = Math.min(width / (dataWidth + marginX * font), height / (dataHeight + marginY * font));
    if (!(next > 0) || !Number.isFinite(next)) return 0;
    if (Math.abs(next - scale) <= scale * 1e-6) return next;
    scale = next;
  }
  return scale;
}

// 10.5px keeps the measured text box (font ascent + descent) at ~14px while
// still reading as the intended 11px label size.
const STRUCTURE_FONT_PX = 10.5;
const STRUCTURE_COMPACT_FONT_PX = 8;
const STRUCTURE_MIN_ZOOM = 0.5;
const STRUCTURE_MAX_ZOOM = 20;

function StructureCanvas({ view, frame, axis = 'xy', marker = null, note = null, label, className = 'wf-structure-canvas', showAxes = true, compact = false, interactive = true }: {
  view: CandidateView;
  frame: StructureFrame;
  axis?: StructureAxis;
  marker?: StructureMarker;
  /** Free-form annotation drawn in the top-left corner (surface height / depth). */
  note?: string | null;
  label: string;
  className?: string;
  showAxes?: boolean;
  /** Candidate cards in 05: smaller labels, no second set of zoom controls. */
  compact?: boolean;
  /** 03 only: wheel zoom, drag pan, double-click reset and compact buttons. */
  interactive?: boolean;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const clipId = `wf-structure-clip-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const [rendered, setRendered] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const viewRef = useRef({ zoom: 1, pan: { x: 0, y: 0 } });
  const dragRef = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);
  const rafRef = useRef<number | null>(null);
  const pendingRef = useRef<{ zoom: number; pan: { x: number; y: number } } | null>(null);

  // Measure the stretched SVG so the font is derived from real pixels instead
  // of the world span (a wide container must not produce 40px labels).
  useEffect(() => {
    const node = svgRef.current;
    if (!node) return;
    const measure = () => {
      const width = node.clientWidth || node.getBoundingClientRect().width;
      const height = node.clientHeight || node.getBoundingClientRect().height;
      setRendered(previous => (Math.abs(previous.width - width) < 0.5 && Math.abs(previous.height - height) < 0.5)
        ? previous
        : { width, height });
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const applyView = useCallback((next: { zoom: number; pan: { x: number; y: number } }) => {
    viewRef.current = next;
    setZoom(next.zoom);
    setPan(next.pan);
  }, []);

  const resetView = useCallback(() => {
    if (pendingRef.current !== null) pendingRef.current = null;
    if (rafRef.current !== null) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
    if (viewRef.current.zoom === 1 && viewRef.current.pan.x === 0 && viewRef.current.pan.y === 0) return;
    applyView({ zoom: 1, pan: { x: 0, y: 0 } });
  }, [applyView]);

  // A different step, snapshot or view axis must not inherit the old zoom.
  useEffect(() => { resetView(); }, [view, axis, resetView]);

  // Wheel/drag updates are coalesced into one state commit per animation frame.
  const queueView = useCallback((next: { zoom: number; pan: { x: number; y: number } }) => {
    pendingRef.current = next;
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const pending = pendingRef.current;
      pendingRef.current = null;
      if (pending) applyView(pending);
    });
  }, [applyView]);

  useEffect(() => () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
  }, []);

  useEffect(() => {
    const node = svgRef.current;
    if (!node || !interactive) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const delta = event.deltaMode === 1
        ? event.deltaY * 16
        : event.deltaMode === 2 ? event.deltaY * 400 : event.deltaY;
      if (!delta) return;
      const current = pendingRef.current ?? viewRef.current;
      const nextZoom = Math.min(STRUCTURE_MAX_ZOOM, Math.max(STRUCTURE_MIN_ZOOM, current.zoom * Math.exp(-delta * 0.0015)));
      if (Math.abs(nextZoom - current.zoom) < 1e-9) return;
      queueView({ zoom: nextZoom, pan: current.pan });
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, [interactive, queueView]);

  const zoomBy = (factor: number) => {
    const current = viewRef.current;
    queueView({
      zoom: Math.min(STRUCTURE_MAX_ZOOM, Math.max(STRUCTURE_MIN_ZOOM, current.zoom * factor)),
      pan: current.pan,
    });
  };

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!interactive || (event.pointerType === 'mouse' && event.button !== 0)) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      x: event.clientX, y: event.clientY,
      panX: viewRef.current.pan.x, panY: viewRef.current.pan.y,
    };
    setDragging(true);
  };
  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag || !interactive) return;
    const current = pendingRef.current ?? viewRef.current;
    queueView({
      zoom: current.zoom,
      pan: { x: drag.panX + (event.clientX - drag.x), y: drag.panY + (event.clientY - drag.y) },
    });
  };
  const endDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const axes = structureAxes(view, axis);
  const span = Math.max(frame.maxU - frame.minU, frame.maxV - frame.minV);
  const dataWidth = Math.max(frame.maxU - frame.minU, 1e-9);
  const dataHeight = Math.max(frame.maxV - frame.minV, 1e-9);
  const targetPx = compact ? STRUCTURE_COMPACT_FONT_PX : STRUCTURE_FONT_PX;
  // Total margin factors (left + right / top + bottom) in font-size units.
  const marginXFactor = showAxes ? 5.2 + 0.6 : 0.6;
  const marginYFactor = showAxes ? 1.9 + 3.9 : 0.6;
  const baseScale = structureCanvasScale(
    rendered.width, rendered.height, dataWidth, dataHeight, targetPx, marginXFactor, marginYFactor,
  );
  // Zoom shrinks the world extent; the font shrinks with it so the rendered
  // text keeps its screen size. A zero-width container falls back to a sane
  // world-space size instead of the old span/32 giant labels.
  const fontSize = baseScale > 0 ? targetPx / (baseScale * zoom) : span / 90;
  // 1 CSS pixel expressed in world units: strokes stay ~1–1.5px on screen.
  const unitPx = baseScale > 0 ? 1 / (baseScale * zoom) : span / 900;
  const strokeWidth = unitPx;
  const tickHalo = unitPx * 1.8;
  const colorOf = (materialId: number | undefined): string => {
    const entry = materialId === undefined ? undefined : view.materials?.[String(materialId)];
    return isHexColor(entry?.color ?? '') ? String(entry?.color) : '#9aa3ab';
  };
  // Zoom keeps the plot center fixed; the pan offset is expressed in screen
  // pixels and converted to world units with the *current* rendered scale.
  const centerU = (frame.minU + frame.maxU) / 2;
  const centerV = (frame.minV + frame.maxV) / 2;
  const currentScale = baseScale > 0 ? baseScale * zoom : 0;
  const panU = currentScale > 0 ? pan.x / currentScale : 0;
  const panV = currentScale > 0 ? pan.y / currentScale : 0;
  const viewFrame: StructureFrame = {
    minU: centerU - dataWidth / zoom / 2 - panU,
    maxU: centerU + dataWidth / zoom / 2 - panU,
    minV: centerV - dataHeight / zoom / 2 + panV,
    maxV: centerV + dataHeight / zoom / 2 + panV,
    unit: frame.unit,
    flatV: frame.flatV,
  };
  const points = view.points.map(point => structurePoint(point, axes));
  const fills = structureFillPolygons(view, points, viewFrame.minV);
  const uTicks = showAxes ? axisTicks(viewFrame.minU, viewFrame.maxU) : [];
  const vTicks = showAxes ? axisTicks(viewFrame.minV, viewFrame.maxV) : [];
  // Plot area in SVG coordinates (y grows downwards). The fills close on
  // `viewFrame.minV`, which is in data coordinates because they are mapped with
  // `-y`, so a panned/zoomed viewport still fills to its own bottom.
  const plotLeft = viewFrame.minU;
  const plotRight = viewFrame.maxU;
  const plotTop = -viewFrame.maxV;
  const plotBottom = -viewFrame.minV;
  // Margins live outside the plot so the axis titles, the tick labels, the
  // scale bar and the depth/surface note never sit on the structure or on each
  // other (N7).
  const marginLeft = showAxes ? fontSize * 5.2 : fontSize * 0.3;
  const marginRight = fontSize * 0.6;
  const marginTop = showAxes ? fontSize * 1.9 : fontSize * 0.3;
  const marginBottom = showAxes ? fontSize * 3.9 : fontSize * 0.3;
  const barLength = (() => {
    const ticks = uTicks.length > 1 ? uTicks : [viewFrame.minU, viewFrame.maxU];
    return Math.abs(ticks[1] - ticks[0]) || span / 4;
  })();
  const barRight = plotRight;
  const barX = barRight - barLength;
  const barY = plotBottom + fontSize * 2.3;
  const barLabelY = plotBottom + fontSize * 1.95;
  const xTitleY = plotBottom + fontSize * 3.35;
  const vTitleX = plotLeft - fontSize * 4.3;
  const vTitleY = (plotTop + plotBottom) / 2;
  const axisTitle = `${axes.verticalLabel} (${viewFrame.unit})`;
  const horizontalTitle = axes.is2D ? `X (${viewFrame.unit})` : `${axis.toUpperCase().charAt(0)} (${viewFrame.unit})`;
  // The origin is labelled once: a vertical `0` on top of the horizontal one
  // reads as `00`.
  const uHasOrigin = uTicks.some(tick => Math.abs(tick) < 1e-9);
  const markerOnRight = marker ? marker.value > (plotLeft + plotRight) / 2 : false;
  const markerNearTop = marker ? -marker.value < plotTop + (plotBottom - plotTop) * 0.25 : false;
  return <div className={`wf-structure-canvas-shell${interactive ? ' is-interactive' : ''}`}>
    <svg ref={svgRef}
      viewBox={frameViewBox(viewFrame, { left: marginLeft, right: marginRight, top: marginTop, bottom: marginBottom })}
      className={`${className}${dragging ? ' is-dragging' : ''}`} role="img" aria-label={label}
      data-dimension={view.dimension} data-flat={viewFrame.flatV ? '1' : '0'}
      onPointerDown={interactive ? onPointerDown : undefined}
      onPointerMove={interactive ? onPointerMove : undefined}
      onPointerUp={interactive ? endDrag : undefined}
      onPointerCancel={interactive ? endDrag : undefined}
      onDoubleClick={interactive ? resetView : undefined}>
    <defs>
      {/* Zoom/pan must not let the material fill bleed under the axis labels:
          geometry is clipped to the plot area, the axes stay outside the clip. */}
      <clipPath id={clipId}>
        <rect x={plotLeft} y={plotTop} width={plotRight - plotLeft} height={plotBottom - plotTop} />
      </clipPath>
    </defs>
    <rect x={plotLeft} y={plotTop} width={plotRight - plotLeft} height={plotBottom - plotTop} fill="#f8fafb" />
    {showAxes && uTicks.map(tick => <line key={`ug-${tick}`} className="wf-axes wf-axes-grid"
      x1={tick} y1={plotBottom} x2={tick} y2={plotTop} stroke="#e6eaee" strokeWidth={strokeWidth} />)}
    {showAxes && vTicks.map(tick => <line key={`vg-${tick}`} className="wf-axes wf-axes-grid"
      x1={plotLeft} y1={-tick} x2={plotRight} y2={-tick} stroke="#e6eaee" strokeWidth={strokeWidth} />)}
    {fills.map((fill, index) => (
      <path key={`f-${index}`} className="wf-fill" data-material={fill.materialId}
        clipPath={`url(#${clipId})`}
        d={fill.path} fillRule="evenodd"
        fill={colorOf(fill.materialId)} fillOpacity={0.3} stroke="none" />
    ))}
    {view.dimension === 2
      ? (view.lines.length
        ? view.lines.map((line, index) => {
          const a = points[line[0]];
          const b = points[line[1]];
          if (!a || !b) return null;
          return <line key={`l-${index}`} className="wf-interface"
            clipPath={`url(#${clipId})`}
            x1={a[0]} y1={-a[1]} x2={b[0]} y2={-b[1]}
            stroke={colorOf(view.lineMaterialIds[index])} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />;
        })
        : points.map((point, index) => <circle key={`p-${index}`} cx={point[0]} cy={-point[1]}
          clipPath={`url(#${clipId})`}
          r={strokeWidth * 2} fill={colorOf(view.materialIds[index])} />))
      : (view.triangles.length
        ? view.triangles.map((triangle, index) => {
          const a = points[triangle[0]];
          const b = points[triangle[1]];
          const c = points[triangle[2]];
          if (!a || !b || !c) return null;
          return <polygon key={`t-${index}`}
            clipPath={`url(#${clipId})`}
            points={`${a[0]},${-a[1]} ${b[0]},${-b[1]} ${c[0]},${-c[1]}`}
            fill={colorOf(view.triangleMaterialIds[index])} fillOpacity="0.9" stroke="none" />;
        })
        : points.map((point, index) => <circle key={`p-${index}`} cx={point[0]} cy={-point[1]}
          clipPath={`url(#${clipId})`}
          r={strokeWidth * 2} fill={colorOf(view.materialIds[index])} />))}
    {marker && <g className="wf-marker" clipPath={`url(#${clipId})`}>
      {marker.orientation === 'vertical'
        ? <line x1={marker.value} y1={plotTop} x2={marker.value} y2={plotBottom} strokeWidth={strokeWidth * 2} />
        : <line x1={plotLeft} y1={-marker.value} x2={plotRight} y2={-marker.value} strokeWidth={strokeWidth * 2} />}
      <text x={marker.orientation === 'vertical'
          ? marker.value + (markerOnRight ? -fontSize * 0.3 : fontSize * 0.3)
          : plotLeft + fontSize * 0.4}
        y={marker.orientation === 'vertical'
          ? plotTop + fontSize * 1.15
          : -marker.value + (markerNearTop ? fontSize * 1.3 : -fontSize * 0.45)}
        textAnchor={marker.orientation === 'vertical' && markerOnRight ? 'end' : 'start'}
        fontSize={fontSize} stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{marker.label}</text>
    </g>}
    {showAxes && <g className="wf-axes wf-axis-line">
      <line x1={plotLeft} y1={plotBottom} x2={plotRight} y2={plotBottom} stroke="#6b7a8a" strokeWidth={strokeWidth} />
      <line x1={plotLeft} y1={plotBottom} x2={plotLeft} y2={plotTop} stroke="#6b7a8a" strokeWidth={strokeWidth} />
      {uTicks.map(tick => <g key={`u-${tick}`} className="wf-axes wf-tick">
        <line x1={tick} y1={plotBottom} x2={tick} y2={plotBottom + fontSize * 0.4} stroke="#6b7a8a" strokeWidth={strokeWidth} />
        <text className="wf-tick-label wf-tick-label--u" x={tick} y={plotBottom + fontSize * 0.95} fontSize={fontSize} textAnchor="middle"
          fill="#26313a" stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{formatTick(tick)}</text>
      </g>)}
      {vTicks.map(tick => <g key={`v-${tick}`} className="wf-axes wf-tick">
        <line x1={plotLeft} y1={-tick} x2={plotLeft - fontSize * 0.4} y2={-tick} stroke="#6b7a8a" strokeWidth={strokeWidth} />
        {!(uHasOrigin && Math.abs(tick) < 1e-9) && <text className="wf-tick-label wf-tick-label--v" x={plotLeft - fontSize * 0.55}
          y={-tick + fontSize * 0.35} fontSize={fontSize} textAnchor="end"
          fill="#26313a" stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{formatTick(tick)}</text>}
      </g>)}
      <text className="wf-axis-title wf-axis-title--x" x={(plotLeft + plotRight) / 2} y={xTitleY} fontSize={fontSize} textAnchor="middle"
        stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{horizontalTitle}</text>
      <text className="wf-axis-title wf-axis-title--v" x={vTitleX} y={vTitleY} fontSize={fontSize} textAnchor="middle"
        transform={`rotate(-90 ${vTitleX} ${vTitleY})`}
        stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{axisTitle}</text>
      <line className="wf-scale-bar" x1={barX} y1={barY} x2={barRight} y2={barY} stroke="#34404b" strokeWidth={strokeWidth * 2.5} />
      <text className="wf-scale-label" x={barX + barLength / 2} y={barLabelY} fontSize={fontSize} textAnchor="middle"
        stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{`${formatTick(barLength)} ${frame.unit}`}</text>
      {note && <text className="wf-structure-note" x={plotLeft} y={plotTop - fontSize * 0.55} fontSize={fontSize} textAnchor="start"
        stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{note}</text>}
    </g>}
    </svg>
    {interactive && <div className="wf-structure-zoom" role="group" aria-label="结构视图缩放">
      <button type="button" aria-label="放大" title="放大" onClick={() => zoomBy(1.25)}>＋</button>
      <button type="button" aria-label="缩小" title="缩小" onClick={() => zoomBy(0.8)}>−</button>
      <button type="button" aria-label="重置视图" title="重置视图" onClick={resetView}>重置</button>
    </div>}
  </div>;
}

function structureLegend(view: CandidateView): Array<{ id: string; name: string; color: string; count: number }> {
  const counts = new Map<string, number>();
  const add = (id: number | undefined) => {
    const key = String(id ?? -1);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  };
  if (view.dimension === 2) {
    if (view.lines.length) view.lineMaterialIds.forEach(add);
    else view.materialIds.forEach(add);
  } else if (view.triangles.length) {
    view.triangleMaterialIds.forEach(add);
  } else {
    view.materialIds.forEach(add);
  }
  return [...counts.entries()]
    .filter(([id]) => id !== '-1')
    .map(([id, count]) => {
      const entry = view.materials?.[id];
      const name = entry?.name ?? view.materialNames?.[id] ?? `材料 ${id}`;
      const color = isHexColor(entry?.color ?? '') ? String(entry.color) : '#9aa3ab';
      return { id, name, color, count };
    });
}

function materialColor(materials: MaterialRow[], name: string): string {
  const row = materials.find(item => item.name === name);
  return isHexColor(row?.color ?? '') ? String(row?.color) : '#9aa3ab';
}

export default function WorkflowApp({ onOpenLegacy }: { onOpenLegacy?: () => void }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [stoppingRun, setStoppingRun] = useState(false);
  const [runNotice, setRunNotice] = useState('');
  const [gds, setGds] = useState<GdsAsset | null>(null);
  const [project, setProject] = useState<ProjectDoc | null>(null);
  const [projectGeometrySource, setProjectGeometrySource] = useState<ProjectGeometrySource>({ kind: 'parametric' });
  const [compile, setCompile] = useState<CompileInfo | null>(null);
  const [route, setRoute] = useState<RoutePlan | null>(null);
  const [segment, setSegment] = useState<RoutePlan['segments'][number] | null>(null);
  const [measurement, setMeasurement] = useState<Measurement | null>(null);
  const [templates, setTemplates] = useState<TemplateSummary[]>([]);
  const [templateId, setTemplateId] = useState('');
  const [libraryName, setLibraryName] = useState('团队共享流程');
  const [libraryDescription, setLibraryDescription] = useState('');
  const [libraryPolicy, setLibraryPolicy] = useState<'reject' | 'clone'>('reject');
  const [libraryBundles, setLibraryBundles] = useState<LibraryBundleSummary[]>([]);
  const [libraryBundleId, setLibraryBundleId] = useState('');
  const [libraryBundleJson, setLibraryBundleJson] = useState('');
  const [libraryResult, setLibraryResult] = useState<LibraryImportResult | null>(null);
  const [libraryStatus, setLibraryStatus] = useState('');
  const [recipeMethod, setRecipeMethod] = useState<'cd_at_height' | 'etch_depth'>('etch_depth');
  const [recipeMaterial, setRecipeMaterial] = useState('Si');
  const [recipeHeight, setRecipeHeight] = useState('0');
  const [recipeX, setRecipeX] = useState('0');
  const [recipeRef, setRecipeRef] = useState('0');
  const [metrics, setMetrics] = useState<Measurement[]>([]);
  const [branchDuration, setBranchDuration] = useState('0.02');
  const [activeRecipeId, setActiveRecipeId] = useState('');
  const [activeRecipeVersion, setActiveRecipeVersion] = useState(1);
  /** Parameters of the recipe the current measurement used (not the form draft). */
  const [activeRecipeSummary, setActiveRecipeSummary] = useState<{
    method: 'cd_at_height' | 'etch_depth';
    material: string;
    x: number | null;
    height: number | null;
    referenceHeight: number | null;
  } | null>(null);
  const [decisions, setDecisions] = useState<MeasurementDecision[]>([]);
  const [decisionPrefixId, setDecisionPrefixId] = useState('');
  const [decisionThroughId, setDecisionThroughId] = useState('');
  const [decisionOperator, setDecisionOperator] = useState<'lt' | 'lte' | 'gt' | 'gte' | 'eq'>('gt');
  const [decisionThreshold, setDecisionThreshold] = useState('0');
  const [decisionTrueParameters, setDecisionTrueParameters] = useState('{}');
  const [decisionFalseParameters, setDecisionFalseParameters] = useState('{}');
  const [metricDetail, setMetricDetail] = useState<{ measurement: Measurement; recipe: Recipe; provenance: Provenance } | null>(null);
  const [experimentDoc, setExperimentDoc] = useState<ExperimentDoc | null>(null);
  const [experimentTable, setExperimentTable] = useState<ExperimentTable | null>(null);
  /** Measurements referenced by experiment replicates: the reason for an
   * ambiguous metric is stored on the measurement, not on the replicate. */
  const [replicateMeasurements, setReplicateMeasurements] = useState<Record<string, Measurement>>({});
  const [layouts, setLayouts] = useState<Array<{ layoutId: string; fileName: string; structureCount: number }>>([]);
  const [activeLayout, setActiveLayout] = useState<LayoutDetail | null>(null);
  const [layoutStructure, setLayoutStructure] = useState('');
  const [selectedPairs, setSelectedPairs] = useState<string[]>([]);
  const [clipBounds, setClipBounds] = useState({ x0: 0, y0: 0, x1: 2, y1: 2 });
  const [clipName, setClipName] = useState('clip');
  const [canvas, setCanvas] = useState<ClipCanvas | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [activeClip, setActiveClip] = useState<Clip | null>(null);
  const [annotationText, setAnnotationText] = useState('');
  const [annotationX, setAnnotationX] = useState('0');
  const [annotationY, setAnnotationY] = useState('0');
  const [doeFactors, setDoeFactors] = useState<DoeFactorRow[]>([
    { stepId: '', parameter: 'rateMagnitude', values: '0.01,0.02,0.03' },
    { stepId: '', parameter: 'duration', values: '0.02,0.04,0.06' },
  ]);
  const [doeReplicates, setDoeReplicates] = useState(3);
  const [doeSeed, setDoeSeed] = useState('20261029');
  const [doeCost, setDoeCost] = useState('0');
  const [tableMin, setTableMin] = useState('');
  const [tableMax, setTableMax] = useState('');
  const [tableSort, setTableSort] = useState('ordinal');
  const [retentionInfo, setRetentionInfo] = useState<{ releasable: string[]; protected: Array<{ runId: string; reasons: string[] }> } | null>(null);
  const [compareCandidates, setCompareCandidates] = useState<Array<{ runId: string; stepId: string; label: string }>>([]);
  const [comparison, setComparison] = useState<CompareResult | null>(null);
  const [candidateViews, setCandidateViews] = useState<Record<string, CandidateView>>({});
  const [candidateViewErrors, setCandidateViewErrors] = useState<Record<string, string>>({});
  const [compareCoverage, setCompareCoverage] = useState<{ compared: string[]; total: number; prefix: boolean } | null>(null);
  /**
   * Compare-table selection. `null` means "not chosen yet" (the first four
   * candidates are checked); `[]` means the user unchecked everything.
   */
  const [comparePick, setComparePick] = useState<string[] | null>(null);
  const [syncZoom, setSyncZoom] = useState(1);
  const [syncAxis, setSyncAxis] = useState<'xy' | 'xz' | 'yz'>('xy');
  const [windowLower, setWindowLower] = useState('');
  const [windowUpper, setWindowUpper] = useState('');
  const [windowError, setWindowError] = useState('');
  const [windowResult, setWindowResult] = useState<ProcessWindowResult | null>(null);
  const windowLowerRef = useRef<HTMLInputElement>(null);
  const windowUpperRef = useRef<HTMLInputElement>(null);
  const [reportTitle, setReportTitle] = useState('工作流报告');
  const [reportConclusions, setReportConclusions] = useState('');
  const [reportDocument, setReportDocument] = useState<ReportDocument | null>(null);
  const reportFrameRef = useRef<HTMLIFrameElement>(null);
  const [calibrationCsv, setCalibrationCsv] = useState('');
  const [calibrationFileName, setCalibrationFileName] = useState('');
  const calibrationInput = useRef<HTMLInputElement>(null);
  const [calibrationName, setCalibrationName] = useState('calibration');
  const [calibrationModel, setCalibrationModel] = useState('scale_offset');
  const [calibrationMethod, setCalibrationMethod] = useState<'least-squares' | 'nelder-mead'>('least-squares');
  const [calibrationDataset, setCalibrationDataset] = useState<CalibrationDataset | null>(null);
  const [calibrationFit, setCalibrationFit] = useState<CalibrationFit | null>(null);
  const [previousCompile, setPreviousCompile] = useState<CompileInfo | null>(null);
  const [compileDiff, setCompileDiff] = useState<{ addedStepIds: string[]; removedStepIds: string[]; movedStepIds: string[]; changedStepIds: string[]; affectedStepIds: string[]; details?: Array<{ stepId: string; before: Record<string, unknown>; after: Record<string, unknown> }> } | null>(null);
  const [templateVariable, setTemplateVariable] = useState('0.02');
  const [experimentRows, setExperimentRows] = useState<Array<{ duration: string; replicates: number; seed: string }>>([
    { duration: '0.01', replicates: 2, seed: '20261029' },
    { duration: '0.02', replicates: 2, seed: '20261030' },
    { duration: '0.03', replicates: 2, seed: '20261031' },
  ]);
  const [experimentName, setExperimentName] = useState('');
  const [experimentStepId, setExperimentStepId] = useState('');
  const [experimentMaxAttempts, setExperimentMaxAttempts] = useState(2);
  const [experimentCost, setExperimentCost] = useState('0');
  const [experimentList, setExperimentList] = useState<ExperimentSummaryRow[]>([]);
  const [experimentDeleteArmed, setExperimentDeleteArmed] = useState('');
  const [showAllCandidates, setShowAllCandidates] = useState(false);
  const [experimentInputsOpen, setExperimentInputsOpen] = useState(true);
  const [autoAnalysisId, setAutoAnalysisId] = useState('');
  const autoAnalysisRef = useRef('');
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [moduleJson, setModuleJson] = useState(DEFAULT_MODULE_JSON);
  const [definitionJson, setDefinitionJson] = useState(DEFAULT_DEFINITION_JSON);
  const [definitionVariables, setDefinitionVariables] = useState('{}');
  const [savedDefinitionId, setSavedDefinitionId] = useState('');
  const [moduleStatus, setModuleStatus] = useState('');
  const [expansion, setExpansion] = useState<ModuleExpansion | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [zone, setZone] = useState<'setup' | 'flow' | 'run' | 'experiment' | 'analyze' | 'report'>('setup');
  const [error, setError] = useState('');
  const [activityLog, setActivityLog] = useState<string[]>([]);
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [compileStale, setCompileStale] = useState(true);
  const [libraryItems, setLibraryItems] = useState<LibraryItem[]>([]);
  const [libraryManagerOpen, setLibraryManagerOpen] = useState(false);
  const [managerCategory, setManagerCategory] = useState<LibraryCategory>('material');
  const [managerItems, setManagerItems] = useState<LibraryItem[]>([]);
  const [managerSelectedId, setManagerSelectedId] = useState('');
  const [managerForm, setManagerForm] = useState<Record<string, string>>({});
  const [managerStatus, setManagerStatus] = useState('');
  const [managerDeleteArmed, setManagerDeleteArmed] = useState(false);
  const [managerLoading, setManagerLoading] = useState(false);
  const [managerError, setManagerError] = useState('');
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryError, setLibraryError] = useState('');
  const managerRequest = useRef(0);
  const [materialCatalog, setMaterialCatalog] = useState<MaterialRow[]>([]);
  const materialCategories = useMemo(
    () => Array.from(new Set(materialCatalog.map(row => row.category ?? 'Generic'))).sort(),
    [materialCatalog],
  );
  const [materialPreset, setMaterialPreset] = useState('');
  const [flowDefinitions, setFlowDefinitions] = useState<FlowDefinitionSummary[]>([]);
  const [recipes, setRecipes] = useState<RecipeSummary[]>([]);
  const [outputTab, setOutputTab] = useState<'output' | 'problems' | 'provenance'>('output');
  const [flowAdvancedOpen, setFlowAdvancedOpen] = useState(false);
  const [libraryAdvancedOpen, setLibraryAdvancedOpen] = useState(false);
  const [setupMode, setSetupMode] = useState<'home' | 'new' | 'examples' | 'project' | 'layout'>('home');
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [projectId, setProjectId] = useState('');
  const [savedProjectJson, setSavedProjectJson] = useState('');
  const [projectSavedAt, setProjectSavedAt] = useState('');
  const [projectNotice, setProjectNotice] = useState('');
  const [overwriteArmed, setOverwriteArmed] = useState<{ projectId: string; name: string } | null>(null);
  const [closeArmed, setCloseArmed] = useState(false);
  const [newProjectArmed, setNewProjectArmed] = useState(false);
  const [deleteArmedId, setDeleteArmedId] = useState('');
  const [selectedFlowStepId, setSelectedFlowStepId] = useState<string | null>(null);
  // 02 step inspector: the specialized model allowlist is fetched once; the
  // JSON drafts stay local until their 应用 button validates them.
  const [specializedModels, setSpecializedModels] = useState<SpecializedModelSpec[]>([]);
  const [nativeParametersText, setNativeParametersText] = useState('');
  const [nativeParametersError, setNativeParametersError] = useState('');
  const [rateSetsText, setRateSetsText] = useState('');
  const [rateSetsError, setRateSetsError] = useState('');
  // Isotropic material table and the directional material-rate pair are JSON
  // drafts too: typing stays local until the matching 应用 button parses it.
  const [isotropicMaterialRatesText, setIsotropicMaterialRatesText] = useState('');
  const [isotropicMaterialRatesError, setIsotropicMaterialRatesError] = useState('');
  const [directionalMaterialRatesText, setDirectionalMaterialRatesText] = useState('');
  const [directionalMaterialRatesError, setDirectionalMaterialRatesError] = useState('');
  const [defaultMaterialRateText, setDefaultMaterialRateText] = useState('');
  const [defaultMaterialRateError, setDefaultMaterialRateError] = useState('');
  const [structurePick, setStructurePick] = useState('');
  const [runProgress, setRunProgress] = useState<Record<string, { currentStepId: string; completed: string[] }>>({});
  // Free-form workspace sizing, persisted per browser. The splitter columns are
  // real grid tracks, so the left tree and the 3D viewport resize live.
  const [leftWidth, setLeftWidth] = useState(() => readStoredSize('wb.ui.leftWidth', 224, 180, 520));
  const [previewHeight, setPreviewHeight] = useState(() => readStoredSize('wb.ui.previewHeight', 420, 240, 760));
  const leftDrag = useRef<{ startX: number; width: number } | null>(null);
  const previewDrag = useRef<{ startY: number; height: number } | null>(null);
  const [structureView, setStructureView] = useState<CandidateView | null>(null);
  const [structureLoading, setStructureLoading] = useState(false);
  const [structureError, setStructureError] = useState('');
  const [measurementMark, setMeasurementMark] = useState<StructureMarker>(null);
  /** A rebuilt Route or re-run Segment dropped a measurement that used to exist. */
  const [measurementInvalidated, setMeasurementInvalidated] = useState(false);
  const [runSnapshots, setRunSnapshots] = useState<Record<string, string[]>>({});
  const [rebuildRouteArmed, setRebuildRouteArmed] = useState(false);
  const [replaceTemplateArmed, setReplaceTemplateArmed] = useState(false);
  const [materialTarget, setMaterialTarget] = useState<{ itemName: string; material: string; target: string; stepName?: string } | null>(null);
  const [layerMaterials, setLayerMaterials] = useState<Record<string, string>>({});
  // Per GDS layer z placement for 3D clips; kept as strings so partial input
  // ("0.") is not silently collapsed to 0 while typing.
  const [layerHeights, setLayerHeights] = useState<Record<string, { baseHeight: string; height: string }>>({});
  const [clipNotice, setClipNotice] = useState('');
  const [draft, setDraft] = useState({
    name: 'New Project', dimension: 2 as 2 | 3, geometry: 'plane' as 'plane' | 'trench' | 'hole' | 'fin',
    source: 'parametric' as 'parametric' | 'layout', unit: 'um' as LengthUnit,
    gridDelta: 0.2, xExtent: 4, yExtent: 4,
    width: 1, depth: 2, radius: 1, finHeight: 1.2, height: 0,
    taperAngle: 0, maskHeight: 0,
    substrateMaterial: 'Si', maskMaterial: 'Mask',
    engine: 'cpu', seed: 7, timeout: SOLVER_TIMEOUT_DEFAULT, raysPerPoint: 16, cpuThreads: 1,
  });
  const [solverDefaultSeconds, setSolverDefaultSeconds] = useState(SOLVER_TIMEOUT_DEFAULT);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsData, setSettingsData] = useState<SettingsPayload | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft>(EMPTY_SETTINGS_DRAFT);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState<'save' | 'reset' | null>(null);
  const [settingsError, setSettingsError] = useState('');
  const [settingsNotice, setSettingsNotice] = useState('');
  const settingsLoadSeq = useRef(0);
  const [projectState, setProjectState] = useState<ProjectWorkflowState>(emptyWorkflowState);
  const [stateNotice, setStateNotice] = useState('');
  const [restoreNotice, setRestoreNotice] = useState('');
  const [stateSaving, setStateSaving] = useState(false);
  const gdsInput = useRef<HTMLInputElement>(null);
  const projectInput = useRef<HTMLInputElement>(null);
  const parametricGeometryRef = useRef<Record<string, unknown> | null>(null);
  const stateSaveTimer = useRef<number | null>(null);
  const lastStateJson = useRef('');
  const restorePending = useRef(false);
  // Incremented by an explicit Compile/Route action. A restore that started on
  // page load must not overwrite that newer user action when it resolves late
  // (the restored Route/Compile would be stale and disable 03).
  const restoreEpoch = useRef(0);

  const projectDirty = !!project && JSON.stringify(project) !== savedProjectJson;

  // A reopened GDS project has no freshly uploaded asset, but its geometry still
  // names the GDS file. The 01 file row must show that instead of "未选择 GDS".
  const boundGdsFileName = (() => {
    if (projectGeometrySource.kind !== 'layout') return '';
    const geometry = (project?.geometry ?? {}) as Record<string, unknown>;
    return typeof geometry.fileName === 'string' ? geometry.fileName : '';
  })();
  // Clip assets are stored as `<clipId>.gds`; the readable name lives on the
  // registered Layout, so prefer it when the project is bound to one.
  const boundLayoutId = projectGeometrySource.kind === 'layout' ? projectGeometrySource.layoutId : undefined;
  const boundLayoutFileName = activeLayout?.fileName
    ?? layouts.find(item => item.layoutId === boundLayoutId)?.fileName
    ?? '';
  const gdsDisplayName = gds?.fileName ?? (boundLayoutFileName || boundGdsFileName);
  const gdsFromRegisteredLayout = !gds && !!(boundGdsFileName || boundLayoutFileName);

  const geometrySaveBlock = (() => {
    if (!project || projectGeometrySource.kind !== 'layout' || projectGeometrySource.clipId) return '';
    const geometry = (project.geometry ?? {}) as Record<string, unknown>;
    if (geometry.kind === 'gds') return '';
    return '先选择或保存一个 Clip 作为工程几何';
  })();

  // Field-level validation shared by the 01 editor and every save entry point
  // (top bar, Ctrl/Cmd+S, File menu). Values match backend ProjectV7 bounds.
  const projectFormErrors = useMemo(() => {
    const errors: Record<string, string> = {};
    if (!project) return errors;
    const domain = (project.domain ?? {}) as Record<string, unknown>;
    const geometry = (project.geometry ?? {}) as Record<string, unknown>;
    const solver = (project.solver ?? {}) as Record<string, unknown>;
    const gridDelta = Number(domain.gridDelta);
    const xExtent = Number(domain.xExtent);
    const is3D = project.dimension === 3;
    const unit = (project.units as { length?: string } | undefined)?.length === 'nm' ? 'nm' as LengthUnit : 'um' as LengthUnit;
    const unitLabel = lengthUnitLabel(unit);
    const perUm = lengthUnitsPerUm(unit);
    // Same floors as backend Domain (2D) / Domain3D (3D), expressed in the
    // project's declared native unit.
    const minExtent = (is3D ? MIN_DOMAIN_EXTENT_3D : MIN_DOMAIN_EXTENT_2D) * perUm;
    const maxExtent = MAX_DOMAIN_EXTENT * perUm;
    const maxGridDelta = 0.5 * perUm;
    if (!Number.isFinite(gridDelta) || gridDelta <= 0 || gridDelta > maxGridDelta) errors['domain.gridDelta'] = `gridDelta 必须 > 0 且 ≤ ${maxGridDelta} ${unitLabel}`;
    if (!Number.isFinite(xExtent) || xExtent < minExtent || xExtent > maxExtent) errors['domain.xExtent'] = `xExtent 必须在 ${minExtent}–${maxExtent} ${unitLabel}`;
    if (is3D) {
      const yExtent = Number(domain.yExtent);
      if (!Number.isFinite(yExtent) || yExtent < minExtent || yExtent > maxExtent) errors['domain.yExtent'] = `yExtent 必须在 ${minExtent}–${maxExtent} ${unitLabel}`;
      if (Number.isFinite(gridDelta) && gridDelta > 0) {
        const minimum = 2 * gridDelta;
        if (xExtent < minimum) errors['domain.xExtent'] = `3D xExtent 至少为 2×gridDelta（${minimum} ${unitLabel}）`;
        if (yExtent < minimum) errors['domain.yExtent'] = `3D yExtent 至少为 2×gridDelta（${minimum} ${unitLabel}）`;
      }
    }
    const kind = String(geometry.kind ?? '');
    const positiveFields: Array<[string, string]> = kind === 'trench'
      ? [['width', '宽度'], ['depth', '深度']]
      : kind === 'hole' ? [['radius', '半径'], ['depth', '深度']]
        : kind === 'fin' ? [['width', '宽度'], ['height', '高度']] : [];
    for (const [key, label] of positiveFields) {
      const value = Number(geometry[key]);
      if (!Number.isFinite(value) || value <= 0) errors[`geometry.${key}`] = `${label}必须 > 0 ${unitLabel}`;
    }
    if (['trench', 'hole', 'fin'].includes(kind)) {
      const taper = Number(geometry.taperAngle ?? 0);
      if (!Number.isFinite(taper) || taper < -45 || taper > 45) errors['geometry.taperAngle'] = 'taperAngle 必须在 -45–45°';
      const maskHeight = Number(geometry.maskHeight ?? 0);
      if (!Number.isFinite(maskHeight) || maskHeight < 0) errors['geometry.maskHeight'] = `maskHeight 必须 ≥ 0 ${unitLabel}`;
    }
    if (kind === 'fin' && Number.isFinite(gridDelta) && gridDelta > 0) {
      const minimum = 2 * gridDelta;
      if (Number(geometry.width) < minimum) errors['geometry.width'] = `Fin 宽度至少为 2×gridDelta（${minimum} ${unitLabel}）`;
      if (Number(geometry.height) < minimum) errors['geometry.height'] = `Fin 高度至少为 2×gridDelta（${minimum} ${unitLabel}）`;
    }
    const rays = Number(solver.raysPerPoint);
    if (!Number.isInteger(rays) || rays < 16 || rays > 4096) errors['solver.raysPerPoint'] = 'raysPerPoint 必须是 16–4096 的整数';
    const threads = Number(solver.cpuThreads);
    if (!Number.isInteger(threads) || threads < 1) errors['solver.cpuThreads'] = 'cpuThreads 必须是 ≥ 1 的整数';
    const timeout = Number(solver.timeoutSeconds ?? 0);
    if (!Number.isInteger(timeout) || timeout < 0 || timeout > SOLVER_TIMEOUT_MAX) errors['solver.timeoutSeconds'] = `timeoutSeconds 必须是 0–${SOLVER_TIMEOUT_MAX} 的整数`;
    const seed = Number(((solver.random ?? {}) as Record<string, unknown>).seed ?? 0);
    if (!Number.isInteger(seed) || seed < 0 || seed > 4294967295) errors['solver.seed'] = 'seed 必须是 0–4294967295 的整数';
    if (geometrySaveBlock) errors['geometry.source'] = geometrySaveBlock;
    return errors;
  }, [project, geometrySaveBlock]);

  const refreshProjects = async (): Promise<ProjectRecord[]> => {
    try {
      const data = await api<{ projects: ProjectRecord[] }>('/api/v1/workflow/projects');
      setProjects(data.projects);
      return data.projects;
    } catch {
      setProjects([]);
      return [];
    }
  };

  /**
   * Everything below is bound to one project. Creating/opening/closing a project
   * must drop all of it, otherwise 04/05/06 keep showing the previous project's
   * experiment, comparison and report.
   */
  const resetProjectScopedState = () => {
    setCompile(null);
    setRoute(null);
    setSegment(null);
    setMeasurement(null);
    setProvenance(null);
    setMetrics([]);
    setMeasurementMark(null);
    setMeasurementInvalidated(false);
    setMetricDetail(null);
    setDecisions([]);
    setActiveRecipeId('');
    setActiveRecipeVersion(1);
    setActiveRecipeSummary(null);
    setCompileStale(true);
    setPreviousCompile(null);
    setCompileDiff(null);
    setRunSnapshots({});
    setStructurePick('');
    setStructureView(null);
    setStructureError('');
    setStructureLoading(false);
    setSelectedFlowStepId(null);
    setExperimentDoc(null);
    setExperimentTable(null);
    setExperimentList([]);
    setExperimentDeleteArmed('');
    setShowAllCandidates(false);
    setReplicateMeasurements({});
    setAutoAnalysisId('');
    autoAnalysisRef.current = '';
    setComparison(null);
    setCompareCandidates([]);
    setCandidateViews({});
    setCandidateViewErrors({});
    setCompareCoverage(null);
    setComparePick(null);
    setSyncZoom(1);
    setSyncAxis('xy');
    setWindowLower('');
    setWindowUpper('');
    setWindowError('');
    setWindowResult(null);
    setTableMin('');
    setTableMax('');
    setTableSort('ordinal');
    setRetentionInfo(null);
    setReportDocument(null);
    setReportConclusions('');
    setCalibrationCsv('');
    setCalibrationDataset(null);
    setCalibrationFit(null);
    setClipNotice('');
    setClipName('clip');
    setSelectedPairs([]);
    setLayoutStructure('');
    setLayerMaterials({});
    setActiveClip(null);
    setActiveLayout(null);
    setCanvas(null);
    setClips([]);
  };

  const applyLoadedProject = (document: ProjectDoc, record?: ProjectRecord) => {
    setProject(document);
    setProjectId(record?.projectId ?? '');
    restorePending.current = !!record?.projectId;
    setSavedProjectJson(record ? JSON.stringify(document) : '');
    setProjectSavedAt(record?.updatedAt ?? '');
    setProjectNotice(record
      ? `已打开 · ${record.name} · ${formatDateTime(record.updatedAt)}`
      : '草稿（未保存）');
    // A freshly uploaded GDS belongs to the previous session, not this project.
    setGds(null);
    const geometry = (document.geometry ?? {}) as Record<string, unknown>;
    if (geometry.kind !== 'gds') parametricGeometryRef.current = geometry;
    setProjectGeometrySource(geometry.kind === 'gds'
      ? { kind: 'layout', clipName: typeof geometry.fileName === 'string' ? geometry.fileName : 'GDS Clip' }
      : { kind: 'parametric' });
    resetProjectScopedState();
    setProjectState(emptyWorkflowState());
    lastStateJson.current = '';
    setStateNotice('');
    setRestoreNotice('');
    setSetupMode('project');
  };

  /**
   * Recover the saved 01 geometry binding. The project only stores the clip's
   * generated GDS asset, so the clip record is looked up by asset/file name;
   * a deleted clip degrades to a named-but-unbound source instead of flipping
   * the page back to "parametric".
   */
  const resolveClipBinding = async (document: ProjectDoc): Promise<ProjectGeometrySource | null> => {
    const geometry = (document.geometry ?? {}) as Record<string, unknown>;
    if (geometry.kind !== 'gds') return null;
    const assetId = typeof geometry.assetId === 'string' ? geometry.assetId : '';
    const fileName = typeof geometry.fileName === 'string' ? geometry.fileName : '';
    try {
      const data = await api<{ clips: Clip[] }>('/api/v1/workflow/clips');
      const candidates = data.clips.filter(item => item.gdsAssetId === assetId
        || item.gdsFileName === fileName
        || item.clipId === fileName.replace(/\.gds$/i, ''));
      // Two clips can share one generated GDS asset (same polygons/layer
      // materials). Prefer the one whose z placement matches the saved
      // geometry so a 3D project reopens with its own layer heights.
      const geometryLayers = Array.isArray(geometry.layers)
        ? geometry.layers as Array<Record<string, unknown>>
        : [];
      const matchesGeometry = (clip: Clip) => geometryLayers.length === clip.layers.length
        && clip.layers.every((item, index) => {
          const row = geometryLayers[index];
          return item.layer === Number(row?.layer)
            && (item.baseHeight ?? null) === (row?.baseHeight ?? null)
            && (item.height ?? null) === (row?.height ?? null);
        });
      const clip = candidates.find(matchesGeometry) ?? candidates[0];
      if (clip) {
        return { kind: 'layout', clipId: clip.clipId, clipName: clip.name, layoutId: clip.layoutId, clipHash: clip.clipHash };
      }
    } catch { /* the clip list is best effort */ }
    // The clip may have been deleted but the registered Layout is still there:
    // keep the filename and let the GDS tab open on that Layout.
    try {
      const data = await api<{ layouts: Array<{ layoutId: string; fileName: string }> }>('/api/v1/workflow/layouts');
      const layout = data.layouts.find(item => item.fileName === fileName);
      if (layout) return { kind: 'layout', clipName: fileName, layoutId: layout.layoutId };
    } catch { /* the layout list is best effort */ }
    return { kind: 'layout', clipName: fileName || 'GDS Clip' };
  };

  /**
   * Load the project's 03 pointer document. Every reference is restored on its
   * own: one 404 clears only that pointer and adds a closable notice, so a
   * collected compile never blanks the whole page.
   */
  const restoreWorkflowState = async (id: string, document?: ProjectDoc) => {
    const epoch = restoreEpoch.current;
    const superseded = () => epoch !== restoreEpoch.current;
    // A Compile/Route action owns restorePending until it records the new
    // state, so an aborted restore must not unblock the debounced state save
    // with a half-restored projectState.
    const abandon = (): boolean => superseded();
    let stored: ProjectWorkflowState;
    try {
      stored = await api<ProjectWorkflowState>(`/api/v1/workflow/projects/${id}/state`);
    } catch {
      restorePending.current = false;
      return;
    }
    if (abandon()) return;
    const notices: string[] = [];
    const next: ProjectWorkflowState = {
      ...emptyWorkflowState(),
      compileId: stored.compileId ?? null,
      // Only a non-empty string is a basis; older states stored no field at all.
      compileBasis: typeof stored.compileBasis === 'string' && stored.compileBasis
        ? stored.compileBasis
        : null,
      routeId: stored.routeId ?? null,
      segmentRuns: stored.segmentRuns ?? [],
      measurementIds: stored.measurementIds ?? [],
      experimentIds: stored.experimentIds ?? [],
    };
    // The Route is restored first: a branch Route runs a derived Compile, and
    // that Compile — not the one recorded in the state — is what 03 executes.
    let restoredRoute: RoutePlan | null = null;
    if (next.routeId) {
      try {
        const data = await api<{ plan: RoutePlan }>(`/api/v1/workflow/routes/${next.routeId}`);
        if (abandon()) return;
        restoredRoute = data.plan;
        setRoute(data.plan);
        // Resume at the planned Segment of a branch attempt (a branch Route
        // leaves its new tail planned). Non-branch Routes keep the previous
        // behaviour: show the Segment the saved state points at.
        const headAttemptId = data.plan.route.headAttemptId;
        const branchPlanned = data.plan.attempts.length > 1
          ? data.plan.segments.find(item => item.status === 'planned' && item.attemptId === headAttemptId) ?? null
          : null;
        const executed = data.plan.segments.filter(item => item.runId && !item.runId.startsWith('pending_'));
        const current = branchPlanned
          ?? [...executed].reverse().find(item => next.segmentRuns.some(row => row.segmentId === item.segmentId))
          ?? executed[executed.length - 1]
          ?? data.plan.segments[data.plan.segments.length - 1]
          ?? null;
        setSegment(current);
      } catch (exc) {
        if ((exc as { status?: number }).status === 404) {
          next.routeId = null;
          next.segmentRuns = [];
          notices.push('上次 Route 已不可用，已清空 Route 状态。');
        } else {
          notices.push(`Route 恢复失败：${exc instanceof Error ? exc.message : String(exc)}`);
        }
      }
    }
    if (next.compileId) {
      try {
        const envelope = await api<CompiledEnvelope>(`/api/v1/workflow/compiles/${next.compileId}`);
        const currentDocument = document ?? project;
        const headAttempt = restoredRoute
          ? restoredRoute.attempts.find(attempt => attempt.attemptId === restoredRoute?.route.headAttemptId)
            ?? restoredRoute.attempts[restoredRoute.attempts.length - 1]
            ?? null
          : null;
        // The base attempt owns the Segment that starts at the first step; the
        // attempts array order is not stable, so do not rely on index 0.
        const baseSegment = restoredRoute
          ? restoredRoute.segments.find(item => item.startAfterStepId === null) ?? restoredRoute.segments[0] ?? null
          : null;
        const baseAttempt = restoredRoute
          ? restoredRoute.attempts.find(attempt => attempt.attemptId === baseSegment?.attemptId)
            ?? restoredRoute.attempts[0]
            ?? null
          : null;
        // A branch attempt keeps the base Route but compiles a derived project
        // (e.g. the tail step 0.01 → 0.04). The head attempt's Compile is the
        // one that will run, so restore that one to keep the branch executable.
        // Whether it is stale is decided by the compile basis alone.
        const branchCompileId = headAttempt && baseAttempt
          && headAttempt.compileId !== baseAttempt.compileId
          ? headAttempt.compileId
          : '';
        let info = compileInfoFromEnvelope(envelope);
        if (branchCompileId) {
          if (branchCompileId !== next.compileId) {
            const branchEnvelope = await api<CompiledEnvelope>(`/api/v1/workflow/compiles/${branchCompileId}`);
            if (abandon()) return;
            info = compileInfoFromEnvelope(branchEnvelope);
          }
          next.compileId = branchCompileId;
        }
        if (abandon()) return;
        setCompile(info);
        // The basis is the fingerprint of the authored project at compile
        // time, so it is the only input to staleness. A definition/branch
        // Compile expands or rewrites steps, so comparing its envelope with
        // project.steps is exactly the guesswork this field replaces.
        if (!next.compileBasis) {
          setCompileStale(true);
          notices.push('旧状态缺少编译基线，请重新编译。');
        } else {
          const fresh = !!currentDocument
            && next.compileBasis === executionCompileBasis(currentDocument);
          setCompileStale(!fresh);
          if (!fresh) notices.push('工程在编译后有修改，需重新编译。');
        }
      } catch (exc) {
        if ((exc as { status?: number }).status === 404) {
          next.compileId = null;
          next.compileBasis = null;
          notices.push('上次编译记录已不可用，已清空编译状态。');
        } else {
          notices.push(`编译记录恢复失败：${exc instanceof Error ? exc.message : String(exc)}`);
        }
      }
    }
    if (next.measurementIds.length) {
      const rows = await Promise.all(next.measurementIds.map(async measurementId => {
        try { return await api<Measurement>(`/api/v1/workflow/measurements/${measurementId}`); } catch { return null; }
      }));
      const found = rows.filter((row): row is Measurement => !!row);
      if (found.length !== next.measurementIds.length) notices.push('部分测量记录已不可用，已保留其余测量。');
      if (found.length) {
        setMetrics(found);
        const latest = found[found.length - 1];
        setMeasurement(latest);
        setMeasurementInvalidated(false);
        if (latest.recipeId) {
          setActiveRecipeId(latest.recipeId);
          setActiveRecipeVersion(latest.recipeVersion);
          try {
            const recipe = await api<Recipe>(`/api/v1/workflow/metrology-recipes/${latest.recipeId}?version=${latest.recipeVersion}`);
            const parameters = (recipe.parameters ?? {}) as Record<string, unknown>;
            const markValue = latest.method === 'cd_at_height' ? Number(parameters.height ?? 0) : Number(parameters.x ?? 0);
            // Restore the recipe form from the saved recipe, never from a
            // default 0: reopening a project must show where it measured.
            setRecipeMethod(latest.method === 'cd_at_height' ? 'cd_at_height' : 'etch_depth');
            if (typeof recipe.material === 'string' && recipe.material) setRecipeMaterial(recipe.material);
            if (latest.method === 'cd_at_height') {
              setRecipeHeight(String(Number(parameters.height ?? 0)));
            } else {
              setRecipeX(String(Number(parameters.x ?? 0)));
              setRecipeRef(String(Number(parameters.referenceHeight ?? 0)));
            }
            setActiveRecipeSummary({
              method: latest.method === 'cd_at_height' ? 'cd_at_height' : 'etch_depth',
              material: typeof recipe.material === 'string' ? recipe.material : latest.material,
              x: latest.method === 'etch_depth' ? Number(parameters.x ?? 0) : null,
              height: latest.method === 'cd_at_height' ? Number(parameters.height ?? 0) : null,
              referenceHeight: latest.method === 'etch_depth' ? Number(parameters.referenceHeight ?? 0) : null,
            });
            setMeasurementMark(latest.method === 'cd_at_height'
              ? { orientation: 'horizontal', value: markValue, label: `测量 CD 高度 ${markValue} µm` }
              : { orientation: 'vertical', value: markValue, label: `测量 X ${markValue} µm` });
          } catch { /* the marker is optional */ }
        }
      } else {
        next.measurementIds = [];
      }
    }
    if (abandon()) return;
    setProjectState(next);
    lastStateJson.current = notices.length ? '' : JSON.stringify(workflowStatePayload(next));
    restorePending.current = false;
    setRestoreNotice(notices.join(' '));
    if (notices.length) {
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  工程状态部分恢复：${notices.join(' ')}`]);
    }
  };

  /** Restore the newest stored 06 report for a project (06 survives reloads). */
  const restoreLatestReport = async (id: string) => {
    try {
      const latest = await api<ReportDocument | null>(`/api/v1/workflow/projects/${id}/reports/latest`);
      if (latest) {
        setReportDocument(latest);
        setReportTitle(latest.title);
        // The report HTML carries the conclusions; the API only returns the
        // document, so read the section back for the editable form.
        try {
          const parsed = new DOMParser().parseFromString(latest.html, 'text/html');
          const heading = [...parsed.querySelectorAll('h2')].find(node => node.textContent?.trim() === '结论');
          const text = heading?.nextElementSibling?.textContent?.trim() ?? '';
          setReportConclusions(text && text !== '未填写' ? text : '');
        } catch {
          setReportConclusions('');
        }
      } else {
        setReportDocument(null);
      }
    } catch {
      setReportDocument(null);
    }
  };

  /** Replace the 03 pointer set after a compile/route/segment/measurement change. */
  /**
   * Point the state at a fresh Compile and record its basis: the fingerprint
   * of the authored project that was sent to the compiler, never of the
   * Compile's expanded/derived steps.
   */
  const recordCompile = (compileId: string, basis: string) => {
    setProjectState(previous => ({
      ...previous, compileId, compileBasis: basis, routeId: null, segmentRuns: [],
    }));
  };

  const recordRoute = (plan: RoutePlan, resetMeasurements = false) => {
    setProjectState(previous => {
      const known = new Map(previous.segmentRuns.map(row => [row.segmentId, row]));
      const segmentRuns = plan.segments
        .filter(item => item.runId && !item.runId.startsWith('pending_'))
        .map(item => ({
          segmentId: item.segmentId,
          runId: item.runId as string,
          measurementId: resetMeasurements ? null : (known.get(item.segmentId)?.measurementId ?? null),
        }));
      return {
        ...previous,
        routeId: plan.route.routeId,
        segmentRuns,
        measurementIds: resetMeasurements ? [] : previous.measurementIds,
      };
    });
  };

  const recordMeasurement = (measurementId: string, segmentId?: string | null) => {
    setProjectState(previous => ({
      ...previous,
      measurementIds: previous.measurementIds.includes(measurementId)
        ? previous.measurementIds
        : [...previous.measurementIds, measurementId],
      segmentRuns: segmentId
        ? previous.segmentRuns.map(row => row.segmentId === segmentId ? { ...row, measurementId } : row)
        : previous.segmentRuns,
    }));
  };

  const recordExperiment = (experimentId: string) => {
    setProjectState(previous => ({
      ...previous,
      experimentIds: previous.experimentIds.includes(experimentId)
        ? previous.experimentIds
        : [...previous.experimentIds, experimentId],
    }));
  };

  /** 300 ms debounce: every pointer change writes one PUT, not one per render. */
  useEffect(() => {
    if (!projectId) return;
    if (restorePending.current) return;
    const payload = workflowStatePayload(projectState);
    const serialized = JSON.stringify(payload);
    if (serialized === lastStateJson.current) return;
    if (stateSaveTimer.current) window.clearTimeout(stateSaveTimer.current);
    stateSaveTimer.current = window.setTimeout(() => {
      stateSaveTimer.current = null;
      void (async () => {
        setStateSaving(true);
        try {
          const saved = await api<ProjectWorkflowState>(`/api/v1/workflow/projects/${projectId}/state`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: serialized,
          });
          lastStateJson.current = serialized;
          setStateNotice('');
          setProjectState(previous => ({ ...previous, updatedAt: saved.updatedAt ?? null }));
        } catch (exc) {
          setStateNotice(`工作流状态未能保存：${exc instanceof Error ? exc.message : String(exc)}`);
        } finally {
          setStateSaving(false);
        }
      })();
    }, 300);
    return () => {
      if (stateSaveTimer.current) { window.clearTimeout(stateSaveTimer.current); stateSaveTimer.current = null; }
    };
  }, [projectId, projectState]);

  const applySavedRecord = async (record: ProjectRecord, document: ProjectDoc) => {
    setProjectId(record.projectId);
    setSavedProjectJson(JSON.stringify(document));
    setProjectSavedAt(record.updatedAt);
    setProjectNotice(`已保存 · ${record.name} · ${formatDateTime(record.updatedAt)}`);
    window.localStorage.setItem(PROJECT_STORAGE_KEY, record.projectId);
    await refreshProjects();
  };

  /**
   * A saved edit must invalidate the Compile immediately: otherwise 02 keeps
   * offering "去 03" and the old Compile silently runs the previous durations.
   * Saving never moves the basis — only a recompile does — so the stored basis
   * is compared with the server-normalized document that was just saved.
   */
  const refreshCompileStaleness = (document: ProjectDoc) => {
    if (!compile) return;
    const basis = projectState.compileBasis;
    setCompileStale(!basis || basis !== executionCompileBasis(document));
  };

  /**
   * The fingerprint must describe the document the server would store, not the
   * imported one: saving fills defaults the raw JSON omits (`maskMaterial`,
   * Fin `taperAngle`/`maskHeight`, `stack.baseHeight`, `step.rateMode`, solver
   * defaults …). The typed normalize endpoint returns exactly that canonical
   * form without persisting or compiling anything, so a Compile basis taken
   * from it still matches the project after an unchanged save.
   *
   * A failed normalize is never silent: the raw document is used (the basis
   * may then look stale after a save) and a closable banner says so.
   */
  const normalizeProjectForBasis = async (document: ProjectDoc): Promise<ProjectDoc> => {
    try {
      const data = await postJson<{ project?: ProjectDoc }>('/api/v1/workflow/projects/normalize', document);
      if (!data || !data.project) throw new Error('响应缺少 project');
      return data.project;
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setRestoreNotice(previous => [
        previous,
        `工程规范化失败，编译基线按原文档记录（保存后可能显示需重新编译）：${message}`,
      ].filter(Boolean).join(' '));
      return document;
    }
  };

  /** The document that actually goes to the server: geometry follows the 01 source tab. */
  const projectDocumentForSave = (): ProjectDoc => {
    if (!project) throw new Error('没有可保存的工程');
    if (projectGeometrySource.kind !== 'layout') return project;
    const clip = (activeClip && activeClip.clipId === projectGeometrySource.clipId ? activeClip : null)
      ?? clips.find(item => item.clipId === projectGeometrySource.clipId)
      ?? null;
    if (clip) return { ...project, geometry: clipGeometryBinding(clip, project.dimension ?? 2) };
    const geometry = (project.geometry ?? {}) as Record<string, unknown>;
    if (geometry.kind !== 'gds') throw new Error('先选择或保存一个 Clip 作为工程几何');
    return project;
  };

  const saveProject = () => run('project-save', async () => {
    if (!project) throw new Error('没有可保存的工程');
    const invalid = Object.values(projectFormErrors);
    if (invalid.length) throw new Error(`工程参数有误：${invalid[0]}`);
    const document = projectDocumentForSave();
    setOverwriteArmed(null);
    try {
      const record = projectId
        ? await projectRequest(`/api/v1/workflow/projects/${projectId}`, 'PUT', document)
        : await projectRequest('/api/v1/workflow/projects', 'POST', document);
      // The server normalizes the document (solver defaults, schema fields);
      // keep that copy so an unchanged save compares equal to its Compile.
      const saved = await api<ProjectDoc>(`/api/v1/workflow/projects/${record.projectId}`);
      setProject(saved);
      await applySavedRecord(record, saved);
      refreshCompileStaleness(saved);
      // A new projectId (or a just-created draft) still needs its state PUT.
      lastStateJson.current = '';
      setProjectState(previous => ({ ...previous }));
    } catch (exc) {
      if ((exc as { status?: number }).status === 409) {
        const listed = await refreshProjects();
        const existing = listed.find(row => row.name === (document.name ?? ''));
        setOverwriteArmed(existing ? { projectId: existing.projectId, name: existing.name } : null);
        setProjectNotice(`已存在同名工程「${document.name ?? ''}」且内容不同；确认覆盖会替换它的步骤，取消则保留当前编辑。`);
        return;
      }
      throw exc;
    }
  });

  const overwriteProject = () => run('project-save', async () => {
    if (!project || !overwriteArmed) return;
    const invalid = Object.values(projectFormErrors);
    if (invalid.length) throw new Error(`工程参数有误：${invalid[0]}`);
    const document = projectDocumentForSave();
    const record = await projectRequest(`/api/v1/workflow/projects/${overwriteArmed.projectId}`, 'PUT', document);
    setOverwriteArmed(null);
    const saved = await api<ProjectDoc>(`/api/v1/workflow/projects/${record.projectId}`);
    setProject(saved);
    await applySavedRecord(record, saved);
    refreshCompileStaleness(saved);
    lastStateJson.current = '';
    setProjectState(previous => ({ ...previous }));
  });

  const openProject = (record: ProjectRecord) => run('project-open', async () => {
    const document = await api<ProjectDoc>(`/api/v1/workflow/projects/${record.projectId}`);
    applyLoadedProject(document, record);
    await activateClipBinding(await resolveClipBinding(document));
    window.localStorage.setItem(PROJECT_STORAGE_KEY, record.projectId);
    await restoreWorkflowState(record.projectId, document);
    await restoreLatestReport(record.projectId);
  });

  const deleteProjectRecord = (record: ProjectRecord) => run('project-delete', async () => {
    await api(`/api/v1/workflow/projects/${record.projectId}`, { method: 'DELETE' });
    setDeleteArmedId('');
    if (projectId === record.projectId) {
      setProject(null);
      setProjectId('');
      setSavedProjectJson('');
      setProjectSavedAt('');
      setProjectNotice('');
      resetProjectScopedState();
      setProjectState(emptyWorkflowState());
      lastStateJson.current = '';
      setRestoreNotice('');
      setStateNotice('');
      window.localStorage.removeItem(PROJECT_STORAGE_KEY);
      setSetupMode('home');
    }
    await refreshProjects();
  });

  const exportProjectJson = () => {
    if (!project) return;
    const blob = new Blob([JSON.stringify(project, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${project.name ?? 'project'}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已导出工程 JSON: ${project.name ?? 'project'}`]);
  };

  const resetToStart = () => {
    setProject(null);
    setProjectId('');
    setSavedProjectJson('');
    setProjectSavedAt('');
    setProjectNotice('');
    setOverwriteArmed(null);
    setCloseArmed(false);
    setNewProjectArmed(false);
    window.localStorage.removeItem(PROJECT_STORAGE_KEY);
    resetProjectScopedState();
    setProjectGeometrySource({ kind: 'parametric' });
    setProjectState(emptyWorkflowState());
    lastStateJson.current = '';
    setStateNotice('');
    setRestoreNotice('');
    parametricGeometryRef.current = null;
    setSetupMode('home');
    void refreshProjects();
  };

  const closeProject = () => {
    if (projectDirty) { setCloseArmed(true); return; }
    resetToStart();
  };

  /** New-project solver timeout default, unrelated to the per-run resource limits. */
  const solverTimeoutFieldError = (): string => {
    const raw = (settingsDraft.solverTimeoutSeconds ?? '').trim();
    if (!/^\d+$/.test(raw)) return 'solver.timeoutSeconds 必须是整数';
    const value = Number(raw);
    if (!Number.isInteger(value) || value < SOLVER_TIMEOUT_MIN || value > SOLVER_TIMEOUT_MAX) {
      return `solver.timeoutSeconds 必须是 ${SOLVER_TIMEOUT_MIN}–${SOLVER_TIMEOUT_MAX} 的整数`;
    }
    return '';
  };

  /** Mirror the backend limit rules so the draft fails fast and visibly. */
  const settingsFieldError = (key: SettingsLimitKey): string => {
    const limit = settingsData?.limits[key];
    if (!limit) return '';
    const fallback = SETTINGS_LIMIT_FALLBACK[key];
    const label = limit.label || fallback.label;
    const unit = limit.unit || fallback.unit;
    const raw = (settingsDraft[key] ?? '').trim();
    if (!/^-?\d+$/.test(raw)) return `${label} 必须是整数`;
    const value = Number(raw);
    // minFreeGiB is a floor: the env value is the smallest value the backend
    // will accept, not a cap like the other four limits.
    const floor = key === 'minFreeGiB' && limit.envValue != null
      ? Math.max(limit.minimum, limit.envValue)
      : limit.minimum;
    if (!Number.isInteger(value) || value < floor) return `${label} 不能小于 ${floor} ${unit}`;
    if (limit.maximum != null && value > limit.maximum) return `${label} 不能大于 ${limit.maximum} ${unit}`;
    return '';
  };

  const loadSettings = async () => {
    const seq = settingsLoadSeq.current + 1;
    settingsLoadSeq.current = seq;
    setSettingsLoading(true);
    setSettingsError('');
    setSettingsNotice('');
    try {
      const payload = await settingsRequest();
      if (settingsLoadSeq.current !== seq) return;
      setSettingsData(payload);
      setSettingsDraft(settingsDraftFrom(payload));
    } catch (err) {
      if (settingsLoadSeq.current !== seq) return;
      setSettingsData(null);
      setSettingsDraft(EMPTY_SETTINGS_DRAFT);
      setSettingsError(err instanceof Error ? err.message : '读取设置失败');
    } finally {
      if (settingsLoadSeq.current === seq) setSettingsLoading(false);
    }
  };

  const saveSettings = async () => {
    if (!settingsData || settingsSaving) return;
    const invalid = [...SETTINGS_LIMIT_KEYS.map(key => settingsFieldError(key)), solverTimeoutFieldError()].find(Boolean);
    if (invalid) { setSettingsError(invalid); setSettingsNotice(''); return; }
    setSettingsSaving('save');
    setSettingsError('');
    setSettingsNotice('');
    try {
      const limits: Record<SettingsLimitKey, number> = {
        maxDataGiB: Number(settingsDraft.maxDataGiB),
        artifactLimitMiB: Number(settingsDraft.artifactLimitMiB),
        rssLimitMiB: Number(settingsDraft.rssLimitMiB),
        maxLogMiB: Number(settingsDraft.maxLogMiB),
        minFreeGiB: Number(settingsDraft.minFreeGiB),
      };
      const payload = await settingsRequest({
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          limits,
          defaults: { solverTimeoutSeconds: Number(settingsDraft.solverTimeoutSeconds) },
        }),
      });
      setSettingsData(payload);
      setSettingsDraft(settingsDraftFrom(payload));
      setSolverDefaultSeconds(payload.defaults?.solverTimeoutSeconds ?? SOLVER_TIMEOUT_DEFAULT);
      setSettingsNotice('已保存，对下一次 run 生效');
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : '保存设置失败');
    } finally {
      setSettingsSaving(null);
    }
  };

  const resetSettings = async () => {
    if (!settingsData || settingsSaving) return;
    if (!window.confirm('将资源上限恢复为环境变量或默认值，并将新工程超时恢复为 600 s？')) return;
    setSettingsSaving('reset');
    setSettingsError('');
    setSettingsNotice('');
    try {
      await settingsRequest({
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          limits: { maxDataGiB: null, artifactLimitMiB: null, rssLimitMiB: null, maxLogMiB: null, minFreeGiB: null },
          defaults: { solverTimeoutSeconds: SOLVER_TIMEOUT_DEFAULT },
        }),
      });
      const payload = await settingsRequest();
      setSettingsData(payload);
      setSettingsDraft(settingsDraftFrom(payload));
      setSolverDefaultSeconds(payload.defaults?.solverTimeoutSeconds ?? SOLVER_TIMEOUT_DEFAULT);
      setSettingsNotice('已恢复默认值，对下一次 run 生效');
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : '恢复默认设置失败');
    } finally {
      setSettingsSaving(null);
    }
  };

  // New-project defaults are fetched once, independently of the settings
  // dialog; an older backend without `defaults` must not block the UI.
  useEffect(() => {
    let cancelled = false;
    settingsRequest()
      .then(payload => {
        if (cancelled) return;
        const value = Number(payload.defaults?.solverTimeoutSeconds);
        if (Number.isInteger(value) && value >= SOLVER_TIMEOUT_MIN && value <= SOLVER_TIMEOUT_MAX) {
          setSolverDefaultSeconds(value);
        }
      })
      .catch(() => { /* keep the local 600 s fallback */ });
    return () => { cancelled = true; };
  }, []);

  // Every open starts from a fresh GET, so a closed dialog never keeps a stale
  // draft from the previous visit.
  useEffect(() => {
    if (!settingsOpen) return;
    setSettingsData(null);
    setSettingsDraft(EMPTY_SETTINGS_DRAFT);
    setSettingsError('');
    setSettingsNotice('');
    void loadSettings();
  }, [settingsOpen]);

  // Ctrl/Cmd+, is the standard preferences shortcut; it opens the same dialog
  // as 文件 → 设置… and does not toggle it closed.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key !== ',') return;
      event.preventDefault();
      setOpenMenu(null);
      setAboutOpen(false);
      setSettingsOpen(true);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (!openMenu && !aboutOpen && !settingsOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpenMenu(null);
      setAboutOpen(false);
      setSettingsOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [openMenu, aboutOpen, settingsOpen]);

  // Ctrl/Cmd+S saves the current project instead of opening the browser's
  // "save page" dialog; every zone shares the same save entry point.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
      event.preventDefault();
      if (!project) { navigateZone('setup'); return; }
      if (busy === 'project-save') return;
      void saveProject();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [project, projectId, busy, projectFormErrors, projectGeometrySource, activeClip, clips]);

  // Unsaved edits: let the browser ask before a refresh/close. No custom modal;
  // the native prompt is the only reliable guard against losing a draft.
  useEffect(() => {
    if (!projectDirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Legacy Chrome/Safari still need a truthy returnValue.
      event.returnValue = '有未保存的更改';
      return event.returnValue;
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [projectDirty]);

  useEffect(() => {
    const stored = window.localStorage.getItem(PROJECT_STORAGE_KEY);
    void (async () => {
      const listed = await refreshProjects();
      if (!stored) return;
      const record = listed.find(row => row.projectId === stored);
      try {
        const document = await api<ProjectDoc>(`/api/v1/workflow/projects/${stored}`);
        applyLoadedProject(document, record ?? {
          schemaVersion: 1, projectId: stored, name: document.name ?? '',
          dimension: document.dimension ?? 2, stepCount: document.steps?.length ?? 0,
          createdAt: '', updatedAt: '',
        });
        await activateClipBinding(await resolveClipBinding(document));
        window.localStorage.setItem(PROJECT_STORAGE_KEY, stored);
        await restoreWorkflowState(stored, document);
        await restoreLatestReport(stored);
      } catch {
        window.localStorage.removeItem(PROJECT_STORAGE_KEY);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The specialized model allowlist never changes while the app is open, so a
  // single fetch is enough. A failure leaves the select usable with the step's
  // current nativeModel instead of blocking the whole inspector.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await api<{ models?: unknown }>('/api/specialized-models');
        if (cancelled || !Array.isArray(payload.models)) return;
        const rows = payload.models.filter((row): row is SpecializedModelSpec => (
          !!row && typeof row === 'object' && typeof (row as { id?: unknown }).id === 'string'
        ));
        setSpecializedModels(rows);
      } catch {
        /* keep the inspector usable without catalog hints */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const loadLibraryItems = async () => {
    setLibraryLoading(true);
    setLibraryError('');
    try {
      const data = await api<{ items: LibraryItem[] }>('/api/v1/workflow/library/items');
      setLibraryItems(data.items);
    } catch (exc) {
      setLibraryItems([]);
      setLibraryError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLibraryLoading(false);
    }
  };

  useEffect(() => {
    void api<Health>('/api/health').then(setHealth).catch(() => setHealth(null));
    void api<{ templates: TemplateSummary[] }>('/api/v1/workflow/templates').then(data => setTemplates(data.templates)).catch(() => undefined);
    void api<{ bundles: LibraryBundleSummary[] }>('/api/v1/workflow/library/bundles')
      .then(data => setLibraryBundles(data.bundles)).catch(() => undefined);
    void loadLibraryItems();
    void api<{ materials: MaterialRow[] }>('/api/materials')
      .then(data => setMaterialCatalog(data.materials)).catch(() => undefined);
    void api<{ definitions: FlowDefinitionSummary[] }>('/api/v1/workflow/flows/definitions')
      .then(data => setFlowDefinitions(data.definitions)).catch(() => undefined);
    void api<{ recipes: RecipeSummary[] }>('/api/v1/workflow/metrology-recipes')
      .then(data => setRecipes(data.recipes)).catch(() => undefined);
  }, []);

  useEffect(() => {
    const steps = project?.steps ?? [];
    setSelectedFlowStepId(previous => steps.some(step => step.id === previous) ? previous : (steps[0]?.id ?? null));
  }, [project?.steps]);

  useEffect(() => {
    const steps = (project?.steps ?? []).filter(step => step.enabled);
    if (!steps.length) return;
    setDoeFactors(previous => {
      const used = new Set<string>();
      let changed = false;
      const next = previous.map((row, index) => {
        if (row.stepId && steps.some(step => step.id === row.stepId) && !used.has(row.stepId)) {
          used.add(row.stepId);
          return row;
        }
        // Rotate through the enabled steps so the default factors never collide
        // on `(stepId, parameter)`. A single-step project keeps every factor on
        // that step and relies on distinct parameters instead.
        const pick = steps.find(step => !used.has(step.id)) ?? steps[Math.min(index, steps.length - 1)];
        used.add(pick.id);
        changed = true;
        return { ...row, stepId: pick.id };
      });
      return changed ? next : previous;
    });
  }, [project]);

  useEffect(() => {
    const steps = (project?.steps ?? []).filter(step => step.enabled);
    setExperimentStepId(previous => steps.some(step => step.id === previous) ? previous : (steps[steps.length - 1]?.id ?? ''));
  }, [project?.steps]);

  useEffect(() => {
    // The default name is generated at creation time (project · 实验 · HH:MM)
    // so two experiments created in the same project never share a name.
    setExperimentName('');
  }, [project?.name]);

  useEffect(() => {
    void refreshExperimentList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Reload the whole Route, not just the Segment the user is looking at.
   * Starting a new Segment cancels the previous run server-side, and that old
   * Segment can only show its new terminal status after a full Route refresh.
   */
  const refreshRoutePlan = async (routeId: string) => {
    const data = await api<{ plan: RoutePlan }>(`/api/v1/workflow/routes/${routeId}`);
    setRoute(data.plan);
    setSegment(previous => previous
      ? data.plan.segments.find(item => item.segmentId === previous.segmentId) ?? previous
      : data.plan.segments[data.plan.segments.length - 1] ?? null);
    return data.plan;
  };

  // Any active Segment — even one the user has navigated away from — keeps the
  // banner and the status poll alive. `/api/health.activeRunId` also covers a
  // run whose Segment is not part of the Route currently shown.
  const anyActiveSegment = (route?.segments ?? [])
    .some(item => ['running', 'queued', 'cancelling'].includes(item.status));
  const activeRunId = health?.activeRunId ?? null;
  useEffect(() => {
    if (!anyActiveSegment && !activeRunId) return;
    const routeId = route?.route.routeId;
    let cancelled = false;
    const tick = async () => {
      try {
        const nextHealth = await api<Health>('/api/health');
        if (cancelled) return;
        setHealth(nextHealth);
        if (routeId) {
          const plan = await refreshRoutePlan(routeId);
          if (!cancelled) recordRoute(plan);
        }
      } catch {
        // A transient poll failure must not clear the running banner.
      }
    };
    void tick();
    const timer = window.setInterval(() => { void tick(); }, 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anyActiveSegment, activeRunId, route?.route.routeId]);

  useEffect(() => {
    const routeId = route?.route.routeId;
    if (!routeId) {
      setDecisions([]);
      return;
    }
    void api<{ decisions: MeasurementDecision[] }>(`/api/v1/workflow/routes/${routeId}/measurement-decisions`)
      .then(data => setDecisions(data.decisions))
      .catch(() => setDecisions([]));
  }, [route?.route.routeId]);

  const recipeMaterialKey = useRef('');
  useEffect(() => {
    const geometry = (project?.geometry ?? {}) as Record<string, unknown>;
    const substrate = typeof geometry.substrateMaterial === 'string' ? geometry.substrateMaterial : '';
    const key = `${project?.name ?? ''}|${substrate}`;
    if (!substrate || recipeMaterialKey.current === key) return;
    recipeMaterialKey.current = key;
    setRecipeMaterial(substrate);
  }, [project]);

  const branchablePrefixes = useMemo(() => {
    const last = route?.stepOrder[route.stepOrder.length - 1];
    return (route?.segments ?? []).filter(item => item.status === 'succeeded' && item.throughStepId !== last);
  }, [route]);

  useEffect(() => {
    const last = route?.stepOrder[route.stepOrder.length - 1];
    const eligible = (route?.segments ?? []).filter(
      item => item.status === 'succeeded' && item.throughStepId !== last,
    );
    setDecisionPrefixId(eligible[eligible.length - 1]?.throughStepId ?? '');
  }, [route]);

  const decisionSuffix = useMemo(() => {
    if (!route || !decisionPrefixId) return [];
    const index = route.stepOrder.indexOf(decisionPrefixId);
    return index < 0 ? [] : route.stepOrder.slice(index + 1);
  }, [route, decisionPrefixId]);

  useEffect(() => {
    setDecisionThroughId(previous => decisionSuffix.includes(previous)
      ? previous
      : (decisionSuffix[decisionSuffix.length - 1] ?? ''));
  }, [decisionSuffix]);

  const authoredByCompiled = useMemo(
    () => new Map((compile?.compile.sourceMap ?? []).map(entry => [entry.stepId, entry.authoredStepId])),
    [compile],
  );
  const compiledByAuthored = useMemo(
    () => new Map((compile?.compile.sourceMap ?? []).map(entry => [entry.authoredStepId, entry.stepId])),
    [compile],
  );

  useEffect(() => {
    const step = (compile?.compile.project.steps ?? []).find(item => item.id === decisionThroughId);
    const authored = authoredByCompiled.get(decisionThroughId);
    if (!step || !authored) return;
    const fields: Array<[string, number | undefined]> = [
      ['duration', step.duration],
      ['thickness', step.thickness],
      ['rateMagnitude', step.rateMagnitude],
    ];
    const field = fields.find(([, value]) => typeof value === 'number');
    const name = field?.[0];
    const value = field?.[1];
    if (!name || typeof value !== 'number') return;
    const presets: Record<string, [number, number]> = {
      duration: [0.03, 0.2],
      thickness: [0.02, 0.05],
      rateMagnitude: [0.05, 0.2],
    };
    const [trueValue, falseValue] = presets[name] ?? [
      Number((value * 0.5).toFixed(6)),
      Number((value * 2).toFixed(6)),
    ];
    setDecisionTrueParameters(previous => (!previous.trim() || previous.trim() === '{}')
      ? JSON.stringify({ [authored]: { [name]: trueValue } })
      : previous);
    setDecisionFalseParameters(previous => (!previous.trim() || previous.trim() === '{}')
      ? JSON.stringify({ [authored]: { [name]: falseValue } })
      : previous);
  }, [compile, decisionThroughId, authoredByCompiled]);

  const authoredSteps = useMemo(() => project?.steps ?? [], [project]);
  const selectedFlowStep = authoredSteps.find(step => step.id === selectedFlowStepId) ?? null;
  const canCompile = !!project && authoredSteps.length > 0 && !busy;
  /**
   * The Compile maps every authored step back through its SourceMap. When an
   * enabled step has no mapping the server dropped it; never offer "创建 Route"
   * in that state. Disabled steps are a normal skip and are reported separately.
   * Coverage only applies to Compiles built from the authored project: a
   * definition Compile expands modules into different step ids, so comparing
   * it with project.steps would report every step as missing.
   */
  const compileCoverageApplies = useMemo(() => {
    if (!compile) return false;
    const compiledAuthored = new Set((compile.authoredSteps ?? []).map(step => step.id));
    return authoredSteps.some(step => compiledAuthored.has(step.id));
  }, [compile, authoredSteps]);
  const compileMissingSteps = useMemo(
    () => (compile && !compileStale && compileCoverageApplies
      ? missingCompiledSteps(authoredSteps, (compile.compile.sourceMap ?? []).map(entry => entry.authoredStepId))
      : []),
    [compile, compileStale, compileCoverageApplies, authoredSteps],
  );
  const compileSkippedSteps = useMemo(
    () => (compile && !compileStale && compileCoverageApplies ? skippedCoverageSteps(authoredSteps) : []),
    [compile, compileStale, compileCoverageApplies, authoredSteps],
  );
  const compileMissingText = coverageStepNames(compileMissingSteps);
  const compileSkippedText = coverageStepNames(compileSkippedSteps);
  const processLibraryItems = useMemo(
    () => libraryItems.filter(item => item.category === 'process'),
    [libraryItems],
  );
  const depositMaterialOptions = useMemo(() => {
    const byName = new Map(materialCatalog.map(row => [row.name, row]));
    return depositableMaterialNames(materialCatalog)
      .map(name => byName.get(name))
      .filter((row): row is MaterialRow => !!row);
  }, [materialCatalog]);
  const selectedDepositMaterialInvalid = !!selectedFlowStep?.depositMaterial
    && !depositMaterialOptions.some(row => row.name === selectedFlowStep.depositMaterial);
  const selectedStepModel = String(selectedFlowStep?.model ?? '');
  const selectedStepOperation = String(selectedFlowStep?.operation ?? 'etch');
  const stepModelOptions = Array.from(new Set([selectedStepModel, ...EDITABLE_STEP_MODELS].filter(Boolean)));
  const stepSupportsOperation = OPERATION_STEP_MODELS.has(selectedStepModel);
  const stepSupportsDuration = DURATION_STEP_MODELS.has(selectedStepModel);
  const stepSupportsRate = RATE_STEP_MODELS.has(selectedStepModel) && selectedStepModel !== 'directional';
  const stepSupportsRateMode = RATE_MODE_STEP_MODELS.has(selectedStepModel);
  const stepSupportsDepositMaterial = selectedStepOperation === 'deposit'
    && DEPOSIT_STEP_MODELS.has(selectedStepModel);
  const stepShowsParticleFields = selectedStepModel === 'singleParticle';
  const selectedRateField = selectedStepModel === 'directional' ? 'directionalRateMagnitude' : 'rateMagnitude';
  const selectedRateValue = Number(
    selectedStepModel === 'directional'
      ? selectedFlowStep?.directionalRateMagnitude ?? 0
      : selectedFlowStep?.rateMagnitude ?? 0,
  );
  /**
   * Model-specific payloads the generic scalar editors cannot express. The
   * readers below only fall back to a default for display; a value is written
   * back only when the user edits the matching field.
   */
  const selectedGdsMask = (() => {
    const value = selectedFlowStep?.gdsMask;
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  })();
  const gdsMaskNumber = (key: string, fallback: number): number => {
    const value = selectedGdsMask[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
  };
  const gdsMaskPosition = (() => {
    const value = selectedGdsMask.position;
    return Array.isArray(value) && value.length === 3 && value.every(entry => typeof entry === 'number' && Number.isFinite(entry))
      ? value as [number, number, number]
      : [0, 0, 0] as [number, number, number];
  })();
  const gdsMaskRetainedRegion = selectedGdsMask.retainedRegion === 'outside' ? 'outside' : 'inside';
  const planarizeUsesStopMaterial = selectedStepModel === 'planarize'
    && typeof selectedFlowStep?.stopMaterial === 'string';
  const directionalUsesRateSets = selectedStepModel === 'directional'
    && Array.isArray(selectedFlowStep?.rateSets)
    && selectedFlowStep.rateSets.length > 0;
  const selectedStepRateMode = String(selectedFlowStep?.rateMode ?? 'uniform');
  // The material table is the third directional mode; it is mutually exclusive
  // with scalar rates and rateSets (the backend treats its presence as the
  // mode switch, so the UI reads it the same way).
  const directionalUsesMaterialRates = selectedStepModel === 'directional'
    && selectedStepRateMode !== 'material'
    && (selectedFlowStep?.materialRates != null || selectedFlowStep?.defaultMaterialRate != null);
  const selectedDirectionalMode = selectedStepModel !== 'directional'
    ? 'scalar'
    : directionalUsesMaterialRates ? 'materialRates'
      : directionalUsesRateSets ? 'rateSets' : 'scalar';
  const selectedIsotropicMethodValue = selectedFlowStep?.isotropicMethod;
  const selectedIsotropicMethod: IsotropicMethod = selectedIsotropicMethodValue === 'levelSet'
    || selectedIsotropicMethodValue === 'geometric'
    ? selectedIsotropicMethodValue
    : 'auto';
  const selectedDefaultMaterialRate = (() => {
    const row = selectedFlowStep?.defaultMaterialRate;
    const value = row && typeof row === 'object' && !Array.isArray(row)
      ? row as Record<string, unknown>
      : {};
    const numberOr = (entry: unknown, fallback: number) => (
      typeof entry === 'number' && Number.isFinite(entry) ? entry : fallback
    );
    return {
      directional: numberOr(value.directional, 0),
      isotropic: numberOr(value.isotropic, 0),
    };
  })();
  // The step override seeds from the project solver value, like the legacy UI.
  const solverTimeStepRatio = (() => {
    const value = project?.solver?.timeStepRatio;
    return typeof value === 'number' && Number.isFinite(value) ? value : 0.25;
  })();
  const selectedDirection = (() => {
    const value = selectedFlowStep?.direction;
    return Array.isArray(value) && value.length === 3
      && value.every(entry => typeof entry === 'number' && Number.isFinite(entry))
      ? value as [number, number, number]
      : [0, 0, -1] as [number, number, number];
  })();
  const selectedNativeModel = String(selectedFlowStep?.nativeModel ?? '');
  const specializedModelOptions = useMemo(() => {
    if (selectedStepModel !== 'specialized') return [] as SpecializedModelSpec[];
    const dimension = project?.dimension;
    return specializedModels.filter(model => (
      (!dimension || !Array.isArray(model.dimensions) || model.dimensions.includes(dimension))
      && (!model.operation || model.operation === selectedStepOperation)
    ));
  }, [specializedModels, selectedStepModel, selectedStepOperation, project?.dimension]);
  const selectedSpecializedModel = specializedModelOptions.find(model => model.id === selectedNativeModel) ?? null;
  const stepMaterialNames = useMemo(() => materialCatalog.map(row => row.name), [materialCatalog]);
  // Re-seed the JSON drafts when the inspected step (or its model) changes, not
  // on every keystroke, so in-progress edits are never clobbered.
  useEffect(() => {
    if (selectedStepModel !== 'specialized') {
      setNativeParametersText('');
      setNativeParametersError('');
      return;
    }
    setNativeParametersText(JSON.stringify(selectedFlowStep?.nativeParameters ?? {}, null, 2));
    setNativeParametersError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFlowStepId, selectedStepModel]);
  useEffect(() => {
    if (selectedStepModel !== 'directional' || selectedDirectionalMode !== 'rateSets') {
      setRateSetsText('');
      setRateSetsError('');
      return;
    }
    setRateSetsText(JSON.stringify(selectedFlowStep?.rateSets, null, 2));
    setRateSetsError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFlowStepId, selectedStepModel, selectedDirectionalMode]);
  useEffect(() => {
    if (selectedStepModel !== 'isotropic' || selectedStepRateMode !== 'material') {
      setIsotropicMaterialRatesText('');
      setIsotropicMaterialRatesError('');
      return;
    }
    const table = selectedFlowStep?.materialRates;
    setIsotropicMaterialRatesText(JSON.stringify(
      table && typeof table === 'object' && !Array.isArray(table) ? table : {},
      null,
      2,
    ));
    setIsotropicMaterialRatesError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFlowStepId, selectedStepModel, selectedStepRateMode]);
  useEffect(() => {
    if (selectedStepModel !== 'directional' || selectedDirectionalMode !== 'materialRates') {
      setDirectionalMaterialRatesText('');
      setDirectionalMaterialRatesError('');
      setDefaultMaterialRateText('');
      setDefaultMaterialRateError('');
      return;
    }
    const table = selectedFlowStep?.materialRates;
    setDirectionalMaterialRatesText(JSON.stringify(
      table && typeof table === 'object' && !Array.isArray(table) ? table : {},
      null,
      2,
    ));
    setDirectionalMaterialRatesError('');
    setDefaultMaterialRateText(JSON.stringify(selectedDefaultMaterialRate, null, 2));
    setDefaultMaterialRateError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFlowStepId, selectedStepModel, selectedDirectionalMode]);
  /**
   * A Route is stale when none of its attempts was built from the current
   * Compile. Branch attempts legitimately use derived Compiles, so any matching
   * attempt keeps the Route runnable; a Route created before a recompile has no
   * match and is blocked.
   */
  const routeStale = !!route && !!compile
    && !route.attempts.some(attempt => attempt.compileId === compile.compileId);
  const canRoute = !!compile && !compileStale && !busy && compileMissingSteps.length === 0;
  const canRun = !!segment && segment.status === 'planned' && !busy && !compileStale && !routeStale;
  /** A continued Segment can only start from a successful predecessor checkpoint. */
  const attemptSegments = segment && route
    ? route.segments.filter(item => item.attemptId === segment.attemptId)
    : [];
  const segmentIndexInAttempt = segment
    ? attemptSegments.findIndex(item => item.segmentId === segment.segmentId)
    : -1;
  const blockingPredecessors = segmentIndexInAttempt > 0
    ? attemptSegments.slice(0, segmentIndexInAttempt).filter(item => item.status !== 'succeeded')
    : [];
  const needsPredecessors = blockingPredecessors.length > 0;
  const canMeasure = !!segment && segment.status === 'succeeded' && !!segment.runId && !busy;
  /** 03/04 both validate the recipe X against the project's computation domain. */
  const recipeDomain = useMemo(() => {
    const domain = (project?.domain ?? {}) as Record<string, unknown>;
    const xExtent = Number(domain.xExtent);
    const yExtent = Number(domain.yExtent);
    const limit = project?.dimension === 3 && Number.isFinite(yExtent) && yExtent > 0
      ? Math.min(xExtent, yExtent)
      : xExtent;
    if (!Number.isFinite(limit) || limit <= 0) return null;
    // The computational domain is centred on the origin: [-extent/2, +extent/2].
    const radius = limit / 2;
    return { min: -radius, max: radius };
  }, [project?.dimension, project?.domain]);
  const recipeXValue = Number(recipeX);
  const recipeXOutOfDomain = recipeMethod === 'etch_depth'
    && (!Number.isFinite(recipeXValue) || (!!recipeDomain && (recipeXValue < recipeDomain.min || recipeXValue > recipeDomain.max)));
  const recipeDomainText = recipeDomain
    ? `${formatNumber(recipeDomain.min)}–${formatNumber(recipeDomain.max)} µm`
    : '计算域未知';
  const activeRecipeXOutOfDomain = !!activeRecipeSummary
    && activeRecipeSummary.method === 'etch_depth'
    && activeRecipeSummary.x !== null
    && !!recipeDomain
    && (activeRecipeSummary.x < recipeDomain.min || activeRecipeSummary.x > recipeDomain.max);
  const experimentRunCount = (experimentDoc?.variants ?? []).reduce(
    (total, variant) => total + variant.replicates.filter(replicate => !!replicate.runId).length, 0,
  );
  const hasExecutedRun = !!segment?.runId
    && !segment.runId.startsWith('pending_')
    && ['succeeded', 'failed'].includes(segment.status);
  const experimentReady = !!project && !!compile && !compileStale && !!activeRecipeId
    && !activeRecipeXOutOfDomain && !recipeXOutOfDomain && authoredSteps.length > 0;
  const experimentBlockReason = !project
    ? '还没有工程：先在 01 新建、打开或导入工程。'
    : !authoredSteps.length
      ? '流程为空：先在 02 添加步骤。'
      : !compile
        ? '流程未编译：先在 02 编译流程。'
        : compileStale
          ? '流程已修改：先在 02 重新编译。'
          : !activeRecipeId
            ? '还没有测量配方：先在 03 执行 Run 并完成一次测量。'
            : activeRecipeXOutOfDomain || recipeXOutOfDomain
              ? `配方 X 超出计算域（${recipeDomainText}）：在 03 调整 X 后重新测量。`
              : '';
  const analysisReady = !!project && !!compile && (experimentRunCount > 0 || !!measurement);
  const reportReady = hasExecutedRun || !!measurement || experimentRunCount > 0;
  /** Row index -> index of the earlier variant with the same duration. */
  const duplicateVariantDurations = useMemo(() => {
    const firstIndex = new Map<string, number>();
    const duplicates = new Map<number, number>();
    experimentRows.forEach((row, index) => {
      const value = Number(row.duration);
      if (!row.duration.trim() || !Number.isFinite(value)) return;
      const key = value.toFixed(6);
      const first = firstIndex.get(key);
      if (first === undefined) firstIndex.set(key, index);
      else duplicates.set(index, first);
    });
    return duplicates;
  }, [experimentRows]);
  const variantDurationConflict = duplicateVariantDurations.size > 0;
  /** A rebuilt Route or re-run Segment drops its measurement; say so in 03. */
  const needsRemeasure = !!route && !measurement && (measurementInvalidated || route.segments.some(item => item.status === 'succeeded'));
  /** Left tree and the right property panel must describe the measurement the same way. */
  const measurementState = !measurement
    ? { label: needsRemeasure ? '需重新测量' : '未测量', hasValue: false }
    : measurementHasValue(measurement)
      ? { label: '已测量', hasValue: true }
      : { label: statusLabel(measurement.status), hasValue: false };
  /** Why "测量" is disabled, shown next to the button instead of only in a tooltip. */
  const measureBlockedReason = recipeXOutOfDomain
    ? `X 超出计算域（${recipeDomainText}），请先调整 X 位置`
    : !segment
      ? '还没有 Run：先创建 Route 并执行 Segment'
      : segment.status !== 'succeeded'
        ? '当前 Segment 尚未成功：先执行完成再测量'
        : !segment.runId
          ? '当前 Run 缺少快照：先重新执行 Segment'
          : '';

  const stepDisplayName = useCallback((stepId: string): string => {
    const compiled = compile?.compile.project.steps.find(step => step.id === stepId);
    const authoredId = authoredByCompiled.get(stepId);
    const authored = authoredSteps.find(step => step.id === authoredId);
    return compiled?.name ?? authored?.name ?? stepId.slice(0, 10);
  }, [compile, authoredByCompiled, authoredSteps]);

  /** The one Segment that currently owns the supervisor slot, if any. */
  const runningSegment = (route?.segments ?? [])
    .find(item => ['running', 'queued', 'cancelling'].includes(item.status)) ?? null;
  const activeRunProgress = runningSegment?.runId ? runProgress[runningSegment.runId] : null;
  const totalStepCount = route?.stepOrder.length ?? 0;
  const completedStepCount = activeRunProgress?.completed.length ?? 0;

  const structureDimension: 2 | 3 = compile?.dimension === 3 || project?.dimension === 3 ? 3 : 2;

  // The 03 timeline always lists every Route step in `route.stepOrder`, even
  // when the owning Segment has not run yet. Pending rows are grey and not
  // clickable; only a succeeded run with a real manifest snapshot can be
  // previewed. This is why a split Route no longer hides its last step.
  const succeededSegmentRuns = useMemo(() => (route?.segments ?? [])
    .filter(item => !!item.runId && !item.runId.startsWith('pending_')
      && ['succeeded', 'timed_out', 'failed', 'cancelled', 'interrupted'].includes(item.status))
    .map(item => item.runId as string), [route]);

  useEffect(() => {
    const missing = succeededSegmentRuns.filter(runId => !(runId in runSnapshots));
    if (missing.length === 0) return;
    let cancelled = false;
    void Promise.all(missing.map(async runId => {
      try {
        const manifest = await api<{ snapshots?: Array<{ stepId?: unknown }> }>(`/api/runs/${runId}`);
        const stepIds = (manifest.snapshots ?? [])
          .map(item => (typeof item?.stepId === 'string' ? item.stepId : ''))
          .filter(Boolean);
        return [runId, stepIds] as const;
      } catch {
        return [runId, [] as string[]] as const;
      }
    })).then(entries => {
      if (cancelled) return;
      setRunSnapshots(previous => ({ ...previous, ...Object.fromEntries(entries) }));
    });
    return () => { cancelled = true; };
  }, [succeededSegmentRuns, runSnapshots]);

  // A running Segment writes one snapshot per completed step. Poll its run
  // manifest while it is live so the timeline, the progress bar and the
  // structure preview advance together; the final succeeded fetch above then
  // takes over.
  const runningSegmentRuns = useMemo(() => (route?.segments ?? [])
    .filter(item => !!item.runId && !item.runId.startsWith('pending_')
      && ['running', 'queued', 'cancelling'].includes(item.status))
    .map(item => item.runId as string), [route]);
  const runningRunKey = runningSegmentRuns.join(',');
  useEffect(() => {
    if (!runningRunKey) return;
    let cancelled = false;
    const tick = async () => {
      const entries = await Promise.all(runningSegmentRuns.map(async runId => {
        try {
          const manifest = await api<{ snapshots?: Array<{ stepId?: unknown }>; currentStepId?: unknown }>(`/api/runs/${runId}`);
          const steps = (manifest.snapshots ?? [])
            .map(item => (typeof item?.stepId === 'string' ? item.stepId : ''))
            .filter(Boolean);
          const currentStepId = typeof manifest.currentStepId === 'string' ? manifest.currentStepId : '';
          return { runId, steps, currentStepId };
        } catch {
          return { runId, steps: [] as string[], currentStepId: '' };
        }
      }));
      if (cancelled) return;
      setRunSnapshots(previous => {
        let changed = false;
        const next = { ...previous };
        for (const entry of entries) {
          const before = previous[entry.runId] ?? [];
          if (before.length !== entry.steps.length || entry.steps.some((stepId, index) => stepId !== before[index])) {
            next[entry.runId] = entry.steps;
            changed = true;
          }
        }
        return changed ? next : previous;
      });
      setRunProgress(previous => {
        let changed = false;
        const next = { ...previous };
        for (const entry of entries) {
          const before = previous[entry.runId];
          if (!before || before.currentStepId !== entry.currentStepId
            || before.completed.length !== entry.steps.length
            || entry.steps.some((stepId, index) => stepId !== before.completed[index])) {
            next[entry.runId] = { currentStepId: entry.currentStepId, completed: entry.steps };
            changed = true;
          }
        }
        return changed ? next : previous;
      });
    };
    void tick();
    const timer = window.setInterval(() => { void tick(); }, 1200);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [runningRunKey]);

  const runStructureSteps = useMemo(() => {
    if (!route) return [] as StructureStepRow[];
    const rows: StructureStepRow[] = [];
    const stepOrder = route.stepOrder;
    /** The Segment that executes `stepId`, by its [startAfter, through] range. */
    const segmentForStep = (stepId: string) => {
      const index = stepOrder.indexOf(stepId);
      if (index < 0) return { item: undefined, segmentIndex: -1 };
      const segmentIndex = route.segments.findIndex(item => {
        const start = item.startAfterStepId ? stepOrder.indexOf(item.startAfterStepId) + 1 : 0;
        const end = stepOrder.indexOf(item.throughStepId);
        return end >= 0 && index >= start && index <= end;
      });
      return { item: segmentIndex >= 0 ? route.segments[segmentIndex] : undefined, segmentIndex };
    };
    const pendingReasonFor = (item: RoutePlan['segments'][number] | undefined, stepId: string, hasSnapshot: boolean): string => {
      if (!item) return '待执行';
      if (item.status === 'running') {
        return item.runId && runProgress[item.runId]?.currentStepId === stepId ? '运行中' : '待执行';
      }
      if (item.status === 'failed') return '执行失败';
      if (item.status === 'timed_out') return '超时';
      if (item.status === 'cancelled') return '已取消';
      if (item.status === 'interrupted') return '已中断';
      if (item.status === 'succeeded' && !hasSnapshot) return '无快照';
      return '待执行';
    };

    // Initial structure: the first succeeded Segment that recorded an
    // `initial` snapshot. Before that it is a grey pending chip, not a fake view.
    const firstSegment = route.segments[0];
    const initialSegmentIndex = route.segments.findIndex(item => !!item.runId
      && !item.runId.startsWith('pending_')
      && !!runSnapshots[item.runId]?.includes('initial'));
    const initialSegment = initialSegmentIndex >= 0 ? route.segments[initialSegmentIndex] : undefined;
    rows.push({
      runId: initialSegment?.runId ?? '',
      stepId: 'initial',
      segmentId: initialSegment?.segmentId ?? firstSegment?.segmentId ?? '',
      segmentIndex: initialSegmentIndex >= 0 ? initialSegmentIndex : 0,
      kind: 'initial',
      label: '初始结构',
      executed: !!initialSegment,
      pendingReason: initialSegment ? undefined : pendingReasonFor(firstSegment, 'initial', false),
    });

    for (const stepId of stepOrder) {
      const { item, segmentIndex } = segmentForStep(stepId);
      const snapshots = item?.runId ? runSnapshots[item.runId] : undefined;
      // A timed-out or failed Segment still leaves every completed step's
      // snapshot on disk; keep those steps inspectable instead of hiding them
      // behind the terminal status.
      const executed = !!item
        && !!item.runId
        && !item.runId.startsWith('pending_')
        && !!snapshots?.includes(stepId);
      rows.push({
        runId: executed ? (item?.runId as string) : '',
        stepId,
        segmentId: item?.segmentId ?? '',
        segmentIndex: Math.max(0, segmentIndex),
        kind: 'step',
        label: stepDisplayName(stepId),
        executed,
        pendingReason: executed ? undefined : pendingReasonFor(item, stepId, !!snapshots?.includes(stepId)),
      });
    }
    return rows;
  }, [route, runSnapshots, runProgress, stepDisplayName]);

  useEffect(() => {
    // While a Segment is live, follow the newest completed step. The polling
    // effect above feeds both the timeline and the preview from the same
    // manifest, so the chip progress and the visible structure stay in sync.
    if (runningSegment?.runId) {
      const latest = [...runStructureSteps].reverse().find(row => row.executed);
      if (latest) {
        const key = `${latest.runId}:${latest.stepId}`;
        setStructurePick(previous => previous === key ? previous : key);
        return;
      }
    }
    const measuredKey = measurement?.sourceRunId && measurement.sourceStepId
      ? `${measurement.sourceRunId}:${measurement.sourceStepId}`
      : '';
    if (measuredKey && runStructureSteps.some(row => `${row.runId}:${row.stepId}` === measuredKey)) {
      setStructurePick(previous => previous === measuredKey ? previous : measuredKey);
      return;
    }
    setStructurePick(previous => runStructureSteps.some(row => `${row.runId}:${row.stepId}` === previous)
      ? previous
      : (() => {
        // Prefer the last step that actually has a snapshot; pending chips stay
        // selectable (they show the "尚未执行" empty state) but are not default.
        const lastExecuted = [...runStructureSteps].reverse().find(row => row.executed);
        const fallback = lastExecuted ?? runStructureSteps[0];
        return fallback ? `${fallback.runId}:${fallback.stepId}` : '';
      })());
  }, [runStructureSteps, measurement?.sourceRunId, measurement?.sourceStepId, runningSegment?.runId]);

  const structureSelection = runStructureSteps.find(row => `${row.runId}:${row.stepId}` === structurePick) ?? null;

  const onLeftSplitterDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    leftDrag.current = { startX: event.clientX, width: leftWidth };
  };
  const onLeftSplitterMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = leftDrag.current;
    if (!drag) return;
    setLeftWidth(Math.min(520, Math.max(180, drag.width + event.clientX - drag.startX)));
  };
  const onLeftSplitterUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = leftDrag.current;
    if (!drag) return;
    leftDrag.current = null;
    const next = Math.min(520, Math.max(180, drag.width + event.clientX - drag.startX));
    setLeftWidth(next);
    try { window.localStorage.setItem('wb.ui.leftWidth', String(Math.round(next))); } catch { /* storage may be full */ }
  };
  const onPreviewSplitterDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    previewDrag.current = { startY: event.clientY, height: previewHeight };
  };
  const onPreviewSplitterMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = previewDrag.current;
    if (!drag) return;
    setPreviewHeight(Math.min(760, Math.max(240, drag.height + event.clientY - drag.startY)));
  };
  const onPreviewSplitterUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = previewDrag.current;
    if (!drag) return;
    previewDrag.current = null;
    const next = Math.min(760, Math.max(240, drag.height + event.clientY - drag.startY));
    setPreviewHeight(next);
    try { window.localStorage.setItem('wb.ui.previewHeight', String(Math.round(next))); } catch { /* storage may be full */ }
  };
  /**
   * A measurement belongs to the run/step that produced it. Switching the 03
   * timeline to another step hides its marker instead of drawing the old X on
   * a structure it never measured.
   */
  const measurementMatchesStructure = !!measurement && !!structureSelection
    && (!measurement.sourceRunId || measurement.sourceRunId === structureSelection.runId)
    && (!measurement.sourceStepId || measurement.sourceStepId === structureSelection.stepId);
  const selectionMeasurement = measurementMatchesStructure ? measurement : null;
  const segmentIndex = segment && route ? route.segments.findIndex(item => item.segmentId === segment.segmentId) : -1;
  const segmentNumber = segmentIndex >= 0 ? segmentIndex + 1 : 1;
  const structureRunId = structureSelection?.runId ?? null;
  const structureStepId = structureSelection?.stepId ?? '';
  const structureStepIds = useMemo(
    () => (structureRunId ? (runSnapshots[structureRunId] ?? [structureStepId]) : []),
    [structureRunId, structureStepId, runSnapshots],
  );
  const structureViewExtent = useMemo(
    () => (structureView ? structureExtent(structureView, 'xy') : null),
    [structureView],
  );
  // Flat (no-op / zero-rate) steps still need a readable label; a measurement
  // reports the etched depth instead of the surface height.
  const structureNote = useMemo(() => {
    if (!structureView || !structureViewExtent) return null;
    // An ambiguous/failed measurement must never look like a measured depth.
    if (selectionMeasurement && !measurementHasValue(selectionMeasurement)) return '测量无有效值';
    const measuredDepth = selectionMeasurement && measurementHasValue(selectionMeasurement) && selectionMeasurement.method === 'etch_depth'
      ? Math.abs(selectionMeasurement.value as number)
      : null;
    if (measuredDepth !== null && measuredDepth >= 5e-4 && selectionMeasurement) {
      return `深度 ${formatMicrons(measuredDepth)} ${displayUnit(selectionMeasurement.unit)}`;
    }
    return structureSurfaceNote(structureView, 'xy');
  }, [structureView, structureViewExtent, selectionMeasurement]);

  // The marker stays where the recipe measured, but without a value it must not
  // claim a depth/height label; and it is hidden on any other run/step.
  const displayMeasurementMark = useMemo<StructureMarker>(() => {
    if (!measurementMark || !measurementMatchesStructure) return null;
    if (!measurement || measurementHasValue(measurement)) return measurementMark;
    return { ...measurementMark, label: '测量位置（无有效值）' };
  }, [measurementMark, measurement, measurementMatchesStructure]);

  /**
   * The same recipe position as a 3D line: an etch-depth X becomes a vertical
   * line along Z, a CD height becomes a horizontal line along X.
   */
  const viewer3dMarker = useMemo<Viewer3DMarker | null>(() => {
    if (!measurementMark || !measurementMatchesStructure) return null;
    return measurementMark.orientation === 'vertical'
      ? { axis: 'x', value: measurementMark.value, label: measurementMark.label }
      : { axis: 'z', value: measurementMark.value, label: measurementMark.label };
  }, [measurementMark, measurementMatchesStructure]);

  /** 3D wording: name the position, never point at a 2D view that is not there. */
  const viewer3dMeasurementText = (): string => {
    if (!selectionMeasurement) return '';
    if (viewer3dMarker) {
      const axisLabel = viewer3dMarker.axis === 'x' ? 'X' : viewer3dMarker.axis === 'z' ? '高度' : 'Y';
      return `测量位置：${axisLabel} = ${formatMicrons(viewer3dMarker.value)} µm`;
    }
    if (measurementHasValue(selectionMeasurement)) {
      return `测量值：${formatMicrons(selectionMeasurement.value as number)} ${displayUnit(selectionMeasurement.unit)}`;
    }
    return `测量${statusLabel(selectionMeasurement.status)}：${measurementReasonText(selectionMeasurement.reason)}`;
  };

  useEffect(() => {
    // 3D reads the raw snapshot inside its worker; fetching a candidate view too
    // would double the network and memory cost for no visible gain.
    if (structureDimension === 3) {
      setStructureView(null);
      setStructureError('');
      setStructureLoading(false);
      return;
    }
    if (!structureRunId || !structureStepId) {
      setStructureView(null);
      setStructureError('');
      return;
    }
    let cancelled = false;
    setStructureLoading(true);
    setStructureError('');
    void api<CandidateView>(`/api/v1/workflow/analysis/candidates/${structureRunId}/${structureStepId}/view`)
      .then(view => { if (!cancelled) setStructureView(view); })
      .catch(exc => {
        if (cancelled) return;
        setStructureView(null);
        setStructureError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => { if (!cancelled) setStructureLoading(false); });
    return () => { cancelled = true; };
  }, [structureRunId, structureStepId, structureDimension, route?.segments.map(item => `${item.segmentId}:${item.status}`).join(',')]);

  const run = async (name: string, task: () => Promise<void>) => {
    const label = actionLabel(name);
    setBusy(name);
    setError('');
    try {
      await task();
      // One result line only: the start of an action is not printed, so the
      // Output panel never shows internal handler ids or a bare ellipsis.
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已${label}`]);
    } catch (exc) {
      const raw = exc instanceof Error ? exc.message : String(exc);
      setError(translateBackendError(raw));
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  ${label}失败：${translateBackendError(raw)}${raw !== translateBackendError(raw) ? `（${raw}）` : ''}`]);
    } finally { setBusy(null); }
  };

  const uploadGds = async (file: File) => {
    setBusy('gds');
    setError('');
    try {
      const response = await fetch(`/api/gds-assets?file_name=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      });
      const text = await response.text();
      let payload: unknown = text;
      try { payload = text ? JSON.parse(text) : {}; } catch { /* text */ }
      if (!response.ok) throw new Error(errorText(payload));
      const asset = payload as GdsAsset;
      setGds(asset);
      // Uploading is only step one: register the file as a Layout immediately
      // and select its first structure so 01 never asks for the same GDS twice.
      try {
        const registered = await postJson<LayoutDetail>('/api/v1/workflow/layouts', {
          assetId: asset.assetId, fileName: asset.fileName,
        });
        setActiveLayout(registered);
        const first = registered.structures[0];
        if (first) selectStructure(registered, first.name);
        await refreshLayouts();
        const listed = await api<{ clips: Clip[] }>(`/api/v1/workflow/clips?layout_id=${registered.layoutId}`);
        setClips(listed.clips);
        setActiveClip(null);
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(null);
    }
  };

  const importProject = async (file: File) => {
    setError('');
    resetProjectScopedState();
    try {
      const parsed = JSON.parse(await file.text()) as ProjectDoc;
      setProject(parsed);
      setProjectId('');
      setSavedProjectJson('');
      setProjectSavedAt('');
      setProjectNotice('导入的工程尚未写入服务端');
      const geometry = (parsed.geometry ?? {}) as Record<string, unknown>;
      if (geometry.kind !== 'gds') parametricGeometryRef.current = geometry;
      setProjectGeometrySource(geometry.kind === 'gds'
        ? { kind: 'layout', clipName: typeof geometry.fileName === 'string' ? geometry.fileName : 'GDS Clip' }
        : { kind: 'parametric' });
      await activateClipBinding(await resolveClipBinding(parsed));
      setCompileStale(true);
      setProjectState(emptyWorkflowState());
      lastStateJson.current = '';
      setRestoreNotice('');
      setStateNotice('');
      setSetupMode('project');
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已导入工程 ${parsed.name ?? '未命名'}`]);
    } catch {
      setError('项目 JSON 无法解析');
    }
  };

  const beginNewProjectDraft = () => {
    setProject(null);
    setProjectId('');
    setSavedProjectJson('');
    setProjectSavedAt('');
    setProjectNotice('');
    resetProjectScopedState();
    setProjectGeometrySource({ kind: 'parametric' });
    setNewProjectArmed(false);
    setProjectState(emptyWorkflowState());
    lastStateJson.current = '';
    setRestoreNotice('');
    setStateNotice('');
    parametricGeometryRef.current = null;
    setSetupMode('new');
    setDraft(previous => ({ ...previous, name: 'New Project', timeout: solverDefaultSeconds }));
    window.localStorage.removeItem(PROJECT_STORAGE_KEY);
  };

  const startNewProject = () => {
    if (project && projectDirty) { setNewProjectArmed(true); return; }
    beginNewProjectDraft();
  };

  const setDraftField = (key: keyof typeof draft, value: string | number) => {
    setDraft(previous => ({ ...previous, [key]: value }));
  };

  const changeDraftUnits = (unit: LengthUnit) => {
    if (unit === draft.unit) return;
    if (unit === 'nm' && draft.source === 'layout') {
      setError('GDS / Layout 工程目前只能使用 µm；请改用参数化几何或保持 µm。');
      return;
    }
    const factor = lengthUnitsPerUm(unit) / lengthUnitsPerUm(draft.unit);
    setDraft(previous => ({
      ...previous,
      unit,
      gridDelta: previous.gridDelta * factor,
      xExtent: previous.xExtent * factor,
      yExtent: previous.yExtent * factor,
      width: previous.width * factor,
      depth: previous.depth * factor,
      radius: previous.radius * factor,
      finHeight: previous.finHeight * factor,
      height: previous.height * factor,
      maskHeight: previous.maskHeight * factor,
    }));
  };

  /**
   * Gallery starter. A GDS sample (e.g. finfet22-bare-wafer) must reopen its
   * layout binding exactly like an imported project: the sample cannot upload
   * anything, so the registered Clip/Layout is resolved by asset/file name.
   * A missing asset is announced instead of silently flipping to parametric,
   * which would drop the demo's 3D mask stack.
   */
  const loadSample = async (sample: Record<string, unknown>, label: string) => {
    setProject(sample as ProjectDoc);
    setProjectId('');
    setSavedProjectJson('');
    setProjectSavedAt('');
    setProjectNotice('');
    resetProjectScopedState();
    const geometry = (sample.geometry ?? {}) as Record<string, unknown>;
    const isGds = geometry.kind === 'gds';
    if (isGds) {
      // A GDS uploaded for the previous project must not rename this asset.
      setGds(null);
      parametricGeometryRef.current = null;
      setProjectGeometrySource({
        kind: 'layout',
        clipName: typeof geometry.fileName === 'string' ? geometry.fileName : 'GDS Clip',
      });
    } else {
      parametricGeometryRef.current = geometry;
      setProjectGeometrySource({ kind: 'parametric' });
    }
    setProjectState(emptyWorkflowState());
    lastStateJson.current = '';
    setRestoreNotice('');
    setStateNotice('');
    setSetupMode('project');
    if (isGds) {
      try {
        const binding = await resolveClipBinding(sample as ProjectDoc);
        await activateClipBinding(binding);
        if (!binding?.clipId && !binding?.layoutId) {
          const fileName = typeof geometry.fileName === 'string' ? geometry.fileName : 'GDS';
          setRestoreNotice(`示例「${label}」引用的 GDS 资源 ${fileName} 尚未在本机注册；请在 01 上传同名 GDS 或重新选择 Clip 后再编译。`);
        }
      } catch (exc) {
        setRestoreNotice(`示例「${label}」的 GDS 资源无法解析：${exc instanceof Error ? exc.message : String(exc)}`);
      }
    }
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已载入示例: ${label}`]);
  };

  const setDraftDimension = (dimension: 2 | 3) => {
    setDraft(previous => ({
      ...previous,
      dimension,
      // Both dimensions accept parametric and GDS sources now.
      source: previous.source,
      geometry: dimension === 2
        ? (previous.geometry === 'trench' ? 'trench' : 'plane')
        : (previous.geometry === 'fin' ? 'fin' : 'hole'),
    }));
  };

  const chooseDraftSource = (source: 'parametric' | 'layout') => {
    // GDS/Layout coordinates stay in the asset's micrometre frame; selecting
    // a layout therefore returns the draft to µm and rescales its fields.
    setDraft(prev => {
      if (source !== 'layout' || prev.unit !== 'nm') return { ...prev, source };
      const factor = 0.001;
      return {
        ...prev,
        source,
        unit: 'um' as LengthUnit,
        gridDelta: prev.gridDelta * factor,
        xExtent: prev.xExtent * factor,
        yExtent: prev.yExtent * factor,
        width: prev.width * factor,
        depth: prev.depth * factor,
        radius: prev.radius * factor,
        finHeight: prev.finHeight * factor,
        height: prev.height * factor,
        maskHeight: prev.maskHeight * factor,
      };
    });
  };

  /**
   * 01 GDS source errors are staged: no Layout, Layout without Clip, Clip not
   * selected. "先上传版图" is only true in the first case.
   */
  const layoutSourceBlockReason = (): string => {
    if (!layouts.length) return '还没有 Layout：先上传 GDS 并注册为 Layout';
    if (!activeLayout || !clips.length) return '已有 Layout，选择结构并保存 Clip';
    if (!activeClip) return '选择已有 Clip 或保存新 Clip';
    return '';
  };

  const createDraftProject = () => {
    if (draft.source === 'layout' && !activeClip) {
      setError(layoutSourceBlockReason());
      return;
    }
    const dimension = draft.dimension;
    const is3d = dimension === 3;
    if (is3d && draft.source === 'layout' && activeClip) {
      const missing = activeClip.layers.find(item => item.baseHeight == null || item.height == null);
      if (missing) {
        setError(`Clip 的图层 L${missing.layer} 缺少 baseHeight/height：先在下方为每层填写层高并保存 Clip`);
        return;
      }
    }
    const name = draft.name.trim() || 'New Project';
    const gridDelta = Math.max(0.01, Number(draft.gridDelta) || 0.2);
    const xExtent = Math.max(gridDelta * 4, Number(draft.xExtent) || 4);
    const yExtent = Math.max(gridDelta * 4, Number(draft.yExtent) || 4);
    const common = {
      taperAngle: Number(draft.taperAngle) || 0,
      maskHeight: Math.max(0, Number(draft.maskHeight) || 0),
      maskTaperAngle: 0,
      substrateMaterial: draft.substrateMaterial || 'Si',
      maskMaterial: draft.maskMaterial || 'Mask',
    };
    const parametric = draft.geometry === 'trench'
      ? { kind: 'trench', width: Number(draft.width) || 1, depth: Number(draft.depth) || 2, ...common }
      : draft.geometry === 'hole'
        ? { kind: 'hole', radius: Number(draft.radius) || 1, depth: Number(draft.depth) || 2, ...common }
        : draft.geometry === 'fin'
          ? { kind: 'fin', width: Number(draft.width) || 1, height: Number(draft.finHeight) || 1.2, ...common, construction: 'native' }
          : { kind: 'plane', height: Number(draft.height) || 0, substrateMaterial: draft.substrateMaterial || 'Si' };
    const layout = draft.source === 'layout' && activeClip
      ? clipGeometryBinding(activeClip, dimension)
      : null;
    const projectPayload: ProjectDoc = {
      schemaVersion: 7, name, dimension,
      units: { length: draft.unit, time: 's', rate: draft.unit === 'nm' ? 'nm/s' : 'um/s', angle: 'deg' },
      domain: is3d
        ? { gridDelta, xExtent, yExtent, boundaryXY: 'reflective' }
        : { gridDelta, xExtent, xBoundary: 'reflective' },
      geometry: layout ?? parametric,
      steps: [],
      solver: {
        timeStepRatio: 0.25,
        raysPerPoint: Math.max(1, Number(draft.raysPerPoint) || 16),
        cpuThreads: Math.max(1, Number(draft.cpuThreads) || 1),
        // 0 is a legal seed: only an empty/invalid value falls back to 7.
        random: {
          mode: 'fixed',
          seed: String(draft.seed ?? '').trim() !== '' && Number.isFinite(Number(draft.seed))
            ? Number(draft.seed)
            : 7,
        },
        enginePreference: draft.engine,
        timeoutSeconds: Math.min(SOLVER_TIMEOUT_MAX, Math.max(SOLVER_TIMEOUT_MIN, Number(draft.timeout) || solverDefaultSeconds)),
      },
      ...(is3d ? { view3D: {
        projection: 'parallel', camera: null, displayMode: 'surface',
        visibleMaterialIds: [], opacityByMaterial: {},
        clip: { enabled: false, axis: 'x', position: 0.0, keep: 'positive' },
        section: { enabled: false, axis: 'y', position: 0.0 },
      } } : { view: { panX: 0, panH: 0, zoom: 1, showGrid: true } }),
    };
    setProject(projectPayload);
    setProjectGeometrySource(layout && activeClip
      ? { kind: 'layout', clipId: activeClip.clipId, clipName: activeClip.name, layoutId: activeClip.layoutId, clipHash: activeClip.clipHash }
      : { kind: 'parametric' });
    resetProjectScopedState();
    setProjectId('');
    setSavedProjectJson('');
    setProjectSavedAt('');
    setProjectNotice('');
    setSetupMode('project');
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  新工程草稿已建立: ${name}`]);
  };

  const compileFlow = () => run('compile-flow', async () => {
    restoreEpoch.current += 1;
    // A restore may have left a fresh-looking "编译成功" summary for the
    // previous Compile. Mark it stale before the request so anything waiting
    // for that banner waits for *this* Compile instead of acting on the old
    // Compile/Route pair.
    setCompileStale(true);
    if (!project) throw new Error('先创建或导入工程');
    const steps = project.steps ?? [];
    if (!steps.length) throw new Error('流程还没有步骤，先在 02 插入或添加步骤');
    const source = projectGeometrySource;
    const document = projectDocumentForSave();
    let result: CompileInfo;
    if (source.kind === 'layout' && source.clipId) {
      result = await postJson<CompileInfo>(`/api/v1/workflow/clips/${source.clipId}/compile`, {
        project: document, flowId: compile?.flowId,
      });
    } else {
      result = await postJson<CompileInfo>('/api/v1/workflow/flows/from-project', {
        project: document, flowId: compile?.flowId,
      });
    }
    if (compile) setPreviousCompile(compile);
    setCompile(result);
    setCompileStale(false);
    setRestoreNotice('');
    // Drop the old Route in the same commit that makes the new Compile fresh.
    // Otherwise the success summary is visible while the previous Route is
    // still current, and 创建 Route would take its "route already exists"
    // shortcut and leave 03 without a Segment.
    setRoute(null);
    setSegment(null);
    setMeasurement(null);
    setProvenance(null);
    // The basis must describe the project *after* the Compile wrote its
    // authored steps (and after the server filled defaults); the pre-compile
    // closure made a 6-step template project look compiled as its old 5
    // steps, so deleting the appended step and saving was wrongly fresh.
    const authored = await normalizeProjectForBasis(document);
    const nextProject: ProjectDoc = {
      ...authored,
      steps: (result.authoredSteps ?? authored.steps ?? []) as ProjectStep[],
    };
    setProject(nextProject);
    recordCompile(result.compileId, executionCompileBasis(nextProject));
    restorePending.current = false;
    // A missing enabled step is never a silent skip: say which step vanished
    // and keep 创建 Route disabled (canRoute reads the same SourceMap).
    const compiledAuthored = (result.compile.sourceMap ?? []).map(entry => entry.authoredStepId);
    const missing = missingCompiledSteps(nextProject.steps ?? [], compiledAuthored);
    if (missing.length) {
      setError(`编译结果缺少步骤 ${coverageStepNames(missing)}，已阻止创建 Route`);
    }
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  流程已编译 · ${result.stepCount} 步`]);
  });

  const saveTemplate = () => run('template', async () => {
    if (!project) throw new Error('先创建或导入工程');
    const templateId = `tpl_${Date.now().toString(36)}`;
    const sourceSteps = (project.steps ?? []) as Array<Record<string, unknown>>;
    if (!sourceSteps.length) throw new Error('流程还没有步骤');
    const enabled = sourceSteps.filter(step => step.enabled);
    const variableIds = new Set(enabled.slice(-2).map(step => step.id));
    const steps = sourceSteps.map(step => variableIds.has(step.id) ? { ...step, duration: '${duration}' } : step);
    await postJson('/api/v1/workflow/templates', {
      schemaVersion: 1, templateId, name: `${project.name ?? 'Project'} · duration variable`, description: '',
      steps, variables: [{ name: 'duration', value: Number(templateVariable) || 0.02, unit: 's' }],
    });
    const data = await api<{ templates: TemplateSummary[] }>('/api/v1/workflow/templates');
    setTemplates(data.templates);
    setTemplateId(templateId);
  });

  const compareCompile = () => run('diff', async () => {
    if (!compile || !previousCompile) throw new Error('没有上一版 Compile');
    const result = await api<typeof compileDiff>(`/api/v1/workflow/compiles/${previousCompile.compileId}/diff/${compile.compileId}`);
    setCompileDiff(result);
  });

  const applyTemplate = (mode: 'replace' | 'append') => run('template', async () => {
    if (!project || !templateId) throw new Error('先选择模板');
    const value = Number(templateVariable) || 0.02;
    const result = await postJson<CompileInfo>('/api/v1/workflow/flows/from-template', {
      project, templateId, flowId: compile?.flowId, variables: { duration: value }, mode,
    });
    if (compile) setPreviousCompile(compile);
    setCompile(result); setCompileStale(false); setRoute(null); setSegment(null); setMeasurement(null); setProvenance(null);
    // Same rule as compileFlow: the authored project the template Compile
    // produced, not the pre-template closure.
    const authored = await normalizeProjectForBasis(project);
    const nextProject: ProjectDoc = {
      ...authored,
      steps: (result.authoredSteps ?? authored.steps ?? []) as ProjectStep[],
    };
    setProject(nextProject);
    recordCompile(result.compileId, executionCompileBasis(nextProject));
  });

  const createRoute = (rebuild = false) => run('route', async () => {
    restoreEpoch.current += 1;
    if (!compile) throw new Error('先编译流程');
    if (compileStale) throw new Error('流程已修改，请重新编译');
    if (compileMissingSteps.length) {
      throw new Error(`编译结果缺少步骤 ${compileMissingText}，已阻止创建 Route`);
    }
    if (route && !rebuild) { navigateZone('run'); return; }
    const enabledSteps = compile.compile.project.steps.filter(step => step.enabled);
    // One Segment for the whole compiled flow. A split at the second-to-last
    // step was a historical default, not a native requirement: it forced a
    // checkpoint/restart in the middle of a 23-step demo. The API still accepts
    // splitAfterStepId for callers that explicitly want a checkpoint.
    const result = await postJson<{ plan: RoutePlan }>('/api/v1/workflow/routes/from-compile', {
      compileId: compile.compileId,
      name: `${compile.projectName} · Route`,
    });
    // Never replace a working Route with one that silently dropped an enabled
    // compiled step: the old Route stays intact and the banner explains why.
    const missingInRoute = missingRouteSteps(enabledSteps.map(step => step.id), result.plan.stepOrder);
    if (missingInRoute.length) {
      const missingNames = coverageStepNames(enabledSteps.filter(step => missingInRoute.includes(step.id)));
      throw new Error(`Route 缺少步骤 ${missingNames}，已阻止创建 Route，旧 Route 保持不变`);
    }
    setRoute(result.plan);
    setSegment(result.plan.segments[0] ?? null);
    setMeasurementInvalidated(!!measurement);
    setMeasurement(null);
    setMetrics([]);
    setMeasurementMark(null);
    setMetricDetail(null);
    setProvenance(null);
    recordRoute(result.plan, true);
    restorePending.current = false;
    setRebuildRouteArmed(false);
    navigateZone('run');
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  Route 已创建 · ${result.plan.segments.length} Segment`]);
  });

  const goToRun = () => {
    if (!route) { void createRoute(); return; }
    navigateZone('run');
  };

  /**
   * Re-running a Segment invalidates its own measurement and every measurement
   * of a later Segment: those later runs start from a checkpoint that no longer
   * exists. The pointer document and the local tables are pruned together.
   */
  const clearMeasurementsFrom = (segmentId: string) => {
    const segments = route?.segments ?? [];
    const index = segments.findIndex(item => item.segmentId === segmentId);
    if (index < 0) return;
    const affectedSegments = new Set(segments.slice(index).map(item => item.segmentId));
    const affectedRuns = new Set(segments.slice(index)
      .map(item => item.runId)
      .filter((runId): runId is string => !!runId && !runId.startsWith('pending_')));
    const dropped = new Set(projectState.segmentRuns
      .filter(row => affectedSegments.has(row.segmentId) && row.measurementId)
      .map(row => row.measurementId as string));
    if (!dropped.size && !affectedRuns.size) return;
    setProjectState(previous => ({
      ...previous,
      segmentRuns: previous.segmentRuns.map(row => affectedSegments.has(row.segmentId)
        ? { ...row, measurementId: null }
        : row),
      measurementIds: previous.measurementIds.filter(id => !dropped.has(id)),
    }));
    setMetrics(previous => previous.filter(item => !affectedRuns.has(item.sourceRunId ?? '')));
    setMeasurement(previous => (previous && affectedRuns.has(previous.sourceRunId ?? '') ? null : previous));
    if (dropped.size || (measurement && affectedRuns.has(measurement.sourceRunId ?? ''))) {
      setMeasurementInvalidated(true);
    }
    setMeasurementMark(null);
    setMetricDetail(previous => (previous && affectedRuns.has(previous.measurement.sourceRunId ?? '') ? null : previous));
    setProvenance(null);
  };

  const execute = () => run('execute', async () => {
    if (!segment) throw new Error('先创建 Route');
    if (compileStale || routeStale) throw new Error('编译已过期，先重新编译并重建 Route');
    clearMeasurementsFrom(segment.segmentId);
    const result = await postJson<{ segment: RoutePlan['segments'][number]; runId: string }>(
      `/api/v1/workflow/segments/${segment.segmentId}/execute`, {},
    );
    setRunNotice('');
    // The backend may have cancelled the previous run to free the single
    // supervisor slot. Only a full Route refresh shows that old Segment as
    // cancelled instead of leaving a stale "running" row behind.
    const plan = route ? await refreshRoutePlan(route.route.routeId) : null;
    if (plan) {
      recordRoute(plan);
      setSegment(plan.segments.find(item => item.segmentId === result.segment.segmentId) ?? result.segment);
    } else {
      setSegment(result.segment);
    }
  });

  /**
   * Stop the live run from the 03 banner. A Segment cancel targets its Run;
   * a Run that has no Segment in the current Route falls back to the
   * supervisor endpoint. Both paths poll until the run is terminal before
   * claiming "已停止".
   */
  const stopRunningRun = async () => {
    const target = runningSegment;
    const orphanRunId = target ? null : (health?.activeRunId ?? null);
    if (!target && !orphanRunId) return;
    setStoppingRun(true);
    setError('');
    setRunNotice('');
    try {
      if (target) {
        await postJson(`/api/v1/workflow/segments/${target.segmentId}/cancel`, {});
        const done = await waitSegment(target.segmentId);
        setSegment(previous => previous && previous.segmentId === done.segmentId ? done : previous);
      } else if (orphanRunId) {
        await postJson(`/api/runs/${orphanRunId}/cancel`, {});
        const deadline = Date.now() + 30000;
        while (Date.now() < deadline) {
          const next = await api<Health>('/api/health');
          setHealth(next);
          if (!next.activeRunId || next.activeRunId !== orphanRunId) break;
          await sleep(500);
        }
      }
      if (route) recordRoute(await refreshRoutePlan(route.route.routeId));
      setHealth(await api<Health>('/api/health'));
      setRunNotice('已停止');
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已停止运行`]);
    } catch (exc) {
      const raw = exc instanceof Error ? exc.message : String(exc);
      setError(translateBackendError(raw));
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  停止运行失败：${translateBackendError(raw)}`]);
    } finally {
      setStoppingRun(false);
    }
  };

  /**
   * "执行前序并运行": a continued Segment needs a successful predecessor
   * checkpoint, so run every unfinished Segment up to the current one in order
   * and stop at the first failure instead of surfacing an English backend error.
   */
  const executeThroughCurrent = () => run('execute', async () => {
    if (!segment || !route) throw new Error('先创建 Route');
    if (compileStale || routeStale) throw new Error('编译已过期，先重新编译并重建 Route');
    const attemptSegments = route.segments.filter(item => item.attemptId === segment.attemptId);
    const index = attemptSegments.findIndex(item => item.segmentId === segment.segmentId);
    if (index < 0) throw new Error('当前 Segment 不在 Route 中');
    const chain = attemptSegments.slice(0, index + 1).filter(item => item.status !== 'succeeded');
    let working = route;
    for (const item of chain) {
      clearMeasurementsFrom(item.segmentId);
      const started = await postJson<{ segment: RoutePlan['segments'][number]; runId: string }>(
        `/api/v1/workflow/segments/${item.segmentId}/execute`, {},
      );
      setSegment(started.segment);
      working = { ...working, segments: working.segments.map(row => row.segmentId === started.segment.segmentId ? started.segment : row) };
      setRoute(working);
      recordRoute(working);
      const done = await waitSegment(item.segmentId);
      setSegment(done);
      working = { ...working, segments: working.segments.map(row => row.segmentId === done.segmentId ? done : row) };
      setRoute(working);
      recordRoute(working);
      if (done.status !== 'succeeded') {
        throw new Error(`执行到「${stepDisplayName(item.throughStepId)}」时${statusLabel(done.status)}，已停止；请先排查该 Segment 再继续。`);
      }
    }
  });

  const ensureRecipe = async (): Promise<{ recipeId: string; recipeVersion: number; metricId: string }> => {
    if (!compile) throw new Error('先编译流程');
    const metricId = `metric_${compile.flowId}_${recipeMethod}`;
    const recipeId = `recipe_${compile.flowId}_${recipeMethod}`;
    const metricUnit = 'um';
    await postJson('/api/v1/workflow/metrics', {
      schemaVersion: 1, metricId, name: `${recipeMethod} metric`, unit: metricUnit, description: 'configurable recipe',
    });
    const parameters = recipeMethod === 'cd_at_height'
      ? { kind: 'cd_at_height', height: Number(recipeHeight) || 0, pairSelection: 'outermost' }
      : { kind: 'etch_depth', x: Number(recipeX) || 0, referenceHeight: Number(recipeRef) || 0 };
    let recipeVersion = 1;
    let recipeSaved = false;
    while (!recipeSaved && recipeVersion <= 20) {
      try {
        await postJson('/api/v1/workflow/metrology-recipes', {
          schemaVersion: 1, recipeId, version: recipeVersion, metricId,
          name: `${recipeMethod} @ ${recipeMaterial}`, dimension: compile.dimension,
          method: recipeMethod, parameters, material: recipeMaterial,
        });
        recipeSaved = true;
      } catch (exc) {
        if (exc instanceof Error && exc.message.includes('already exists')) recipeVersion += 1;
        else throw exc;
      }
    }
    if (!recipeSaved) throw new Error('配方版本无法保存');
    return { recipeId, recipeVersion, metricId };
  };

  const measure = () => run('measure', async () => {
    if (!segment?.runId) throw new Error('先完成运行');
    if (recipeXOutOfDomain) throw new Error(`X 超出计算域（${recipeDomainText}），请先调整 X 位置`);
    const { recipeId, recipeVersion } = await ensureRecipe();
    const result = await postJson<Measurement>('/api/v1/workflow/measurements/from-run', {
      recipeId, recipeVersion, runId: segment.runId, stepId: segment.throughStepId,
    });
    setMeasurement(result);
    setMeasurementInvalidated(false);
    const bound = await postJson<Measurement>(`/api/v1/workflow/measurements/${result.measurementId}/bind`, {
      segmentId: segment.segmentId, runId: segment.runId, stepId: segment.throughStepId,
    });
    setMeasurement(bound);
    setProvenance(await api<Provenance>(`/api/v1/workflow/measurements/${result.measurementId}/provenance`));
    const listed = await api<{ measurements: Measurement[] }>(`/api/v1/workflow/measurements?run_id=${segment.runId}`);
    setMetrics(listed.measurements);
    recordMeasurement(result.measurementId, segment.segmentId);
    setActiveRecipeId(recipeId);
    setActiveRecipeVersion(recipeVersion);
    setActiveRecipeSummary({
      method: recipeMethod,
      material: recipeMaterial,
      x: recipeMethod === 'etch_depth' ? (Number(recipeX) || 0) : null,
      height: recipeMethod === 'cd_at_height' ? (Number(recipeHeight) || 0) : null,
      referenceHeight: recipeMethod === 'etch_depth' ? (Number(recipeRef) || 0) : null,
    });
    const markValue = recipeMethod === 'cd_at_height' ? (Number(recipeHeight) || 0) : (Number(recipeX) || 0);
    setMeasurementMark(recipeMethod === 'cd_at_height'
      ? { orientation: 'horizontal', value: markValue, label: `测量 CD 高度 ${markValue} µm` }
      : { orientation: 'vertical', value: markValue, label: `测量 X ${markValue} µm` });
  });

  const decideMeasurementBranch = () => run('decision', async () => {
    if (!route) throw new Error('先创建 Route');
    if (!branchablePrefixes.length) throw new Error('没有保留后续步骤的成功前缀 Segment');
    if (!decisionPrefixId) throw new Error('没有可用的已成功前缀 Segment');
    if (!decisionThroughId) throw new Error('没有可用的分支结束步骤');
    const thresholdText = decisionThreshold.trim();
    const threshold = thresholdText ? Number(thresholdText) : 0;
    if (!Number.isFinite(threshold)) throw new Error('决策阈值必须是有限数字');
    const recipe = activeRecipeId
      ? { recipeId: activeRecipeId, recipeVersion: activeRecipeVersion }
      : await ensureRecipe();
    const trueParameters = parseAuthoredParameters(decisionTrueParameters, 'true 分支');
    const falseParameters = parseAuthoredParameters(decisionFalseParameters, 'false 分支');
    const result = await postJson<MeasurementDecisionResponse>(
      `/api/v1/workflow/routes/${route.route.routeId}/measurement-decisions`,
      {
        startAfterStepId: decisionPrefixId,
        branchThroughStepId: decisionThroughId,
        recipeId: recipe.recipeId,
        recipeVersion: recipe.recipeVersion,
        operator: decisionOperator,
        threshold,
        branches: {
          true: { parameters: trueParameters },
          false: { parameters: falseParameters },
        },
      },
    );
    setRoute(result.plan);
    setSegment(result.plan.segments.find(item => item.segmentId === result.decision.branchSegmentId) ?? null);
    setDecisions(previous => [...previous, result.decision]);
    setMeasurement(result.measurement);
    recordRoute(result.plan);
    recordMeasurement(result.measurement.measurementId, result.decision.branchSegmentId);
    setActiveRecipeId(recipe.recipeId);
    setActiveRecipeVersion(recipe.recipeVersion);
    setProvenance(null);
  });

  const replayDecision = (decision: MeasurementDecision) => run('decision-replay', async () => {
    if (!route) throw new Error('先创建 Route');
    const result = await postJson<MeasurementDecisionResponse>(
      `/api/v1/workflow/routes/${route.route.routeId}/measurement-decisions/${decision.decisionId}/replay`, {},
    );
    setRoute(result.plan);
    setSegment(result.plan.segments.find(item => item.segmentId === result.decision.branchSegmentId) ?? null);
    setDecisions(previous => [...previous, result.decision]);
    setMeasurement(result.measurement);
    recordRoute(result.plan);
    recordMeasurement(result.measurement.measurementId, result.decision.branchSegmentId);
  });


  const executeAll = () => run('execute', async () => {
    if (!route) throw new Error('先创建 Route');
    if (compileStale || routeStale) throw new Error('编译已过期，先重新编译并重建 Route');
    let working = route;
    for (const item of route.segments.filter(segmentItem => segmentItem.status === 'planned')) {
      clearMeasurementsFrom(item.segmentId);
      const started = await postJson<{ segment: RoutePlan['segments'][number]; runId: string }>(
        `/api/v1/workflow/segments/${item.segmentId}/execute`, {},
      );
      setSegment(started.segment);
      working = { ...working, segments: working.segments.map(row => row.segmentId === started.segment.segmentId ? started.segment : row) };
      setRoute(working);
      recordRoute(working);
      const done = await waitSegment(item.segmentId);
      setSegment(done);
      working = { ...working, segments: working.segments.map(row => row.segmentId === done.segmentId ? done : row) };
      setRoute(working);
      recordRoute(working);
    }
  });

  const branchLastStep = () => run('branch', async () => {
    if (!project || !compile || !route) throw new Error('先完成前缀 Route');
    const enabledSteps = compile.compile.project.steps.filter(step => step.enabled);
    if (enabledSteps.length < 2) throw new Error('至少需要两步');
    const tail = enabledSteps[enabledSteps.length - 1];
    const prefix = enabledSteps[enabledSteps.length - 2];
    const authoredTail = compile.compile.sourceMap?.find(entry => entry.stepId === tail.id)?.authoredStepId ?? tail.id;
    const cloned = JSON.parse(JSON.stringify(project)) as typeof project & { steps?: Array<Record<string, unknown>> };
    const target = cloned.steps?.find(step => step.id === authoredTail);
    if (!target) throw new Error('找不到尾步骤');
    target.duration = Number(branchDuration) || 0.02;
    const adopted = await postJson<CompileInfo>('/api/v1/workflow/flows/from-project', { project: cloned, flowId: compile.flowId });
    setCompile(adopted);
    setCompileStale(false);
    // The branch Compile rewrites the tail duration in a *copy*; the authored
    // project is unchanged, so the basis still describes it (normalized, so an
    // imported project that omits defaults does not look edited after a save).
    const authored = await normalizeProjectForBasis(project);
    setProjectState(previous => ({
      ...previous, compileId: adopted.compileId, compileBasis: executionCompileBasis(authored),
    }));
    const result = await postJson<{ plan: RoutePlan }>(`/api/v1/workflow/routes/${route.route.routeId}/branches`, {
      compileId: adopted.compileId, startAfterStepId: prefix.id, changedStepIds: [tail.id],
    });
    setRoute(result.plan);
    const tailSegment = result.plan.segments[result.plan.segments.length - 1];
    setSegment(tailSegment);
    recordRoute(result.plan);
    setMeasurement(null); setProvenance(null); setMetrics([]);
  });

  const refreshExperimentList = async (preferred: string[] = []): Promise<ExperimentSummaryRow[]> => {
    try {
      const data = await api<{ experiments: Array<{ experimentId: string; name: string; updatedAt: string }> }>('/api/v1/workflow/experiments');
      const listed = await Promise.all(data.experiments.slice(0, 12).map(async row => {
        try {
          const doc = await api<ExperimentDoc>(`/api/v1/workflow/experiments/${row.experimentId}`);
          return { ...row, name: doc.name || row.name, status: doc.status, variantCount: doc.variants.length };
        } catch {
          return { ...row, status: 'unknown', variantCount: 0 };
        }
      }));
      const known = new Set(listed.map(row => row.experimentId));
      // Experiments saved in the project state must stay visible even when they
      // are older than the newest page of the global list.
      const restored = await Promise.all(preferred.filter(id => !known.has(id)).slice(0, 8).map(async id => {
        try {
          const doc = await api<ExperimentDoc>(`/api/v1/workflow/experiments/${id}`);
          return { experimentId: id, name: doc.name, updatedAt: '', status: doc.status, variantCount: doc.variants.length };
        } catch {
          return null;
        }
      }));
      const byId = new Map<string, ExperimentSummaryRow>();
      for (const row of [...listed, ...restored.filter((item): item is ExperimentSummaryRow => !!item)]) byId.set(row.experimentId, row);
      const ordered = [
        ...preferred.map(id => byId.get(id)).filter((row): row is ExperimentSummaryRow => !!row),
        ...listed.filter(row => !preferred.includes(row.experimentId)).map(row => byId.get(row.experimentId) as ExperimentSummaryRow),
      ].slice(0, 12);
      setExperimentList(ordered);
      return ordered;
    } catch {
      setExperimentList([]);
      return [];
    }
  };

  // Restore the experiments recorded in the project state (schema 11) so 04
  // lists them after reopening a project instead of only after a new run.
  const stateExperimentIds = projectState.experimentIds.join(',');
  useEffect(() => {
    if (!projectId) return;
    const ids = projectState.experimentIds;
    if (!ids.length) return;
    let cancelled = false;
    void refreshExperimentList(ids).then(() => {
      if (cancelled) return;
      const target = ids[ids.length - 1];
      void api<ExperimentDoc>(`/api/v1/workflow/experiments/${target}`)
        .then(doc => setExperimentDoc(previous => previous ?? doc))
        .catch(() => undefined);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, stateExperimentIds]);

  const resizeVariants = (count: number) => {
    const size = Math.max(1, Math.min(16, Math.floor(count) || 1));
    setExperimentRows(previous => {
      if (size <= previous.length) return previous.slice(0, size);
      return [...previous, ...Array.from({ length: size - previous.length }, (_, offset) => {
        const index = previous.length + offset;
        return { duration: (0.01 + index * 0.01).toFixed(2), replicates: 2, seed: String(20261029 + index) };
      })];
    });
  };

  const updateVariantRow = (index: number, patch: Partial<{ duration: string; replicates: number; seed: string }>) => {
    setExperimentRows(previous => previous.map((row, rowIndex) => rowIndex === index ? { ...row, ...patch } : row));
  };

  const removeVariantRow = (index: number) => {
    setExperimentRows(previous => previous.length <= 1 ? previous : previous.filter((_, rowIndex) => rowIndex !== index));
  };

  const experimentTargetStep = (project?.steps ?? []).filter(step => step.enabled)
    .find(step => step.id === experimentStepId) ?? null;

  const enabledProjectSteps = useMemo(
    () => (project?.steps ?? []).filter(step => step.enabled),
    [project?.steps],
  );

  /**
   * Inline DOE validation: a repeated `(stepId, parameter)` pair is a hard 422
   * from the API, so it is surfaced on the factor row and blocks the button.
   */
  const doeFactorErrors = useMemo(() => {
    const seen = new Map<string, number>();
    return doeFactors.map((row, index) => {
      if (!row.stepId) return '请选择该因子作用的步骤';
      const key = `${row.stepId}/${row.parameter}`;
      const first = seen.get(key);
      if (first !== undefined) return `与因子 ${first + 1} 重复：同一 (步骤, 参数) 只能配置一次`;
      seen.set(key, index);
      const values = row.values.split(',').map(value => Number(value.trim())).filter(value => Number.isFinite(value));
      if (values.length < 2 || values.length > 10) return '取值需要 2–10 个数字（逗号分隔）';
      return '';
    });
  }, [doeFactors]);
  const doeBlocked = doeFactorErrors.some(Boolean);

  const addDoeFactor = () => {
    setDoeFactors(previous => {
      const used = new Set(previous.map(row => `${row.stepId}/${row.parameter}`));
      for (const step of enabledProjectSteps) {
        for (const parameter of DOE_PARAMETERS) {
          if (!used.has(`${step.id}/${parameter.id}`)) {
            return [...previous, {
              stepId: step.id,
              parameter: parameter.id,
              values: DOE_DEFAULT_VALUES[parameter.id] ?? '0.01,0.02',
            }];
          }
        }
      }
      const fallback = enabledProjectSteps[0]?.id ?? '';
      return [...previous, { stepId: fallback, parameter: 'duration', values: DOE_DEFAULT_VALUES.duration }];
    });
  };

  const defaultExperimentName = (): string => {
    const stamp = new Date().toTimeString().slice(0, 5);
    const base = `${project?.name ?? compile?.projectName ?? '工程'} · 实验 · ${stamp}`;
    const names = new Set(experimentList.map(row => row.name));
    if (!names.has(base)) return base;
    let suffix = 2;
    while (names.has(`${base} · ${suffix}`)) suffix += 1;
    return `${base} · ${suffix}`;
  };

  const createPersistentExperiment = () => run('experiment-create', async () => {
    if (!project || !compile || !activeRecipeId) throw new Error('先在 03 完成一次测量以固定配方');
    if (compileStale) throw new Error('流程已修改，请先在 02 重新编译');
    if (activeRecipeXOutOfDomain || recipeXOutOfDomain) throw new Error(`配方 X 超出计算域（${recipeDomainText}），请先在 03 调整 X 并重新测量`);
    const target = experimentTargetStep;
    if (!target) throw new Error('流程没有可调步骤');
    if (experimentRows.length < 1 || experimentRows.length > 16) throw new Error('变体数必须在 1–16 之间');
    const variants = experimentRows.map((row, index) => {
      const duration = Number(row.duration);
      if (!Number.isFinite(duration) || duration <= 0) throw new Error(`变体 ${index + 1} 的 duration 必须是正数（s）`);
      const replicates = Math.round(Number(row.replicates));
      if (!Number.isFinite(replicates) || replicates < 1 || replicates > 8) throw new Error(`变体 ${index + 1} 的重复数必须在 1–8 之间`);
      const seed = Number(row.seed);
      if (!Number.isFinite(seed)) throw new Error(`变体 ${index + 1} 的 seed 必须是整数`);
      return { parameters: { [target.id]: { duration } }, replicates, seed: Math.round(seed) };
    });
    const name = experimentName.trim() || defaultExperimentName();
    const doc = await postJson<ExperimentDoc>('/api/v1/workflow/experiments', {
      name, project, flowId: compile.flowId, compileId: compile.compileId,
      recipeId: activeRecipeId, recipeVersion: activeRecipeVersion, variants,
      maxAttempts: Math.min(4, Math.max(1, Math.round(experimentMaxAttempts) || 1)),
      costPerRunUsd: Number(experimentCost) || 0,
    });
    setExperimentDoc(doc);
    setExperimentTable(null);
    setComparison(null);
    setCandidateViews({});
    setCandidateViewErrors({});
    setCompareCoverage(null);
    setComparePick(null);
    setAutoAnalysisId('');
    setShowAllCandidates(false);
    setExperimentInputsOpen(false);
    recordExperiment(doc.experimentId);
    await refreshExperimentList();
  });

  const runPersistentExperiment = () => run('experiment', async () => {
    if (!experimentDoc) throw new Error('先创建实验');
    const experimentId = experimentDoc.experimentId;
    await postJson(`/api/v1/workflow/experiments/${experimentId}/run`, {});
    let finished: ExperimentDoc | null = null;
    for (let index = 0; index < 900; index += 1) {
      const doc = await api<ExperimentDoc>(`/api/v1/workflow/experiments/${experimentId}`);
      setExperimentDoc(doc);
      if (['completed', 'failed'].includes(doc.status)) { finished = doc; break; }
      await new Promise(resolve => window.setTimeout(resolve, 500));
    }
    setExperimentInputsOpen(false);
    await refreshExperimentList();
    if (finished) {
      setExperimentTable(null);
      setAutoAnalysisId('');
      autoAnalysisRef.current = '';
    }
  });

  const openExperiment = (experimentId: string) => run('experiment-open', async () => {
    const doc = await api<ExperimentDoc>(`/api/v1/workflow/experiments/${experimentId}`);
    setExperimentDoc(doc);
    setExperimentTable(null);
    setComparison(null);
    setCandidateViews({});
    setCandidateViewErrors({});
    setCompareCoverage(null);
    setComparePick(null);
    setShowAllCandidates(false);
    setAutoAnalysisId('');
    autoAnalysisRef.current = '';
    setExperimentInputsOpen(!['completed', 'failed'].includes(doc.status));
    navigateZone('experiment');
  });

  const deleteExperiment = (row: ExperimentSummaryRow) => run('experiment-delete', async () => {
    if (['running', 'queued'].includes(row.status)) throw new Error('实验运行中，先取消');
    await api(`/api/v1/workflow/experiments/${row.experimentId}`, { method: 'DELETE' });
    setExperimentDeleteArmed('');
    setProjectState(previous => ({
      ...previous,
      experimentIds: previous.experimentIds.filter(id => id !== row.experimentId),
    }));
    if (experimentDoc?.experimentId === row.experimentId) {
      setExperimentDoc(null);
      setExperimentTable(null);
      setComparison(null);
      setCandidateViews({});
      setCandidateViewErrors({});
      setCompareCoverage(null);
      setComparePick(null);
      setAutoAnalysisId('');
      autoAnalysisRef.current = '';
    }
    await refreshExperimentList();
  });

  const refreshLayouts = async () => {
    const data = await api<{ layouts: Array<{ layoutId: string; fileName: string; structureCount: number }> }>('/api/v1/workflow/layouts');
    setLayouts(data.layouts);
    return data.layouts;
  };

  const DEFAULT_LAYER_HEIGHT = { baseHeight: '0', height: '0.2' };

  /** The Clip editor follows the dimension of the project (or new-project draft). */
  const clipDimension = (): 2 | 3 => (
    setupMode === 'new' ? draft.dimension : (project?.dimension === 3 ? 3 : 2)
  );

  /** Grid used to validate 3D layer thickness before the Clip is saved. */
  const clipGridDelta = (): number => {
    if (setupMode === 'new') return Number(draft.gridDelta) || 0.2;
    const domain = (project?.domain ?? {}) as Record<string, unknown>;
    return Number(domain.gridDelta) || 0.2;
  };

  const selectStructure = (layout: LayoutDetail, name: string) => {
    const structure = layout.structures.find(item => item.name === name);
    setLayoutStructure(name);
    const pairs = (structure?.layerDatatypes ?? []).map(([layer, datatype]) => `${layer}:${datatype}`);
    setSelectedPairs(pairs);
    setLayerMaterials(previous => Object.fromEntries(pairs.map(key => [key, previous[key] ?? 'Si'])));
    setLayerHeights(previous => Object.fromEntries(pairs.map(key => [key, previous[key] ?? DEFAULT_LAYER_HEIGHT])));
    setClipNotice('');
    if (structure?.bounds) {
      setClipBounds({ ...structure.bounds });
      setCanvas(null);
    }
  };

  const clipLayers = () => selectedPairs.map(key => {
    const [layer, datatype] = key.split(':').map(Number);
    const base = { layer, datatype, material: layerMaterials[key] || 'Si' };
    if (clipDimension() !== 3) return base;
    const heights = layerHeights[key] ?? DEFAULT_LAYER_HEIGHT;
    return { ...base, baseHeight: Number(heights.baseHeight), height: Number(heights.height) };
  });

  /** 3D layer heights must be finite and at least two grid intervals thick. */
  const validateClipLayerHeights = (layers: Array<{ layer: number; baseHeight?: number; height?: number }>) => {
    if (clipDimension() !== 3) return;
    const grid = clipGridDelta();
    const minimum = 2 * grid;
    for (const row of layers) {
      const baseHeight = Number(row.baseHeight);
      const height = Number(row.height);
      if (!Number.isFinite(baseHeight) || !Number.isFinite(height)) {
        throw new Error(`图层 L${row.layer} 的 baseHeight/height 必须是有限数值`);
      }
      if (height <= 0) throw new Error(`图层 L${row.layer} 的厚度必须大于 0 µm`);
      if (height < minimum) {
        throw new Error(`图层 L${row.layer} 的厚度 ${height} µm 小于两个网格步长（${minimum} µm，gridDelta=${grid}）：请增大厚度或减小 gridDelta`);
      }
    }
  };

  const registerLayout = () => run('layout', async () => {
    if (!gds) throw new Error('先上传 GDS');
    const layout = await postJson<LayoutDetail>('/api/v1/workflow/layouts', {
      assetId: gds.assetId, fileName: gds.fileName,
    });
    setActiveLayout(layout);
    const first = layout.structures[0];
    if (first) selectStructure(layout, first.name);
    await refreshLayouts();
    const listed = await api<{ clips: Clip[] }>(`/api/v1/workflow/clips?layout_id=${layout.layoutId}`);
    setClips(listed.clips);
  });

  const chooseLayout = async (layoutId: string) => {
    if (!layoutId) { setActiveLayout(null); setActiveClip(null); return; }
    const layout = await api<LayoutDetail>(`/api/v1/workflow/layouts/${layoutId}`);
    setActiveLayout(layout);
    setActiveClip(null);
    const first = layout.structures[0];
    if (first) selectStructure(layout, first.name);
    const listed = await api<{ clips: Clip[] }>(`/api/v1/workflow/clips?layout_id=${layoutId}`);
    setClips(listed.clips);
  };

  const previewClip = () => run('preview', async () => {
    if (!activeLayout) throw new Error('先注册 Layout');
    const layers = clipLayers();
    if (!layers.length) throw new Error('至少选择一个层');
    const preview = await postJson<ClipCanvas>(`/api/v1/workflow/layouts/${activeLayout.layoutId}/preview`, {
      structure: layoutStructure, layers, bbox: clipBounds,
    });
    setCanvas(preview);
    setActiveClip(null);
    setClipNotice('');
  });

  const sameClipContent = (
    clip: Clip,
    layers: Array<{ layer: number; datatype: number | null; material: string; baseHeight?: number; height?: number }>,
  ) => {
    if (clip.layoutId !== activeLayout?.layoutId) return false;
    if (clip.structure !== layoutStructure) return false;
    if (['x0', 'y0', 'x1', 'y1'].some(key => Math.abs(Number(clip.bbox[key as keyof Clip['bbox']]) - clipBounds[key as keyof typeof clipBounds]) > 1e-9)) return false;
    if (clip.layers.length !== layers.length) return false;
    return clip.layers.every((item, index) => item.layer === layers[index].layer
      && item.datatype === layers[index].datatype
      && item.material === layers[index].material
      && (item.baseHeight ?? null) === (layers[index].baseHeight ?? null)
      && (item.height ?? null) === (layers[index].height ?? null));
  };

  const saveClip = () => run('clip', async () => {
    if (!activeLayout) throw new Error('先注册 Layout');
    const layers = clipLayers();
    if (!layers.length) throw new Error('至少选择一个层');
    validateClipLayerHeights(layers);
    const name = (clipName || `clip ${clips.length + 1}`).trim();
    const existing = clips.filter(item => item.name === name);
    const duplicate = existing.find(item => sameClipContent(item, layers));
    if (duplicate) {
      setActiveClip(duplicate);
      setCanvas(duplicate);
      setClipNotice(`已存在同名同内容的 Clip「${name}」，已直接选中它，未重复创建。`);
      return;
    }
    if (existing.length) {
      setClipNotice('');
      throw new Error(`已存在同名 Clip「${name}」但内容不同；请修改 Clip 名称后再保存，避免两个同名条目。`);
    }
    const annotations = annotationText.trim()
      ? [{ text: annotationText.trim(), x: Number(annotationX) || 0, y: Number(annotationY) || 0 }]
      : [];
    const clip = await postJson<Clip>('/api/v1/workflow/clips', {
      layoutId: activeLayout.layoutId, name,
      structure: layoutStructure, layers, bbox: clipBounds, annotations,
    });
    setClips(previous => [clip, ...previous.filter(item => item.name !== clip.name)]);
    setActiveClip(clip);
    setCanvas(clip);
    setAnnotationText('');
    setClipNotice(`已保存 Clip「${clip.name}」。`);
  });

  const selectClip = (clip: Clip) => {
    // `selectStructure` clears the preview canvas; select the structure first so
    // the Clip (with its annotations) is the last thing that sets the canvas.
    if (activeLayout) selectStructure(activeLayout, clip.structure);
    setActiveClip(clip);
    setCanvas(clip);
    setClipName(clip.name);
    setClipNotice('');
    setClipBounds({ ...clip.bbox });
    setLayerMaterials(previous => Object.fromEntries(clip.layers.map(item => [`${item.layer}:${item.datatype ?? 0}`, item.material || 'Si'])
      .map(([key, value]) => [key, previous[key] ?? value])));
    setLayerHeights(previous => Object.fromEntries(clip.layers.map(item => {
      const key = `${item.layer}:${item.datatype ?? 0}`;
      const fallback = previous[key] ?? DEFAULT_LAYER_HEIGHT;
      return [key, {
        baseHeight: String(item.baseHeight ?? fallback.baseHeight),
        height: String(item.height ?? fallback.height),
      }];
    })));
  };

  /** 01 project tab: selecting a clip only binds the source; save writes geometry. */
  const chooseClipForProject = (clip: Clip) => {
    selectClip(clip);
    setProjectGeometrySource({ kind: 'layout', clipId: clip.clipId, clipName: clip.name, layoutId: clip.layoutId, clipHash: clip.clipHash });
    setCompileStale(true);
  };

  /** Reopened GDS project: show the bound clip as the active one, not just its name. */
  const activateClipBinding = async (binding: ProjectGeometrySource | null) => {
    if (!binding) return;
    setProjectGeometrySource(binding);
    if (!binding.clipId) return;
    try {
      const clip = await api<Clip>(`/api/v1/workflow/clips/${binding.clipId}`);
      selectClip(clip);
    } catch { /* the header still shows the bound clip name */ }
  };

  // Entering the GDS / Layout form must list registered Layouts immediately,
  // not only after an upload or after opening the 版图/Clip page.
  useEffect(() => {
    const layoutFormVisible = setupMode === 'layout' || (setupMode === 'new' && draft.source === 'layout');
    if (!layoutFormVisible) return;
    void refreshLayouts().catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setupMode, draft.source]);

  // The 01 GDS tab must list saved layouts/clips without requiring a fresh upload.
  useEffect(() => {
    if (!project || projectGeometrySource.kind !== 'layout') return;
    void (async () => {
      try {
        const [clipList, layoutList] = await Promise.all([
          api<{ clips: Clip[] }>('/api/v1/workflow/clips'),
          api<{ layouts: Array<{ layoutId: string; fileName: string; structureCount: number }> }>('/api/v1/workflow/layouts'),
        ]);
        setClips(clipList.clips);
        setLayouts(layoutList.layouts);
        const layoutId = projectGeometrySource.layoutId
          ?? clipList.clips.find(item => item.clipId === projectGeometrySource.clipId)?.layoutId;
        if (layoutId && !activeLayout) {
          const layout = await api<LayoutDetail>(`/api/v1/workflow/layouts/${layoutId}`);
          setActiveLayout(layout);
          const bound = clipList.clips.find(item => item.clipId === projectGeometrySource.clipId);
          if (bound) selectStructure(layout, bound.structure);
        }
      } catch { /* the tab still works with the upload path */ }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.name, projectGeometrySource.kind, projectGeometrySource.clipId]);

  const addAnnotation = () => run('annotation', async () => {
    if (!activeClip) throw new Error('先选择 Clip');
    if (!annotationText.trim()) throw new Error('填写标注文本');
    const updated = await postJson<Clip>(`/api/v1/workflow/clips/${activeClip.clipId}/annotations`, {
      text: annotationText.trim(), x: Number(annotationX) || 0, y: Number(annotationY) || 0,
    });
    setActiveClip(updated);
    setCanvas(updated);
    setClips(previous => previous.map(item => item.clipId === updated.clipId ? updated : item));
    setAnnotationText('');
  });

  const useClipAsGeometry = (clip: Clip, compileNow: boolean) => run('clip-use', async () => {
    if (!project) throw new Error('先创建或导入工程');
    const dimension = project.dimension ?? 2;
    if (dimension === 3) {
      const missing = clip.layers.find(item => item.baseHeight == null || item.height == null);
      if (missing) throw new Error(`Clip 的图层 L${missing.layer} 缺少 baseHeight/height：请在上方为每层填写层高并重新保存 Clip`);
    }
    setProjectGeometrySource({
      kind: 'layout', clipId: clip.clipId, clipName: clip.name,
      layoutId: clip.layoutId, clipHash: clip.clipHash,
    });
    const geometry = clipGeometryBinding(clip, dimension);
    const nextProject: ProjectDoc = { ...project, geometry };
    setProject(nextProject);
    if (!compileNow) {
      setCompileStale(true);
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  工程几何已切换为 Clip ${clip.name}`]);
      return;
    }
    if (!(nextProject.steps ?? []).length) throw new Error('流程还没有步骤，先在 02 插入或添加步骤');
    const result = await postJson<CompileInfo>(`/api/v1/workflow/clips/${clip.clipId}/compile`, {
      project: nextProject, flowId: compile?.flowId,
    });
    if (compile) setPreviousCompile(compile);
    setCompile(result); setCompileStale(false); setRoute(null); setSegment(null); setMeasurement(null); setProvenance(null);
    const authored = await normalizeProjectForBasis(nextProject);
    const compiledProject: ProjectDoc = {
      ...authored,
      steps: (result.authoredSteps ?? authored.steps ?? []) as ProjectStep[],
    };
    setProject(compiledProject);
    recordCompile(result.compileId, executionCompileBasis(compiledProject));
  });

  const parseJsonObject = (label: string, text: string): Record<string, unknown> => {
    let parsed: unknown;
    try { parsed = JSON.parse(text); } catch { throw new Error(`${label} 不是合法 JSON`); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${label} 必须是 JSON 对象`);
    return parsed as Record<string, unknown>;
  };

  const definitionIdFromJson = useMemo(() => {
    try {
      const parsed = JSON.parse(definitionJson) as { definitionId?: unknown };
      return typeof parsed.definitionId === 'string' ? parsed.definitionId : '';
    } catch { return ''; }
  }, [definitionJson]);
  const activeDefinitionId = savedDefinitionId || definitionIdFromJson;

  const readDefinitionVariables = () => {
    const parsed = parseJsonObject('流程变量 JSON', definitionVariables);
    for (const [name, value] of Object.entries(parsed)) {
      const kind = typeof value;
      if (value === null || !['string', 'number', 'boolean'].includes(kind)) throw new Error(`变量 ${name} 必须是标量`);
    }
    return parsed as Record<string, string | number | boolean>;
  };

  const saveModule = () => run('module', async () => {
    const payload = parseJsonObject('模块 JSON', moduleJson);
    const saved = await postJson<{ moduleId: string }>('/api/v1/workflow/modules', payload);
    setModuleStatus(`模块 ${saved.moduleId} 已保存`);
    setExpansion(null);
  });

  const saveDefinition = () => run('definition', async () => {
    const payload = parseJsonObject('流程定义 JSON', definitionJson);
    const saved = await postJson<{ definitionId: string }>('/api/v1/workflow/flows/definitions', payload);
    setSavedDefinitionId(saved.definitionId);
    setModuleStatus(`流程定义 ${saved.definitionId} 已保存`);
    setExpansion(null);
  });

  const expandDefinition = () => run('expand', async () => {
    if (!activeDefinitionId) throw new Error('流程定义 JSON 缺少 definitionId');
    const result = await postJson<ModuleExpansion>(
      `/api/v1/workflow/flows/definitions/${activeDefinitionId}/expand`,
      { variables: readDefinitionVariables() },
    );
    setExpansion(result);
    setModuleStatus(`展开 ${result.stepCount} 步 · 深度 ${result.maxDepth}`);
  });

  const compileDefinition = () => run('definition-compile', async () => {
    if (!activeDefinitionId) throw new Error('流程定义 JSON 缺少 definitionId');
    if (!project) throw new Error('先导入工程 JSON');
    const result = await postJson<CompileInfo>(
      `/api/v1/workflow/flows/definitions/${activeDefinitionId}/compile`,
      { project, variables: readDefinitionVariables() },
    );
    if (compile) setPreviousCompile(compile);
    setCompile(result); setCompileStale(false); setRoute(null); setSegment(null); setMeasurement(null); setProvenance(null);
    // A definition Compile expands the definition into the Compile; it does
    // not rewrite the authored project, so the basis is that unchanged
    // (normalized) project — never compile.project's expanded steps.
    const authored = await normalizeProjectForBasis(project);
    recordCompile(result.compileId, executionCompileBasis(authored));
    setModuleStatus(`模块化流程已编译 · ${result.stepCount} 步`);
  });

  const refreshLibraryBundles = async () => {
    const data = await api<{ bundles: LibraryBundleSummary[] }>('/api/v1/workflow/library/bundles');
    setLibraryBundles(data.bundles);
    return data.bundles;
  };

  const describeLibraryResult = (result: LibraryImportResult) => {
    const parts = [
      `${result.modules.filter(item => item.status === 'imported').length} 模块修订`,
      `${result.definitionIds.length} 定义`,
      `${result.templateIds.length} 模板`,
      `${result.experimentIds.length} 实验模板`,
    ];
    const skipped = result.skipped.length ? ` · 跳过 ${result.skipped.length}` : '';
    const renamed = result.renames.length ? ` · 新 ID ${result.renames.length}` : '';
    return `导入 ${result.bundleId} (${result.policy}): ${parts.join(' / ')}${skipped}${renamed}`;
  };

  const exportLibrary = () => run('library', async () => {
    if (!libraryName.trim()) throw new Error('先填写共享库名称');
    const bundle = await postJson<LibraryBundle>('/api/v1/workflow/library/export', {
      name: libraryName.trim(),
      description: libraryDescription,
      experimentIds: experimentDoc ? [experimentDoc.experimentId] : [],
    });
    setLibraryBundleJson(JSON.stringify(bundle, null, 2));
    setLibraryBundleId(bundle.bundleId);
    setLibraryResult(null);
    setLibraryStatus(
      `已导出 ${bundle.bundleId} · ${bundle.modules.length} 模块修订 / ${bundle.definitions.length} 定义 / ${bundle.templates.length} 模板 / ${bundle.experimentTemplates.length} 实验模板`,
    );
    await refreshLibraryBundles();
  });

  const importLibraryBundle = () => run('library', async () => {
    if (!libraryBundleId) throw new Error('先选择共享库 bundle');
    const result = await postJson<LibraryImportResult>(
      `/api/v1/workflow/library/bundles/${libraryBundleId}/import`,
      { policy: libraryPolicy },
    );
    setLibraryResult(result);
    setLibraryStatus(describeLibraryResult(result));
    await refreshLibraryBundles();
  });

  const importLibraryJson = () => run('library', async () => {
    const bundle = parseJsonObject('共享库 JSON', libraryBundleJson);
    const result = await postJson<LibraryImportResult>('/api/v1/workflow/library/import', {
      bundle, policy: libraryPolicy,
    });
    setLibraryBundleId(result.bundleId);
    setLibraryResult(result);
    setLibraryStatus(describeLibraryResult(result));
    await refreshLibraryBundles();
  });

  const refreshLibraryItems = async (category?: LibraryCategory) => {
    const query = category ? `?category=${category}` : '';
    const data = await api<{ items: LibraryItem[] }>(`/api/v1/workflow/library/items${query}`);
    if (category) setManagerItems(data.items);
    else setLibraryItems(data.items);
    return data.items;
  };

  const managerDefaults = (category: LibraryCategory): Record<string, string> => {
    if (category === 'material') {
      const first = materialCatalog[0];
      return {
        name: '', catalog: first?.category ?? 'Generic', material: first?.name ?? 'Si',
        displayName: '', color: first?.color ?? '#4c80ff', role: 'film', notes: '',
      };
    }
    if (category === 'process') return {
      name: '', operation: 'etch', model: 'isotropic', rateMagnitude: '0.1', duration: '0.1',
      depositMaterial: '', stickingProbability: '1', sourceExponent: '1', notes: '',
    };
    if (category === 'flow') return { name: '', flowRef: '', notes: '' };
    return {
      name: '', recipeRef: recipes[0] ? `${recipes[0].recipeId}@${recipes[0].version}` : '',
      designKind: 'doe', variantCount: '8', replicates: '1', seed: '20261029', notes: '',
    };
  };

  const managerFormFromItem = (item: LibraryItem): Record<string, string> => {
    if (item.category === 'material') {
      return {
        name: item.name,
        catalog: String(item.payload.catalog ?? ''),
        material: String(item.payload.material ?? ''),
        displayName: String(item.payload.displayName ?? ''),
        color: String(item.payload.color ?? '#4c80ff'),
        role: MATERIAL_ROLES.some(([value]) => value === String(item.payload.role ?? ''))
          ? String(item.payload.role)
          : 'film',
        notes: String(item.payload.notes ?? ''),
      };
    }
    if (item.category === 'process') {
      return {
        name: item.name,
        operation: String(item.payload.operation ?? 'etch'),
        model: String(item.payload.model ?? 'isotropic'),
        rateMagnitude: String(item.payload.rateMagnitude ?? '0.1'),
        duration: String(item.payload.duration ?? '0.1'),
        depositMaterial: String(item.payload.depositMaterial ?? ''),
        stickingProbability: String(item.payload.stickingProbability ?? '1'),
        sourceExponent: String(item.payload.sourceExponent ?? '1'),
        notes: item.description,
      };
    }
    if (item.category === 'flow') {
      const definitionId = typeof item.payload.definitionId === 'string' ? item.payload.definitionId : '';
      const templateId = typeof item.payload.templateId === 'string' ? item.payload.templateId : '';
      return {
        name: item.name,
        flowRef: definitionId ? `definition:${definitionId}` : templateId ? `template:${templateId}` : '',
        notes: String(item.payload.notes ?? item.description),
      };
    }
    const design = (item.payload.design ?? {}) as Record<string, unknown>;
    return {
      name: item.name,
      recipeRef: `${String(item.payload.recipeId ?? '')}@${String(item.payload.recipeVersion ?? 1)}`,
      designKind: String(design.kind ?? 'doe'),
      variantCount: String(design.variantCount ?? design.variants ?? 1),
      replicates: String(design.replicates ?? 1),
      seed: String(design.seed ?? 0),
      notes: String(item.payload.notes ?? item.description),
    };
  };

  const loadManagerCategory = async (category: LibraryCategory) => {
    const requestId = managerRequest.current + 1;
    managerRequest.current = requestId;
    setManagerCategory(category);
    setManagerSelectedId('');
    setManagerForm(managerDefaults(category));
    setManagerStatus('');
    setManagerDeleteArmed(false);
    setManagerItems([]);
    setManagerLoading(true);
    setManagerError('');
    try {
      const data = await api<{ items: LibraryItem[] }>(`/api/v1/workflow/library/items?category=${category}`);
      if (managerRequest.current !== requestId) return;
      setManagerItems(data.items);
    } catch (exc) {
      if (managerRequest.current !== requestId) return;
      setManagerItems([]);
      setManagerError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (managerRequest.current === requestId) setManagerLoading(false);
    }
  };

  const openLibraryManager = (category: LibraryCategory = 'material') => {
    setZone('flow');
    setLibraryManagerOpen(true);
    void loadManagerCategory(category);
  };

  // 01–06 (top bar, menus, left tree) are the only navigation: leaving 02
  // always closes the library manager so coming back lands on the flow editor.
  const navigateZone = (target: typeof zone) => {
    setLibraryManagerOpen(false);
    setZone(target);
  };

  const selectManagerItem = (item: LibraryItem) => {
    setManagerSelectedId(item.itemId);
    setManagerForm(managerFormFromItem(item));
    setManagerStatus('');
    setManagerDeleteArmed(false);
  };

  const buildManagerPayload = (): Record<string, unknown> => {
    if (managerCategory === 'material') {
      const payload: Record<string, unknown> = {
        catalog: managerForm.catalog ?? '',
        material: managerForm.material ?? '',
      };
      if (managerForm.displayName?.trim()) payload.displayName = managerForm.displayName.trim();
      if (managerForm.color?.trim()) payload.color = managerForm.color.trim();
      if (managerForm.role?.trim()) payload.role = managerForm.role.trim();
      if (managerForm.notes?.trim()) payload.notes = managerForm.notes.trim();
      return payload;
    }
    if (managerCategory === 'process') {
      const model = managerForm.model || 'isotropic';
      const payload: Record<string, unknown> = {
        operation: managerForm.operation || 'etch',
        model,
        rateMagnitude: Number(managerForm.rateMagnitude) || 0,
        duration: Number(managerForm.duration) || 0.01,
        rateMode: 'uniform',
      };
      if ((managerForm.operation || 'etch') === 'deposit') {
        payload.depositMaterial = managerForm.depositMaterial?.trim() || 'SiO2';
      }
      if (model === 'singleParticle') {
        payload.stickingProbability = Number(managerForm.stickingProbability) || 0;
        payload.sourceExponent = Number(managerForm.sourceExponent) || 0;
      }
      if (model === 'directional') {
        payload.direction = [0, 0, -1];
        payload.directionalRateMagnitude = Number(managerForm.rateMagnitude) || 0;
        payload.isotropicRateMagnitude = 0;
        payload.calculateVisibility = true;
        delete payload.rateMagnitude;
        delete payload.rateMode;
      }
      return payload;
    }
    if (managerCategory === 'flow') {
      const payload: Record<string, unknown> = { name: managerForm.name };
      const [kind, value] = (managerForm.flowRef ?? '').split(':');
      if (kind === 'definition' && value) payload.definitionId = value;
      else if (kind === 'template' && value) payload.templateId = value;
      else throw new Error('选择要引用的流程定义或模板');
      if (managerForm.notes?.trim()) payload.notes = managerForm.notes.trim();
      return payload;
    }
    const [recipeId, versionText] = (managerForm.recipeRef ?? '').split('@');
    if (!recipeId) throw new Error('选择要引用的测量配方');
    const payload: Record<string, unknown> = {
      recipeId,
      recipeVersion: Number(versionText) || 1,
      design: {
        kind: managerForm.designKind || 'doe',
        variantCount: Math.max(1, Number(managerForm.variantCount) || 1),
        replicates: Math.max(1, Number(managerForm.replicates) || 1),
        seed: Number(managerForm.seed) || 0,
      },
    };
    if (managerForm.notes?.trim()) payload.notes = managerForm.notes.trim();
    return payload;
  };

  const saveManagerItem = () => run('library-save', async () => {
    const name = (managerForm.name ?? '').trim();
    if (!name) throw new Error('填写条目名称');
    const payload = buildManagerPayload();
    if (managerSelectedId) {
      await api<LibraryItem>(`/api/v1/workflow/library/items/${managerSelectedId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description: managerForm.notes ?? '', payload }),
      });
      setManagerStatus(`已保存 ${name}`);
    } else {
      const created = await postJson<LibraryItem>('/api/v1/workflow/library/items', {
        category: managerCategory, name, description: managerForm.notes ?? '', payload,
      });
      setManagerSelectedId(created.itemId);
      setManagerStatus(`已新增 ${name}`);
    }
    await refreshLibraryItems(managerCategory);
    await refreshLibraryItems();
  });

  const deleteManagerItem = () => run('library-delete', async () => {
    if (!managerSelectedId) throw new Error('先选择条目');
    const target = managerItems.find(item => item.itemId === managerSelectedId);
    await api(`/api/v1/workflow/library/items/${managerSelectedId}`, { method: 'DELETE' });
    setManagerSelectedId('');
    setManagerForm(managerDefaults(managerCategory));
    setManagerStatus(`已删除 ${target?.name ?? managerSelectedId}`);
    setManagerDeleteArmed(false);
    await refreshLibraryItems(managerCategory);
    await refreshLibraryItems();
  });

  const updateProjectMaterial = (key: 'substrateMaterial' | 'maskMaterial', value: string) => {
    setProject(previous => {
      if (!previous) return previous;
      const geometry = { ...(previous.geometry ?? {}) } as Record<string, unknown>;
      if (geometry.kind === 'gds') return previous;
      // A plane has no mask layer; writing maskMaterial would be rejected by ProjectV7.
      if (geometry.kind === 'plane' && key === 'maskMaterial') return previous;
      geometry[key] = value;
      return { ...previous, geometry };
    });
    setCompileStale(true);
  };

  /** 01 editor: domain / geometry / solver fields write straight into ProjectV7. */
  const updateProjectDomain = (patch: Record<string, unknown>) => {
    setProject(previous => previous
      ? { ...previous, domain: { ...(previous.domain ?? {}), ...patch } }
      : previous);
    setCompileStale(true);
  };

  const updateProjectSolver = (patch: Record<string, unknown>) => {
    setProject(previous => {
      if (!previous) return previous;
      const solver = { ...(previous.solver ?? {}) } as Record<string, unknown>;
      const random = { ...((solver.random ?? {}) as Record<string, unknown>) };
      const { seed, ...rest } = patch;
      if (seed !== undefined) {
        random.mode = 'fixed';
        random.seed = seed;
      }
      return { ...previous, solver: { ...solver, ...rest, random } };
    });
    setCompileStale(true);
  };

  const updateProjectGeometryField = (patch: Record<string, unknown>) => {
    setProject(previous => {
      if (!previous) return previous;
      const geometry = { ...(previous.geometry ?? {}) } as Record<string, unknown>;
      if (geometry.kind === 'gds') return previous;
      const next = { ...geometry, ...patch };
      parametricGeometryRef.current = next;
      return { ...previous, geometry: next };
    });
    setCompileStale(true);
  };

  const changeProjectGeometryKind = (kind: string) => {
    setProject(previous => {
      if (!previous) return previous;
      const geometry = defaultParametricGeometry(previous.dimension ?? 2, kind, (previous.geometry ?? {}) as Record<string, unknown>);
      parametricGeometryRef.current = geometry;
      return { ...previous, geometry };
    });
    setCompileStale(true);
  };

  /** Switch 01 back to parametric geometry and drop the Clip-only fields. */
  const chooseParametricGeometry = () => {
    setProject(previous => {
      if (!previous) return previous;
      const current = (previous.geometry ?? {}) as Record<string, unknown>;
      if (current.kind !== 'gds') {
        parametricGeometryRef.current = current;
        return previous;
      }
      const fallback = parametricGeometryRef.current
        ?? defaultParametricGeometry(previous.dimension ?? 2, previous.dimension === 3 ? 'hole' : 'plane', {});
      return { ...previous, geometry: fallback };
    });
    setProjectGeometrySource({ kind: 'parametric' });
    setCompileStale(true);
  };

  const chooseLayoutGeometry = () => {
    if (!project) return;
    const clip = activeClip ?? clips.find(item => item.clipId === projectGeometrySource.clipId) ?? null;
    setProjectGeometrySource(clip
      ? { kind: 'layout', clipId: clip.clipId, clipName: clip.name, layoutId: clip.layoutId, clipHash: clip.clipHash }
      : { kind: 'layout', clipName: projectGeometrySource.clipName });
    setCompileStale(true);
  };

  /** 2D ↔ 3D switch: domain, geometry and view fields must move together. */
  const changeProjectDimension = (dimension: 2 | 3) => {
    if (!project || project.dimension === dimension) return;
    setProject(previous => {
      if (!previous) return previous;
      const domain = (previous.domain ?? {}) as Record<string, unknown>;
      const gridDelta = Math.min(0.5, Math.max(0.01, Number(domain.gridDelta) || 0.2));
      const xExtent = Math.max(1, Number(domain.xExtent) || 4);
      const yExtent = Math.max(2 * gridDelta, Number(domain.yExtent) || xExtent);
      const boundary = String(domain.xBoundary ?? domain.boundaryXY ?? 'reflective');
      const currentGeometry = (previous.geometry ?? {}) as Record<string, unknown>;
      const geometry = dimension === 3 && currentGeometry.kind === 'gds'
        ? defaultParametricGeometry(3, 'hole', {})
        : currentGeometry;
      if (geometry.kind !== 'gds') parametricGeometryRef.current = geometry;
      return {
        ...previous,
        dimension,
        domain: dimension === 3
          ? { gridDelta, xExtent, yExtent, boundaryXY: boundary === 'periodic' ? 'periodic' : 'reflective' }
          : { gridDelta, xExtent, xBoundary: boundary === 'periodic' ? 'periodic' : 'reflective' },
        geometry,
        view: dimension === 2
          ? (previous.view ?? { panX: 0, panH: 0, zoom: 1, showGrid: true })
          : undefined,
        view3D: dimension === 3
          ? (previous.view3D ?? {
            projection: 'parallel', camera: null, displayMode: 'surface',
            visibleMaterialIds: [], opacityByMaterial: {},
            clip: { enabled: false, axis: 'x', position: 0.0, keep: 'positive' },
            section: { enabled: false, axis: 'y', position: 0.0 },
          })
          : undefined,
      };
    });
    if (dimension === 3) setProjectGeometrySource({ kind: 'parametric' });
    setCompileStale(true);
  };

  const changeProjectUnits = (target: LengthUnit) => {
    if (!project || projectUnits === target) return;
    const result = convertProjectUnits(project as Record<string, unknown>, target);
    if (result.blockedReason) {
      setProjectNotice(result.blockedReason);
      return;
    }
    setProject(result.project as typeof project);
    setCompileStale(true);
    setProjectNotice(`长度单位已切换为 ${lengthUnitLabel(target)}；所有长度与速率已按比例换算。`);
  };

  const applyMaterialToProject = (material: string): string => {
    if (!project) return '';
    const geometry = { ...(project.geometry ?? {}) } as Record<string, unknown>;
    if (geometry.kind === 'gds' && Array.isArray(geometry.layers) && geometry.layers.length) {
      setProject(previous => {
        if (!previous) return previous;
        const next = { ...(previous.geometry ?? {}) } as Record<string, unknown>;
        next.layers = (next.layers as Array<Record<string, unknown>>).map((row, index) => index === 0 ? { ...row, material } : row);
        return { ...previous, geometry: next };
      });
      setCompileStale(true);
      return 'GDS 首层材料';
    }
    updateProjectMaterial('substrateMaterial', material);
    return '工程基底材料';
  };

  const commitSteps = (updater: (steps: ProjectStep[]) => ProjectStep[]) => {
    setProject(previous => previous ? { ...previous, steps: updater(previous.steps ?? []) } : previous);
    setCompileStale(true);
  };

  const updateSelectedStep = (patch: Record<string, unknown>) => {
    if (!selectedFlowStepId) return;
    const materialNames = depositableMaterialNames(materialCatalog);
    commitSteps(steps => steps.map(step => (step.id === selectedFlowStepId
      ? sanitizeProjectStep({ ...step, ...patch }, materialNames)
      : step)));
  };

  /**
   * Mutating variant for the truly-exclusive modes (planarize cutoff vs
   * stopMaterial, directional scalar vs rateSets): `delete` removes a key
   * instead of leaving an `undefined` that still serializes into the step.
   */
  const updateSelectedStepWith = (mutate: (draft: ProjectStep) => void) => {
    if (!selectedFlowStepId) return;
    const materialNames = depositableMaterialNames(materialCatalog);
    commitSteps(steps => steps.map(step => {
      if (step.id !== selectedFlowStepId) return step;
      const draft: ProjectStep = { ...step };
      mutate(draft);
      return sanitizeProjectStep(draft, materialNames);
    }));
  };

  /**
   * Merge one gdsMask key. The required defaults make the first edit of a step
   * without a mask produce a complete, backend-valid GdsBooleanMaskV7 instead
   * of a partial object that the strict model rejects.
   */
  const patchSelectedGdsMask = (patch: Record<string, unknown>) => {
    updateSelectedStep({
      gdsMask: {
        layer: gdsMaskNumber('layer', 0),
        datatype: gdsMaskNumber('datatype', 0),
        unitScale: gdsMaskNumber('unitScale', 1),
        position: [...gdsMaskPosition],
        baseHeight: gdsMaskNumber('baseHeight', 0),
        height: gdsMaskNumber('height', 0.1),
        retainedRegion: gdsMaskRetainedRegion,
        ...selectedGdsMask,
        ...patch,
      },
    });
  };

  const patchSelectedGdsMaskPosition = (axis: 0 | 1 | 2, value: number) => {
    const position: [number, number, number] = [gdsMaskPosition[0], gdsMaskPosition[1], gdsMaskPosition[2]];
    position[axis] = value;
    patchSelectedGdsMask({ position });
  };

  const patchSelectedDirection = (axis: 0 | 1 | 2, value: number) => {
    const direction: [number, number, number] = [selectedDirection[0], selectedDirection[1], selectedDirection[2]];
    direction[axis] = value;
    updateSelectedStep({
      direction,
      // Scalar directional steps require all three fields; editing a direction
      // of a hand-written step must not leave isotropicRateMagnitude absent.
      isotropicRateMagnitude: typeof selectedFlowStep?.isotropicRateMagnitude === 'number'
        ? selectedFlowStep.isotropicRateMagnitude : 0,
    });
  };

  const switchPlanarizeMode = (mode: string) => {
    updateSelectedStepWith(draft => {
      if (mode === 'stopMaterial') {
        delete draft.cutoffHeight;
        if (typeof draft.stopMaterial !== 'string' || !draft.stopMaterial) {
          draft.stopMaterial = materialCatalog.find(row => row.name === 'SiO2')?.name
            ?? materialCatalog[0]?.name ?? 'SiO2';
        }
        if (typeof draft.overPolish !== 'number') draft.overPolish = 0;
      } else {
        delete draft.stopMaterial;
        delete draft.overPolish;
        if (typeof draft.cutoffHeight !== 'number') draft.cutoffHeight = 0;
      }
    });
  };

  const switchDirectionalMode = (mode: string) => {
    if (mode === 'materialRates') {
      // Seed the default pair from the current scalar rates (or the previously
      // configured default), then let the sanitizer drop scalars/rateSets.
      const nextDefault: DirectionalMaterialRate = {
        directional: typeof selectedFlowStep?.directionalRateMagnitude === 'number'
          ? selectedFlowStep.directionalRateMagnitude
          : selectedDefaultMaterialRate.directional || 0.02,
        isotropic: typeof selectedFlowStep?.isotropicRateMagnitude === 'number'
          ? selectedFlowStep.isotropicRateMagnitude
          : selectedDefaultMaterialRate.isotropic,
      };
      updateSelectedStepWith(draft => {
        delete draft.rateSets;
        delete draft.rateMagnitude;
        delete draft.directionalRateMagnitude;
        delete draft.isotropicRateMagnitude;
        delete draft.calculateVisibility;
        if (!isUsableDirection(draft.direction)) draft.direction = [selectedDirection[0], selectedDirection[1], selectedDirection[2]];
        if (!draft.materialRates || typeof draft.materialRates !== 'object' || Array.isArray(draft.materialRates)) {
          draft.materialRates = {};
        }
        draft.defaultMaterialRate = nextDefault;
      });
      return;
    }
    if (mode === 'rateSets') {
      const rateSets = [{
        direction: [selectedDirection[0], selectedDirection[1], selectedDirection[2]],
        directionalVelocity: typeof selectedFlowStep?.directionalRateMagnitude === 'number'
          ? selectedFlowStep.directionalRateMagnitude : 0.02,
        isotropicVelocity: typeof selectedFlowStep?.isotropicRateMagnitude === 'number'
          ? selectedFlowStep.isotropicRateMagnitude : 0,
        calculateVisibility: true,
      }];
      updateSelectedStepWith(draft => {
        delete draft.direction;
        delete draft.directionalRateMagnitude;
        delete draft.isotropicRateMagnitude;
        delete draft.calculateVisibility;
        delete draft.materialRates;
        delete draft.defaultMaterialRate;
        draft.rateSets = rateSets;
      });
      setRateSetsText(JSON.stringify(rateSets, null, 2));
      setRateSetsError('');
      return;
    }
    const first = Array.isArray(selectedFlowStep?.rateSets) && selectedFlowStep.rateSets.length > 0
      ? selectedFlowStep.rateSets[0] as Record<string, unknown>
      : null;
    const firstDirection = Array.isArray(first?.direction) && first.direction.length === 3
      && first.direction.every(entry => typeof entry === 'number' && Number.isFinite(entry))
      ? first.direction as [number, number, number]
      : selectedDirection;
    updateSelectedStepWith(draft => {
      delete draft.rateSets;
      delete draft.calculateVisibility;
      delete draft.materialRates;
      delete draft.defaultMaterialRate;
      draft.direction = [firstDirection[0], firstDirection[1], firstDirection[2]];
      draft.directionalRateMagnitude = typeof first?.directionalVelocity === 'number'
        ? first.directionalVelocity
        : typeof draft.directionalRateMagnitude === 'number' ? draft.directionalRateMagnitude
          : selectedDefaultMaterialRate.directional || 0.02;
      draft.isotropicRateMagnitude = typeof first?.isotropicVelocity === 'number'
        ? first.isotropicVelocity
        : typeof draft.isotropicRateMagnitude === 'number' ? draft.isotropicRateMagnitude
          : selectedDefaultMaterialRate.isotropic;
    });
    setRateSetsText('');
    setRateSetsError('');
  };

  const switchIsotropicMethod = (method: string) => {
    updateSelectedStepWith(draft => {
      if (method === 'auto' || method === 'levelSet' || method === 'geometric') {
        draft.isotropicMethod = method;
      } else {
        delete draft.isotropicMethod;
      }
      if (method === 'geometric') delete draft.isotropicTimeStepRatio;
    });
  };

  const toggleIsotropicTimeStepRatio = (checked: boolean) => {
    updateSelectedStepWith(draft => {
      if (checked) {
        if (typeof draft.isotropicTimeStepRatio !== 'number') {
          draft.isotropicTimeStepRatio = solverTimeStepRatio;
        }
      } else {
        delete draft.isotropicTimeStepRatio;
      }
    });
  };

  const applyNativeParametersText = () => {
    const { value, error } = parseNativeParametersText(nativeParametersText);
    if (error || !value) {
      setNativeParametersError(error || '参数格式无效');
      return;
    }
    setNativeParametersError('');
    updateSelectedStep({ nativeParameters: value });
  };

  const applyRateSetsText = () => {
    const { value, error } = parseRateSetsText(rateSetsText);
    if (error || !value) {
      setRateSetsError(error || 'rateSets 格式无效');
      return;
    }
    setRateSetsError('');
    updateSelectedStep({ rateSets: value });
  };

  const applyIsotropicMaterialRatesText = () => {
    const { value, error } = parseMaterialRateJsonText(
      isotropicMaterialRatesText, 'materialRates', '{"Si": 0.1, "SiO2": 0}',
    );
    if (error || !value) {
      setIsotropicMaterialRatesError(error || 'materialRates 格式无效');
      return;
    }
    setIsotropicMaterialRatesError('');
    updateSelectedStepWith(draft => {
      draft.rateMode = 'material';
      draft.materialRates = value as Record<string, number>;
    });
  };

  const applyDirectionalMaterialRatesText = () => {
    const { value, error } = parseMaterialRateJsonText(
      directionalMaterialRatesText, 'materialRates', '{"Si":{"directional":0.3,"isotropic":0.05}}',
    );
    if (error || !value) {
      setDirectionalMaterialRatesError(error || 'materialRates 格式无效');
      return;
    }
    setDirectionalMaterialRatesError('');
    updateSelectedStepWith(draft => {
      draft.materialRates = value as Record<string, DirectionalMaterialRate>;
    });
  };

  const applyDefaultMaterialRateText = () => {
    const { value, error } = parseMaterialRateJsonText(
      defaultMaterialRateText, 'defaultMaterialRate', '{"directional":0,"isotropic":0}',
    );
    if (error || !value) {
      setDefaultMaterialRateError(error || 'defaultMaterialRate 格式无效');
      return;
    }
    setDefaultMaterialRateError('');
    updateSelectedStepWith(draft => {
      draft.defaultMaterialRate = value as DirectionalMaterialRate;
    });
  };

  const moveAuthoredStep = (stepId: string, delta: number) => {
    commitSteps(steps => {
      const index = steps.findIndex(step => step.id === stepId);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= steps.length) return steps;
      const next = [...steps];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const reorderAuthoredStep = (stepId: string, targetId: string) => {
    if (stepId === targetId) return;
    commitSteps(steps => {
      const from = steps.findIndex(step => step.id === stepId);
      const to = steps.findIndex(step => step.id === targetId);
      if (from < 0 || to < 0) return steps;
      const next = [...steps];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  };

  const addAuthoredStep = () => {
    const step = blankStep();
    commitSteps(steps => [...steps, step]);
    setSelectedFlowStepId(step.id);
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已添加步骤 ${step.name}`]);
  };

  const deleteAuthoredStep = (stepId: string) => {
    commitSteps(steps => steps.filter(step => step.id !== stepId));
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已删除步骤 ${stepId}`]);
  };

  const insertLibraryItem = (item: LibraryItem, flowMode: 'append' | 'replace' = 'append') => run('library-insert', async () => {
    if (item.category === 'material') {
      if (!project) throw new Error('材料插入需要先创建或导入工程（01）');
      const material = String(item.payload.material ?? item.name);
      setMaterialPreset(item.name);
      if (selectedFlowStep && selectedFlowStep.operation === 'deposit') {
        updateSelectedStep({ depositMaterial: material });
        setMaterialTarget({ itemName: item.name, material, target: '当前沉积步骤', stepName: selectedFlowStep.name });
        setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  材料 ${material} 已写入步骤 ${selectedFlowStep.name} 的 depositMaterial`]);
      } else {
        const target = applyMaterialToProject(material);
        setMaterialTarget({ itemName: item.name, material, target: target || '工程材料' });
        setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  材料 ${material} 已写入${target || '工程材料'}`]);
      }
      setManagerStatus('');
      return;
    }
    if (!project) throw new Error('先创建或导入工程');
    if (item.category === 'experiment') {
      navigateZone('experiment');
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  实验模板 ${item.name} 在 04 中使用`]);
      return;
    }
    if (item.category === 'process') {
      const step = stepFromLibrary(item.payload, item.name);
      commitSteps(steps => flowMode === 'replace' ? [step] : [...steps, step]);
      setSelectedFlowStepId(step.id);
      setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已从库插入 process: ${item.name}`]);
      return;
    }
    const templateId = typeof item.payload.templateId === 'string' ? item.payload.templateId : '';
    const definitionId = typeof item.payload.definitionId === 'string' ? item.payload.definitionId : '';
    let inserted: ProjectStep[] = [];
    if (templateId) {
      const template = await api<{ steps: Array<Record<string, unknown>>; variables?: Array<{ name: string; value: unknown }> }>(
        `/api/v1/workflow/templates/${templateId}`,
      );
      const variables = Object.fromEntries((template.variables ?? []).map(row => [row.name, row.value]));
      inserted = template.steps.map(row => stepFromLibrary(
        substituteTemplate(row, variables) as Record<string, unknown>, item.name,
      ));
    } else if (definitionId) {
      const expanded = await postJson<ModuleExpansion>(
        `/api/v1/workflow/flows/definitions/${definitionId}/expand`, { variables: {} },
      );
      inserted = expanded.steps.map(row => stepFromLibrary(row.step as Record<string, unknown>, item.name));
    } else {
      throw new Error('该流程预设没有可插入的 templateId 或 definitionId');
    }
    if (!inserted.length) throw new Error('流程预设没有步骤');
    commitSteps(steps => flowMode === 'replace' ? inserted : [...steps, ...inserted]);
    setSelectedFlowStepId(inserted[0].id);
    setActivityLog(previous => [...previous.slice(-79), `${clockTime()}  已插入流程 ${item.name} · ${inserted.length} 步`]);
  });

  const createDoe = () => run('doe', async () => {
    if (!project || !compile || !activeRecipeId) throw new Error('先在 03 完成一次测量以固定配方');
    if (compileStale) throw new Error('流程已修改，请先在 02 重新编译');
    if (doeFactors.length < 1 || doeFactors.length > 5) throw new Error('DOE 因子数必须在 1–5 之间');
    if (doeBlocked) throw new Error(doeFactorErrors.find(Boolean) ?? '先修正 DOE 因子');
    const factors = doeFactors.map(row => ({
      stepId: row.stepId,
      parameter: row.parameter,
      values: row.values.split(',').map(value => Number(value.trim())).filter(value => Number.isFinite(value)),
    }));
    const duplicates = new Set(factors.map(row => `${row.stepId}/${row.parameter}`));
    if (duplicates.size !== factors.length) throw new Error('同一 (步骤, 参数) 只能配置一次');
    const replicates = Math.round(Number(doeReplicates));
    if (!Number.isFinite(replicates) || replicates < 1 || replicates > 8) throw new Error('DOE 重复数必须在 1–8 之间');
    const seed = Number(doeSeed);
    if (!Number.isFinite(seed)) throw new Error('DOE 种子必须是整数');
    const name = experimentName.trim() || `${compile.projectName} · DOE`;
    const document = await postJson<ExperimentDoc>('/api/v1/workflow/experiments/doe', {
      name, project, flowId: compile.flowId,
      compileId: compile.compileId,
      recipeId: activeRecipeId, recipeVersion: activeRecipeVersion, factors,
      replicates, seed: Math.round(seed),
      maxAttempts: Math.min(4, Math.max(1, Math.round(experimentMaxAttempts) || 1)),
      costPerRunUsd: Number(doeCost) || 0,
    });
    setExperimentDoc(document);
    setExperimentTable(null);
    setComparison(null);
    setCandidateViews({});
    setCandidateViewErrors({});
    setCompareCoverage(null);
    setComparePick(null);
    setAutoAnalysisId('');
    setExperimentInputsOpen(false);
    recordExperiment(document.experimentId);
    await refreshExperimentList();
  });

  const previewRetention = () => run('retention', async () => {
    if (!experimentDoc) throw new Error('先创建实验');
    const info = await postJson<{ releasable: string[]; protected: Array<{ runId: string; reasons: string[] }> }>(
      `/api/v1/workflow/experiments/${experimentDoc.experimentId}/retention/preview`, { mode: 'release-runs' },
    );
    setRetentionInfo(info);
  });

  const applyRetention = (mode: 'keep-all' | 'release-runs') => run('retention', async () => {
    if (!experimentDoc) throw new Error('先创建实验');
    await postJson(`/api/v1/workflow/experiments/${experimentDoc.experimentId}/retention`, { mode });
    setExperimentDoc(await api<ExperimentDoc>(`/api/v1/workflow/experiments/${experimentDoc.experimentId}`));
    setRetentionInfo(await postJson<{ releasable: string[]; protected: Array<{ runId: string; reasons: string[] }> }>(
      `/api/v1/workflow/experiments/${experimentDoc.experimentId}/retention/preview`, { mode: 'release-runs' },
    ));
  });

  const loadExperimentTable = () => run('table', async () => {
    if (!experimentDoc) throw new Error('先创建实验');
    const query = new URLSearchParams();
    if (tableMin.trim()) query.set('min_value', tableMin.trim());
    if (tableMax.trim()) query.set('max_value', tableMax.trim());
    query.set('sort', tableSort);
    query.set('order', tableSort === 'ordinal' ? 'asc' : 'desc');
    setExperimentTable(await api<ExperimentTable>(`/api/v1/workflow/experiments/${experimentDoc.experimentId}/table?${query.toString()}`));
  });

  const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms));

  const waitSegment = async (segmentId: string) => {
    // Wait past the project's own solver timeout (plus overhead) so the UI
    // shows the real terminal status instead of a false "did not finish"; the
    // floor stays at the historical 16 min budget.
    const rawSolverSeconds = Number(compile?.compile.project.solver?.timeoutSeconds ?? project?.solver?.timeoutSeconds ?? SOLVER_TIMEOUT_DEFAULT);
    const solverSeconds = Number.isFinite(rawSolverSeconds) ? rawSolverSeconds : SOLVER_TIMEOUT_DEFAULT;
    const waitMs = Math.max(16 * 60 * 1000, (solverSeconds + 120) * 1000);
    const waitMinutes = Math.round(waitMs / 60000);
    const deadline = Date.now() + waitMs;
    let lastStatus = 'planned';
    while (Date.now() < deadline) {
      const current = await api<RoutePlan['segments'][number]>(`/api/v1/workflow/segments/${segmentId}`);
      lastStatus = String(current.status);
      if (['succeeded', 'failed', 'cancelled', 'interrupted', 'timed_out'].includes(lastStatus)) return current;
      await sleep(1000);
    }
    throw new Error(`segment ${segmentId} did not finish within ${waitMinutes} min (last status: ${lastStatus})`);
  };

  const showMetricDetail = (item: Measurement) => run('metric-detail', async () => {
    const [recipe, provenance] = await Promise.all([
      api<Recipe>(`/api/v1/workflow/metrology-recipes/${item.recipeId}?version=${item.recipeVersion}`),
      api<Provenance>(`/api/v1/workflow/measurements/${item.measurementId}/provenance`),
    ]);
    setMetricDetail({ measurement: item, recipe, provenance });
  });

  const analysisStepId = useMemo(() => {
    const steps = (compile?.compile.project.steps ?? []).filter(step => step.enabled);
    return steps[steps.length - 1]?.id ?? '';
  }, [compile]);

  const experimentCandidates = (doc: ExperimentDoc | null): Array<{ runId: string; stepId: string; label: string }> => {
    if (!doc || !analysisStepId) return [];
    const limit = showAllCandidates ? doc.variants.length : 12;
    return doc.variants.slice(0, limit).map(variant => {
      const replicate = variant.replicates.find(item => item.runId) ?? variant.replicates[0];
      return {
        runId: replicate?.runId ?? '',
        stepId: analysisStepId,
        label: variantConditionLabel(variant, replicate?.seed),
      };
    }).filter(row => row.runId && row.stepId);
  };

  const loadAnalysis = async (rows: Array<{ runId: string; stepId: string; label: string }>, picks: string[] | null = comparePick) => {
    const candidates = rows.filter(item => item.runId && item.stepId);
    if (!candidates.length) throw new Error('实验还没有可分析的 Run');
    const total = candidates.length;
    // Only the selected (≤ COMPARE_CANDIDATE_LIMIT) candidates go to the
    // pairwise comparison; every candidate still gets its own structure view.
    // `null` picks the default first four, an explicit `[]` compares nothing.
    const picked = picks === null
      ? candidates.slice(0, COMPARE_CANDIDATE_LIMIT)
      : candidates.filter(item => picks.includes(candidateKey(item)));
    const compareRows = picked.slice(0, COMPARE_CANDIDATE_LIMIT);
    const prefix = compareRows.every((row, index) => candidateKey(row) === candidateKey(candidates[index]));
    if (compareRows.length >= 2) {
      const result = await postJson<CompareResult>('/api/v1/workflow/analysis/compare', {
        candidates: compareRows, tolerance: 1e-4,
      });
      setComparison(result);
    } else {
      setComparison(null);
    }
    const comparedKeys = compareRows.map(candidateKey);
    setCompareCoverage({ compared: comparedKeys, total, prefix });
    // The compare table is the truth for the checkboxes: keep the picks in sync
    // with what the backend actually compared.
    setComparePick(comparedKeys);
    const outcomes = await Promise.allSettled(candidates.map(item => api<CandidateView>(
      `/api/v1/workflow/analysis/candidates/${item.runId}/${item.stepId}/view`,
    )));
    const views: Record<string, CandidateView> = {};
    const failures: Record<string, string> = {};
    outcomes.forEach((outcome, index) => {
      const key = candidateKey(candidates[index]);
      if (outcome.status === 'fulfilled') views[key] = outcome.value;
      else failures[key] = outcome.reason instanceof Error ? outcome.reason.message : String(outcome.reason);
    });
    setCandidateViews(views);
    setCandidateViewErrors(failures);
  };

  const loadExperimentCandidates = () => {
    const rows = experimentCandidates(experimentDoc);
    if (!rows.length) return;
    setComparePick(null);
    setCompareCandidates(rows);
  };

  /** "显示全部": lift the 12-candidate preview cap and load every variant. */
  const loadAllExperimentCandidates = () => {
    setShowAllCandidates(true);
    if (!experimentDoc || !analysisStepId) return;
    const rows = experimentDoc.variants.map(variant => {
      const replicate = variant.replicates.find(item => item.runId) ?? variant.replicates[0];
      return {
        runId: replicate?.runId ?? '',
        stepId: analysisStepId,
        label: variantConditionLabel(variant, replicate?.seed),
      };
    }).filter(row => row.runId && row.stepId);
    if (!rows.length) return;
    setComparePick(null);
    setCompareCandidates(rows);
    void run('compare', () => loadAnalysis(rows, rows.slice(0, COMPARE_CANDIDATE_LIMIT).map(candidateKey))).catch(() => undefined);
  };

  const addCurrentCandidate = () => {
    if (!hasExecutedRun || !segment?.runId) return;
    setComparePick(null);
    setCompareCandidates(previous => [
      ...previous.filter(item => item.runId !== segment.runId),
      { runId: segment.runId!, stepId: segment.throughStepId, label: `当前 Run · ${statusLabel(segment.status)}` },
    ]);
  };

  const runComparison = () => run('compare', async () => {
    const candidates = compareCandidates.filter(item => item.runId && item.stepId);
    if (candidates.length < 1) throw new Error('实验还没有可分析的 Run');
    if (comparedCandidateKeys.length < 2) throw new Error('至少选择 2 个候选');
    await loadAnalysis(candidates, comparedCandidateKeys);
  });

  /**
   * The checked candidates in the compare table. `comparePick` is the single
   * source of truth: `null` falls back to the first 4 candidates, `[]` is an
   * explicit "compare nothing" selection, checkboxes toggle it directly, and a
   * finished comparison syncs it to the actually compared keys.
   */
  useEffect(() => {
    setComparePick(null);
  }, [compareCandidates]);

  const comparedCandidateKeys = useMemo(() => {
    const known = new Set(compareCandidates.map(candidateKey));
    if (comparePick === null) return compareCandidates.slice(0, COMPARE_CANDIDATE_LIMIT).map(candidateKey);
    return comparePick.filter(key => known.has(key));
  }, [comparePick, compareCandidates]);
  /** Checkboxes changed after the last comparison: the table is one click behind. */
  const comparePickPending = !!compareCoverage && (
    comparedCandidateKeys.length !== compareCoverage.compared.length
    || comparedCandidateKeys.some((key, index) => key !== compareCoverage.compared[index])
  );

  /** Pick which ≤4 candidates the compare table covers; views are unaffected. */
  const toggleCompareCandidate = (key: string) => {
    setComparePick(current => {
      const known = new Set(compareCandidates.map(candidateKey));
      const base = current === null
        ? compareCandidates.slice(0, COMPARE_CANDIDATE_LIMIT).map(candidateKey)
        : current.filter(item => known.has(item));
      if (base.includes(key)) return base.filter(item => item !== key);
      if (base.length >= COMPARE_CANDIDATE_LIMIT) return base;
      return [...base, key];
    });
  };

  // 04 → 05 hand-off: once the experiment has results, 05 presents them without
  // asking the user to press "load candidates" again.
  useEffect(() => {
    if (zone !== 'analyze') return;
    if (!experimentDoc || !['completed', 'failed'].includes(experimentDoc.status)) return;
    if (autoAnalysisRef.current === experimentDoc.experimentId) return;
    const rows = experimentCandidates(experimentDoc);
    if (!rows.length) return;
    autoAnalysisRef.current = experimentDoc.experimentId;
    setAutoAnalysisId(experimentDoc.experimentId);
    setComparePick(null);
    setCompareCandidates(rows);
    void run('compare', () => loadAnalysis(rows, rows.slice(0, COMPARE_CANDIDATE_LIMIT).map(candidateKey))).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zone, experimentDoc, analysisStepId]);

  // 04 needs the Chinese reason behind an ambiguous replicate metric, and the
  // experiment document only carries the id. Fetch the missing measurements once.
  const replicateMeasurementIds = useMemo(() => {
    const ids = new Set<string>();
    for (const variant of experimentDoc?.variants ?? []) {
      for (const replicate of variant.replicates) {
        if (replicate.measurementId && typeof replicate.value !== 'number') ids.add(replicate.measurementId);
      }
    }
    return [...ids].sort();
  }, [experimentDoc]);

  useEffect(() => {
    const missing = replicateMeasurementIds.filter(id => !(id in replicateMeasurements));
    if (!missing.length) return;
    let cancelled = false;
    void Promise.all(missing.map(async id => {
      try { return [id, await api<Measurement>(`/api/v1/workflow/measurements/${id}`)] as const; }
      catch { return null; }
    })).then(rows => {
      if (cancelled) return;
      const found = rows.filter((row): row is readonly [string, Measurement] => !!row);
      if (!found.length) return;
      setReplicateMeasurements(previous => ({ ...previous, ...Object.fromEntries(found) }));
    });
    return () => { cancelled = true; };
  }, [replicateMeasurementIds, replicateMeasurements]);

  const experimentConditionRows = useMemo(() => {
    if (!experimentDoc) return [];
    return experimentDoc.variants.map(variant => ({
      ordinal: variant.ordinal,
      label: variantConditionLabel(variant),
      status: experimentVariantStatus(variant),
    }));
  }, [experimentDoc]);

  const experimentResultRows = useMemo(() => {
    if (!experimentDoc) return [];
    return experimentDoc.variants.map(variant => {
      const replicates = variant.replicates.map(replicate => {
        const metric = replicate.measurementId ? replicateMeasurements[replicate.measurementId] : undefined;
        const status = metric?.status ?? replicate.measurementStatus ?? (replicate.value != null ? 'measured' : '');
        const value = typeof replicate.value === 'number' ? replicate.value : (typeof metric?.value === 'number' ? metric.value : null);
        const valid = status === 'measured' && value !== null && Number.isFinite(value);
        return {
          ...replicate,
          metricStatus: status,
          metricValue: valid ? value : null,
          metricReason: valid ? null : (metric?.reason ?? replicate.reason ?? null),
        };
      });
      const values = replicates
        .map(replicate => replicate.metricValue)
        .filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
      const mean = values.length ? values.reduce((total, value) => total + value, 0) / values.length : null;
      const std = values.length > 1 && mean !== null
        ? Math.sqrt(values.reduce((total, value) => total + (value - mean) ** 2, 0) / (values.length - 1))
        : null;
      return {
        ordinal: variant.ordinal,
        condition: variantConditionLabel(variant),
        status: experimentVariantStatus(variant),
        replicates,
        runSucceeded: replicates.filter(replicate => replicate.status === 'succeeded').length,
        validMetrics: values.length,
        total: replicates.length,
        mean,
        std,
        unit: displayUnit(variant.replicates.find(replicate => replicate.unit)?.unit ?? experimentDoc.unit),
      };
    });
  }, [experimentDoc, replicateMeasurements]);

  /** Run success and metric validity are separate facts; never collapse them into "成功". */
  const experimentMetricSummary = useMemo(() => {
    const rows = experimentDoc?.variants.flatMap(variant => variant.replicates) ?? [];
    const total = rows.length;
    const runSucceeded = rows.filter(replicate => replicate.status === 'succeeded').length;
    const validMetrics = rows.filter(replicate => {
      const metric = replicate.measurementId ? replicateMeasurements[replicate.measurementId] : undefined;
      return (metric?.status ?? replicate.measurementStatus) === 'measured' && typeof replicate.value === 'number';
    }).length;
    return { total, runSucceeded, validMetrics };
  }, [experimentDoc, replicateMeasurements]);

  /**
   * A planned/queued experiment has no result yet: 04 must say "尚未运行" instead
   * of judging every replicate as "测量无有效值".
   */
  const experimentNotRun = useMemo(() => {
    if (!experimentDoc) return true;
    const rows = experimentDoc.variants.flatMap(variant => variant.replicates);
    if (!rows.length) return true;
    const finished = rows.filter(replicate => ['succeeded', 'failed'].includes(replicate.status)).length;
    return finished === 0 && ['planned', 'queued', 'draft', 'created'].includes(experimentDoc.status);
  }, [experimentDoc]);

  const windowBlockedReason = !experimentDoc
    ? '先完成实验或测量：还没有可分析的实验。'
    : experimentRunCount === 0
      ? '先完成实验或测量：实验尚未运行。'
      : '';
  const refreshCandidatesBlockedReason = !experimentDoc || experimentRunCount === 0
    ? '当前工程还没有已完成的实验/运行'
    : busy
      ? '正在执行其他操作，完成后再刷新候选'
      : '';

  /** Validate the two bounds together so lower > upper is reported in place. */
  const windowBoundsError = (lowerText: string, upperText: string): string => {
    if (!lowerText.trim() || !upperText.trim()) return '';
    const lower = Number(lowerText);
    const upper = Number(upperText);
    if (!Number.isFinite(lower) || !Number.isFinite(upper)) return '填写有效的窗口上下限（数字）';
    return lower >= upper ? '窗口下限必须小于上限' : '';
  };

  const updateWindowBound = (kind: 'lower' | 'upper', value: string) => {
    const nextLower = kind === 'lower' ? value : windowLower;
    const nextUpper = kind === 'upper' ? value : windowUpper;
    if (kind === 'lower') setWindowLower(value);
    else setWindowUpper(value);
    setWindowError(windowBoundsError(nextLower, nextUpper));
  };

  const computeProcessWindow = () => {
    const lower = Number(windowLower);
    const upper = Number(windowUpper);
    const problem = windowBoundsError(windowLower, windowUpper)
      || windowBlockedReason
      || (!windowLower.trim() || !windowUpper.trim() || !Number.isFinite(lower) || !Number.isFinite(upper)
        ? '填写有效的窗口上下限（数字）'
        : lower >= upper ? '窗口下限必须小于上限' : '');
    if (problem) {
      // Inline next to the offending inputs, then bring them into view instead
      // of relying on the single red line at the top of the page.
      setWindowError(problem);
      const target = windowLowerRef.current ?? windowUpperRef.current;
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      target?.focus({ preventScroll: true });
      return;
    }
    setWindowError('');
    void run('window', async () => {
      if (!experimentDoc) throw new Error('先创建实验');
      setWindowResult(await postJson<ProcessWindowResult>('/api/v1/workflow/analysis/process-window', {
        experimentId: experimentDoc.experimentId, lower, upper,
      }));
    });
  };

  const generateReport = () => run('report', async () => {
    if (!reportReady) throw new Error('先完成 03 运行与测量，或在 04 运行一个实验');
    const lower = Number(windowLower);
    const upper = Number(windowUpper);
    const payload: Record<string, unknown> = {
      title: reportTitle.trim() || '工作流报告',
      conclusions: reportConclusions,
    };
    if (projectId) payload.projectId = projectId;
    if (experimentDoc) payload.experimentId = experimentDoc.experimentId;
    if (measurement) payload.measurementId = measurement.measurementId;
    if (hasExecutedRun && segment?.runId) { payload.runId = segment.runId; payload.stepId = segment.throughStepId; }
    if (Number.isFinite(lower) && Number.isFinite(upper) && lower < upper) { payload.lower = lower; payload.upper = upper; }
    setReportDocument(await postJson<ReportDocument>('/api/v1/workflow/analysis/report', payload));
  });

  const reportFileName = (): string => {
    const title = (reportDocument?.title || reportTitle || '报告').replace(/[\\/:*?"<>|]+/g, '-').trim();
    return `${title || '报告'}.html`;
  };

  const openReport = () => {
    if (!reportDocument) return;
    const url = URL.createObjectURL(new Blob([reportDocument.html], { type: 'text/html' }));
    window.open(url, '_blank');
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  const downloadReportHtml = () => {
    if (!reportDocument) return;
    const url = URL.createObjectURL(new Blob([reportDocument.html], { type: 'text/html' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = reportFileName();
    anchor.click();
    URL.revokeObjectURL(url);
  };

  /** Grow the preview to the report body so the structure view is on screen. */
  const resizeReportFrame = () => {
    const frame = reportFrameRef.current;
    if (!frame) return;
    const apply = () => {
      const doc = frame.contentDocument;
      if (!doc) return;
      const height = Math.max(
        doc.documentElement?.scrollHeight ?? 0,
        doc.body?.scrollHeight ?? 0,
        520,
      );
      frame.style.height = `${Math.min(height + 16, 12000)}px`;
    };
    apply();
    // The body is laid out one frame after `load`; re-measure so the preview
    // never keeps a half-sized box that clips the structure section.
    window.requestAnimationFrame(apply);
    window.setTimeout(apply, 120);
  };

  const loadCalibrationFile = async (file: File) => {
    setCalibrationCsv(await file.text());
    setCalibrationFileName(file.name);
    setCalibrationName(file.name.replace(/\.[^.]+$/, ''));
    setCalibrationDataset(null);
    setCalibrationFit(null);
  };

  const createCalibrationDataset = () => run('calibration', async () => {
    if (!calibrationCsv.trim()) throw new Error('先导入 CSV');
    const dataset = await postJson<CalibrationDataset>('/api/v1/workflow/calibration/datasets', {
      name: calibrationName || 'calibration', csv: calibrationCsv,
    });
    setCalibrationDataset(dataset);
    setCalibrationFit(null);
  });

  const fitCalibration = () => run('calibration', async () => {
    if (!calibrationDataset) throw new Error('先创建数据集');
    setCalibrationFit(await postJson<CalibrationFit>('/api/v1/workflow/calibration/fit', {
      datasetId: calibrationDataset.datasetId, model: calibrationModel, method: calibrationMethod,
    }));
  });

  const analysisFrame = useMemo(() => {
    const views = Object.values(candidateViews);
    if (!views.length) return null;
    return mergeFrames(views.map(view => structureFrame(view, syncAxis)), syncZoom);
  }, [candidateViews, syncAxis, syncZoom]);

  const candidateViewsHave3D = useMemo(
    () => Object.values(candidateViews).some(view => view.dimension === 3),
    [candidateViews],
  );
  const candidateUnit = Object.values(candidateViews)[0]?.coordinateUnit ?? 'um';
  /** 05 shows only the current Route / current experiment runs. */
  const analysisRunIds = useMemo(() => {
    const ids = new Set<string>();
    for (const item of route?.segments ?? []) {
      if (item.runId && !item.runId.startsWith('pending_')) ids.add(item.runId);
    }
    for (const variant of experimentDoc?.variants ?? []) {
      for (const replicate of variant.replicates) {
        if (replicate.runId) ids.add(replicate.runId);
      }
    }
    return ids;
  }, [route, experimentDoc]);
  /**
   * The metric table always has at least the current measurement, but never a
   * stale run and never two recipes for the same run/step (which used to show
   * "已测量 0.2" next to "无交点" for the same snapshot).
   */
  const analysisMetrics = useMemo(() => {
    const source = metrics.length ? metrics : (measurement ? [measurement] : []);
    const byRunStep = new Map<string, Measurement>();
    for (const item of source) {
      if (analysisRunIds.size && item.sourceRunId && !analysisRunIds.has(item.sourceRunId)) continue;
      byRunStep.set(`${item.sourceRunId ?? ''}:${item.sourceStepId ?? ''}`, item);
    }
    return [...byRunStep.values()];
  }, [metrics, measurement, analysisRunIds]);

  /** Readable run name for the metric table; the raw id stays in `title`. */
  const runDisplayLabel = useCallback((runId: string | null | undefined): string => {
    if (!runId) return '—';
    const variant = experimentDoc?.variants.find(item =>
      item.replicates.some(replicate => replicate.runId === runId));
    if (variant) return variantConditionLabel(variant);
    const index = route?.segments.findIndex(item => item.runId === runId) ?? -1;
    if (index >= 0) return `当前 Run · 第 ${index + 1} 段`;
    return 'Run 已记录';
  }, [experimentDoc, route]);

  const libraryDuplicateNames = useMemo(() => {
    const counts = new Map<string, number>();
    libraryItems.forEach(item => counts.set(item.name, (counts.get(item.name) ?? 0) + 1));
    return new Set([...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name));
  }, [libraryItems]);

  const managerDuplicateNames = useMemo(() => {
    const counts = new Map<string, number>();
    managerItems.forEach(item => counts.set(item.name, (counts.get(item.name) ?? 0) + 1));
    return new Set([...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name));
  }, [managerItems]);

  const recipeOptions = useMemo(() => {
    const latest = new Map<string, RecipeSummary>();
    for (const recipe of recipes) {
      const current = latest.get(recipe.recipeId);
      if (!current || recipe.version > current.version) latest.set(recipe.recipeId, recipe);
    }
    return [...latest.values()].sort((left, right) => left.name.localeCompare(right.name));
  }, [recipes]);

  const flowReferenceCount = flowDefinitions.length + templates.length;
  const managerSaveBlocked = managerCategory === 'flow'
    ? (!flowReferenceCount ? '先在 02 保存流程模板或流程定义，流程库条目才有可引用对象。' : !(managerForm.flowRef ?? '') ? '选择要引用的流程定义或模板后才能保存。' : '')
    : managerCategory === 'experiment'
      ? (!recipeOptions.length ? '先在 03 完成一次测量以生成测量配方，实验库条目才能引用它。' : '')
      : '';

  const managerRowMeta = (item: LibraryItem): string => {
    if (item.category === 'material') {
      return [item.payload.catalog, item.payload.material, item.payload.role].filter(Boolean).join(' · ');
    }
    if (item.category === 'process') {
      const operation = item.payload.operation === 'deposit' ? '沉积' : item.payload.operation === 'etch' ? '刻蚀' : String(item.payload.operation ?? '');
      const model = STEP_LABELS[String(item.payload.model ?? '')] ?? String(item.payload.model ?? '');
      return `${operation} · ${model} · ${String(item.payload.rateMagnitude ?? '—')} µm/s · ${String(item.payload.duration ?? '—')} s`;
    }
    if (item.category === 'flow') {
      const definitionId = typeof item.payload.definitionId === 'string' ? item.payload.definitionId : '';
      const templateId = typeof item.payload.templateId === 'string' ? item.payload.templateId : '';
      return definitionId ? `流程定义 · ${definitionId}` : templateId ? `流程模板 · ${templateId}` : '流程引用';
    }
    return `配方 · ${String(item.payload.recipeId ?? '—')} v${String(item.payload.recipeVersion ?? 1)}`;
  };

  // Only experiments recorded in the open project's workflow state are shown;
  // the global list would otherwise leak the previous project's experiment.
  const visibleExperimentList = useMemo(() => {
    if (!projectId) return experimentList;
    const known = new Set(projectState.experimentIds);
    return experimentList.filter(row => known.has(row.experimentId));
  }, [experimentList, projectId, projectState.experimentIds]);

  const renderExperimentList = () => visibleExperimentList.length === 0 ? null : (
    <div className="wf-experiment-list" aria-label="实验列表">
      <div className="wf-card-subhead">实验列表（点击切换查看）</div>
      {visibleExperimentList.map(row => {
        const running = ['running', 'queued'].includes(row.status);
        const armed = experimentDeleteArmed === row.experimentId;
        return <div key={row.experimentId} className={row.experimentId === experimentDoc?.experimentId ? 'wf-experiment-row active' : 'wf-experiment-row'}>
          <button
            type="button"
            className="wf-experiment-open"
            onClick={() => void openExperiment(row.experimentId)}
          >
            <strong>{row.name}</strong>
            <span>{statusLabel(row.status)} · {row.variantCount} 变体 · {formatDateTime(row.updatedAt)}</span>
          </button>
          {armed
            ? <>
              <span className="wf-muted">确认删除「{row.name}」？只删除实验记录，不删 Run/测量。</span>
              <button type="button" className="wf-link" disabled={!!busy} onClick={() => void deleteExperiment(row)}>确认删除</button>
              <button type="button" className="wf-link" onClick={() => setExperimentDeleteArmed('')}>取消</button>
            </>
            : <button
              type="button"
              className="wf-link"
              disabled={running || !!busy}
              title={running ? '实验运行中，先取消' : '删除该实验记录（不影响 Run 与测量）'}
              onClick={() => setExperimentDeleteArmed(row.experimentId)}
            >删除</button>}
        </div>;
      })}
    </div>
  );

  const renderFlowBar = () => (
    <div className="spx-flow-bar">
      <div className="spx-flow-actions">
        <button type="button" className="wf-button ghost spx-flow-save" disabled={!project || busy === 'project-save'}
          title={project ? '保存当前工程（Ctrl/Cmd+S）' : '先打开工程'}
          onClick={() => void saveProject()}>{busy === 'project-save' ? '保存中…' : '保存工程'}</button>
        <span className={`spx-compile-badge${compile ? (compileStale ? ' stale' : ' ok') : ''}`}
          title={compile ? `Compile ${compile.compileId}` : '尚未编译流程'}>
          {compile ? (compileStale ? '需重新编译' : '已编译') : '未编译'}
        </span>
        <button className="wf-button primary" disabled={!canCompile}
          title={authoredSteps.length === 0 ? '流程为空：先插入或添加步骤' : '编译当前步骤，生成 Compile 与绑定 (binding)'}
          onClick={() => void compileFlow()}>{busy === 'compile-flow' ? '编译中…' : compile ? '重新编译' : '编译流程'}</button>
        {route && !compileStale
          ? <button className="wf-button" onClick={goToRun}>去 03</button>
          : compile && !compileStale
            ? <button className="wf-button" disabled={!canRoute} onClick={() => void createRoute()}>{busy === 'route' ? '创建中…' : '创建 Route'}</button>
            : null}
        {route && <button className="wf-button ghost" disabled={compileStale || !!busy}
          title={compileStale ? '编译已过期：先重新编译，再重建 Route' : '基于当前 Compile 重建 Route'}
          onClick={() => setRebuildRouteArmed(true)}>重建 Route…</button>}
        {route && compileStale && <span className="wf-block-reason" role="alert" aria-label="Route 需重建">
          工程在编译后有修改，需重新编译；旧 Route 需重建。
        </span>}
      </div>
    </div>
  );

  const renderLibraryManager = () => (
    <div className="spx-library-view" aria-label="库管理">
      <nav className="spx-breadcrumb" aria-label="面包屑">
        <button type="button" className="spx-link" aria-label="返回流程" title="返回流程编辑"
          onClick={() => navigateZone('flow')}>02 流程</button>
        <span aria-hidden="true">/</span>
        <span>{LIBRARY_CATEGORY_LABELS[managerCategory]}</span>
      </nav>
      <div className="spx-library-category-head">
        <strong>{managerLoading
          ? LIBRARY_CATEGORY_LABELS[managerCategory]
          : `${LIBRARY_CATEGORY_LABELS[managerCategory]} · ${managerItems.length} 条`}</strong>
        {managerLoading && <span className="wf-muted">加载中…</span>}
        {managerError && <span className="spx-library-load-error">{`加载失败：${managerError}`}</span>}
        {managerError && <button type="button" className="wf-button ghost" onClick={() => void loadManagerCategory(managerCategory)}>重试</button>}
      </div>
      <div className="spx-library-manager-body">
        <div className="spx-library-manager-list">
          {managerLoading && <div className="wf-muted spx-library-empty">加载中…</div>}
          {!managerLoading && managerError && <div className="spx-library-load-error spx-library-empty">{managerError}<button type="button" className="wf-button ghost" onClick={() => void loadManagerCategory(managerCategory)}>重试</button></div>}
          {!managerLoading && !managerError && managerItems.length === 0 && <div className="wf-muted spx-library-empty">
            {managerCategory === 'flow'
              ? <>
                  <span>流程库暂无条目</span>
                  <button type="button" className="spx-link" onClick={() => navigateZone('flow')}>先在 02 保存流程模板</button>
                </>
              : managerCategory === 'experiment'
                ? '实验库暂无条目，可先在 04 创建实验模板。'
                : `${LIBRARY_CATEGORY_LABELS[managerCategory]}暂无条目，可用中间栏的“新增”创建。`}
          </div>}
          {!managerLoading && !managerError && managerItems.map(item => {
            const duplicate = managerDuplicateNames.has(item.name);
            const color = item.category === 'material' && isHexColor(String(item.payload.color ?? ''))
              ? String(item.payload.color)
              : '#c7cfd6';
            return <button
              key={item.itemId} type="button"
              className={`spx-library-row${managerSelectedId === item.itemId ? ' active' : ''}`}
              title={`${item.name} · ${managerRowMeta(item)}${duplicate ? ` · ${item.itemId}` : ''}`}
              onClick={() => selectManagerItem(item)}
            >
              <i style={{ background: color }} aria-hidden="true" />
              <strong>{item.name}</strong>
              <span>{managerRowMeta(item)}</span>
              {duplicate && <span className="spx-library-row-id">重名</span>}
            </button>;
          })}
        </div>
        <div className="spx-library-manager-actions">
          <button type="button" className="text" aria-label="新增库条目" title="新增条目" onClick={() => {
            setManagerSelectedId('');
            setManagerForm(managerDefaults(managerCategory));
            setManagerStatus('');
            setManagerDeleteArmed(false);
          }}>新增</button>
          <button type="button" aria-label="删除库条目" title={managerSelectedId ? '删除选中条目（需二次确认）' : '先选择条目'}
            disabled={!managerSelectedId}
            className={managerDeleteArmed ? 'danger text' : 'text'}
            onClick={() => {
              if (!managerDeleteArmed) { setManagerDeleteArmed(true); return; }
              void deleteManagerItem();
            }}>删除</button>
          {managerDeleteArmed && <>
            <div className="spx-library-confirm-delete">
              确认删除「{managerItems.find(item => item.itemId === managerSelectedId)?.name ?? '所选条目'}」？删除后无法恢复。
            </div>
            <button type="button" className="spx-link" onClick={() => { setManagerDeleteArmed(false); }}>取消删除</button>
          </>}
        </div>
        <div className="spx-library-manager-form">
          <label className="spx-field"><span>名称</span><input aria-label="库条目名称" value={managerForm.name ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, name: event.target.value }))} /></label>
          {managerCategory === 'material' && <>
            <label className="spx-field"><span>Catalog</span>
              <select aria-label="库材料 Catalog" value={managerForm.catalog ?? ''} onChange={event => {
                const catalog = event.target.value;
                const first = materialCatalog.find(row => (row.category ?? 'Generic') === catalog);
                setManagerForm(prev => ({
                  ...prev, catalog,
                  material: first?.name ?? '',
                  color: first?.color ?? prev.color,
                }));
              }}>
                {materialCategories.map(category => <option key={category} value={category}>{category}</option>)}
              </select>
            </label>
            <label className="spx-field"><span>Material</span>
              <select aria-label="库材料名称" value={managerForm.material ?? ''} onChange={event => {
                const material = event.target.value;
                const row = materialCatalog.find(item => item.name === material);
                setManagerForm(prev => ({ ...prev, material, color: row?.color ?? prev.color }));
              }}>
                {materialCatalog.filter(row => (row.category ?? 'Generic') === (managerForm.catalog ?? '')).map(row => <option key={row.name} value={row.name}>{row.name}</option>)}
              </select>
            </label>
            <div className="spx-field"><span>颜色</span>
              <div className="spx-color-row">
                <input aria-label="库材料颜色" className={isHexColor(managerForm.color ?? '') ? '' : 'invalid'}
                  value={managerForm.color ?? ''} placeholder="#rrggbb"
                  onChange={event => setManagerForm(prev => ({ ...prev, color: event.target.value }))} />
                <label className={isHexColor(managerForm.color ?? '') ? 'spx-color-swatch' : 'spx-color-swatch invalid'}
                  title={isHexColor(managerForm.color ?? '') ? '点击选择颜色' : '颜色必须是 #rrggbb'}
                  style={isHexColor(managerForm.color ?? '') ? { background: managerForm.color } : undefined}>
                  <input type="color" aria-label="库材料颜色选择器"
                    value={isHexColor(managerForm.color ?? '') ? (managerForm.color ?? '#000000') : '#000000'}
                    onChange={event => setManagerForm(prev => ({ ...prev, color: event.target.value }))} />
                </label>
              </div>
            </div>
            <label className="spx-field"><span>显示名</span><input aria-label="库材料显示名" value={managerForm.displayName ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, displayName: event.target.value }))} /></label>
            <label className="spx-field"><span>角色</span>
              <select aria-label="库材料角色" value={managerForm.role ?? 'film'} onChange={event => setManagerForm(prev => ({ ...prev, role: event.target.value }))}>
                {MATERIAL_ROLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </>}
          {managerCategory === 'process' && <>
            <label className="spx-field"><span>操作</span>
              <select aria-label="库工艺操作" value={managerForm.operation ?? 'etch'} onChange={event => setManagerForm(prev => ({ ...prev, operation: event.target.value }))}>
                <option value="etch">etch · 刻蚀</option>
                <option value="deposit">deposit · 沉积</option>
              </select>
            </label>
            <label className="spx-field"><span>模型</span>
              <select aria-label="库工艺模型" value={managerForm.model ?? 'isotropic'} onChange={event => setManagerForm(prev => ({ ...prev, model: event.target.value }))}>
                <option value="isotropic">isotropic · 各向同性</option>
                <option value="singleParticle">singleParticle · 单粒子</option>
                <option value="directional">directional · 定向</option>
                <option value="multiParticle">multiParticle · 多粒子</option>
                <option value="removeMaterial">removeMaterial · 移除材料</option>
                <option value="planarize">planarize · 平坦化</option>
              </select>
            </label>
            <label className="spx-field"><span>速率 (µm/s)</span><input aria-label="库工艺速率" type="number" step="0.01" value={managerForm.rateMagnitude ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, rateMagnitude: event.target.value }))} /></label>
            <label className="spx-field"><span>时长 (s)</span><input aria-label="库工艺时长" type="number" step="0.01" value={managerForm.duration ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, duration: event.target.value }))} /></label>
            {managerForm.operation === 'deposit' && <label className="spx-field"><span title="只列可沉积材料（排除硬掩膜/光刻胶）">沉积材料</span>
              <select aria-label="库工艺沉积材料" value={managerForm.depositMaterial ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, depositMaterial: event.target.value }))}>
                <option value="">选择材料</option>
                {depositMaterialOptions.map(row => <option key={row.name} value={row.name}>{row.name}</option>)}
              </select>
            </label>}
            {managerForm.model === 'singleParticle' && <>
              <label className="spx-field"><span>粘附系数 stickingProbability</span><input aria-label="库工艺 stickingProbability" type="number" step="0.01" value={managerForm.stickingProbability ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, stickingProbability: event.target.value }))} /></label>
              <label className="spx-field"><span>源指数 sourceExponent</span><input aria-label="库工艺 sourceExponent" type="number" step="0.1" value={managerForm.sourceExponent ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, sourceExponent: event.target.value }))} /></label>
            </>}
          </>}
          {managerCategory === 'flow' && <label className="spx-field"><span>引用流程定义 / 模板</span>
            <select aria-label="库流程引用" value={managerForm.flowRef ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, flowRef: event.target.value }))} disabled={!flowReferenceCount}>
              <option value="">{flowReferenceCount ? '选择引用' : '无可引用的流程'}</option>
              <optgroup label="流程定义">
                {flowDefinitions.map(row => <option key={row.definitionId} value={`definition:${row.definitionId}`}>{row.name} · {row.definitionId}</option>)}
              </optgroup>
              <optgroup label="流程模板">
                {templates.map(row => <option key={row.templateId} value={`template:${row.templateId}`}>{row.name} · {row.stepCount} 步</option>)}
              </optgroup>
            </select>
          </label>}
          {managerCategory === 'experiment' && <>
            <label className="spx-field"><span>测量配方</span>
              <select aria-label="库实验配方" value={managerForm.recipeRef ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, recipeRef: event.target.value }))} disabled={!recipeOptions.length}>
                <option value="">{recipeOptions.length ? '选择配方' : '尚无测量配方'}</option>
                {recipeOptions.map(row => <option key={row.recipeId} value={`${row.recipeId}@${row.version}`}>{row.name} · v{row.version} · {row.method}</option>)}
              </select>
            </label>
            <label className="spx-field"><span>设计类型</span>
              <select aria-label="库实验设计" value={managerForm.designKind ?? 'doe'} onChange={event => setManagerForm(prev => ({ ...prev, designKind: event.target.value }))}>
                <option value="doe">doe · 全因子</option>
                <option value="manual">manual · 手工</option>
              </select>
            </label>
            <label className="spx-field"><span>变体数</span><input aria-label="库实验变体数" type="number" min={1} value={managerForm.variantCount ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, variantCount: event.target.value }))} /></label>
            <label className="spx-field"><span>重复数</span><input aria-label="库实验重复数" type="number" min={1} value={managerForm.replicates ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, replicates: event.target.value }))} /></label>
            <label className="spx-field"><span>随机种子</span><input aria-label="库实验种子" type="number" value={managerForm.seed ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, seed: event.target.value }))} /></label>
          </>}
          <label className="spx-field"><span>备注 / 描述</span><input aria-label="库条目备注" value={managerForm.notes ?? ''} onChange={event => setManagerForm(prev => ({ ...prev, notes: event.target.value }))} /></label>
          {managerSaveBlocked && <div className="spx-library-reason" role="note">{managerSaveBlocked}</div>}
          <div className="spx-library-manager-form-actions">
            <button type="button" className="wf-button primary" disabled={!!busy || !!managerSaveBlocked} onClick={() => void saveManagerItem()}>{busy === 'library-save' ? '保存中…' : '保存'}</button>
            <button type="button" className="wf-button" onClick={() => {
              if (managerSelectedId) {
                const item = managerItems.find(row => row.itemId === managerSelectedId);
                if (item) setManagerForm(managerFormFromItem(item));
              } else {
                setManagerForm(managerDefaults(managerCategory));
              }
              setManagerStatus('');
              setManagerDeleteArmed(false);
            }}>取消</button>
          </div>
          {managerStatus && <div className="wf-summary spx-banner" role="status"><span className="spx-banner-text">{managerStatus}</span><BannerClose onClose={() => setManagerStatus('')} /></div>}
        </div>
      </div>
    </div>
  );

  const clipLayerMaterial = (layer: number): string => {
    const key = selectedPairs.find(item => Number(item.split(':')[0]) === layer);
    return (key ? layerMaterials[key] : '') || activeClip?.layers.find(item => item.layer === layer)?.material || 'Si';
  };

  const clipLabel = (clip: Clip) => clip.name;

  /** Saved 3D clips carry a z placement per layer; show it wherever a clip is listed. */
  const clipHasLayerHeights = (clip: Clip) => clip.layers.some(item => item.height != null);
  const clipLayerHeightText = (clip: Clip) => clip.layers
    .map(item => `L${item.layer} ${item.baseHeight ?? '—'}→${item.height ?? '—'} µm`)
    .join(' · ');

  const renderClipCanvas = (source: ClipCanvas) => {
    const unit = 'µm';
    const xTicks = axisTicks(source.bbox.x0, source.bbox.x1);
    const yTicks = axisTicks(source.bbox.y0, source.bbox.y1);
    const span = Math.max(source.width, source.height, 1e-6);
    const fontSize = span / 26;
    const strokeWidth = span / 400;
    const tickHalo = fontSize * 0.1;
    const barLength = xTicks.length > 1 ? Math.abs(xTicks[1] - xTicks[0]) : span / 4;
    // The plot keeps its own frame; titles, ticks and the scale bar live in the
    // margins so nothing is drawn on top of the layout (N7).
    const marginLeft = fontSize * 5.2;
    const marginRight = fontSize * 0.6;
    const marginTop = fontSize * 0.9;
    const marginBottom = fontSize * 3.9;
    const barRight = source.width;
    const barX = barRight - barLength;
    const barY = source.height + fontSize * 2.4;
    const barLabelY = source.height + fontSize * 2.0;
    const xTitleY = source.height + fontSize * 3.4;
    const yTitleX = -fontSize * 4.3;
    const yTitleY = source.height / 2;
    const uHasOrigin = xTicks.some(tick => Math.abs(tick) < 1e-9);
    const legendRows = Array.from(new Set(source.polygons.map(polygon => polygon.layer)))
      .map(layer => ({ layer, material: clipLayerMaterial(layer), color: materialColor(materialCatalog, clipLayerMaterial(layer)) }));
    // `setCanvas(clip)` keeps the saved Clip (with its annotations); a bare
    // preview has no clipId and belongs to whatever Clip is active.
    const canvasClipId = (source as Partial<Clip>).clipId;
    const canvasAnnotations = activeClip && (!canvasClipId || canvasClipId === activeClip.clipId)
      ? activeClip.annotations
      : [];
    return <div className="wf-canvas-wrap">
      <svg viewBox={`${-marginLeft} ${-marginTop} ${source.width + marginLeft + marginRight} ${source.height + marginTop + marginBottom}`}
        className="wf-clip-canvas" role="img" aria-label="版图 Clip 画布">
        <rect x={0} y={0} width={source.width} height={source.height} fill="#f8fafb" />
        {xTicks.map(tick => <g key={`x-${tick}`} className="wf-axes wf-tick">
          <line x1={tick} y1={0} x2={tick} y2={source.height} stroke="#e6eaee" strokeWidth={strokeWidth} />
          <line x1={tick} y1={source.height} x2={tick} y2={source.height + fontSize * 0.4} stroke="#6b7a8a" strokeWidth={strokeWidth} />
          <text className="wf-tick-label wf-tick-label--u" x={tick} y={source.height + fontSize * 0.95} fontSize={fontSize} textAnchor="middle"
            fill="#26313a" stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{formatTick(tick)}</text>
        </g>)}
        {yTicks.map(tick => <g key={`y-${tick}`} className="wf-axes wf-tick">
          <line x1={0} y1={source.height - tick} x2={source.width} y2={source.height - tick} stroke="#e6eaee" strokeWidth={strokeWidth} />
          <line x1={0} y1={source.height - tick} x2={-fontSize * 0.4} y2={source.height - tick} stroke="#6b7a8a" strokeWidth={strokeWidth} />
          {!(uHasOrigin && Math.abs(tick) < 1e-9) && <text className="wf-tick-label wf-tick-label--v" x={-fontSize * 0.55}
            y={source.height - tick + fontSize * 0.35} fontSize={fontSize} textAnchor="end"
            fill="#26313a" stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{formatTick(tick)}</text>}
        </g>)}
        {source.polygons.map((polygon, index) => {
          const color = materialColor(materialCatalog, clipLayerMaterial(polygon.layer));
          return <polygon
            key={`${polygon.layer}-${index}`}
            points={polygon.points.map(([x, y]) => `${x},${source.height - y}`).join(' ')}
            fill={color}
            fillOpacity="0.45"
            stroke={color}
            strokeWidth={strokeWidth}
          />;
        })}
        {/* Annotations are stored in layout µm; the canvas is drawn in pixels. */}
        {canvasAnnotations.map(annotation => {
          const spanX = (source.bbox.x1 - source.bbox.x0) || 1;
          const spanY = (source.bbox.y1 - source.bbox.y0) || 1;
          const cx = ((annotation.x - source.bbox.x0) / spanX) * source.width;
          const cy = source.height - ((annotation.y - source.bbox.y0) / spanY) * source.height;
          return <g key={annotation.annotationId} className="wf-annotation-group"
            data-x={annotation.x} data-y={annotation.y} data-cx={cx.toFixed(3)} data-cy={cy.toFixed(3)}>
            <circle cx={cx} cy={cy} r={Math.max(fontSize * 0.5, span / 80)} className="wf-annotation"
              strokeWidth={Math.max(fontSize * 0.08, span / 800)} />
            <text x={cx + fontSize * 0.6} y={cy - fontSize * 0.3} className="wf-annotation-text"
              fontSize={fontSize} style={{ fontSize }} fill="#07121d" stroke="#ffffff" strokeWidth={tickHalo * 2} paintOrder="stroke">
              {annotation.text}
            </text>
          </g>;
        })}
        <g className="wf-axes wf-axis-line">
          <line x1={0} y1={source.height} x2={source.width} y2={source.height} stroke="#6b7a8a" strokeWidth={strokeWidth} />
          <line x1={0} y1={source.height} x2={0} y2={0} stroke="#6b7a8a" strokeWidth={strokeWidth} />
          <text className="wf-axis-title wf-axis-title--x" x={source.width / 2} y={xTitleY} fontSize={fontSize} textAnchor="middle"
            stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{`X (${unit})`}</text>
          <text className="wf-axis-title wf-axis-title--v" x={yTitleX} y={yTitleY} fontSize={fontSize} textAnchor="middle"
            transform={`rotate(-90 ${yTitleX} ${yTitleY})`}
            stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{`Y (${unit})`}</text>
          <line className="wf-scale-bar" x1={barX} y1={barY} x2={barRight} y2={barY} stroke="#34404b" strokeWidth={strokeWidth * 3} />
          <text className="wf-scale-label" x={barX + barLength / 2} y={barLabelY} fontSize={fontSize} textAnchor="middle"
            stroke="#ffffff" strokeWidth={tickHalo} paintOrder="stroke">{`${formatTick(barLength)} ${unit}`}</text>
        </g>
      </svg>
      <div className="wf-muted">{source.polygonCount} 多边形 · {source.pointCount} 点 · {source.width.toFixed(3)} × {source.height.toFixed(3)} {unit}</div>
      <div className="spx-layer-legend">
        {legendRows.map(row => <span key={row.layer}>
          <i style={{ background: row.color }} aria-hidden="true" />L{row.layer} · {row.material}
        </span>)}
      </div>
    </div>;
  };

  const renderClipEditor = (mode: 'draft' | 'project', showList = true) => (
    <div className="wf-clip-editor">
      <div className="spx-file-row">
        <div className={gdsDisplayName ? 'spx-file-name filled' : 'spx-file-name'} title={gdsDisplayName || undefined}>
          {gdsDisplayName || '未选择 GDS 文件'}
        </div>
        <button type="button" className="wf-button ghost spx-browse" disabled={!!busy} onClick={() => gdsInput.current?.click()}>{gdsDisplayName ? '更换 GDS' : '上传 GDS'}</button>
      </div>
      {gds
        ? <div className="spx-form-note" title={gds.assetId}>已上传 GDS{activeLayout ? ` · ${activeLayout.fileName}` : ''}</div>
        : gdsFromRegisteredLayout
          ? <div className="spx-form-note">来自已注册 Layout{activeLayout ? ` · ${activeLayout.fileName}` : ''}（无需重新上传）</div>
          : null}
      <div className="wf-template-row">
        <button className="wf-button ghost"
          disabled={!!busy || !gds}
          title={activeLayout && !gds ? '该 GDS 已注册为 Layout' : !gds ? '先上传 GDS' : '注册为版图'}
          onClick={() => void registerLayout()}>
          {activeLayout && !gds ? '已注册 Layout' : '注册为 Layout'}
        </button>
        <select aria-label="版图选择" value={activeLayout?.layoutId ?? ''} onChange={event => void chooseLayout(event.target.value)}>
          <option value="">{layouts.length ? '选择 Layout' : '尚无 Layout'}</option>
          {layouts.map(item => <option key={item.layoutId} value={item.layoutId}>{item.fileName} · {item.structureCount} 结构</option>)}
        </select>
      </div>
      {activeLayout && <>
        <div className="wf-template-row">
          <select aria-label="版图结构" value={layoutStructure} onChange={event => selectStructure(activeLayout, event.target.value)}>
            {activeLayout.structures.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
          </select>
          <input aria-label="Clip 名称" value={clipName} onChange={event => setClipName(event.target.value)} placeholder="Clip 名称" />
        </div>
        <div className="wf-layer-picks">
          {(activeLayout.structures.find(item => item.name === layoutStructure)?.layerDatatypes ?? []).map(([layer, datatype]) => {
            const key = `${layer}:${datatype}`;
            const material = layerMaterials[key] || 'Si';
            const heights = layerHeights[key] ?? DEFAULT_LAYER_HEIGHT;
            return <div key={key} className="spx-layer-pick">
              <input type="checkbox" aria-label={`图层 ${layer}/${datatype}`} checked={selectedPairs.includes(key)}
                onChange={event => setSelectedPairs(previous => event.target.checked ? [...previous, key] : previous.filter(item => item !== key))} />
              <span>L{layer}/{datatype}</span>
              <select aria-label={`图层 ${layer} 材料`} value={material}
                onChange={event => setLayerMaterials(previous => ({ ...previous, [key]: event.target.value }))}>
                {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name}</option>)}
              </select>
              {clipDimension() === 3 && <>
                <span className="spx-layer-height"><span>基底高度 baseHeight (µm)</span>
                  <input aria-label={`图层 ${layer} 基底高度`} type="number" step="0.01" value={heights.baseHeight}
                    onChange={event => setLayerHeights(previous => ({ ...previous, [key]: { ...heights, baseHeight: event.target.value } }))} />
                </span>
                <span className="spx-layer-height"><span>厚度 height (µm)</span>
                  <input aria-label={`图层 ${layer} 厚度`} type="number" step="0.01" value={heights.height}
                    onChange={event => setLayerHeights(previous => ({ ...previous, [key]: { ...heights, height: event.target.value } }))} />
                </span>
              </>}
              <i style={{ background: materialColor(materialCatalog, material) }} aria-hidden="true" />
            </div>;
          })}
        </div>
        <div className="wf-bounds">
          {(['x0', 'y0', 'x1', 'y1'] as const).map(key => <input key={key} aria-label={`Clip ${key}`} value={String(clipBounds[key])} onChange={event => setClipBounds(previous => ({ ...previous, [key]: Number(event.target.value) }))} />)}
          <button className="wf-button ghost" disabled={!!busy} onClick={() => void previewClip()}>预览</button>
          <button className="wf-button ghost" disabled={!!busy} onClick={() => void saveClip()}>保存 Clip</button>
        </div>
      </>}
      {clipNotice && <div className="spx-clip-conflict spx-banner" role="status"><span className="spx-banner-text">{clipNotice}</span><BannerClose onClose={() => setClipNotice('')} /></div>}
      {canvas && renderClipCanvas(canvas)}
      {activeClip && <div className="wf-summary"><strong>{activeClip.name}</strong><span>{activeClip.layers.length} 层 · {activeClip.polygonCount} 多边形</span><span><code>{activeClip.clipHash.slice(0, 12)}…</code></span><span>{activeClip.annotations.length} 标注</span></div>}
      {activeClip && <div className="wf-template-row wf-annotation-form">
        <label className="spx-field"><span>标注文本</span>
          <input aria-label="标注文本" value={annotationText} onChange={event => setAnnotationText(event.target.value)} placeholder="例如：孔口" />
        </label>
        <label className="spx-field"><span>标注 X（µm）</span>
          <input aria-label="标注 X" value={annotationX} onChange={event => setAnnotationX(event.target.value)} />
        </label>
        <label className="spx-field"><span>标注 Y（µm）</span>
          <input aria-label="标注 Y" value={annotationY} onChange={event => setAnnotationY(event.target.value)} />
        </label>
        <button className="wf-button ghost" disabled={!!busy || !annotationText.trim()} onClick={() => void addAnnotation()}>添加标注</button>
      </div>}
      {showList && clips.length > 0 && <div className="spx-clip-summary-list" aria-label="Clip 列表">
        {clips.map(clip => <button key={clip.clipId} type="button" title={`${clip.clipId} · ${clip.gdsFileName}`} className={activeClip?.clipId === clip.clipId ? 'active' : ''} onClick={() => selectClip(clip)}>
          <strong>{clipLabel(clip)}</strong>
          <span>{clip.layers.length} 层 · {clip.polygonCount} 多边形</span>
          {clipHasLayerHeights(clip) && <span>{clipLayerHeightText(clip)}</span>}
          <span>x {clip.bbox.x0}…{clip.bbox.x1} · y {clip.bbox.y0}…{clip.bbox.y1} µm</span>
        </button>)}
      </div>}
      {activeClip && mode === 'draft' && <div className="wf-muted">已选择 Clip：{activeClip.name}。保存工程后，02 编译将使用该 Clip 作为几何。</div>}
      {activeClip && mode === 'project' && <div className="wf-template-row">
        <button className="wf-button ghost" disabled={!!busy} onClick={() => void useClipAsGeometry(activeClip, false)}>用作工程几何</button>
        <button className="wf-button primary" disabled={!!busy || !(project?.steps?.length)}
          title={!(project?.steps?.length) ? '流程还没有步骤，先在 02 添加步骤' : '使用该 Clip 编译当前流程'}
          onClick={() => void useClipAsGeometry(activeClip, true)}>{busy === 'clip-use' ? '编译中…' : '用此 Clip 编译'}</button>
      </div>}
    </div>
  );

  const currentSection = ZONES.find(item => item.id === zone) ?? ZONES[0];
  const projectUnits: LengthUnit = (project?.units as { length?: string } | undefined)?.length === 'nm' ? 'nm' : 'um';
  const lengthUnit = lengthUnitLabel(projectUnits);
  const rateUnit = rateUnitLabel(projectUnits);
  const unitsPerUm = lengthUnitsPerUm(projectUnits);
  const projectDomain = (project?.domain ?? {}) as Record<string, unknown>;
  const projectGeometry = (project?.geometry ?? {}) as Record<string, unknown>;
  const projectSolver = (project?.solver ?? {}) as Record<string, unknown>;
  const projectRandom = (projectSolver.random ?? {}) as Record<string, unknown>;
  const projectGeometryKind = String(projectGeometry.kind ?? '');
  const projectParametricKinds = project?.dimension === 3 ? ['hole', 'fin'] : ['plane', 'trench'];
  const projectFormInvalid = Object.keys(projectFormErrors).length > 0;
  const geometrySourceLabel = projectGeometrySource.kind === 'layout'
    ? `Clip · ${projectGeometrySource.clipName ?? '未选择'}`
    : '参数化';
  const saveLabel = busy === 'project-save'
    ? '保存中…'
    : !project
      ? '未打开工程'
      : !projectId
        ? '草稿（未保存）· 保存'
        : projectDirty
          ? '有未保存更改 · 保存'
          : `已保存${projectSavedAt ? ` · ${formatDateTime(projectSavedAt)}` : ''}`;
  const solverTimeoutError = solverTimeoutFieldError();
  const settingsValidationError = [...SETTINGS_LIMIT_KEYS.map(key => settingsFieldError(key)), solverTimeoutError]
    .find(Boolean) ?? '';
  const renderDomainField = (label: string, key: 'gridDelta' | 'xExtent' | 'yExtent', step = '0.01', title?: string) => {
    const error = projectFormErrors[`domain.${key}`];
    // gridDelta has no backend floor beyond > 0; extents use the dimension's
    // real floor so the spinner/validation cannot re-impose the old 1 µm rule.
    const min = key === 'gridDelta'
      ? 0
      : (project?.dimension === 3 ? MIN_DOMAIN_EXTENT_3D : MIN_DOMAIN_EXTENT_2D) * unitsPerUm;
    const max = (key === 'gridDelta' ? 0.5 : MAX_DOMAIN_EXTENT) * unitsPerUm;
    const nativeStep = Number(step) * unitsPerUm;
    return <label className={`spx-field${error ? ' invalid' : ''}`} key={key}>
      <span title={title ?? key}>{label}</span>
      <input aria-label={`工程 ${key}`} type="number" min={min} max={max} step={nativeStep} value={String(projectDomain[key] ?? '')}
        aria-invalid={!!error}
        onChange={event => updateProjectDomain({ [key]: Number(event.target.value) })} />
      {error && <em className="spx-field-error">{error}</em>}
    </label>;
  };

  const renderGeometryField = (label: string, key: 'height' | 'width' | 'depth' | 'radius' | 'taperAngle' | 'maskHeight', step = '0.01', title?: string) => {
    const error = projectFormErrors[`geometry.${key}`];
    return <label className={`spx-field${error ? ' invalid' : ''}`} key={key}>
      <span title={title ?? key}>{label}</span>
      <input aria-label={`工程几何 ${key}`} type="number" step={step} value={String(projectGeometry[key] ?? '')}
        aria-invalid={!!error}
        onChange={event => updateProjectGeometryField({ [key]: Number(event.target.value) })} />
      {error && <em className="spx-field-error">{error}</em>}
    </label>;
  };

  const renderSolverField = (label: string, key: 'enginePreference' | 'seed' | 'timeoutSeconds' | 'raysPerPoint' | 'cpuThreads', title?: string) => {
    const error = projectFormErrors[`solver.${key}`];
    const value = key === 'seed' ? projectRandom.seed : projectSolver[key];
    if (key === 'enginePreference') {
      return <label className="spx-field" key={key}>
        <span title={title ?? key}>{label}</span>
        <select aria-label="工程引擎" value={String(value ?? 'cpu')}
          onChange={event => updateProjectSolver({ enginePreference: event.target.value })}>
          <option value="cpu">CPU</option>
          <option value="gpu">GPU</option>
          <option value="auto">Auto</option>
        </select>
      </label>;
    }
    return <label className={`spx-field${error ? ' invalid' : ''}`} key={key}>
      <span title={title ?? key}>{label}</span>
      <input aria-label={`工程 ${key}`} type="number" step="1" min={key === 'timeoutSeconds' ? 0 : undefined}
        max={key === 'timeoutSeconds' ? SOLVER_TIMEOUT_MAX : undefined} value={String(value ?? '')}
        aria-invalid={!!error}
        onChange={event => updateProjectSolver({ [key]: Number(event.target.value) })} />
      {error && <em className="spx-field-error">{error}</em>}
    </label>;
  };

  return (
    <div className="spx-app">
      <header className="spx-frame">
        <div className="spx-titlebar">
          <div className="spx-brand">
            <span className="spx-brand-mark">VP</span>
            <strong>Etch Workbench</strong>
            <span className="spx-doc-name">{currentSection.num} {currentSection.label}</span>
          </div>
        </div>
        <div className="spx-menubar">
          {openMenu && <div className="spx-menu-scrim" onClick={() => setOpenMenu(null)} />}
          {[
            ['file', '文件'], ['edit', '编辑'], ['view', '视图'], ['project', '工程'],
            ['run', '运行'], ['tools', '工具'], ['help', '帮助'],
          ].map(([id, label]) => <div className="spx-menu" key={id}>
            <button type="button" className={openMenu === id ? 'spx-menu-button active' : 'spx-menu-button'} aria-expanded={openMenu === id} onClick={() => setOpenMenu(openMenu === id ? null : id)}>{label}</button>
            {openMenu === id && <div className="spx-menu-popover" role="menu">
              {id === 'file' && <>
                <button role="menuitem" onClick={() => { setOpenMenu(null); projectInput.current?.click(); }}>导入工程 JSON…</button>
                <button role="menuitem" disabled={!project} onClick={() => { setOpenMenu(null); void saveProject(); }}>保存工程</button>
                <button role="menuitem" disabled={!project} onClick={() => { setOpenMenu(null); exportProjectJson(); }}>导出工程 JSON…</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); setSetupMode('new'); setDraft(prev => ({ ...prev, source: 'layout' })); gdsInput.current?.click(); }}>上传 GDS 版图…</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); closeProject(); }}>关闭工程 / 返回开始页</button>
                <button role="menuitem" title="Ctrl+," onClick={() => { setOpenMenu(null); setAboutOpen(false); setSettingsOpen(true); }}>设置…</button>
              </>}
              {id === 'edit' && <>
                <button role="menuitem" onClick={() => { setOpenMenu(null); setActivityLog([]); }}>清空输出日志</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); setComparison(null); setCandidateViews({}); setSyncZoom(1); setSyncAxis('xy'); }}>重置分析视图</button>
              </>}
              {id === 'view' && <>
                {ZONES.map(item => <button key={item.id} role="menuitem" onClick={() => { setOpenMenu(null); navigateZone(item.id); }}>{item.num} {item.label} — {item.title}</button>)}
                {onOpenLegacy && <button role="menuitem" onClick={() => { setOpenMenu(null); onOpenLegacy(); }}>传统工作区</button>}
              </>}
              {id === 'project' && <>
                <button role="menuitem" disabled={!project} onClick={() => { setOpenMenu(null); void saveProject(); }}>保存工程</button>
                <button role="menuitem" disabled={!project} onClick={() => { setOpenMenu(null); exportProjectJson(); }}>导出工程 JSON…</button>
                <button role="menuitem" disabled={!canCompile} onClick={() => { setOpenMenu(null); void compileFlow(); }}>编译流程</button>
                <button role="menuitem" disabled={!canRoute} onClick={() => { setOpenMenu(null); void createRoute(); }}>创建 Route</button>
                <button role="menuitem" disabled={!project} onClick={() => { setOpenMenu(null); void saveTemplate(); }}>保存当前步骤为模板</button>
                <button role="menuitem" disabled={!libraryName.trim()} onClick={() => { setOpenMenu(null); void exportLibrary(); }}>导出共享库</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); navigateZone('flow'); }}>打开流程编辑器</button>
              </>}
              {id === 'run' && <>
                <button role="menuitem" disabled={!canRun} onClick={() => { setOpenMenu(null); void (needsPredecessors ? executeThroughCurrent() : execute()); }}>
                  {needsPredecessors ? '执行前序并运行' : '执行当前 Segment'}
                </button>
                <button role="menuitem" disabled={!route || compileStale || routeStale} onClick={() => { setOpenMenu(null); void executeAll(); }}>执行全部 Segment</button>
                <button role="menuitem" disabled={!canMeasure} onClick={() => { setOpenMenu(null); void measure(); }}>测量并绑定来源证据</button>
                <button role="menuitem" disabled={!route || !decisionPrefixId} onClick={() => { setOpenMenu(null); void decideMeasurementBranch(); }}>测量驱动分支</button>
              </>}
              {id === 'tools' && <>
                <button role="menuitem" disabled={!gds} onClick={() => { setOpenMenu(null); void registerLayout(); }}>注册当前 GDS 为 Layout</button>
                <button role="menuitem" disabled={!activeLayout} onClick={() => { setOpenMenu(null); void previewClip(); }}>预览 Clip</button>
                <button role="menuitem" disabled={!activeLayout} onClick={() => { setOpenMenu(null); void saveClip(); }}>保存 Clip</button>
                <button role="menuitem" disabled={!compile || !activeRecipeId} onClick={() => { setOpenMenu(null); void createDoe(); }}>设计 DOE</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); navigateZone('analyze'); }}>打开分析与标定</button>
              </>}
              {id === 'help' && <>
                <button role="menuitem" onClick={() => { setOpenMenu(null); setAboutOpen(true); }}>关于 Etch Workbench</button>
                <button role="menuitem" onClick={() => { setOpenMenu(null); window.open('/api/health', '_blank'); }}>打开健康检查 JSON</button>
              </>}
            </div>}
          </div>)}
          <div className="spx-menu-spacer" />
          <div className="spx-functions" role="tablist" aria-label="Workflow functions">
            {ZONES.map(item => <button
              key={item.id}
              type="button"
              aria-pressed={zone === item.id}
              className={zone === item.id ? 'active' : ''}
              onClick={() => navigateZone(item.id)}
            >{item.num} {item.label}</button>)}
          </div>
        </div>
      </header>

      {error && <div className="wf-error spx-error spx-banner" role="alert"><span className="spx-banner-text" title={error}>{error}</span><BannerClose onClose={() => setError('')} /></div>}

      <div className="spx-body" style={{ '--spx-left-width': `${leftWidth}px` } as CSSProperties}>
        <aside className="spx-left">
          <div className="spx-panel">
            <div className="spx-panel-title"><span>工程</span></div>
            <div className="spx-tree" role="tree" aria-label="Project tree">
              <div className="spx-tree-root"><span>▾</span><strong>{project?.name ?? '未打开工程'}</strong></div>
              <div className="spx-tree-meta">{project ? `${project.dimension ?? '?'}D · ${project.steps?.length ?? 0} 步 · ${projectGeometrySource.kind === 'layout' ? 'GDS Clip' : '参数化'}` : '新建工程或导入 ProjectV7'}</div>
              {[
                {
                  label: '编译',
                  target: 'flow' as const,
                  value: compile ? `${compile.stepCount} 步` : '—',
                  badge: compile ? (compileStale ? '需重新编译' : '已编译') : '未编译',
                  badgeClass: compile ? (compileStale ? 'stale' : 'succeeded') : 'planned',
                  title: compile ? `Compile ${compile.compileId}` : '尚未编译流程',
                },
                {
                  label: 'Route',
                  target: 'run' as const,
                  value: route ? `${route.segments.length} Segment` : '—',
                  badge: route ? '已创建' : '未创建',
                  badgeClass: route ? 'succeeded' : 'planned',
                  title: route ? `Route ${route.route.routeId}` : '尚未创建 Route',
                },
                {
                  label: 'Run',
                  target: 'run' as const,
                  value: segment ? `第 ${segmentNumber} 段` : '—',
                  badge: segment ? statusLabel(segment.status) : '未运行',
                  badgeClass: segment?.status === 'succeeded' ? 'succeeded' : segment?.status === 'running' ? 'running' : segment?.status === 'failed' ? 'failed' : 'planned',
                  title: segment ? `第 ${segmentNumber} 段 · Run ${segment.runId ?? '未记录'} · Segment ${segment.segmentId}` : '尚未执行 Segment',
                },
                {
                  label: '测量',
                  target: 'run' as const,
                  value: measurement ? `第 ${segmentNumber} 段 · ${measurementState.label}` : '—',
                  badge: measurementState.label,
                  badgeClass: measurement
                    ? (measurementHasValue(measurement) ? 'measured' : measurement.status)
                    : 'planned',
                  title: measurement ? `${measurementValueText(measurement)} · Measurement ${measurement.measurementId}` : needsRemeasure ? 'Route 已重建或 Segment 重跑，旧测量已失效，需重新测量' : '尚未测量',
                },
                {
                  label: '实验',
                  target: 'experiment' as const,
                  value: experimentDoc ? `${experimentDoc.variants.length} 变体` : '—',
                  badge: experimentDoc ? statusLabel(experimentDoc.status) : '未创建',
                  badgeClass: experimentDoc?.status === 'completed' ? 'succeeded' : experimentDoc ? 'running' : 'planned',
                  title: experimentDoc ? `Experiment ${experimentDoc.experimentId}` : '尚未创建实验',
                },
                {
                  label: '分析',
                  target: 'analyze' as const,
                  value: compareCandidates.length ? `${compareCandidates.length} 候选` : '—',
                  badge: comparison ? '已载入' : '未载入',
                  badgeClass: comparison ? 'succeeded' : 'planned',
                  title: '候选比较 / process window / 标定',
                },
                {
                  label: '报告',
                  target: 'report' as const,
                  value: reportDocument ? `已生成 · ${formatDateTime(reportDocument.createdAt)}` : '—',
                  badge: reportDocument ? '已生成' : '未生成',
                  badgeClass: reportDocument ? 'succeeded' : 'planned',
                  title: reportDocument ? `${reportDocument.title} · ${formatDateTime(reportDocument.createdAt)}` : '尚未生成报告',
                },
              ].map(item => <div
                key={item.label}
                role="treeitem"
                tabIndex={0}
                aria-selected={zone === item.target}
                className={zone === item.target ? 'spx-tree-item active' : 'spx-tree-item'}
                onClick={() => navigateZone(item.target)}
                onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') navigateZone(item.target); }}
                title={item.title}
              ><span className="spx-tree-label">{item.label}</span><span className="spx-tree-value">{item.value}</span>{item.badge && <span className={`spx-tree-status ${item.badgeClass}`}>{item.badge}</span>}</div>)}
            </div>
            <div className="spx-tree-legend" aria-label="状态图例">
              <span>待执行/未创建</span><span>运行中</span><span>成功/已测量</span><span>失败/待重跑</span>
            </div>
          </div>
          <div className="spx-panel">
            <div className="spx-panel-title"><span>库</span></div>
            <div className="spx-tree" role="tree" aria-label="库分类">
              {([
                { label: '材料', category: 'material' as const, count: libraryItems.filter(item => item.category === 'material').length },
                { label: '工艺', category: 'process' as const, count: libraryItems.filter(item => item.category === 'process').length },
                { label: '流程', category: 'flow' as const, count: libraryItems.filter(item => item.category === 'flow').length },
                { label: '实验', category: 'experiment' as const, count: libraryItems.filter(item => item.category === 'experiment').length },
              ]).map(item => {
                const active = zone === 'flow' && libraryManagerOpen && managerCategory === item.category;
                return <div
                  key={item.label}
                  role="treeitem"
                  tabIndex={0}
                  aria-selected={active}
                  className={active ? 'spx-tree-item active' : 'spx-tree-item'}
                  onClick={() => openLibraryManager(item.category)}
                  onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') openLibraryManager(item.category); }}
                ><span>{item.label}</span><span className="spx-count">{item.count}</span></div>;
              })}
              {([
                { label: '版图', count: layouts.length, onClick: () => { navigateZone('setup'); setSetupMode('layout'); void refreshLayouts(); } },
                { label: 'Clip', count: clips.length, onClick: () => { navigateZone('setup'); setSetupMode('layout'); void refreshLayouts(); } },
                { label: 'Bundles', count: libraryBundles.length, onClick: () => { navigateZone('flow'); setLibraryAdvancedOpen(true); } },
              ]).map(item => <div
                key={item.label}
                role="treeitem"
                tabIndex={0}
                className="spx-tree-item"
                onClick={item.onClick}
                onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') item.onClick(); }}
              ><span>{item.label}</span><span className="spx-count">{item.count}</span></div>)}
            </div>
          </div>
          <div className="spx-left-foot">
            {onOpenLegacy && <button type="button" className="spx-link" onClick={onOpenLegacy}>传统工作区</button>}
            <span>v0.1.0</span>
          </div>
        </aside>
        <div className="spx-splitter" role="separator" aria-orientation="vertical" aria-label="调整左栏宽度"
          onPointerDown={onLeftSplitterDown} onPointerMove={onLeftSplitterMove}
          onPointerUp={onLeftSplitterUp} onPointerCancel={onLeftSplitterUp} />

        <section className="spx-center">
          <div className="spx-doc-header">
            <div>
              <div className="spx-eyebrow">WORKFLOW / {currentSection.num}</div>
              <h1>{libraryManagerOpen ? LIBRARY_CATEGORY_LABELS[managerCategory] : currentSection.title}</h1>
              <p>{libraryManagerOpen ? '按分类维护可复用条目' : currentSection.desc}</p>
            </div>
            <div className="spx-doc-state">
              {project && <span className="spx-state-pill">{`${project.dimension ?? '?'}D · ${geometrySourceLabel}`}</span>}
              {project && <button
                type="button"
                className={`spx-state-pill spx-save-button${projectId && !projectDirty ? ' ok' : ''}`}
                disabled={busy === 'project-save'}
                title="保存当前工程（Ctrl/Cmd+S）"
                onClick={() => void saveProject()}
              >{saveLabel}</button>}
              {stateSaving && <span className="spx-sync-hint">同步状态…</span>}
            </div>
          </div>
          {(restoreNotice || stateNotice) && <div className="spx-banner spx-inline-notice" role="status">
            <span className="spx-banner-text">{[restoreNotice, stateNotice].filter(Boolean).join(' ')}</span>
            <BannerClose onClose={() => { setRestoreNotice(''); setStateNotice(''); }} />
          </div>}

          <main className="wf-grid spx-doc-body">
        <section className={`spx-page wf-setup ${zone === 'setup' ? '' : 'wf-hidden'}`}>
          {!project && <div className="spx-command-row">
            <button type="button" className={setupMode === 'new' ? 'spx-command active' : 'spx-command'} onClick={startNewProject}>
              <span className="spx-command-icon">＋</span>
              <strong>新建工程</strong>
              <span>定义名称、维度与几何来源</span>
            </button>
            <button type="button" className={setupMode === 'examples' ? 'spx-command active' : 'spx-command'} onClick={() => {
              setSetupMode('examples');
              document.getElementById('spx-examples')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }}>
              <span className="spx-command-icon">▦</span>
              <strong>从示例开始</strong>
              <span>载入 2D / 3D starter project</span>
            </button>
            <button type="button" className="spx-command" onClick={() => projectInput.current?.click()}>
              <span className="spx-command-icon">⇧</span>
              <strong>导入 JSON</strong>
              <span>高级：导入现有 ProjectV7</span>
            </button>
          </div>}

          {newProjectArmed && <div className="spx-rebuild" role="alert">
            <span>当前工程「{project?.name ?? '未命名'}」有未保存更改。新建工程会清空当前编辑，未保存内容将丢失。</span>
            <button type="button" className="wf-button" onClick={() => setNewProjectArmed(false)}>取消</button>
            <button type="button" className="wf-button primary" onClick={beginNewProjectDraft}>确认新建</button>
          </div>}

          <div className="spx-sheet spx-recent-projects">
            <div className="spx-sheet-title">最近工程</div>
            <div className="spx-layout-list" aria-label="最近工程列表">
              {projects.length === 0 && <div className="wf-empty spx-library-empty">
                还没有已保存工程。用上方“新建工程”建立草稿，保存后会出现在这里。
              </div>}
              {projects.map(record => <div key={record.projectId} className="spx-project-row">
                <button type="button" className="spx-project-open"
                  onClick={() => void openProject(record)}
                  title={`${record.projectId} · 更新于 ${formatDateTime(record.updatedAt)}`}
                >
                  <strong>{record.name}</strong>
                  <span>{record.dimension}D · {record.stepCount} 步 · {formatDateTime(record.updatedAt)}</span>
                </button>
                <button type="button" className="wf-button ghost"
                  aria-label={`打开工程 ${record.name}`}
                  disabled={!!busy}
                  onClick={() => void openProject(record)}
                >打开</button>
                <button type="button" className="wf-button ghost"
                  onClick={() => setDeleteArmedId(deleteArmedId === record.projectId ? '' : record.projectId)}
                >{deleteArmedId === record.projectId ? '取消' : '删除'}</button>
              </div>)}
            </div>
            {deleteArmedId && <div className="spx-rebuild" role="alert">
              <span>确认删除工程「{projects.find(item => item.projectId === deleteArmedId)?.name ?? deleteArmedId}」？删除后无法恢复。</span>
              <button type="button" className="wf-button" onClick={() => setDeleteArmedId('')}>取消</button>
              <button type="button" className="wf-button primary" disabled={!!busy} onClick={() => {
                const record = projects.find(item => item.projectId === deleteArmedId);
                if (record) void deleteProjectRecord(record);
              }}>确认删除</button>
            </div>}
          </div>

          {!project && <div className="spx-sheet spx-examples" id="spx-examples">
            <div className="spx-sheet-title">示例工程</div>
            <div className="spx-samples">
              {SAMPLE_EXAMPLES.map(item => <button type="button" className="spx-sample" key={item.id}
                data-example-id={item.id} title={`${item.source}\n${item.note}`}
                onClick={() => { void loadSample(item.sample, item.label); }}>
                <strong>{item.label}</strong>
                <span>{item.meta}</span>
                <em>载入示例</em>
              </button>)}
              <button type="button" className="spx-sample" data-example-id="gds-mask-import"
                title={'examples/GDSReader\n需要上传 GDS；2D 图层→材料，3D 还需 baseHeight/height。'}
                onClick={() => {
                  setSetupMode('new');
                  setDraft(previous => ({ ...previous, source: 'layout' }));
                  gdsInput.current?.click();
                }}>
                <strong>GDS Mask Import</strong>
                <span>2D / 3D · GDS 图层→材料</span>
                <em>上传 GDS</em>
              </button>
            </div>
          </div>}

          {(project || gds) && <div className="spx-attached-row">
            {project && <div className="spx-file-name filled">工程 · {project.name ?? '未命名'} · {project.dimension ?? '?'}D · {project.steps?.length ?? 0} 步</div>}
            {gds && <div className="spx-file-name filled">GDS · {gds.fileName}</div>}
          </div>}

          {setupMode === 'new' && <div className="spx-sheet">
            <div className="spx-sheet-title">新建工程</div>
            <div className="spx-form-groups">
              <section className="spx-field-group">
                <div className="spx-group-title">基本</div>
                <div className="spx-field-grid">
                  <label className="spx-field span-2"><span>工程名称</span><input aria-label="新工程名称" value={draft.name} onChange={event => setDraftField('name', event.target.value)} /></label>
                  <div className="spx-field"><span>维度</span>
                    <div className="spx-segment">
                      <button type="button" className={draft.dimension === 2 ? 'active' : ''} onClick={() => setDraftDimension(2)}>2D</button>
                      <button type="button" className={draft.dimension === 3 ? 'active' : ''} onClick={() => setDraftDimension(3)}>3D</button>
                    </div>
                  </div>
                  <label className="spx-field"><span>基底材料</span>
                    <select aria-label="新工程基底材料" value={draft.substrateMaterial} onChange={event => setDraftField('substrateMaterial', event.target.value)}>
                      {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                    </select>
                  </label>
                  <label className="spx-field"><span>掩膜材料</span>
                    <select aria-label="新工程掩膜材料" value={draft.maskMaterial} onChange={event => setDraftField('maskMaterial', event.target.value)}>
                      {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                    </select>
                  </label>
                </div>
              </section>

              <section className="spx-field-group">
                <div className="spx-group-title">几何来源</div>
                <div className="spx-segment spx-source-pick">
                  <button type="button" className={draft.source === 'parametric' ? 'active' : ''} onClick={() => chooseDraftSource('parametric')}>参数化</button>
                  <button type="button" className={draft.source === 'layout' ? 'active' : ''}
                    title="从 GDS 版图 Clip 构建几何"
                    onClick={() => chooseDraftSource('layout')}>GDS / Layout</button>
                </div>
                {draft.dimension === 3 && draft.source === 'layout' && <p className="spx-help">3D 版图几何：为每个图层填写基底高度与厚度（至少两个网格步长）。</p>}
                {draft.source === 'parametric' && <>
                  <div className="spx-geometry-picks">
                    {(draft.dimension === 2 ? ['plane', 'trench'] : ['hole', 'fin']).map(kind => <button
                      type="button" key={kind}
                      className={draft.geometry === kind ? 'active' : ''}
                      onClick={() => setDraftField('geometry', kind)}
                    >{kind === 'plane' ? 'Plane' : kind === 'trench' ? 'Trench' : kind === 'hole' ? 'Hole' : 'Fin'}</button>)}
                  </div>
                  <div className="spx-field-grid">
                    {draft.geometry === 'plane' && <label className="spx-field"><span title="height">{`平面高度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程高度" type="number" value={draft.height} onChange={event => setDraftField('height', Number(event.target.value))} /></label>}
                    {draft.geometry === 'trench' && <>
                      <label className="spx-field"><span title="width">{`沟槽宽度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 Trench 宽度" type="number" value={draft.width} onChange={event => setDraftField('width', Number(event.target.value))} /></label>
                      <label className="spx-field"><span title="depth">{`沟槽深度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 Trench 深度" type="number" value={draft.depth} onChange={event => setDraftField('depth', Number(event.target.value))} /></label>
                    </>}
                    {draft.geometry === 'hole' && <>
                      <label className="spx-field"><span title="radius">{`孔半径 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程孔半径" type="number" value={draft.radius} onChange={event => setDraftField('radius', Number(event.target.value))} /></label>
                      <label className="spx-field"><span title="depth">{`孔深度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程孔深度" type="number" value={draft.depth} onChange={event => setDraftField('depth', Number(event.target.value))} /></label>
                    </>}
                    {draft.geometry === 'fin' && <>
                      <label className="spx-field"><span title="width">{`Fin 宽度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 Fin 宽度" type="number" value={draft.width} onChange={event => setDraftField('width', Number(event.target.value))} /></label>
                      <label className="spx-field"><span title="finHeight">{`Fin 高度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 Fin 高度" type="number" value={draft.finHeight} onChange={event => setDraftField('finHeight', Number(event.target.value))} /></label>
                    </>}
                    {draft.geometry !== 'plane' && <>
                      <label className="spx-field"><span title="taperAngle">侧壁角 (deg)</span><input aria-label="新工程 taper" type="number" value={draft.taperAngle} onChange={event => setDraftField('taperAngle', Number(event.target.value))} /></label>
                      <label className="spx-field"><span title="maskHeight">{`掩膜高度 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 maskHeight" type="number" value={draft.maskHeight} onChange={event => setDraftField('maskHeight', Number(event.target.value))} /></label>
                    </>}
                  </div>
                </>}
                {draft.source === 'layout' && <div className="spx-inline-section">
                  {renderClipEditor('draft')}
                </div>}
              </section>

              <section className="spx-field-group">
                <div className="spx-group-title" title="Domain &amp; Grid">计算域与网格</div>
                <div className="spx-field-grid">
                  <label className="spx-field"><span>长度单位</span>
                    <select aria-label="新工程长度单位" value={draft.unit} onChange={event => changeDraftUnits(event.target.value as LengthUnit)}>
                      <option value="um">µm</option>
                      <option value="nm">nm</option>
                    </select>
                  </label>
                  <label className="spx-field"><span title="gridDelta">{`网格步长 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 gridDelta" type="number" value={draft.gridDelta} onChange={event => setDraftField('gridDelta', Number(event.target.value))} /></label>
                  <label className="spx-field"><span title="xExtent">{`X 范围 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 xExtent" type="number" value={draft.xExtent} onChange={event => setDraftField('xExtent', Number(event.target.value))} /></label>
                  {draft.dimension === 3 && <label className="spx-field"><span title="yExtent">{`Y 范围 (${lengthUnitLabel(draft.unit)})`}</span><input aria-label="新工程 yExtent" type="number" value={draft.yExtent} onChange={event => setDraftField('yExtent', Number(event.target.value))} /></label>}
                </div>
              </section>

              <section className="spx-field-group">
                <div className="spx-group-title" title="Solver">求解器</div>
                <div className="spx-field-grid">
                  <label className="spx-field"><span title="enginePreference">引擎</span>
                    <select aria-label="新工程引擎" value={draft.engine} onChange={event => setDraftField('engine', event.target.value)}>
                      <option value="cpu">CPU</option>
                      <option value="gpu">GPU</option>
                      <option value="auto">Auto</option>
                    </select>
                  </label>
                  <label className="spx-field"><span title="random.seed">随机种子</span><input aria-label="新工程 seed" type="number" value={draft.seed} onChange={event => setDraftField('seed', Number(event.target.value))} /></label>
                  <label className="spx-field"><span title="timeoutSeconds">超时（s）</span><input aria-label="新工程 timeout" type="number" value={draft.timeout} onChange={event => setDraftField('timeout', Number(event.target.value))} /></label>
                  <label className="spx-field"><span title="raysPerPoint">每点射线数</span><input aria-label="新工程 rays" type="number" value={draft.raysPerPoint} onChange={event => setDraftField('raysPerPoint', Number(event.target.value))} /></label>
                  <label className="spx-field"><span title="cpuThreads">CPU 线程</span><input aria-label="新工程线程" type="number" value={draft.cpuThreads} onChange={event => setDraftField('cpuThreads', Number(event.target.value))} /></label>
                </div>
              </section>
            </div>
            <div className="spx-sheet-actions">
              <button type="button" className="wf-button primary" onClick={createDraftProject}
                title={draft.source === 'layout' && !activeClip ? layoutSourceBlockReason() : '建立工程草稿；保存到服务端在工程页完成'}>创建工程草稿</button>
              <button type="button" className="wf-button" onClick={() => setSetupMode('home')}>取消</button>
            </div>
            <p className="spx-form-note">草稿只存在于当前页面；到工程页点击“保存工程”才会写入服务端。步骤在 02 中从库插入或手动添加。</p>
          </div>}

          {setupMode === 'layout' && <div className="spx-sheet">
            <div className="spx-sheet-title">版图 / Clip</div>
            <div className="spx-sheet-actions">
              {project && <button type="button" className="wf-button" onClick={() => setSetupMode('project')}>返回工程定义</button>}
              <span className="spx-help" style={{ margin: 0 }}>版图/Clip 只影响几何来源，不改变工程步骤。</span>
            </div>
            <div className="spx-inline-section">
              <div className="spx-form-title">Layouts</div>
              {layouts.length === 0
                ? <div className="wf-empty spx-library-empty">还没有 Layout。用下方“上传 GDS”选择文件，再点“注册为 Layout”。</div>
                : <div className="spx-layout-list" aria-label="版图列表">
                  {layouts.map(item => <button key={item.layoutId} type="button"
                    className={activeLayout?.layoutId === item.layoutId ? 'active' : ''}
                    onClick={() => void chooseLayout(item.layoutId)}
                  >
                    <strong>{item.fileName}</strong>
                    <span className="wf-muted">{item.structureCount} 结构</span>
                    <span className="spx-count">{activeLayout?.layoutId === item.layoutId ? '当前' : '查看'}</span>
                  </button>)}
                </div>}
            </div>
            <div className="spx-inline-section">
              <div className="spx-form-title">Clips</div>
              {clips.length === 0
                ? <div className="wf-empty spx-library-empty">该 Layout 还没有 Clip。选择结构、图层材料与范围后保存 Clip，即可作为 2D 工程几何。</div>
                : <div className="spx-clip-summary-list" aria-label="Clip 列表">
                  {clips.map(clip => <button key={clip.clipId} type="button" title={`${clip.clipId} · ${clip.gdsFileName}`}
                    className={activeClip?.clipId === clip.clipId ? 'active' : ''}
                    onClick={() => selectClip(clip)}
                  >
                    <strong>{clipLabel(clip)}</strong>
                    <span>{clip.layers.length} 层 · {clip.polygonCount} 多边形</span>
                    {clipHasLayerHeights(clip) && <span>{clipLayerHeightText(clip)}</span>}
                    <span>x {clip.bbox.x0}…{clip.bbox.x1} · y {clip.bbox.y0}…{clip.bbox.y1} µm</span>
                  </button>)}
                </div>}
            </div>
            <div className="spx-inline-section">{renderClipEditor(project ? 'project' : 'draft', false)}</div>
            <p className="spx-form-note">版图/Clip 只作为几何来源，不会覆盖当前工程；需要时在工程页把 Clip 设为几何。</p>
          </div>}

          {setupMode === 'project' && project && <div className="spx-sheet">
            <div className="spx-sheet-title">工程定义 / {project.name ?? '未命名工程'}</div>
            <div className="spx-project-status" role="status" title={projectId ? `工程 ${projectId}` : undefined}>
              <strong className={projectId && !projectDirty ? 'ok' : ''}>工程详情</strong>
              <span>{projectId ? `已写入服务端${projectSavedAt ? ` · 更新于 ${formatDateTime(projectSavedAt)}` : ''}` : '尚未写入服务端'}</span>
              {projectNotice && <span className="spx-project-notice spx-banner-inline">{projectNotice}<BannerClose onClose={() => setProjectNotice('')} /></span>}
            </div>
            <div className="spx-summary-grid">
              <div><span>维度</span><strong>{project.dimension ?? '—'}D</strong></div>
              <div><span>几何来源</span><strong>{geometrySourceLabel}</strong></div>
              <div><span>步骤</span><strong>{project.steps?.length ?? 0}</strong></div>
              <div><span>求解器</span><strong>{ENGINE_LABELS[String(projectSolver.enginePreference ?? '')] ?? '—'}</strong></div>
            </div>
            <div className="spx-sheet-actions">
              <button type="button" className="wf-button primary" disabled={!!busy || projectFormInvalid}
                title={projectFormInvalid ? Object.values(projectFormErrors)[0] : '保存工程定义与步骤'}
                onClick={() => void saveProject()}>
                {busy === 'project-save' ? '保存中…' : projectId ? '保存更改' : '保存工程'}
              </button>
              <button type="button" className="wf-button" disabled={!!busy} onClick={exportProjectJson}>导出工程 JSON</button>
              <button type="button" className="wf-button" onClick={() => navigateZone('flow')}>去 02 定义流程</button>
              <button type="button" className="wf-button" onClick={closeProject}>关闭工程</button>
            </div>
            {projectFormInvalid && <div className="spx-field-error-summary" role="alert" aria-label="工程参数错误">
              参数有误：{Object.values(projectFormErrors).join('；')}
            </div>}
            {overwriteArmed && <div className="spx-rebuild" role="alert">
              <span>已存在同名工程「{overwriteArmed.name}」。覆盖已有工程？会替换它的步骤（当前编辑 {project.steps?.length ?? 0} 步）。</span>
              <button type="button" className="wf-button" onClick={() => { setOverwriteArmed(null); setProjectNotice('已取消覆盖，当前编辑保留。'); }}>取消</button>
              <button type="button" className="wf-button primary" disabled={!!busy} onClick={() => void overwriteProject()}>确认覆盖</button>
            </div>}
            {closeArmed && <div className="spx-rebuild" role="alert">
              <span>工程「{project.name ?? '未命名'}」有未保存更改，关闭会丢弃这些修改。</span>
              <button type="button" className="wf-button" onClick={() => setCloseArmed(false)}>取消</button>
              <button type="button" className="wf-button primary" onClick={resetToStart}>确认关闭</button>
            </div>}
            {compile && <div className={compileStale ? 'wf-summary' : 'wf-summary success'}>
              <strong>{compileStale ? '流程已修改，需要重新编译' : '流程已编译'}</strong>
              <span>{compile.stepCount} 步 · {compile.dimension}D · {compile.projectName}</span>
              <details className="spx-compile-details">
                <summary>编译详情</summary>
                <dl>
                  <dt>Compile ID</dt><dd>{compile.compileId}</dd>
                  <dt>绑定 (binding)</dt><dd>{compile.bindingId}</dd>
                  <dt>Flow</dt><dd>{compile.flowId}</dd>
                </dl>
              </details>
            </div>}

            <div className="spx-inline-section" aria-label="编辑计算域">
              <div className="spx-form-title">编辑参数 · 计算域 / 网格</div>
              <div className="spx-field-grid">
                <div className="spx-field"><span>维度</span>
                  <div className="spx-segment">
                    <button type="button" className={project.dimension === 2 ? 'active' : ''} onClick={() => changeProjectDimension(2)}>2D</button>
                    <button type="button" className={project.dimension === 3 ? 'active' : ''} onClick={() => changeProjectDimension(3)}>3D</button>
                  </div>
                </div>
                <label className="spx-field"><span>长度单位</span>
                  <select aria-label="工程长度单位" value={projectUnits}
                    onChange={event => changeProjectUnits(event.target.value as LengthUnit)}>
                    <option value="um">µm</option>
                    <option value="nm">nm</option>
                  </select>
                </label>
                {renderDomainField(`网格步长 (${lengthUnit})`, 'gridDelta', '0.001', 'gridDelta')}
                {renderDomainField(`X 范围 (${lengthUnit})`, 'xExtent', '0.1', 'xExtent')}
                {project.dimension === 3 && renderDomainField(`Y 范围 (${lengthUnit})`, 'yExtent', '0.1', 'yExtent')}
                <label className="spx-field"><span>{project.dimension === 3 ? '边界 XY' : '边界 X'}</span>
                  <select aria-label="工程边界类型"
                    value={String((project.dimension === 3 ? projectDomain.boundaryXY : projectDomain.xBoundary) ?? 'reflective')}
                    onChange={event => updateProjectDomain(project.dimension === 3
                      ? { boundaryXY: event.target.value }
                      : { xBoundary: event.target.value })}>
                    <option value="reflective">reflective · 反射</option>
                    <option value="periodic">periodic · 周期</option>
                  </select>
                </label>
              </div>
            </div>

            <div className="spx-inline-section" aria-label="编辑几何">
              <div className="spx-form-title">编辑参数 · 几何</div>
              {projectGeometrySource.kind === 'layout'
                ? <p className="spx-help">当前几何来自 GDS Clip「{projectGeometrySource.clipName ?? '未选择'}」；几何字段由 Clip 的图层→材料映射决定。</p>
                : <>
                  <div className="spx-geometry-picks">
                    {projectParametricKinds.map(kind => <button type="button" key={kind}
                      className={projectGeometryKind === kind ? 'active' : ''}
                      onClick={() => changeProjectGeometryKind(kind)}
                    >{kind === 'plane' ? 'Plane' : kind === 'trench' ? 'Trench' : kind === 'hole' ? 'Hole' : 'Fin'}</button>)}
                  </div>
                  <div className="spx-field-grid">
                    {projectGeometryKind === 'plane' && renderGeometryField(`平面高度 (${lengthUnit})`, 'height')}
                    {projectGeometryKind === 'trench' && <>
                      {renderGeometryField(`沟槽宽度 (${lengthUnit})`, 'width')}
                      {renderGeometryField(`沟槽深度 (${lengthUnit})`, 'depth')}
                    </>}
                    {projectGeometryKind === 'hole' && <>
                      {renderGeometryField(`孔半径 (${lengthUnit})`, 'radius')}
                      {renderGeometryField(`孔深度 (${lengthUnit})`, 'depth')}
                    </>}
                    {projectGeometryKind === 'fin' && <>
                      {renderGeometryField(`Fin 宽度 (${lengthUnit})`, 'width')}
                      {renderGeometryField(`Fin 高度 (${lengthUnit})`, 'height')}
                    </>}
                    {['trench', 'hole', 'fin'].includes(projectGeometryKind) && <>
                      {renderGeometryField('侧壁角 (deg)', 'taperAngle')}
                      {renderGeometryField(`掩膜高度 (${lengthUnit})`, 'maskHeight')}
                    </>}
                  </div>
                </>}
              {geometrySaveBlock && <p className="spx-field-error" role="alert">{geometrySaveBlock}</p>}
            </div>

            <div className="spx-inline-section" aria-label="编辑求解器">
              <div className="spx-form-title">编辑参数 · 求解器</div>
              <div className="spx-field-grid">
                {renderSolverField('引擎', 'enginePreference', 'enginePreference')}
                {renderSolverField('随机种子', 'seed', 'random.seed')}
                {renderSolverField('超时（s）', 'timeoutSeconds', 'timeoutSeconds')}
                {renderSolverField('每点射线数', 'raysPerPoint', 'raysPerPoint')}
                {renderSolverField('CPU 线程', 'cpuThreads', 'cpuThreads')}
              </div>
            </div>

            <div className="spx-inline-section">
              <div className="spx-form-title">材料</div>
              {projectGeometryKind === 'gds' && <p className="spx-help">GDS 工程的基底材料由图层→材料映射决定，请在下方 Clip 编辑器设置。</p>}
              <div className="spx-field-grid" hidden={projectGeometryKind === 'gds'}>
                <label className="spx-field"><span>基底材料 substrateMaterial</span>
                  <select aria-label="工程基底材料" value={String(projectGeometry.substrateMaterial ?? 'Si')}
                    onChange={event => updateProjectMaterial('substrateMaterial', event.target.value)}>
                    {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                  </select>
                </label>
                {projectGeometryKind !== 'plane' && <label className="spx-field"><span>掩膜材料 maskMaterial</span>
                  <select aria-label="工程掩膜材料" value={String(projectGeometry.maskMaterial ?? 'Mask')}
                    onChange={event => updateProjectMaterial('maskMaterial', event.target.value)}>
                    {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                  </select>
                </label>}
              </div>
              <p className="spx-help">材料来源 /api/materials，直接写入 ProjectV7 的 geometry 字段。</p>
            </div>
            <div className="spx-inline-section">
              <div className="spx-form-title">几何来源</div>
              <div className="spx-segment spx-source-pick">
                <button type="button" className={projectGeometrySource.kind === 'parametric' ? 'active' : ''}
                  onClick={chooseParametricGeometry}>参数化</button>
                <button type="button" className={projectGeometrySource.kind === 'layout' ? 'active' : ''}
                  title="使用 GDS 版图 Clip 作为工程几何"
                  onClick={chooseLayoutGeometry}>GDS / Layout</button>
              </div>
              {project.dimension === 3 && projectGeometrySource.kind === 'layout'
                && <p className="spx-help">3D 版图几何：每个图层需要基底高度 baseHeight 与厚度 height（至少两个网格步长）。</p>}
              {projectGeometrySource.kind === 'parametric'
                ? <div className="wf-muted">几何类型：{projectGeometryKind || '—'}</div>
                : <>
                  {!projectGeometrySource.clipId && <p className="spx-field-error" role="alert">先选择或保存一个 Clip 作为工程几何</p>}
                  {clips.length > 0 && <div className="spx-clip-summary-list" aria-label="已有 Clip">
                    {clips.map(clip => <button key={clip.clipId} type="button" title={`${clip.clipId} · ${clip.gdsFileName}`}
                      className={projectGeometrySource.clipId === clip.clipId ? 'active' : ''}
                      onClick={() => chooseClipForProject(clip)}
                    >
                      <strong>{clipLabel(clip)}</strong>
                      <span>{clip.layers.length} 层 · {clip.polygonCount} 多边形</span>
                    {clipHasLayerHeights(clip) && <span>{clipLayerHeightText(clip)}</span>}
                      <span>x {clip.bbox.x0}…{clip.bbox.x1} · y {clip.bbox.y0}…{clip.bbox.y1} µm</span>
                    </button>)}
                  </div>}
                  {renderClipEditor('project', false)}
                </>}
            </div>
          </div>}

          <input ref={gdsInput} type="file" accept=".gds,.gdsii" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void uploadGds(file); }} />
          <input ref={projectInput} type="file" accept=".json" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void importProject(file); }} />
        </section>

        <section className={`wf-card wf-flow ${zone === 'flow' ? '' : 'wf-hidden'}`}>
          <div className="wf-card-head">
            <span>02</span>
            <h2>{libraryManagerOpen ? LIBRARY_CATEGORY_LABELS[managerCategory] : '流程编辑'}</h2>
            {libraryManagerOpen && <p className="wf-card-desc">按分类维护可复用条目</p>}
          </div>
          {!project && !libraryManagerOpen && <div className="wf-empty">先在 01 创建或导入工程；库管理不需要打开工程。</div>}
          {project && !libraryManagerOpen && renderFlowBar()}
          {libraryManagerOpen && renderLibraryManager()}
          {project && !libraryManagerOpen && <>
            <div className="spx-flow-summary">
              <div><span>工程</span><strong>{project.name ?? '未命名'}</strong></div>
              <div><span>维度</span><strong>{project.dimension ?? '?'}D</strong></div>
              <div><span>几何</span><strong>{projectGeometrySource.kind === 'layout' ? `Clip · ${projectGeometrySource.clipName ?? '—'}` : '参数化'}</strong></div>
              <div><span>步骤</span><strong>{authoredSteps.length}</strong></div>
            </div>

            {compile && <div className={compileStale ? 'wf-summary' : compileMissingSteps.length ? 'wf-summary warning' : 'wf-summary success'}>
              <strong>{compileStale ? 'Compile 已过期，需要重新编译' : compileMissingSteps.length ? '编译结果不完整' : '编译成功'}</strong>
              <span>{enabledCoverageSteps(authoredSteps).length} 个启用步骤 · 编译 {compile.stepCount} 步 · {compile.dimension}D · 工程 {compile.projectName}</span>
              {compileStale && <span>重新编译后才能创建新的 Route。</span>}
              {compileMissingSteps.length > 0 && <div className="wf-block-reason" role="alert" aria-label="编译缺少步骤">
                编译结果缺少步骤 {compileMissingText}，已阻止创建 Route
              </div>}
              {compileSkippedSteps.length > 0 && <div className="wf-skip-note" role="status" aria-label="已跳过步骤">
                已跳过 {compileSkippedSteps.length} 个未启用步骤：{compileSkippedText}
              </div>}
              <details className="spx-compile-details">
                <summary>查看编译详情</summary>
                <dl>
                  <dt>Compile ID</dt><dd>{compile.compileId}</dd>
                  <dt>绑定 (binding)</dt><dd>{compile.bindingId}</dd>
                  <dt>步骤数</dt><dd>{compile.stepCount}</dd>
                  <dt>Flow</dt><dd>{compile.flowId}</dd>
                </dl>
              </details>
            </div>}

            {!libraryManagerOpen && <>

            {rebuildRouteArmed && <div className="spx-rebuild" role="alert">
              <span>重建 Route 会丢弃当前 Run 与测量结果（现有 {route?.segments.length ?? 0} Segment）。</span>
              <button type="button" className="wf-button" onClick={() => setRebuildRouteArmed(false)}>取消</button>
              <button type="button" className="wf-button primary" disabled={!!busy} onClick={() => void createRoute(true)}>确认重建</button>
            </div>}
            <div className="spx-flow-editor">
              <div className="spx-library-browser" aria-label="插入源">
                <div className="spx-library-head"><span>插入源 · 工艺预设</span></div>
                <div className="spx-library-note">材料 / 流程 / 实验在左树“库”管理</div>
                <div className="spx-library-category-head compact">
                  <strong>工艺预设</strong>
                  <span className={libraryError ? 'spx-library-load-error' : 'wf-muted'}>
                    {libraryLoading ? '加载中…' : libraryError ? '加载失败' : `${processLibraryItems.length} 条`}
                  </span>
                  {libraryError && <button type="button" className="wf-button ghost" onClick={() => void loadLibraryItems()}>重试</button>}
                </div>
                <div className="spx-library-list" aria-label="工艺预设">
                  {libraryLoading && <div className="wf-muted spx-library-empty">加载中…</div>}
                  {!libraryLoading && libraryError && <div className="wf-muted spx-library-empty">加载失败：{libraryError}<button type="button" className="wf-button ghost" onClick={() => void loadLibraryItems()}>重试</button></div>}
                  {!libraryLoading && !libraryError && processLibraryItems.length === 0 && <div className="wf-muted spx-library-empty">
                    工艺库暂无条目。可在左树“库 → 工艺”新增工艺预设。
                  </div>}
                  {!libraryLoading && !libraryError && processLibraryItems.map(item => {
                    const blocked = !project;
                    const meta = `${item.payload.operation === 'deposit' ? '沉积' : '刻蚀'} · ${STEP_LABELS[String(item.payload.model ?? '')] ?? String(item.payload.model ?? '')} · ${String(item.payload.rateMagnitude ?? '—')} µm/s · ${String(item.payload.duration ?? '—')} s`;
                    return <div
                      key={item.itemId}
                      className="spx-library-item"
                      title={item.itemId}
                      draggable={!blocked}
                      onDragStart={event => { event.dataTransfer.setData('text/plain', item.itemId); event.dataTransfer.effectAllowed = 'copy'; }}
                    >
                      <div className="spx-library-item-main">
                        <strong>{item.name}{libraryDuplicateNames.has(item.name) ? '（重名）' : ''}</strong>
                        <span>{meta}</span>
                      </div>
                      <button type="button" className="spx-link" disabled={blocked}
                        title={blocked ? '先在 01 创建或导入工程' : '插入到流程末尾'}
                        onClick={() => void insertLibraryItem(item)}>插入</button>
                    </div>;
                  })}
                </div>
                {!project && <p className="spx-material-blocked">插入工艺预设需要先创建工程（01）。</p>}
                {materialTarget && <div className="spx-material-badge" role="status">
                  <i style={{ background: materialColor(materialCatalog, materialTarget.material) }} aria-hidden="true" />
                  <span>当前材料 <code>{materialTarget.material}</code>（预设 {materialTarget.itemName}）→ {materialTarget.target}{materialTarget.stepName ? ` · ${materialTarget.stepName}` : ''}</span>
                </div>}
              </div>

              <div className="spx-flow-steps"
                aria-label="流程步骤编辑区"
                onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }}
                onDrop={event => {
                  event.preventDefault();
                  if (event.dataTransfer.getData('application/x-workflow-step')) return;
                  const itemId = event.dataTransfer.getData('text/plain');
                  const item = processLibraryItems.find(row => row.itemId === itemId);
                  if (item) void insertLibraryItem(item);
                }}
              >
                <div className="spx-flow-steps-head">
                  <span>流程步骤</span>
                  <button type="button" className="wf-button ghost" onClick={addAuthoredStep}>＋ 添加步骤</button>
                </div>
                {authoredSteps.length === 0
                  ? <div className="wf-empty">把左侧工艺预设拖到这里，或点击“插入”。</div>
                  : <div className="spx-flow-step-scroll">
                    <table className="wf-table" aria-label="流程步骤">
                      <thead><tr><th>#</th><th>步骤</th><th>操作 / 模型</th><th>时长 / 速率</th><th>启用</th><th>排序</th></tr></thead>
                      <tbody>{authoredSteps.map((step, index) => {
                        const rate = step.model === 'directional'
                          ? Number(step.directionalRateMagnitude ?? 0)
                          : Number(step.rateMagnitude ?? 0);
                        const rateText = RATE_STEP_MODELS.has(step.model) ? `${rate} ${rateUnit}` : '—';
                        const modelLabel = STEP_LABELS[step.model] ?? step.model;
                        const operationLabel = step.operation === 'deposit' ? '沉积' : step.operation === 'etch' ? '刻蚀' : (step.operation ?? '');
                        // Geometry steps carry their capability in model fields
                        // (mask layer, cutoff, material, native model), not in a
                        // rate/duration, so the model cell names them.
                        const capability = geometryStepSummary(step, lengthUnit);
                        return <tr key={step.id}
                          draggable
                          className={`${selectedFlowStepId === step.id ? 'selected' : ''}${materialTarget && step.depositMaterial === materialTarget.material ? ' material-match' : ''}`}
                          onClick={() => setSelectedFlowStepId(step.id)}
                          onDragStart={event => {
                            event.dataTransfer.setData('application/x-workflow-step', step.id);
                            event.dataTransfer.effectAllowed = 'move';
                          }}
                          onDragOver={event => {
                            if (!event.dataTransfer.types.includes('application/x-workflow-step')) return;
                            event.preventDefault();
                            event.dataTransfer.dropEffect = 'move';
                          }}
                          onDrop={event => {
                            const dragged = event.dataTransfer.getData('application/x-workflow-step');
                            if (!dragged) return;
                            event.preventDefault();
                            event.stopPropagation();
                            reorderAuthoredStep(dragged, step.id);
                          }}
                        >
                          <td>{String(index + 1).padStart(2, '0')}</td>
                          <td className="wf-step-name" title={step.name}>{step.name}</td>
                          <td>{[operationLabel, modelLabel, capability].filter(Boolean).join(' · ')}</td>
                          <td>{DURATION_STEP_MODELS.has(step.model) ? `${Number(step.duration ?? 0)} s · ${rateText}` : '—'}</td>
                          <td>{step.enabled ? '启用' : '跳过'}</td>
                          <td className="wf-step-order">
                            <button type="button" className="wf-step-move" aria-label={`上移 ${step.name}`} disabled={index === 0}
                              onClick={event => { event.stopPropagation(); moveAuthoredStep(step.id, -1); }}>↑</button>
                            <button type="button" className="wf-step-move" aria-label={`下移 ${step.name}`} disabled={index === authoredSteps.length - 1}
                              onClick={event => { event.stopPropagation(); moveAuthoredStep(step.id, 1); }}>↓</button>
                          </td>
                        </tr>;
                      })}</tbody>
                    </table>
                  </div>}
              </div>

              <div className="spx-step-inspector" aria-label="步骤参数编辑">
                <div className="spx-step-inspector-title">步骤参数</div>
                {selectedFlowStep ? <div className="spx-step-form">
                  <label className="spx-field" data-field="name"><span title="name">名称</span><input aria-label="步骤名称" value={selectedFlowStep.name} onChange={event => updateSelectedStep({ name: event.target.value })} /></label>
                  <label className="spx-field" data-field="enabled"><span title="enabled">启用</span><input aria-label="步骤启用" type="checkbox" checked={selectedFlowStep.enabled} onChange={event => updateSelectedStep({ enabled: event.target.checked })} /></label>
                  {stepSupportsOperation && <label className="spx-field" data-field="operation"><span title="operation">操作</span>
                    <select aria-label="步骤操作" value={selectedStepOperation} onChange={event => updateSelectedStep({ operation: event.target.value })}>
                      <option value="etch">刻蚀 (etch)</option>
                      <option value="deposit">沉积 (deposit)</option>
                    </select>
                  </label>}
                  <label className="spx-field" data-field="model"><span title="model">模型</span>
                    <select aria-label="步骤模型" value={selectedStepModel} onChange={event => updateSelectedStep({ model: event.target.value })}>
                      {stepModelOptions.map(model => <option key={model} value={model}>{STEP_LABELS[model] ?? model}</option>)}
                    </select>
                  </label>
                  {stepSupportsDuration && <label className="spx-field" data-field="duration"><span title="duration">时长（s）</span><input aria-label="步骤时长" type="number" step="0.01" value={Number(selectedFlowStep.duration ?? 0)} onChange={event => updateSelectedStep({ duration: Number(event.target.value) })} /></label>}
                  {stepSupportsRate && <label className="spx-field" data-field="rate"><span title={selectedRateField}>速率（{rateUnit}）</span><input aria-label="步骤速率" type="number" step="0.01" value={selectedRateValue} onChange={event => updateSelectedStep({ [selectedRateField]: Number(event.target.value) })} /></label>}
                  {stepSupportsRateMode && <label className="spx-field" data-field="rateMode"><span title="rateMode">速率模式</span>
                    <select aria-label="步骤速率模式" value={selectedStepModel === 'isotropic' || selectedStepOperation === 'etch' ? selectedStepRateMode : 'uniform'} disabled={selectedStepModel !== 'isotropic' && selectedStepOperation !== 'etch'} onChange={event => updateSelectedStep({ rateMode: event.target.value })}>
                      <option value="uniform">均匀 (uniform)</option>
                      {(selectedStepModel === 'isotropic' || selectedStepOperation === 'etch') && <option value="material">按材料 (material)</option>}
                    </select>
                  </label>}
                  {selectedStepModel === 'isotropic' && <>
                    <label className="spx-field" data-field="isotropicMethod"><span title="isotropicMethod">各向同性算法</span>
                      <select aria-label="isotropicMethod 各向同性算法" value={selectedIsotropicMethod} onChange={event => switchIsotropicMethod(event.target.value)}>
                        <option value="auto">自动 (auto)</option>
                        <option value="levelSet">Level-set</option>
                        <option value="geometric">几何偏移 (geometric)</option>
                      </select>
                    </label>
                    {selectedIsotropicMethod !== 'geometric' && <>
                      <label className="spx-field" data-field="isotropicTimeStepRatio.override"><span title="isotropicTimeStepRatio">覆盖 level-set 时间步比例</span>
                        <input aria-label="isotropicTimeStepRatio 覆盖 level-set 时间步比例" type="checkbox" checked={typeof selectedFlowStep.isotropicTimeStepRatio === 'number'} onChange={event => toggleIsotropicTimeStepRatio(event.target.checked)} />
                      </label>
                      {typeof selectedFlowStep.isotropicTimeStepRatio === 'number' && <label className="spx-field" data-field="isotropicTimeStepRatio"><span title="isotropicTimeStepRatio">level-set 时间步比例</span>
                        <input aria-label="isotropicTimeStepRatio 数值" type="number" min={0.05} max={0.5} step={0.01} value={selectedFlowStep.isotropicTimeStepRatio} onChange={event => updateSelectedStep({ isotropicTimeStepRatio: Number(event.target.value) })} />
                      </label>}
                    </>}
                    {selectedIsotropicMethod === 'geometric' && <p className="spx-help">几何偏移只支持统一速率（选择它会自动切回 uniform 并删除材料表），且厚度至少一个网格。</p>}
                    {selectedIsotropicMethod !== 'geometric' && selectedStepRateMode === 'material' && <>
                      <label className="spx-field" data-field="materialRates"><span title="materialRates">materialRates（JSON 对象）</span>
                        <textarea aria-label="materialRates 各向同性 JSON" value={isotropicMaterialRatesText} spellCheck={false} onChange={event => setIsotropicMaterialRatesText(event.target.value)} />
                      </label>
                      {isotropicMaterialRatesError && <em className="spx-field-error">{isotropicMaterialRatesError}</em>}
                      <div className="spx-step-form-actions">
                        <button type="button" className="wf-button" onClick={applyIsotropicMaterialRatesText}>应用</button>
                        <span className="wf-muted">{'{"Si": 0.1, "SiO2": 0}'}</span>
                      </div>
                      <p className="spx-help">0 表示保护；未列出的材料使用“速率（{rateUnit}）”；刻蚀与沉积都按操作符号写入。</p>
                    </>}
                  </>}
                  {stepSupportsDepositMaterial && <label className="spx-field" data-field="depositMaterial"><span title="depositMaterial · 只列可沉积材料（排除硬掩膜/光刻胶）">沉积材料</span>
                    <select aria-label="步骤沉积材料" value={String(selectedFlowStep.depositMaterial ?? '')} onChange={event => updateSelectedStep({ depositMaterial: event.target.value || undefined })}>
                      <option value="">—</option>
                      {depositMaterialOptions.map(row => <option key={row.name} value={row.name}>{row.name}{materialPreset === row.name ? ' · 库' : ''}{materialTarget?.material === row.name ? ' · 当前材料' : ''}</option>)}
                    </select>
                    {selectedDepositMaterialInvalid && <em className="spx-field-error">当前材料 {String(selectedFlowStep.depositMaterial)} 不可沉积，请重新选择</em>}
                  </label>}
                  {stepShowsParticleFields && <>
                    <label className="spx-field" data-field="stickingProbability"><span title="stickingProbability">粘附系数</span><input aria-label="步骤 stickingProbability" type="number" step="0.01" value={Number(selectedFlowStep.stickingProbability ?? 1)} onChange={event => updateSelectedStep({ stickingProbability: Number(event.target.value) })} /></label>
                    <label className="spx-field" data-field="sourceExponent"><span title="sourceExponent">源指数</span><input aria-label="步骤 sourceExponent" type="number" step="0.1" value={Number(selectedFlowStep.sourceExponent ?? 1)} onChange={event => updateSelectedStep({ sourceExponent: Number(event.target.value) })} /></label>
                  </>}
                  {selectedStepModel === 'gdsBoolean' && <>
                    <label className="spx-field" data-field="booleanOperation"><span title="booleanOperation">布尔操作</span>
                      <select aria-label="GDS 布尔操作" value={String(selectedFlowStep.booleanOperation ?? 'union')} onChange={event => updateSelectedStep({ booleanOperation: event.target.value })}>
                        <option value="intersection">intersection · 交集</option>
                        <option value="difference">difference · 差集</option>
                        <option value="union">union · 并集</option>
                      </select>
                    </label>
                    <label className="spx-field" data-field="applyToAll"><span title="applyToAll">应用到全部</span>
                      <input aria-label="GDS 布尔应用到全部" type="checkbox" checked={selectedFlowStep.applyToAll !== false} onChange={event => updateSelectedStep({ applyToAll: event.target.checked })} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.layer"><span title="gdsMask.layer">掩膜层 layer</span>
                      <input aria-label="GDS 掩膜层 layer" type="number" min={0} max={65535} step={1} value={gdsMaskNumber('layer', 0)} onChange={event => patchSelectedGdsMask({ layer: Math.max(0, Math.trunc(Number(event.target.value) || 0)) })} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.datatype"><span title="gdsMask.datatype">掩膜数据类型 datatype</span>
                      <input aria-label="GDS 掩膜数据类型 datatype" type="number" min={0} max={32767} step={1} value={gdsMaskNumber('datatype', 0)} onChange={event => patchSelectedGdsMask({ datatype: Math.max(0, Math.trunc(Number(event.target.value) || 0)) })} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.unitScale"><span title="gdsMask.unitScale">单位缩放 unitScale</span>
                      <input aria-label="GDS 掩膜单位缩放 unitScale" type="number" min={0.000001} max={1000000} step={0.0001} value={gdsMaskNumber('unitScale', 1)} onChange={event => { const value = Number(event.target.value); patchSelectedGdsMask({ unitScale: Number.isFinite(value) && value > 0 ? value : 1 }); }} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.baseHeight"><span title="gdsMask.baseHeight">起始高度 baseHeight（µm）</span>
                      <input aria-label="GDS 掩膜起始高度 baseHeight（µm）" type="number" min={0} step={0.01} value={gdsMaskNumber('baseHeight', 0)} onChange={event => patchSelectedGdsMask({ baseHeight: Number(event.target.value) })} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.height"><span title="gdsMask.height">掩膜高度 height（µm）</span>
                      <input aria-label="GDS 掩膜高度 height（µm）" type="number" min={0} step={0.01} value={gdsMaskNumber('height', 0.1)} onChange={event => { const value = Number(event.target.value); patchSelectedGdsMask({ height: Number.isFinite(value) && value > 0 ? value : 0.1 }); }} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.retainedRegion"><span title="gdsMask.retainedRegion">保留区域 retainedRegion</span>
                      <select aria-label="GDS 掩膜保留区域 retainedRegion" value={gdsMaskRetainedRegion} onChange={event => patchSelectedGdsMask({ retainedRegion: event.target.value })}>
                        <option value="inside">inside · 保留内部</option>
                        <option value="outside">outside · 保留外部</option>
                      </select>
                    </label>
                    <label className="spx-field" data-field="gdsMask.position.x"><span title="gdsMask.position[0]">位置 X（µm）</span>
                      <input aria-label="GDS 掩膜位置 X（µm）" type="number" step={0.01} value={gdsMaskPosition[0]} onChange={event => patchSelectedGdsMaskPosition(0, Number(event.target.value))} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.position.y"><span title="gdsMask.position[1]">位置 Y（µm）</span>
                      <input aria-label="GDS 掩膜位置 Y（µm）" type="number" step={0.01} value={gdsMaskPosition[1]} onChange={event => patchSelectedGdsMaskPosition(1, Number(event.target.value))} />
                    </label>
                    <label className="spx-field" data-field="gdsMask.position.z"><span title="gdsMask.position[2]">位置 Z（µm）</span>
                      <input aria-label="GDS 掩膜位置 Z（µm）" type="number" step={0.01} value={gdsMaskPosition[2]} onChange={event => patchSelectedGdsMaskPosition(2, Number(event.target.value))} />
                    </label>
                  </>}
                  {selectedStepModel === 'planarize' && <>
                    <label className="spx-field" data-field="planarizeMode"><span title="cutoffHeight | stopMaterial">平坦化模式</span>
                      <select aria-label="平坦化模式" value={planarizeUsesStopMaterial ? 'stopMaterial' : 'cutoffHeight'} onChange={event => switchPlanarizeMode(event.target.value)}>
                        <option value="cutoffHeight">按高度截止 (cutoffHeight)</option>
                        <option value="stopMaterial">停在材料 (stopMaterial)</option>
                      </select>
                    </label>
                    {!planarizeUsesStopMaterial && <label className="spx-field" data-field="cutoffHeight"><span title="cutoffHeight">截止高度 cutoffHeight（{lengthUnit}）</span>
                      <input aria-label={`平坦化截止高度（${lengthUnit}）`} type="number" step={0.01} value={typeof selectedFlowStep.cutoffHeight === 'number' ? selectedFlowStep.cutoffHeight : 0} onChange={event => updateSelectedStep({ cutoffHeight: Number(event.target.value) })} />
                    </label>}
                    {planarizeUsesStopMaterial && <>
                      <label className="spx-field" data-field="stopMaterial"><span title="stopMaterial">停止材料 stopMaterial</span>
                        <select aria-label="平坦化停止材料" value={String(selectedFlowStep.stopMaterial ?? '')} onChange={event => { if (event.target.value) updateSelectedStep({ stopMaterial: event.target.value }); }}>
                          {!stepMaterialNames.includes(String(selectedFlowStep.stopMaterial ?? '')) && <option value={String(selectedFlowStep.stopMaterial ?? '')}>{String(selectedFlowStep.stopMaterial ?? '—')}</option>}
                          {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                        </select>
                      </label>
                      <label className="spx-field" data-field="overPolish"><span title="overPolish">过抛光 overPolish（{lengthUnit}）</span>
                        <input aria-label={`平坦化过抛光 overPolish（${lengthUnit}）`} type="number" min={0} max={100} step={0.01} value={typeof selectedFlowStep.overPolish === 'number' ? selectedFlowStep.overPolish : 0} onChange={event => updateSelectedStep({ overPolish: Number(event.target.value) })} />
                      </label>
                    </>}
                  </>}
                  {selectedStepModel === 'removeMaterial' && <label className="spx-field" data-field="material"><span title="material">移除材料 material</span>
                    <select aria-label="移除材料" value={String(selectedFlowStep.material ?? '')} onChange={event => { if (event.target.value) updateSelectedStep({ material: event.target.value }); }}>
                      <option value="">—</option>
                      {!stepMaterialNames.includes(String(selectedFlowStep.material ?? '')) && String(selectedFlowStep.material ?? '') !== '' && <option value={String(selectedFlowStep.material)}>{String(selectedFlowStep.material)}</option>}
                      {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                    </select>
                  </label>}
                  {selectedStepModel === 'specialized' && <>
                    <label className="spx-field" data-field="nativeModel"><span title="nativeModel">原生模型 nativeModel</span>
                      <select aria-label="专用模型 nativeModel" value={selectedNativeModel} onChange={event => updateSelectedStep({ nativeModel: event.target.value })}>
                        {!selectedNativeModel && <option value="">选择原生模型</option>}
                        {selectedNativeModel && !specializedModelOptions.some(model => model.id === selectedNativeModel)
                          && <option value={selectedNativeModel}>{selectedNativeModel}</option>}
                        {specializedModelOptions.map(model => <option key={model.id} value={model.id}>{model.label ? `${model.label} · ${model.id}` : model.id}</option>)}
                      </select>
                    </label>
                    {specializedModelOptions.length === 0 && <em className="spx-field-error">当前维度与操作没有可用的专用模型</em>}
                    {!!selectedSpecializedModel && ((selectedSpecializedModel.fields?.length ?? 0) > 0 || (selectedSpecializedModel.optionalFields?.length ?? 0) > 0) && <div className="spx-help" aria-label="专用模型字段说明">
                      {(selectedSpecializedModel.fields ?? []).map(field => <div key={field.key}>{field.label ?? field.key} · {field.kind ?? 'value'}</div>)}
                      {(selectedSpecializedModel.optionalFields ?? []).map(field => <div key={`optional-${field.key}`}>{field.label ?? field.key} · {field.kind ?? 'value'}（可选）</div>)}
                    </div>}
                    <label className="spx-field" data-field="nativeParameters"><span title="nativeParameters">nativeParameters（JSON 对象）</span>
                      <textarea aria-label="专用模型参数 JSON" value={nativeParametersText} spellCheck={false} onChange={event => setNativeParametersText(event.target.value)} />
                    </label>
                    {nativeParametersError && <em className="spx-field-error">{nativeParametersError}</em>}
                    <div className="spx-step-form-actions">
                      <button type="button" className="wf-button" onClick={applyNativeParametersText}>应用</button>
                      <span className="wf-muted">仅写入合法的 JSON 对象</span>
                    </div>
                  </>}
                  {selectedStepModel === 'directional' && <>
                    <label className="spx-field" data-field="directionalMode"><span title="mode">速率模式</span>
                      <select aria-label="定向速率模式" value={selectedDirectionalMode} onChange={event => switchDirectionalMode(event.target.value)}>
                        <option value="scalar">标量（direction + 速率）</option>
                        <option value="rateSets">rateSets（多速率组）</option>
                        <option value="materialRates">materialRates（材料速率表）</option>
                      </select>
                    </label>
                    {selectedDirectionalMode !== 'rateSets' && <>
                      <label className="spx-field" data-field="direction.x"><span title="direction[0]">方向 X</span>
                        <input aria-label="定向方向 X" type="number" step={0.01} value={selectedDirection[0]} onChange={event => patchSelectedDirection(0, Number(event.target.value))} />
                      </label>
                      <label className="spx-field" data-field="direction.y"><span title="direction[1]">方向 Y</span>
                        <input aria-label="定向方向 Y" type="number" step={0.01} value={selectedDirection[1]} onChange={event => patchSelectedDirection(1, Number(event.target.value))} />
                      </label>
                      <label className="spx-field" data-field="direction.z"><span title="direction[2]">方向 Z</span>
                        <input aria-label="定向方向 Z" type="number" step={0.01} value={selectedDirection[2]} onChange={event => patchSelectedDirection(2, Number(event.target.value))} />
                      </label>
                      {selectedDirectionalMode === 'scalar' && <>
                        <label className="spx-field" data-field="directionalRateMagnitude"><span title="directionalRateMagnitude">定向速率（{rateUnit}）</span>
                          <input aria-label={`定向速率（${rateUnit}）`} type="number" min={0} max={2} step={0.01} value={Number(selectedFlowStep.directionalRateMagnitude ?? 0.02)} onChange={event => updateSelectedStep({ directionalRateMagnitude: Number(event.target.value) })} />
                        </label>
                        <label className="spx-field" data-field="isotropicRateMagnitude"><span title="isotropicRateMagnitude">各向同性速率（{rateUnit}）</span>
                          <input aria-label={`各向同性速率（${rateUnit}）`} type="number" min={0} max={2} step={0.01} value={Number(selectedFlowStep.isotropicRateMagnitude ?? 0)} onChange={event => updateSelectedStep({ isotropicRateMagnitude: Number(event.target.value) })} />
                        </label>
                      </>}
                    </>}
                    {selectedDirectionalMode === 'rateSets' && <>
                      <label className="spx-field" data-field="rateSets"><span title="rateSets">rateSets（JSON 数组）</span>
                        <textarea aria-label="rateSets JSON" value={rateSetsText} spellCheck={false} onChange={event => setRateSetsText(event.target.value)} />
                      </label>
                      {rateSetsError && <em className="spx-field-error">{rateSetsError}</em>}
                      <div className="spx-step-form-actions">
                        <button type="button" className="wf-button" onClick={applyRateSetsText}>应用</button>
                        <span className="wf-muted">direction 3 个数字；速率 0–2 {rateUnit}</span>
                      </div>
                    </>}
                    {selectedDirectionalMode === 'materialRates' && <>
                      <label className="spx-field" data-field="materialRates"><span title="materialRates">materialRates（JSON 对象）</span>
                        <textarea aria-label="materialRates 定向材料 JSON" value={directionalMaterialRatesText} spellCheck={false} onChange={event => setDirectionalMaterialRatesText(event.target.value)} />
                      </label>
                      {directionalMaterialRatesError && <em className="spx-field-error">{directionalMaterialRatesError}</em>}
                      <div className="spx-step-form-actions">
                        <button type="button" className="wf-button" onClick={applyDirectionalMaterialRatesText}>应用</button>
                        <span className="wf-muted">{'{"Si":{"directional":0.3,"isotropic":0.05}}'}</span>
                      </div>
                      <label className="spx-field" data-field="defaultMaterialRate"><span title="defaultMaterialRate">defaultMaterialRate（JSON 对象）</span>
                        <textarea aria-label="defaultMaterialRate JSON" value={defaultMaterialRateText} spellCheck={false} onChange={event => setDefaultMaterialRateText(event.target.value)} />
                      </label>
                      {defaultMaterialRateError && <em className="spx-field-error">{defaultMaterialRateError}</em>}
                      <div className="spx-step-form-actions">
                        <button type="button" className="wf-button" onClick={applyDefaultMaterialRateText}>应用</button>
                        <span className="wf-muted">{'{"directional":0,"isotropic":0}'}</span>
                      </div>
                      <p className="spx-help">未列出的材料使用 defaultMaterialRate；0 表示不动；刻蚀/沉积由操作自动决定符号。</p>
                    </>}
                  </>}
                  <div className="spx-step-form-actions" title={selectedFlowStep.id}>
                    {compiledByAuthored.get(selectedFlowStep.id) && <button type="button" className="wf-button ghost"
                      disabled={!runStructureSteps.find(row => row.kind === 'step' && row.stepId === compiledByAuthored.get(selectedFlowStep.id))}
                      title="在 03 查看该步骤的结构"
                      onClick={() => {
                        const compiledStepId = compiledByAuthored.get(selectedFlowStep.id);
                        const row = runStructureSteps.find(item => item.kind === 'step' && item.stepId === compiledStepId);
                        if (!row) return;
                        setZone('run');
                        setStructurePick(`${row.runId}:${row.stepId}`);
                      }}>查看结构</button>}
                    <button type="button" className="wf-button ghost" onClick={() => deleteAuthoredStep(selectedFlowStep.id)}>删除步骤</button>
                  </div>
                  <details className="spx-advanced">
                    <summary>高级字段（JSON）</summary>
                    <div className="spx-advanced-body">
                      <p className="spx-help">只读视图：编辑器未覆盖的后端字段也会显示在这里，渲染与展开都不会写入工程。</p>
                      <pre aria-label="步骤字段 JSON" style={{ margin: 0, maxHeight: 220, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', font: '10px/1.5 "Consolas", monospace' }}>{JSON.stringify(selectedFlowStep, null, 2)}</pre>
                    </div>
                  </details>
                </div> : <p className="spx-help spx-step-empty">选择步骤后编辑参数</p>}
              </div>
            </div>
            <details className="spx-advanced">
              <summary>模板（可选）</summary>
              <div className="spx-advanced-body">
                <p className="spx-help">模板作用于当前流程：“插入模板”把模板步骤追加到末尾；“替换流程”丢弃现有步骤后写入模板。</p>
                <div className="spx-flow-template">
                  <label><span>模板</span>
                    <select aria-label="模板选择" value={templateId} onChange={event => setTemplateId(event.target.value)}>
                      <option value="">{templates.length ? '选择模板' : '尚无模板'}</option>
                      {templates.map(item => <option key={item.templateId} value={item.templateId}>{item.name} · {item.stepCount} 步</option>)}
                    </select>
                  </label>
                  <label><span>覆盖变量 duration（s）</span><input aria-label="模板变量值" value={templateVariable} onChange={event => setTemplateVariable(event.target.value)} /></label>
                  <button className="wf-button" aria-label="保存当前步骤为模板" disabled={!project || !authoredSteps.length || !!busy} onClick={() => void saveTemplate()}>保存为模板</button>
                  <button className="wf-button" disabled={!project || !templateId || !!busy} onClick={() => setReplaceTemplateArmed(true)}>替换流程</button>
                  <button className="wf-button" disabled={!project || !templateId || !!busy} onClick={() => void applyTemplate('append')}>插入模板</button>
                  <button className="wf-button" disabled={!compile || !previousCompile || !!busy} onClick={() => void compareCompile()}>比较上一版</button>
                </div>
                <p className="spx-help">“覆盖变量 duration（s）”会替换所选模板里的 <code>{'${duration}'}</code>，单位秒，仅作用于该模板。</p>
                {templates.length === 0 && <p className="spx-material-blocked">尚无模板：先点击“保存为模板”把当前步骤写成模板。</p>}
                {replaceTemplateArmed && <div className="spx-rebuild" role="alert">
                  <span>替换流程会丢弃当前 {authoredSteps.length} 个步骤，并写入所选模板的步骤。此操作不可撤销。</span>
                  <button type="button" className="wf-button" onClick={() => setReplaceTemplateArmed(false)}>取消</button>
                  <button type="button" className="wf-button primary" disabled={!!busy} onClick={() => { setReplaceTemplateArmed(false); void applyTemplate('replace'); }}>确认替换</button>
                </div>}
                {compileDiff && <div className="wf-summary"><strong>编译差异</strong><span>+{compileDiff.addedStepIds.length} / -{compileDiff.removedStepIds.length} / 移动 {compileDiff.movedStepIds.length} / 修改 {compileDiff.changedStepIds.length}</span><span>受影响步骤: {compileDiff.affectedStepIds.map(id => id.slice(0, 8)).join(', ') || '—'}</span>{compileDiff.details?.slice(0, 3).map(item => <span key={item.stepId}>{item.stepId.slice(0, 8)}: {String(item.before.duration ?? '—')} → {String(item.after.duration ?? '—')}</span>)}</div>}
              </div>
            </details>

            <details className="spx-advanced" open={flowAdvancedOpen} onToggle={event => setFlowAdvancedOpen(event.currentTarget.open)}>
              <summary>高级：模块化流程定义（JSON）</summary>
              <div className="spx-advanced-body">
                <div className="wf-card-subhead">模块化流程</div>
                <p className="wf-muted">保存模块与流程定义后展开预览；编译复用现有 Compile 与 SourceMap，不另起执行路径。</p>
                <textarea aria-label="模块 JSON" value={moduleJson} onChange={event => setModuleJson(event.target.value)} placeholder="模块 JSON" />
                <div className="wf-template-row">
                  <button className="wf-button ghost" disabled={!!busy} onClick={() => void saveModule()}>{busy === 'module' ? '保存中…' : '保存模块'}</button>
                  <span className="wf-muted">moduleId 唯一；内容变更会递增模块修订</span>
                </div>
                <textarea aria-label="流程定义 JSON" value={definitionJson} onChange={event => setDefinitionJson(event.target.value)} placeholder="流程定义 JSON" />
                <div className="wf-template-row">
                  <input aria-label="流程变量 JSON" value={definitionVariables} onChange={event => setDefinitionVariables(event.target.value)} placeholder='{"rate": 0.2}' />
                  <button className="wf-button ghost" disabled={!!busy} onClick={() => void saveDefinition()}>{busy === 'definition' ? '保存中…' : '保存定义'}</button>
                  <button className="wf-button ghost" disabled={!activeDefinitionId || !!busy} onClick={() => void expandDefinition()}>{busy === 'expand' ? '展开中…' : '展开预览'}</button>
                  <button className="wf-button primary" disabled={!activeDefinitionId || !project || !!busy} onClick={() => void compileDefinition()}>{busy === 'definition-compile' ? '编译中…' : '编译模块化流程'}</button>
                </div>
                {moduleStatus && <div className="wf-summary spx-banner" role="status"><strong>{moduleStatus}</strong><span>definition {activeDefinitionId || '—'}</span><BannerClose onClose={() => setModuleStatus('')} /></div>}
                {expansion && <>
                  <div className="wf-summary">
                    <strong>展开预览</strong>
                    <span>{expansion.stepCount} 步 · 深度 {expansion.maxDepth}</span>
                    <span>{expansion.moduleInstances.length} 模块实例 · {expansion.iterations.length} 循环迭代</span>
                  </div>
                  {expansion.variables.length > 0 && <div className="wf-muted">变量: {expansion.variables.map(item => `${item.name}=${String(item.value)}${item.unit ? ` ${item.unit}` : ''} (${item.source})`).join(' · ')}</div>}
                  {expansion.iterations.length > 0 && <div className="wf-muted">循环: {expansion.iterations.map(item => `${item.path} · ${item.variable}[${item.index}]=${String(item.value)}`).join(' · ')}</div>}
                  {expansion.moduleInstances.length > 0 && <div className="wf-muted">模块实例: {expansion.moduleInstances.map(item => `${item.instancePath} → ${item.moduleId}`).join(' · ')}</div>}
                  <table className="wf-table">
                    <thead><tr><th>步骤 ID</th><th>模块 / 路径</th><th>参数</th></tr></thead>
                    <tbody>
                      {expansion.steps.map(step => <tr key={step.authoredStepId}>
                        <td><code>{step.authoredStepId}</code></td>
                        <td><code>{step.moduleId ? `${step.moduleId} @ ${step.moduleInstanceIdPath}` : step.flowPath}</code></td>
                        <td>{Object.entries(step.parameterProvenance).map(([key, item]) => `${key}=${JSON.stringify(item.resolvedValue)}`).join(' · ')}</td>
                      </tr>)}
                    </tbody>
                  </table>
                </>}
              </div>
            </details>

            <details className="spx-advanced" open={libraryAdvancedOpen} onToggle={event => setLibraryAdvancedOpen(event.currentTarget.open)}>
              <summary>高级：共享流程库</summary>
              <div className="spx-advanced-body">
                <p className="wf-muted">导出全部模块、流程定义、模板与当前实验的可复用条件；导入时明确 reject/clone 冲突策略。实验只保留条件、重复数与 seed，不带运行状态。</p>
                <div className="wf-template-row">
                  <input aria-label="共享库名称" value={libraryName} onChange={event => setLibraryName(event.target.value)} placeholder="共享库名称" />
                  <input aria-label="共享库描述" value={libraryDescription} onChange={event => setLibraryDescription(event.target.value)} placeholder="描述（可选）" />
                  <button className="wf-button ghost" aria-label="导出共享库" disabled={!!busy} onClick={() => void exportLibrary()}>{busy === 'library' ? '处理中…' : '导出共享库'}</button>
                </div>
                <div className="wf-template-row">
                  <select aria-label="共享库 bundle 选择" value={libraryBundleId} onChange={event => setLibraryBundleId(event.target.value)}>
                    <option value="">选择 bundle</option>
                    {libraryBundles.map(item => <option key={item.bundleId} value={item.bundleId}>{item.name} · {item.bundleId.slice(0, 12)}…</option>)}
                  </select>
                  <select aria-label="共享库导入策略" value={libraryPolicy} onChange={event => setLibraryPolicy(event.target.value as 'reject' | 'clone')}>
                    <option value="reject">reject · 冲突即失败</option>
                    <option value="clone">clone · 生成新 ID</option>
                  </select>
                  <button className="wf-button primary" aria-label="导入共享库" disabled={!libraryBundleId || !!busy} onClick={() => void importLibraryBundle()}>{busy === 'library' ? '处理中…' : '导入共享库'}</button>
                </div>
                {libraryStatus && <div className="wf-summary spx-banner" role="status"><strong>{libraryStatus}</strong><span>{libraryBundles.length} 个本地 bundle</span><BannerClose onClose={() => setLibraryStatus('')} /></div>}
                {libraryBundles.length > 0 && <table className="wf-table" aria-label="共享库列表">
                  <thead><tr><th>共享库</th><th>内容</th><th>创建时间</th></tr></thead>
                  <tbody>
                    {libraryBundles.slice(0, 8).map(item => <tr key={item.bundleId}>
                      <td><code>{item.bundleId}</code><div className="wf-muted">{item.name}</div></td>
                      <td>{item.moduleCount} 模块修订 · {item.definitionCount} 定义 · {item.templateCount} 模板 · {item.experimentTemplateCount} 实验模板</td>
                      <td>{formatDateTime(item.createdAt)}</td>
                    </tr>)}
                  </tbody>
                </table>}
                <details className="spx-advanced">
                  <summary>高级：粘贴 bundle JSON</summary>
                  <div className="spx-advanced-body">
                    <textarea aria-label="共享库 JSON" value={libraryBundleJson} onChange={event => setLibraryBundleJson(event.target.value)} placeholder="粘贴 bundle JSON 以便跨机器交换" />
                    <div className="wf-template-row">
                      <button className="wf-button ghost" aria-label="从 JSON 导入共享库" disabled={!libraryBundleJson.trim() || !!busy} onClick={() => void importLibraryJson()}>{busy === 'library' ? '处理中…' : '从 JSON 导入'}</button>
                      <span className="wf-muted">bundleId 由内容 hash 生成；reject 保留原 ID，clone 为定义/模板/实验生成新 ID。</span>
                    </div>
                  </div>
                </details>
                {libraryResult && <div className="wf-summary" aria-label="共享库导入结果">
                  <strong>{libraryResult.bundleId}</strong>
                  <span>定义: {libraryResult.definitionIds.join(', ') || '—'}</span>
                  <span>模板: {libraryResult.templateIds.join(', ') || '—'}</span>
                  <span>实验: {libraryResult.experimentIds.join(', ') || '—'}</span>
                  {libraryResult.renames.length > 0 && <span>新 ID: {libraryResult.renames.map(item => `${item.previousId} → ${item.importedId}`).join(' · ')}</span>}
                  {libraryResult.skipped.length > 0 && <span>跳过: {libraryResult.skipped.join(' · ')}</span>}
                  {libraryResult.conflicts.length > 0 && <span>冲突: {libraryResult.conflicts.join(' · ')}</span>}
                </div>}
              </div>
            </details>
            </>}
          </>}
        </section>

        <section className={`wf-card wf-run ${zone === 'run' ? '' : 'wf-hidden'}`}>
          {runNotice && <div className="wf-run-notice" role="status" aria-label="运行提示">{runNotice}</div>}
          {!route && <div className="wf-empty">在 02 编译流程并创建 Route 后，这里执行 Segment 与测量。</div>}
          {(routeStale || needsRemeasure) && <div className="wf-summary success">
            {routeStale && <span className="wf-rerun">流程已变更，现有 Segment 待重跑</span>}
            {needsRemeasure && <span className="wf-rerun">测量已失效，需重新测量</span>}
          </div>}
          {compileSkippedSteps.length > 0 && <div className="wf-skip-note" role="status" aria-label="已跳过步骤">
            已跳过 {compileSkippedSteps.length} 个未启用步骤：{compileSkippedText}
          </div>}
          {compileMissingSteps.length > 0 && <div className="wf-block-reason" role="alert" aria-label="编译缺少步骤">
            编译结果缺少步骤 {compileMissingText}，已阻止创建 Route
          </div>}
          {(compileStale || routeStale) && <div className="wf-block-reason" role="alert" aria-label="执行禁用原因">
            {compileStale ? '编译已过期，先重新编译并重建 Route。' : 'Route 基于旧 Compile，先重新编译并重建 Route。'}
          </div>}

          <div className="wf-structure-panel" aria-label="当前 Run 结构">
            {(runningSegment || health?.activeRunId) && <div className="wf-run-progress" aria-label="运行进度"
              title={runningSegment ? `已完成 ${completedStepCount}/${totalStepCount} 步` : undefined}>
              {runningSegment && <div className="wf-run-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={totalStepCount} aria-valuenow={completedStepCount}>
                <span style={{ width: `${totalStepCount ? Math.min(100, Math.round((completedStepCount / totalStepCount) * 100)) : 0}%` }} />
              </div>}
              <button type="button" className="wf-button" disabled={stoppingRun} onClick={() => void stopRunningRun()}>
                {stoppingRun ? '停止中…' : '停止运行'}
              </button>
            </div>}
            {runStructureSteps.length > 0 && <div className="wf-structure-timeline" role="tablist" aria-label="结构步骤时间轴">
              {runStructureSteps.map((row, index) => {
                const key = `${row.runId}:${row.stepId}`;
                const active = key === structurePick;
                return <Fragment key={key}>
                  {index > 0 && <span className="wf-step-chip-sep" aria-hidden="true">→</span>}
                  <button type="button" role="tab" aria-selected={active}
                    className={`wf-step-chip${active ? ' active' : ''}${row.kind === 'initial' ? ' initial' : ''}${row.executed ? '' : ' pending'}`}
                    data-step-id={row.stepId} data-run-id={row.runId} data-segment-id={row.segmentId}
                    data-kind={row.kind} data-executed={row.executed ? 'true' : 'false'}
                    title={`${row.label} · ${row.stepId}${row.executed ? ` · ${row.runId}` : ` · ${row.pendingReason ?? '待执行'}`}`}
                    onClick={() => setStructurePick(key)}>
                    <span className="wf-step-chip-index" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                    <span className="wf-step-chip-label" title={row.label}>{row.label}</span>
                    <span className={`wf-step-chip-state${row.executed ? ' ok' : ' pending'}`}>{row.executed ? '成功' : row.pendingReason ?? '待执行'}</span>
                  </button>
                </Fragment>;
              })}
            </div>}
            {structureSelection && structureSelection.executed && structureDimension === 3
              ? <div className="wf-structure-body wf-structure-body--3d">
                <div className="wf-structure-3d">
                  <WorkflowViewer3D
                    runId={structureSelection.runId}
                    stepId={structureSelection.stepId}
                    dimension={3}
                    height={previewHeight}
                    stepIds={structureStepIds}
                    marker={viewer3dMarker}
                  />
                  <div className="wf-preview-resizer" role="separator" aria-orientation="horizontal" aria-label="调整预览高度"
                    onPointerDown={onPreviewSplitterDown} onPointerMove={onPreviewSplitterMove}
                    onPointerUp={onPreviewSplitterUp} onPointerCancel={onPreviewSplitterUp} />
                </div>
                <div className="wf-structure-side">
                  <div className="wf-structure-info">
                    <span className="wf-muted">步骤</span>
                    <strong title={structureSelection.stepId}>{structureSelection.label}</strong>
                    <span className="wf-muted" title={structureSelection.runId}>第 {structureSelection.segmentIndex + 1} 段 · Run 已记录</span>
                  </div>
                  {selectionMeasurement && <div className="wf-structure-info">
                    <span className="wf-muted">测量值</span>
                    <strong title={measurementReasonDetail(selectionMeasurement.reason)}>
                      {measurementHasValue(selectionMeasurement) ? `${formatMicrons(selectionMeasurement.value as number)} ${displayUnit(selectionMeasurement.unit)}` : '—'}
                    </strong>
                    <span className="wf-muted">{viewer3dMeasurementText()}</span>
                  </div>}
                  {!selectionMeasurement && <div className="wf-muted">{measurement ? '测量属于另一个步骤，切回该步骤查看' : '测量后在此显示测量位置'}</div>}
                </div>
              </div>
              : structureSelection && structureView
                ? <div className="wf-structure-body">
                  <StructureCanvas
                    view={structureView}
                    frame={structureFrame(structureView, 'xy')}
                    axis="xy"
                    marker={displayMeasurementMark}
                    note={structureNote}
                    label="当前 Run 结构视图"
                  />
                  <div className="wf-structure-side">
                    <div className="spx-field">
                      <span>视图轴</span>
                      <code className="wf-axis-chip" aria-label="结构视图轴">X – 高度</code>
                    </div>
                    <div className="wf-structure-stats">
                      <span>拓扑</span><code>{topologyLabel(structureView.topology)}</code>
                      <span>点</span><code>{structureView.pointCount}</code>
                      <span>线</span><code>{structureView.lineCount}</code>
                      <span>三角形</span><code>{structureView.triangleCount}</code>
                      <span>单位</span><code>{structureUnit(structureView)}</code>
                      <span>X 范围</span><code>{structureViewExtent ? `${formatNumber(structureViewExtent.minU)} … ${formatNumber(structureViewExtent.maxU)}` : '—'}</code>
                      <span>高度范围</span><code>{structureViewExtent
                        ? structureViewExtent.flatV
                          ? `${formatMicrons(structureViewExtent.maxV)}（平坦）`
                          : `${formatMicrons(structureViewExtent.minV)} … ${formatMicrons(structureViewExtent.maxV)}`
                        : '—'}</code>
                    </div>
                    <div className="wf-structure-legend">
                      {structureLegend(structureView).map(row => <div key={row.id}>
                        <i style={{ background: row.color }} aria-hidden="true" />
                        <span style={{ marginLeft: 0, color: '#26313a' }}>{row.name}</span>
                        <span>{row.count}</span>
                      </div>)}
                    </div>
                    {selectionMeasurement && displayMeasurementMark && <div className="wf-muted">测量标记：{displayMeasurementMark.label}</div>}
                  </div>
                </div>
                : <div className="wf-structure-empty">
                  <div className="wf-empty">
                    {structureSelection
                      ? !structureSelection.executed
                        ? `该步骤尚未执行（${structureSelection.pendingReason ?? '待执行'}），执行对应 Segment 后显示结构。`
                        : structureLoading
                          ? '结构快照加载中…'
                          : structureError
                            ? `暂无结构快照：${structureError}`
                            : '结构视图准备中…'
                      : segment?.status === 'running'
                        ? 'Segment 运行中，完成后显示结构。'
                        : route
                          ? '执行下方的「执行当前 Segment」后，这里按「初始结构 → 每个工艺步骤」预览结构。'
                          : '在 02 编译流程并创建 Route 后，这里显示每个工艺步骤的结构。'}
                  </div>
                </div>}
          </div>

          {segment && <>
            <div className="spx-run-actions">
              <button className="wf-button primary" disabled={!canRun}
                title={needsPredecessors
                  ? `前序 ${blockingPredecessors.length} 个 Segment 尚未成功；点击后按顺序执行到当前 Segment`
                  : '从当前 Segment 的起点执行'}
                onClick={() => void (needsPredecessors ? executeThroughCurrent() : execute())}>
                {busy === 'execute' ? '启动中…' : needsPredecessors ? '执行前序并运行' : '执行当前 Segment'}
              </button>
              <button className="wf-button" disabled={!route || !!busy || compileStale || routeStale} onClick={() => void executeAll()}>执行全部 Segment</button>
              {runningSegment && runningSegment.segmentId !== segment?.segmentId && segment?.status === 'planned' && (
                <span className="wf-muted" aria-label="自动停止提示">将自动停止上一个运行</span>
              )}
              {segment?.status === 'planned' && needsPredecessors && <span className="wf-muted" aria-label="前序 Segment 提示">
                前序 {blockingPredecessors.length} 个 Segment 尚未成功：会按顺序执行到当前 Segment，失败即停止。
              </span>}
              <button className="wf-button" disabled={!canMeasure || recipeXOutOfDomain}
                title={measureBlockedReason || '按当前配方测量当前 Run'}
                onClick={() => void measure()}>
                {busy === 'measure' ? '测量中…' : '测量'}
              </button>
            </div>
            {measureBlockedReason && <div className="wf-block-reason" aria-label="测量禁用原因">{measureBlockedReason}</div>}
            <div className="wf-recipe spx-recipe-inline">
              <div className="wf-recipe-title">测量配方 · 作用于当前 Run 的 {stepDisplayName(segment.throughStepId)}</div>
              <div className="wf-recipe-fields">
                <label><span>测量方法</span>
                  <select aria-label="测量方法" value={recipeMethod} onChange={event => setRecipeMethod(event.target.value as typeof recipeMethod)}>
                    <option value="etch_depth" title="etch_depth">刻蚀深度</option>
                    <option value="cd_at_height" title="cd_at_height">指定高度 CD</option>
                  </select>
                </label>
                <label><span>测量材料</span>
                  <select aria-label="测量材料" value={recipeMaterial} onChange={event => setRecipeMaterial(event.target.value)}>
                    {materialCatalog.map(row => <option key={row.name} value={row.name}>{row.name} · {row.category ?? 'Generic'}</option>)}
                  </select>
                </label>
                {recipeMethod === 'cd_at_height'
                  ? <label><span>CD 高度（µm）</span><input aria-label="CD 高度" value={recipeHeight} onChange={event => setRecipeHeight(event.target.value)} /></label>
                  : <>
                    <label><span>X 位置（µm）</span><input aria-label="深度 X" value={recipeX} onChange={event => setRecipeX(event.target.value)} /></label>
                    <label><span>参考高度（µm）</span><input aria-label="参考高度" value={recipeRef} onChange={event => setRecipeRef(event.target.value)} /></label>
                  </>}
              </div>
              {activeRecipeSummary && (
                <div className="wf-recipe-current" aria-label="当前配方">
                  <strong>当前配方</strong>
                  <span>{metricMethodLabel(activeRecipeSummary.method)} · {activeRecipeSummary.material}</span>
                  {activeRecipeSummary.method === 'etch_depth'
                    ? <span>X = {formatNumber(activeRecipeSummary.x ?? 0)} µm · 参考高度 {formatNumber(activeRecipeSummary.referenceHeight ?? 0)} µm</span>
                    : <span>高度 = {formatNumber(activeRecipeSummary.height ?? 0)} µm</span>}
                  <span className="wf-muted">v{activeRecipeVersion}</span>
                  {activeRecipeXOutOfDomain && <span className="wf-field-error" role="alert">X 超出计算域（{recipeDomainText}）</span>}
                </div>
              )}
              {recipeMethod === 'etch_depth' && recipeXOutOfDomain && (
                <div className="wf-field-error" role="alert" aria-label="配方 X 校验错误">
                  X 超出计算域（{recipeDomainText}）：请把 X 调整到域内再测量。
                </div>
              )}
              <p className="wf-muted wf-recipe-note">深度：在 X 位置量到参考高度的高度差；指定高度 CD：在给定高度量开口宽度。材料决定交点所属材料。</p>
            </div>
            {segment.status === 'running' && <div className="wf-progress"><i /></div>}
            {measurement && <div className={`wf-summary${measurementHasValue(measurement) ? '' : ' warning'}`} aria-label="当前测量">
              <strong>测量 · {statusLabel(measurement.status)}</strong>
              <span title={measurementReasonDetail(measurement.reason)}>{measurementValueText(measurement)}</span>
              {!measurementHasValue(measurement) && <span>说明：{measurementReasonText(measurement.reason)}</span>}
              <span>绑定 (binding)：{bindingLabel(measurement.bindingStatus)} · 等价 (equivalence)：{bindingLabel(measurement.equivalenceStatus)}</span>
            </div>}
            {metrics.length > 0 && <table className="wf-table" aria-label="测量记录表"><thead><tr><th>状态</th><th>数值</th><th>单位</th><th>说明</th><th>配方 / 步骤</th><th>绑定 / 等价</th><th /></tr></thead><tbody>{metrics.map(item => <tr key={item.measurementId} title={measurementReasonDetail(item.reason)}>
              <td><span className={`wf-status ${item.status}`}>{statusLabel(item.status)}</span></td>
              <td>{measurementHasValue(item) ? formatNumber(item.value) : '—'}</td>
              <td>{measurementUnitText(item)}</td>
              <td>{measurementHasValue(item) ? '—' : measurementReasonText(item.reason)}</td>
              <td>{item.recipeId} v{item.recipeVersion} · <span title={item.method}>{metricMethodLabel(item.method)}</span> · {item.material} · {stepDisplayName(item.sourceStepId ?? '')}</td>
              <td>{bindingLabel(item.bindingStatus)} / {bindingLabel(item.equivalenceStatus)}</td>
              <td><button className="wf-link" aria-label={`查看 ${item.measurementId} 证据`} onClick={() => void showMetricDetail(item)}>详情</button></td>
            </tr>)}</tbody></table>}
            {metricDetail && <div className="wf-evidence">
              <div className="wf-evidence-head"><strong>{metricDetail.measurement.measurementId}</strong><span className={`wf-status ${metricDetail.measurement.status}`}>{statusLabel(metricDetail.measurement.status)}</span></div>
              <div className="wf-evidence-grid">
                <span>方法</span><code>{metricDetail.recipe.method}</code>
                <span>材料</span><code>{metricDetail.recipe.material}</code>
                <span>配方</span><code>{metricDetail.recipe.recipeId} v{metricDetail.recipe.version}</code>
                <span>参数</span><code>{JSON.stringify(metricDetail.recipe.parameters)}</code>
                <span>快照</span><code>{metricDetail.measurement.sourceSnapshotHash.slice(0, 16)}…</code>
                <span>交点</span><code>{metricDetail.measurement.intersections.join(', ') || '—'}</code>
                <span>不确定度</span><code>{metricDetail.measurement.uncertainty ?? '—'} ({uncertaintyLabel(metricDetail.measurement.uncertaintyStatus)})</code>
                <span>绑定 (binding)</span><code>{bindingLabel(metricDetail.measurement.bindingStatus)}{metricDetail.measurement.bindingEvidence ? ` · ${metricDetail.measurement.bindingEvidence.methodVersion}` : ''}</code>
                <span>等价 (equivalence)</span><code>{bindingLabel(metricDetail.measurement.equivalenceStatus)}</code>
                <span>证据记录</span><code>{metricDetail.provenance.verifications.map(row => `${row.status}/${row.evidence.methodVersion}`).join(', ') || '—'}</code>
              </div>
            </div>}
            {provenance && <div className="wf-provenance">
              <div className="wf-muted">验证记录: {provenance.verifications.length}</div>
              {provenance.verifications.map(item => <div className="wf-verify" key={item.verificationId}><span className={`wf-status ${item.status}`}>{statusLabel(item.status)}</span><code>{item.verificationId.slice(0, 10)}…</code><span>{item.evidence.methodVersion}</span></div>)}
            </div>}

            <details className="spx-advanced">
              <summary>高级：分支与测量决策</summary>
              <div className="spx-advanced-body">
                <div className="wf-branch-row">
                  <input aria-label="尾步时长" value={branchDuration} onChange={event => setBranchDuration(event.target.value)} />
                  <button className="wf-button ghost" disabled={!route || !!busy} onClick={() => void branchLastStep()}>修改尾步并分支</button>
                </div>
                <div className="wf-decision">
                  <div className="wf-recipe-title">测量驱动分支</div>
                  <select
                    aria-label="决策前缀 Segment"
                    value={decisionPrefixId}
                    onChange={event => setDecisionPrefixId(event.target.value)}
                    disabled={!branchablePrefixes.length}
                  >
                    {!branchablePrefixes.length && <option value="">无可用前缀（先执行保留后续步骤的 Segment）</option>}
                    {branchablePrefixes.map(item => (
                      <option key={item.segmentId} value={item.throughStepId}>
                        {item.segmentId.slice(0, 12)} · through {authoredByCompiled.get(item.throughStepId) ?? item.throughStepId.slice(0, 10)}
                      </option>
                    ))}
                  </select>
                  <select
                    aria-label="决策分支止于"
                    value={decisionThroughId}
                    onChange={event => setDecisionThroughId(event.target.value)}
                    disabled={!decisionSuffix.length}
                  >
                    {!decisionSuffix.length && <option value="">无后续步骤</option>}
                    {decisionSuffix.map(stepId => (
                      <option key={stepId} value={stepId}>{authoredByCompiled.get(stepId) ?? stepId.slice(0, 10)}</option>
                    ))}
                  </select>
                  <div className="wf-muted">
                    可用前缀：{branchablePrefixes.length ? branchablePrefixes.map(item => authoredByCompiled.get(item.throughStepId) ?? item.throughStepId.slice(0, 10)).join(' / ') : '无（先执行前缀 Segment）'}
                    {' · '}当前配方：{activeRecipeId ? `${activeRecipeId} v${activeRecipeVersion}` : '未固定（将按上方配方创建）'}
                  </div>
                  <div className="wf-decision-row">
                    <select aria-label="决策比较符" value={decisionOperator} onChange={event => setDecisionOperator(event.target.value as typeof decisionOperator)}>
                      <option value="gt">&gt;</option>
                      <option value="gte">≥</option>
                      <option value="lt">&lt;</option>
                      <option value="lte">≤</option>
                      <option value="eq">=</option>
                    </select>
                    <input aria-label="决策阈值" value={decisionThreshold} onChange={event => setDecisionThreshold(event.target.value)} placeholder="threshold" />
                  </div>
                  <textarea aria-label="true 分支参数 JSON" value={decisionTrueParameters} onChange={event => setDecisionTrueParameters(event.target.value)} placeholder='true 分支：{"step_02":{"duration":0.02}}' />
                  <textarea aria-label="false 分支参数 JSON" value={decisionFalseParameters} onChange={event => setDecisionFalseParameters(event.target.value)} placeholder='false 分支：{"step_02":{"duration":0.05}}' />
                  <button className="wf-button primary" aria-label="测量并生成分支" disabled={!route || !branchablePrefixes.length || !decisionThroughId || !!busy} onClick={() => void decideMeasurementBranch()}>
                    {busy === 'decision' ? '测量并决策中…' : '测量并生成分支'}
                  </button>
                  <div className="wf-muted">决策只改动「分支止于」之前的步骤；止于中间步骤时，可在其运行结束后继续测量决策，形成同一 workflow run 的序列。</div>
                </div>
                {decisions.length > 0 && <div className="wf-muted">
                  工作流 run：{Array.from(new Set(decisions.map(item => item.workflowRunId))).map(runId => {
                    const rows = decisions.filter(item => item.workflowRunId === runId);
                    return `${runId}（${rows.length} 决策 / ${rows.filter(item => item.replayOf).length} 重放）`;
                  }).join('；')}
                </div>}
                {decisions.length > 0 && <table className="wf-table" aria-label="测量决策列表">
                  <thead><tr><th>测量</th><th>条件</th><th>结果</th><th>分支 / 解析参数</th><th>Workflow run</th><th /></tr></thead>
                  <tbody>{decisions.map(item => <tr key={item.decisionId}>
                    <td>{item.measuredValue.toFixed(4)} {displayUnit(item.unit)}<br /><code className="wf-mono">{item.measurementId.slice(0, 12)}</code><br /><span className="wf-muted">绑定 {bindingLabel(item.measurementBindingStatus)}</span></td>
                    <td><code className="wf-mono">{item.operator} {item.threshold}</code><br /><span className="wf-muted">{item.recipeId} v{item.recipeVersion}</span></td>
                    <td><span className={`wf-status ${item.result ? 'succeeded' : 'failed'}`}>{item.result ? 'true' : 'false'}</span><br />{item.chosenBranch}</td>
                    <td><code className="wf-mono">{JSON.stringify(item.resolvedParameters)}</code><br /><span className="wf-muted">止于 {authoredByCompiled.get(item.branchThroughStepId ?? '') ?? item.branchThroughStepId?.slice(0, 10) ?? '—'}</span>{item.replayOf && <><br /><span className="wf-muted">replay of {item.replayOf.slice(0, 12)}</span></>}</td>
                    <td><code className="wf-mono">{item.workflowRunId}</code><br /><span className="wf-muted">{item.targetCompileId.slice(0, 12)}</span></td>
                    <td><button className="wf-link" aria-label={`重放决策 ${item.decisionId}`} disabled={!!busy} onClick={() => void replayDecision(item)}>重放决策</button></td>
                  </tr>)}</tbody>
                </table>}
              </div>
            </details>
          </>}
        </section>

        {zone === 'experiment' && <section className="wf-card wf-wide">
          <div className="wf-card-head"><span>04</span><h2>实验设计</h2></div>

          {experimentDoc && <div className="wf-result-panel" aria-label="实验结果">
            <div className="wf-result-head">
              <strong>{experimentDoc.name}</strong>
              <span className={`wf-status ${experimentDoc.status}`}>{statusLabel(experimentDoc.status)}</span>
              <span className="wf-muted">实验状态：{statusLabel(experimentDoc.status)}{experimentDoc.kind === 'doe' ? ' · DOE' : ''}</span>
              <span className="wf-muted">{experimentDoc.variants.length} 变体 · 配方 v{experimentDoc.recipeVersion}</span>
              {experimentDoc.design && <span className="wf-muted" aria-label="DOE 设计摘要">
                {experimentDoc.design.conditionCount} 条件 × {experimentDoc.design.replicates} 重复 = {experimentDoc.design.logicalRunCount} 逻辑执行
              </span>}
              <span className="wf-spacer" />
              <button className={experimentRunCount > 0 ? 'wf-button' : 'wf-button primary'} disabled={!!busy} onClick={() => void runPersistentExperiment()}>
                {busy === 'experiment' ? '实验运行中…' : experimentRunCount > 0 ? '重新运行实验' : '运行实验'}
              </button>
              <button className={experimentRunCount > 0 ? 'wf-button primary' : 'wf-button'} disabled={experimentRunCount === 0 || !!busy} onClick={() => navigateZone('analyze')}>在 05 分析</button>
            </div>
            {experimentDoc.queue && <div className="wf-summary">
              <strong>队列</strong>
              <span>{experimentDoc.queue.succeeded}/{experimentDoc.queue.total} 运行成功 · 待处理 {experimentDoc.queue.pending} · 运行中 {experimentDoc.queue.running}</span>
              <span>预计剩余 {experimentDoc.queue.estimatedSecondsRemaining} s</span>
              <span>{experimentDoc.queue.estimatedCostUsd > 0
                ? `成本 $${experimentDoc.queue.estimatedCostUsd.toFixed(4)}`
                : '本地 CPU · 无云成本'}</span>
              <span>重试 {experimentDoc.queue.retries}</span>
            </div>}
            {experimentMetricSummary.total > 0 && (experimentNotRun
              ? <div className="wf-summary" aria-label="实验指标汇总" role="status">
                <strong>指标汇总</strong>
                <span>尚未运行：运行实验后才会出现每个变体的运行与指标状态。</span>
              </div>
              : <div
                className={`wf-summary${experimentMetricSummary.validMetrics === 0 ? ' warning' : ''}`}
                aria-label="实验指标汇总" role={experimentMetricSummary.validMetrics === 0 ? 'alert' : 'status'}>
                <strong>指标汇总</strong>
                <span>{experimentMetricSummary.runSucceeded}/{experimentMetricSummary.total} 运行成功 · {experimentMetricSummary.validMetrics}/{experimentMetricSummary.total} 有有效指标</span>
                {experimentMetricSummary.validMetrics === 0 && <span>没有有效指标：运行本身成功，但测量结果全部为歧义或无值。请在 03 调整测量位置/材料后重新测量，均值与统计暂不可用。</span>}
              </div>)}
            {experimentResultRows.length > 0 ? <table className="wf-table wf-result-table" aria-label="实验结果表">
              <thead><tr><th>变体</th><th>条件</th><th>重复（运行状态 / 指标）</th><th>均值（{displayUnit(experimentResultRows[0]?.unit ?? experimentDoc?.unit)}）</th><th>标准差（{displayUnit(experimentResultRows[0]?.unit ?? experimentDoc?.unit)}）</th><th>运行 / 指标</th></tr></thead>
              <tbody>{experimentResultRows.map(row => <tr key={row.ordinal}>
                <td>V{row.ordinal}</td>
                <td className="wf-condition">{row.condition}</td>
                <td><div className="wf-repeats">{row.replicates.map((replicate, index) => <span className="wf-repeat" key={`${row.ordinal}-${index}`} title={measurementReasonDetail(replicate.metricReason) ?? undefined}>
                  <span>#{index + 1}</span>
                  <span className={`wf-status ${replicate.status}`}>{statusLabel(replicate.status)}</span>
                  <code>{replicate.metricValue === null ? '—' : formatNumber(replicate.metricValue)}</code>
                  <span>{replicate.metricValue === null ? '' : displayUnit(replicate.unit ?? row.unit)}</span>
                  <span className={`wf-status ${replicate.metricStatus || 'planned'}`}>
                    {replicate.metricStatus ? statusLabel(replicate.metricStatus) : experimentNotRun ? '待运行' : '无指标'}
                  </span>
                  {replicate.metricValue === null && !experimentNotRun && ['succeeded', 'failed'].includes(replicate.status)
                    && <span className="wf-muted">说明：{measurementReasonText(replicate.metricReason)}</span>}
                </span>)}</div></td>
                <td>{row.mean === null ? '—' : formatNumber(row.mean)}</td>
                <td>{row.std === null ? '—' : formatNumber(row.std)}</td>
                <td>
                  <span className={`wf-status ${row.status}`}>{statusLabel(row.status)}</span>
                  <span className="wf-muted"> · {experimentNotRun ? '尚未运行' : `${row.validMetrics}/${row.total} 有效`}</span>
                </td>
              </tr>)}</tbody>
            </table> : <div className="wf-empty">实验已创建，点击“运行实验”后在这里显示每个变体的重复测量与统计。</div>}
          </div>}

          {!experimentReady && <div className="wf-empty" aria-label="实验空态">
            <strong>还不能创建实验</strong>
            <span>{experimentBlockReason}</span>
            <div className="wf-empty-actions">
              {!project && <button className="wf-button primary" onClick={() => navigateZone('setup')}>去 01 新建工程</button>}
              {project && (!compile || compileStale) && <button className="wf-button primary" onClick={() => navigateZone('flow')}>去 02 编译流程</button>}
              {project && compile && !compileStale && !activeRecipeId && <button className="wf-button primary" onClick={() => navigateZone('run')}>去 03 运行测量</button>}
            </div>
          </div>}

          {renderExperimentList()}

          {experimentReady && <details className="spx-advanced wf-experiment-inputs" open={experimentInputsOpen}
            onToggle={event => setExperimentInputsOpen((event.currentTarget as HTMLDetailsElement).open)}>
            <summary>实验输入（名称 / 变体 / DOE 因子）</summary>
            <div className="spx-advanced-body">
              <div className="spx-field-grid">
                <label className="spx-field span-2"><span>实验名称</span>
                  <input aria-label="实验名称" value={experimentName} placeholder={defaultExperimentName()} onChange={event => setExperimentName(event.target.value)} />
                </label>
                <label className="spx-field"><span>变体数（1–16）</span>
                  <input aria-label="变体数" type="number" min={1} max={16} value={experimentRows.length} onChange={event => resizeVariants(Number(event.target.value))} />
                </label>
                <label className="spx-field"><span>可调步骤（duration）</span>
                  <select aria-label="实验可调步骤" value={experimentTargetStep?.id ?? ''} onChange={event => setExperimentStepId(event.target.value)}>
                    {(project?.steps ?? []).filter(step => step.enabled).map((step, index) => <option key={step.id} value={step.id}>{stepOptionLabel(step, index)}</option>)}
                  </select>
                </label>
                <label className="spx-field"><span>失败重试（1–4）</span>
                  <input aria-label="实验失败重试" type="number" min={1} max={4} value={experimentMaxAttempts} onChange={event => setExperimentMaxAttempts(Number(event.target.value) || 1)} />
                </label>
                <label className="spx-field"><span>单次成本（USD）</span>
                  <input aria-label="实验单次成本" type="number" min={0} step="0.01" value={experimentCost} onChange={event => setExperimentCost(event.target.value)} />
                </label>
              </div>

              <div className="wf-card-subhead">变体条件 · 作用于 {experimentTargetStep?.name ?? '—'} 的 duration</div>
              <table className="wf-table wf-variant-table" aria-label="变体输入表">
                <thead><tr><th>#</th><th>条件摘要</th><th>duration（s）</th><th>重复数（1–8）</th><th>seed</th><th>操作</th></tr></thead>
                <tbody>{experimentRows.map((row, index) => <tr key={index} className={duplicateVariantDurations.has(index) ? 'has-error' : ''}>
                  <td>V{index + 1}</td>
                  <td className="wf-condition">V{index + 1} · {formatNumber(Number(row.duration))} s · seed {row.seed} · ×{row.replicates}</td>
                  <td>
                    <input aria-label={`变体 ${index + 1} duration`} value={row.duration}
                      aria-invalid={duplicateVariantDurations.has(index) ? 'true' : undefined}
                      onChange={event => updateVariantRow(index, { duration: event.target.value })} />
                    {duplicateVariantDurations.has(index) && <em className="wf-field-error" role="alert">
                      与 V{(duplicateVariantDurations.get(index) ?? 0) + 1} 重复
                    </em>}
                  </td>
                  <td><input aria-label={`变体 ${index + 1} replicates`} type="number" min={1} max={8} value={row.replicates} onChange={event => updateVariantRow(index, { replicates: Number(event.target.value) || 1 })} /></td>
                  <td><input aria-label={`变体 ${index + 1} seed`} type="number" value={row.seed} onChange={event => updateVariantRow(index, { seed: event.target.value })} /></td>
                  <td><button type="button" className="wf-link" disabled={experimentRows.length <= 1} onClick={() => removeVariantRow(index)}>移除</button></td>
                </tr>)}</tbody>
              </table>
              <div className="spx-run-actions">
                <button className="wf-button ghost" disabled={experimentRows.length >= 16} onClick={() => resizeVariants(experimentRows.length + 1)}>＋ 添加变体</button>
                <button className="wf-button primary" disabled={!!busy || variantDurationConflict}
                  title={variantDurationConflict ? '变体 duration 重复，请先修正标红的行' : '按当前变体创建实验'}
                  onClick={() => void createPersistentExperiment()}>{busy === 'experiment-create' ? '创建中…' : '创建实验'}</button>
                <span className="wf-muted">{experimentRows.length} 变体 × 平均 {formatNumber(experimentRows.reduce((total, row) => total + row.replicates, 0) / Math.max(1, experimentRows.length), 3)} 次重复</span>
              </div>
              {variantDurationConflict && <div className="wf-block-reason" aria-label="变体禁用原因">
                变体 duration 必须互不相同：请修正标红的行。需要同一条件的重复运行，请使用“重复数”而不是重复变体。
              </div>}

              <div className="wf-card-subhead">DOE 全因子</div>
              <p className="wf-muted">每个因子 2–10 个取值（逗号分隔），1–5 个因子；组合数 = 各因子取值数相乘。</p>
              <div className="wf-doe-factors">
                {doeFactors.map((row, index) => <div className={`wf-doe-factor${doeFactorErrors[index] ? ' has-error' : ''}`} key={index}>
                  <label className="spx-field"><span>因子 {index + 1} 步骤</span>
                    <select aria-label={`因子 ${index + 1} 步骤`} value={row.stepId} onChange={event => setDoeFactors(previous => previous.map((item, itemIndex) => itemIndex === index ? { ...item, stepId: event.target.value } : item))}>
                      {(project?.steps ?? []).filter(step => step.enabled).map((step, stepIndex) => <option key={step.id} value={step.id}>{stepOptionLabel(step, stepIndex)}</option>)}
                    </select>
                  </label>
                  <label className="spx-field"><span>参数</span>
                    <select aria-label={`因子 ${index + 1} 参数`} value={row.parameter} onChange={event => setDoeFactors(previous => previous.map((item, itemIndex) => itemIndex === index ? { ...item, parameter: event.target.value } : item))}>
                      {DOE_PARAMETERS.map(parameter => <option key={parameter.id} value={parameter.id}>{parameter.label}</option>)}
                    </select>
                  </label>
                  <label className="spx-field"><span>取值（2–10 个，逗号分隔）</span>
                    <input aria-label={`因子 ${index + 1} 取值`} value={row.values}
                      aria-invalid={doeFactorErrors[index] ? 'true' : undefined}
                      onChange={event => setDoeFactors(previous => previous.map((item, itemIndex) => itemIndex === index ? { ...item, values: event.target.value } : item))} />
                  </label>
                  <button type="button" className="wf-link" disabled={doeFactors.length <= 1} onClick={() => setDoeFactors(previous => previous.filter((_, itemIndex) => itemIndex !== index))}>移除</button>
                  {doeFactorErrors[index] && <div className="wf-field-error" role="alert" data-factor={index + 1}>{doeFactorErrors[index]}</div>}
                </div>)}
              </div>
              <div className="spx-run-actions">
                <button className="wf-button ghost" disabled={doeFactors.length >= 5} onClick={addDoeFactor}>＋ 添加因子</button>
              </div>
              <div className="spx-field-grid">
                <label className="spx-field"><span>DOE 重复数（1–8）</span>
                  <input aria-label="DOE 重复数" type="number" min={1} max={8} value={doeReplicates} onChange={event => setDoeReplicates(Number(event.target.value) || 1)} />
                </label>
                <label className="spx-field"><span>随机种子</span>
                  <input aria-label="DOE 种子" value={doeSeed} onChange={event => setDoeSeed(event.target.value)} />
                </label>
                <label className="spx-field"><span>单次成本（USD）</span>
                  <input aria-label="单次成本" type="number" min={0} step="0.01" value={doeCost} onChange={event => setDoeCost(event.target.value)} />
                </label>
              </div>
              <div className="spx-run-actions">
                <button className="wf-button primary" disabled={!!busy || doeBlocked}
                  title={doeBlocked ? '有重复或无效的因子，请先修正高亮的行' : '按全因子组合设计 DOE'}
                  onClick={() => void createDoe()}>设计 DOE</button>
              </div>
              {doeBlocked && <div className="wf-block-reason" aria-label="DOE 禁用原因">
                先修正因子：{doeFactorErrors.find(Boolean)}
              </div>}
              {experimentDoc?.design && <div className="wf-summary"><strong>DOE</strong><span>{experimentDoc.design.conditionCount} 条件 × {experimentDoc.design.replicates} 重复 = {experimentDoc.design.logicalRunCount} 逻辑执行</span></div>}
              {experimentDoc?.kind === 'doe' && <div className="spx-field-grid">
                <label className="spx-field"><span>均值下限（{displayUnit(experimentResultRows[0]?.unit)}）</span>
                  <input aria-label="筛选最小均值" value={tableMin} onChange={event => setTableMin(event.target.value)} placeholder="均值 ≥" />
                </label>
                <label className="spx-field"><span>均值上限（{displayUnit(experimentResultRows[0]?.unit)}）</span>
                  <input aria-label="筛选最大均值" value={tableMax} onChange={event => setTableMax(event.target.value)} placeholder="均值 ≤" />
                </label>
                <label className="spx-field"><span>排序字段</span>
                  <select aria-label="排序字段" value={tableSort} onChange={event => setTableSort(event.target.value)}>
                    <option value="ordinal">#</option>
                    <option value="mean">均值</option>
                    <option value="std">标准差</option>
                    <option value="n">样本数</option>
                  </select>
                </label>
              </div>}
              {experimentDoc?.kind === 'doe' && <div className="spx-run-actions">
                <button className="wf-button ghost" disabled={!!busy} onClick={() => void loadExperimentTable()}>筛选 / 排序</button>
                <a className="wf-button ghost" href={`/api/v1/workflow/experiments/${experimentDoc.experimentId}/export.csv`} download>导出 CSV</a>
              </div>}
              {experimentTable && <table className="wf-table"><thead><tr><th>#</th><th>样本数</th><th>均值</th><th>标准差</th><th>CI95</th><th>统计状态</th></tr></thead><tbody>{experimentTable.rows.map(row => <tr key={row.ordinal}><td>{row.ordinal}</td><td>{row.n}</td><td>{formatNumber(row.mean)}</td><td>{formatNumber(row.std)}</td><td>{formatNumber(row.ci95)}</td><td>{statusLabel(row.statisticalStatus)}</td></tr>)}</tbody></table>}

              {experimentDoc && <details className="spx-advanced">
                <summary>高级：保留策略</summary>
                <div className="spx-advanced-body">
                  <div className="spx-run-actions">
                    <button className="wf-button ghost" disabled={!!busy} onClick={() => void previewRetention()}>预览释放</button>
                    <button className="wf-button ghost" disabled={!!busy} onClick={() => void applyRetention('release-runs')}>应用 release-runs</button>
                    <button className="wf-button ghost" disabled={!!busy} onClick={() => void applyRetention('keep-all')}>恢复 keep-all</button>
                    <span className="wf-muted">当前 {experimentDoc.retention?.mode ?? 'keep-all'}</span>
                  </div>
                  {retentionInfo && <div className="wf-summary"><strong>保留策略</strong><span>可释放 {retentionInfo.releasable.length} 个 Run</span><span>受保护 {retentionInfo.protected.length} 个 Run</span></div>}
                </div>
              </details>}
            </div>
          </details>}
        </section>}
        {zone === 'analyze' && <section className="wf-card wf-wide">
          <div className="wf-card-head"><span>05</span><h2>分析</h2></div>

          {!analysisReady && <div className="wf-empty" aria-label="分析空态">
            <strong>还没有可分析的结果</strong>
            <span>{!project
              ? '先在 01 新建、打开或导入工程。'
              : !compile
                ? '先在 02 编译流程。'
                : '先在 04 创建并运行实验，或先在 03 完成一次运行与测量。'}</span>
            <div className="wf-empty-actions">
              {!project && <button className="wf-button primary" onClick={() => navigateZone('setup')}>去 01 新建工程</button>}
              {project && !compile && <button className="wf-button primary" onClick={() => navigateZone('flow')}>去 02 编译流程</button>}
              <button className="wf-button primary" onClick={() => navigateZone('experiment')}>去 04 创建实验</button>
              <button className="wf-button ghost" onClick={() => navigateZone('run')}>去 03 运行测量</button>
            </div>
          </div>}

          <div className="wf-card-subhead">候选 / 多视图</div>
          <div className="spx-run-actions">
            <button className="wf-button ghost" disabled={!!refreshCandidatesBlockedReason} onClick={loadExperimentCandidates}>刷新候选</button>
            <button className="wf-button ghost" disabled={!hasExecutedRun || !!busy} onClick={addCurrentCandidate}>加入当前 Run</button>
            <button className="wf-button primary" disabled={compareCandidates.length < 2 || comparedCandidateKeys.length < 2 || !!busy}
              title={compareCandidates.length < 2 ? '先选择至少 2 个候选'
                : comparedCandidateKeys.length < 2 ? '至少选择 2 个候选' : '比较所选候选并加载结构视图'}
              onClick={() => void runComparison()}>
              {busy === 'compare' ? '比较中…' : '比较并加载视图'}
            </button>
            {autoAnalysisId && autoAnalysisId === experimentDoc?.experimentId && <span className="wf-muted">已自动载入该实验的候选</span>}
          </div>
          {experimentDoc && experimentDoc.variants.length > 12 && (
            <div className="wf-block-reason" aria-label="候选截断说明">
              前 {Math.min(12, experimentDoc.variants.length)}/共 {experimentDoc.variants.length} 个候选
              {!showAllCandidates && <button type="button" className="wf-link" onClick={loadAllExperimentCandidates}>显示全部</button>}
            </div>
          )}
          {refreshCandidatesBlockedReason && <div className="wf-block-reason" aria-label="刷新候选禁用原因">
            {refreshCandidatesBlockedReason}
          </div>}
          {compareCandidates.length < 2 && <div className="wf-block-reason" aria-label="比较禁用原因">
            先选择至少 2 个候选：点击“刷新候选”载入实验变体，或“加入当前 Run”添加一个对照。
          </div>}
          {compareCandidates.length >= 2 && comparedCandidateKeys.length < 2 && <div className="wf-block-reason" aria-label="比较禁用原因">
            至少选择 2 个候选：在候选表中勾选要对比的候选。
          </div>}

          {analysisReady && <>
            {measurement && <div className="wf-summary" aria-label="测量结果">
              <strong>{statusLabel(measurement.status)}</strong>
              <span title={measurementReasonDetail(measurement.reason)}>{measurementValueText(measurement)}</span>
              {!measurementHasValue(measurement) && <span>说明：{measurementReasonText(measurement.reason)}</span>}
              <span>绑定：{bindingLabel(measurement.bindingStatus)} · 等价：{bindingLabel(measurement.equivalenceStatus)}</span>
              <span>配方 v{measurement.recipeVersion}{measurement.createdAt ? ` · ${formatDateTime(measurement.createdAt)}` : ''}</span>
            </div>}

            {experimentDoc && <div className="wf-analysis-conditions" aria-label="实验条件">
              <div className="wf-result-head">
                <strong>{experimentDoc.name}</strong>
                <span className={`wf-status ${experimentDoc.status}`}>{statusLabel(experimentDoc.status)}</span>
                <span className="wf-muted">{experimentDoc.variants.length} 变体 · 配方 v{experimentDoc.recipeVersion}{experimentDoc.kind === 'doe' ? ' · DOE' : ''}</span>
              </div>
              <div className="wf-condition-chips">
                {experimentConditionRows.map(row => <span className="wf-condition-chip" key={row.ordinal}>
                  <strong>{row.label}</strong>
                  <span className={`wf-status ${row.status}`}>{statusLabel(row.status)}</span>
                </span>)}
              </div>
            </div>}

            {compareCandidates.length > 0 && <table className="wf-table" id="wf-candidate-table" aria-label="候选表">
              <thead><tr><th>候选</th><th>步骤</th><th>状态</th><th>来源</th><th>对比</th></tr></thead>
              <tbody>{compareCandidates.map(candidate => {
                const variant = experimentDoc?.variants.find(item =>
                  item.replicates.some(replicate => replicate.runId === candidate.runId));
                const key = candidateKey(candidate);
                const checked = comparedCandidateKeys.includes(key);
                return <tr key={key} title={`${candidate.runId} / ${candidate.stepId}`}>
                  <td>{candidate.label}</td>
                  <td>{stepDisplayName(candidate.stepId)}</td>
                  <td><span className={`wf-status ${variant ? experimentVariantStatus(variant) : 'succeeded'}`}>
                    {variant ? statusLabel(experimentVariantStatus(variant)) : '已记录'}
                  </span></td>
                  <td title={candidate.runId}>{variant ? '实验 Run' : '当前 Run'}</td>
                  <td>
                    <input type="checkbox" aria-label={`对比候选 ${candidate.label}`} checked={checked}
                      disabled={!checked && comparedCandidateKeys.length >= COMPARE_CANDIDATE_LIMIT}
                      title={checked ? '从对比表中移除（仍显示结构视图）'
                        : comparedCandidateKeys.length >= COMPARE_CANDIDATE_LIMIT
                          ? `对比表最多 ${COMPARE_CANDIDATE_LIMIT} 个候选；先取消一个再选`
                          : '加入对比表（结构视图始终全部显示）'}
                      onChange={() => toggleCompareCandidate(key)} />
                  </td>
                </tr>;
              })}</tbody>
            </table>}

            {compareCoverage && (comparePickPending || compareCoverage.total > COMPARE_CANDIDATE_LIMIT) && <div className="wf-block-reason" aria-label="对比候选说明">
              {comparePickPending
                ? `已修改对比选择（${comparedCandidateKeys.length}/${compareCoverage.total}）；点击“比较并加载视图”更新对比表。`
                : `对比${compareCoverage.prefix ? '前' : '所选'} ${compareCoverage.compared.length}/${compareCoverage.total} 个候选；其余候选的结构视图仍全部加载与显示。`}
              <button type="button" className="wf-link" onClick={() => document.getElementById('wf-candidate-table')?.scrollIntoView({ block: 'center' })}>选择对比候选</button>
              <button type="button" className="wf-link" disabled={!!busy}
                onClick={() => void run('compare', () => loadAnalysis(compareCandidates, comparedCandidateKeys)).catch(() => undefined)}>显示全部视图</button>
            </div>}

            {Object.keys(candidateViews).length > 0 && <div className="wf-compare-controls">
              <label>同步缩放 <input aria-label="同步缩放" type="range" min="0.5" max="3" step="0.1" value={syncZoom} onChange={event => setSyncZoom(Number(event.target.value))} /></label>
              {candidateViewsHave3D
                ? <label>视图轴
                  <select aria-label="同步轴" value={syncAxis} onChange={event => setSyncAxis(event.target.value as typeof syncAxis)}>
                    <option value="xy">XY</option>
                    <option value="xz">XZ</option>
                    <option value="yz">YZ</option>
                  </select>
                </label>
                : <span className="wf-axis-chip" aria-label="同步轴">X – 高度</span>}
              <span className="wf-muted">单位 {displayUnit(candidateUnit)} · 所有候选共用同一坐标范围</span>
            </div>}

            {compareCandidates.length > 0 && <div className="wf-compare-grid">
              {compareCandidates.map((candidate, index) => {
                const view = candidateViews[`${candidate.runId}:${candidate.stepId}`];
                const viewError = candidateViewErrors[`${candidate.runId}:${candidate.stepId}`];
                const firstLabel = comparison?.candidates[0]?.label ?? compareCandidates[0]?.label;
                const pairRow = index > 0
                  ? comparison?.pairwise.find(row =>
                    (row.left === candidate.label && row.right === firstLabel) || (row.right === candidate.label && row.left === firstLabel))
                  : undefined;
                return <figure key={`${candidate.runId}:${candidate.stepId}`} title={`${candidate.runId} / ${candidate.stepId}`}>
                  <figcaption>
                    <strong>{candidate.label}</strong>
                    <span>{view ? `${view.dimension}D · ${topologyLabel(view.topology)} · ${view.dimension === 2 ? 'X–高度' : syncAxis.toUpperCase()} · ${structureUnit(view)}` : viewError ? `视图加载失败：${viewError}` : '视图加载中…'}</span>
                    {pairRow && <span>与 {firstLabel} 最大点偏差 {formatNumber(pairRow.maxPointDelta)} µm · 边界偏差 {formatNumber(pairRow.boundsDelta)} µm</span>}
                  </figcaption>
                  {view && analysisFrame ? <StructureCanvas
                    view={view}
                    frame={analysisFrame}
                    axis={syncAxis}
                    note={view.dimension === 2 ? structureSurfaceNote(view, syncAxis) : null}
                    className="wf-compare-canvas"
                    label={`候选 ${index + 1} 结构视图`}
                    compact
                    interactive={false}
                  /> : <div className="wf-empty">{viewError ? `视图加载失败：${viewError}` : '视图加载中…'}</div>}
                  {view && <div className="wf-compare-legend">
                    {structureLegend(view).map(row => <span key={row.id}><i style={{ background: row.color }} aria-hidden="true" />{row.name}<em>{row.count}</em></span>)}
                  </div>}
                </figure>;
              })}
            </div>}

            {comparison && <table className="wf-table" aria-label="候选对比表">
              <thead><tr><th>对比</th><th>状态</th><th>最大点偏差（µm）</th><th>边界偏差（µm）</th><th>材料偏差</th><th>原因</th></tr></thead>
              <tbody>{comparison.pairwise.map((row, index) => <tr key={index}>
                <td>{row.left} ↔ {row.right}</td>
                <td><span className={`wf-status ${row.status}`}>{toleranceLabel(row.status)}</span></td>
                <td>{formatNumber(row.maxPointDelta)}</td>
                <td>{formatNumber(row.boundsDelta)}</td>
                <td>{materialDeltaText(row.materialDelta)}</td>
                <td>{row.reasons.map(reason => compareReasonLabel(reason)).join('；') || '—'}</td>
              </tr>)}</tbody>
            </table>}

            <div className="wf-card-subhead">测量指标</div>
            {analysisMetrics.length > 0
              ? <table className="wf-table" aria-label="测量指标表">
                <thead><tr><th>Run</th><th>步骤</th><th>方法</th><th>状态</th><th>数值</th><th>单位</th><th>说明</th><th>绑定状态</th></tr></thead>
                <tbody>{analysisMetrics.map(item => <tr key={item.measurementId} title={item.measurementId}>
                  <td title={item.sourceRunId ?? undefined}>{runDisplayLabel(item.sourceRunId)}</td>
                  <td>{item.sourceStepId ? stepDisplayName(item.sourceStepId) : '—'}</td>
                  <td>{metricMethodLabel(item.method)}</td>
                  <td><span className={`wf-status ${item.status}`}>{statusLabel(item.status)}</span></td>
                  <td>{measurementHasValue(item) ? formatNumber(item.value) : '—'}</td>
                  <td>{measurementUnitText(item)}</td>
                  <td title={measurementReasonDetail(item.reason)}>{measurementHasValue(item) ? '—' : measurementReasonText(item.reason)}</td>
                  <td><span className={`wf-status ${item.bindingStatus === 'verified' ? 'succeeded' : 'planned'}`}>{bindingLabel(item.bindingStatus)}</span></td>
                </tr>)}</tbody>
              </table>
              : <div className="wf-empty" aria-label="测量指标空态">
                <strong>还没有测量记录</strong>
                <span>在 03 执行 Segment 后点击“测量”，这里会列出每次测量的数值与绑定状态。</span>
              </div>}

            <details className="spx-advanced wf-analysis-advanced">
              <summary>高级：Process Window（工艺窗口）与基础标定</summary>
              <div className="spx-advanced-body">
                <div className="wf-card-subhead">Process Window（工艺窗口）</div>
                <p className="wf-muted">窗口按测量单位（{displayUnit(experimentDoc?.unit)}）给出；每个条件至少 2 次重复才能评估置信区间。</p>
                {!experimentDoc && <div className="wf-muted">需要实验数据：先在 04 创建并运行实验。</div>}
                {experimentDoc && experimentRunCount === 0 && <div className="wf-muted">实验尚未运行，窗口无法计算。</div>}
                <div className="spx-field-grid">
                  <label className="spx-field"><span>窗口下限（{displayUnit(experimentDoc?.unit)}）</span>
                    <input aria-label="窗口下限" ref={windowLowerRef} value={windowLower}
                      aria-invalid={windowError ? 'true' : undefined}
                      onChange={event => updateWindowBound('lower', event.target.value)} placeholder="下限" />
                  </label>
                  <label className="spx-field"><span>窗口上限（{displayUnit(experimentDoc?.unit)}）</span>
                    <input aria-label="窗口上限" ref={windowUpperRef} value={windowUpper}
                      aria-invalid={windowError ? 'true' : undefined}
                      onChange={event => updateWindowBound('upper', event.target.value)} placeholder="上限" />
                  </label>
                  <div className="wf-field-action">
                    <button className="wf-button primary"
                      disabled={!experimentDoc || experimentRunCount === 0 || !!busy}
                      title={windowBlockedReason || '按当前实验的重复测量计算工艺窗口'}
                      onClick={() => void computeProcessWindow()}>计算窗口</button>
                  </div>
                  {windowError && <div className="wf-field-error span-2" role="alert" aria-label="窗口校验错误">{windowError}</div>}
                </div>
                {windowBlockedReason && <div className="wf-block-reason" aria-label="窗口禁用原因">{windowBlockedReason}</div>}
                {windowResult && <>
                  <div className="wf-summary"><strong>工艺窗口 [{formatNumber(windowResult.lower)}, {formatNumber(windowResult.upper)}] {displayUnit(experimentDoc?.unit)}</strong>
                    <span>通过 {windowResult.summary.pass} · 超出 {windowResult.summary.fail} · 不确定 {windowResult.summary.uncertain} · 未评估 {windowResult.summary.notEvaluated}</span>
                  </div>
                  <table className="wf-table" aria-label="工艺窗口表"><thead><tr><th>变体</th><th>均值</th><th>CI95</th><th>状态</th><th>余量</th></tr></thead><tbody>{windowResult.rows.map(row => <tr key={row.ordinal}>
                    <td>V{row.ordinal}</td>
                    <td>{formatNumber(row.mean)}</td>
                    <td>{formatNumber(row.ci95)}</td>
                    <td><span className={`wf-status ${row.status}`}>{statusLabel(row.status)}</span></td>
                    <td>{formatNumber(windowResult.candidates.find(item => item.ordinal === row.ordinal)?.margin ?? null)}</td>
                  </tr>)}</tbody></table>
                  {windowResult.factorWindows.length > 0 && <div className="wf-muted">因子窗口：{windowResult.factorWindows.map(item => `${item.parameter} [${formatNumber(item.min)}, ${formatNumber(item.max)}]`).join(' · ')}</div>}
                </>}

                <div className="wf-card-subhead">基础标定</div>
                <div className="spx-field span-2">
                  <span>标定数据 CSV</span>
                  <div className="spx-file-row">
                    <div className={calibrationCsv ? 'spx-file-name filled' : 'spx-file-name'} title={calibrationFileName || undefined}>
                      {calibrationFileName || '未选择文件'}
                    </div>
                    <button type="button" className="wf-button ghost spx-browse" onClick={() => calibrationInput.current?.click()}>选择文件</button>
                  </div>
                  <input ref={calibrationInput} aria-label="标定 CSV 文件" type="file" accept=".csv,text/csv" hidden
                    onChange={event => { const file = event.target.files?.[0]; if (file) void loadCalibrationFile(file); }} />
                </div>
                <details className="spx-advanced wf-csv-format">
                  <summary>CSV 格式</summary>
                  <div className="spx-advanced-body">
                    <p className="wf-muted">列定义：<code>measured, simulated, weight(可选), label(可选)</code></p>
                    <pre className="wf-code-block">{'measured,simulated,weight,label\n0.0120,0.0115,1.0,V1'}</pre>
                  </div>
                </details>
                <div className="spx-field-grid">
                  <label className="spx-field"><span>模型</span>
                    <select aria-label="标定模型" value={calibrationModel} onChange={event => setCalibrationModel(event.target.value)}>
                      <option value="scale">scale (1p)</option>
                      <option value="scale_offset">scale + offset (2p)</option>
                      <option value="quadratic">quadratic (3p)</option>
                      <option value="power">power (2p)</option>
                      <option value="power_offset">power + offset (3p)</option>
                    </select>
                  </label>
                  <label className="spx-field"><span>方法</span>
                    <select aria-label="标定方法" value={calibrationMethod} onChange={event => setCalibrationMethod(event.target.value as typeof calibrationMethod)}>
                      <option value="least-squares">least-squares</option>
                      <option value="nelder-mead">nelder-mead</option>
                    </select>
                  </label>
                </div>
                {!calibrationCsv && <div className="wf-block-reason" aria-label="标定禁用原因">先选择标定 CSV，再创建数据集并拟合。</div>}
                <div className="spx-run-actions">
                  <button className="wf-button ghost" disabled={!calibrationCsv || !!busy}
                    title={calibrationCsv ? '用所选 CSV 创建标定数据集' : '先选择标定 CSV'}
                    onClick={() => void createCalibrationDataset()}>创建数据集</button>
                  <button className="wf-button primary" disabled={!calibrationDataset || !!busy}
                    title={!calibrationCsv ? '先选择标定 CSV' : !calibrationDataset ? '先创建数据集' : '按所选模型与方法拟合'}
                    onClick={() => void fitCalibration()}>拟合</button>
                </div>
                {calibrationDataset && <div className="wf-muted">数据集 {calibrationDataset.name} · 训练 {calibrationDataset.trainCount} / 验证 {calibrationDataset.validationCount} · {calibrationDataset.sha256.slice(0, 12)}…</div>}
                {calibrationFit && <>
                  <div className="wf-summary"><strong>{calibrationFit.model} / {calibrationFit.method}</strong><span>{calibrationFit.parameterNames.map((name, index) => `${name}=${formatNumber(calibrationFit.parameters[index], 5)}`).join(' · ')}</span><span className={calibrationFit.validationStatus === 'validated' ? 'wf-status verified' : 'wf-status'}>{calibrationFit.validationStatus === 'validated' ? '已验证' : calibrationFit.validationStatus}</span></div>
                  <table className="wf-table"><thead><tr><th>数据集</th><th>样本数</th><th>RMSE</th><th>MAE</th><th>最大绝对误差</th></tr></thead><tbody>
                    <tr><td>训练</td><td>{calibrationFit.trainResiduals.n}</td><td>{formatNumber(calibrationFit.trainResiduals.rmse, 5)}</td><td>{formatNumber(calibrationFit.trainResiduals.mae, 5)}</td><td>{formatNumber(calibrationFit.trainResiduals.maxAbs, 5)}</td></tr>
                    <tr><td>验证</td><td>{calibrationFit.validationResiduals.n}</td><td>{formatNumber(calibrationFit.validationResiduals.rmse, 5)}</td><td>{formatNumber(calibrationFit.validationResiduals.mae, 5)}</td><td>{formatNumber(calibrationFit.validationResiduals.maxAbs, 5)}</td></tr>
                  </tbody></table>
                </>}
              </div>
            </details>
          </>}
        </section>}
        {zone === 'report' && <section className="wf-card wf-wide">
          <div className="wf-card-head"><span>06</span><h2>报告</h2></div>

          <div className="spx-summary-grid wf-report-summary" aria-label="报告内容摘要">
            <div><span>工程</span><strong>{project?.name ?? '未提供'}</strong></div>
            <div><span>流程</span><strong>{compile ? `${compile.projectName} · ${compile.stepCount} 步` : '未提供'}</strong></div>
            <div><span>编译</span><strong>{compile ? `${compile.dimension}D · ${compile.stepCount} 步` : '未提供'}</strong></div>
            <div><span>Route</span><strong>{route ? `${route.route.name} · ${route.segments.length} Segment` : '未提供'}</strong></div>
            <div><span>Run</span><strong>{hasExecutedRun && segment ? statusLabel(segment.status) : '未提供'}</strong></div>
            <div><span>测量</span><strong>{measurement ? measurementValueText(measurement) : '未提供'}</strong></div>
            <div><span>实验</span><strong>{experimentDoc ? `${experimentDoc.name} · ${experimentDoc.variants.length} 变体` : '未提供'}</strong></div>
            <div><span>报告</span><strong>{reportDocument ? `已生成 · ${formatDateTime(reportDocument.createdAt)}` : '未生成'}</strong></div>
          </div>

          <div className="spx-field-grid wf-report-form">
            <label className="spx-field span-2"><span>报告标题</span>
              <input aria-label="报告标题" value={reportTitle} onChange={event => setReportTitle(event.target.value)} />
            </label>
            <label className="spx-field span-2"><span>结论 / 候选说明（写入报告）</span>
              <textarea aria-label="报告结论" value={reportConclusions} onChange={event => setReportConclusions(event.target.value)} placeholder="填写本次结论 / 候选选择说明" />
            </label>
          </div>

          {!reportReady && <div className="wf-block-reason" aria-label="报告禁用原因">
            生成报告前需先完成 03 运行与测量，或在 04 运行一个实验。
          </div>}

          <div className="spx-run-actions">
            <button className="wf-button primary" disabled={!reportReady || !!busy || !reportTitle.trim()} onClick={() => void generateReport()}>
              {busy === 'report' ? '生成中…' : '生成报告'}
            </button>
            <button className="wf-button ghost" disabled={!reportDocument} onClick={openReport}>打开报告</button>
            <button className="wf-button ghost" disabled={!reportDocument} onClick={downloadReportHtml}>下载 HTML</button>
          </div>

          {!reportReady && <div className="wf-empty" aria-label="报告空态">
            <strong>还没有可报告的结果</strong>
            <span>报告包含流程/编译、Route、Run、测量、实验统计与结构视图；先完成 03 运行与测量。</span>
            <div className="wf-empty-actions">
              <button className="wf-button primary" onClick={() => navigateZone('run')}>去 03 运行测量</button>
              <button className="wf-button ghost" onClick={() => navigateZone('experiment')}>去 04 创建实验</button>
            </div>
          </div>}

          {reportDocument && <>
            <div className="wf-result-head wf-report-result">
              <strong>{reportDocument.title}</strong>
              <span className="wf-status succeeded">已生成</span>
              <span className="wf-muted">{formatDateTime(reportDocument.createdAt)}</span>
            </div>
            <div className="wf-report-conclusions">
              <strong>结论 / 候选说明</strong>
              <p>{reportConclusions.trim() || '未提供'}</p>
            </div>
            <details className="spx-advanced wf-report-tech">
              <summary>技术详情（ID 与哈希）</summary>
              <div className="wf-evidence-grid">
                <span>报告</span><code>{reportDocument.reportId}</code>
                <span>工程</span><code>{projectId ?? '—'}</code>
                <span>编译</span><code>{compile?.compileId ?? '—'}</code>
                <span>Route</span><code>{route?.route.routeId ?? '—'}</code>
                <span>Run</span><code>{segment?.runId ?? '—'}</code>
                <span>测量</span><code>{measurement?.measurementId ?? '—'}</code>
                <span>实验</span><code>{experimentDoc?.experimentId ?? '—'}</code>
                <span>SHA256</span><code>{reportDocument.sha256}</code>
              </div>
            </details>
            <iframe ref={reportFrameRef} className="wf-report-preview" title="报告预览" srcDoc={reportDocument.html}
              onLoad={resizeReportFrame} />
          </>}
        </section>}
          </main>

          <div className="spx-output">
            <div className="spx-output-tabs">
              {([
                ['output', '输出'],
                ['problems', '问题'],
                ['provenance', '来源证据'],
              ] as Array<[typeof outputTab, string]>).map(([id, label]) => <button
                key={id} type="button" className={outputTab === id ? 'active' : ''}
                onClick={() => setOutputTab(id)}
              >{label}</button>)}
            </div>
            <div className="spx-output-body">
              {outputTab === 'output' && <>
                {activityLog.length === 0 && <div className="spx-log muted">就绪。新建工程或上传版图开始。</div>}
                {activityLog.slice(-8).map((line, index) => <div className="spx-log" key={`${index}-${line}`}>{line}</div>)}
                {error && <div className="spx-log error">{error}</div>}
              </>}
              {outputTab === 'problems' && (() => {
                const problems: string[] = [];
                if (error) problems.push(`错误：${error}`);
                if (!project) problems.push('尚未打开工程：在 01 新建、打开或导入工程。');
                else if (!(project.steps ?? []).length) problems.push('流程为空：在 02 从库插入或添加步骤。');
                else if (!compile) problems.push('流程未编译：在 02 点击“编译流程”。');
                else if (compileStale) problems.push('流程已修改：需要重新编译后才能创建 Route。');
                else if (!route) problems.push('尚未创建 Route：在 02 点击“创建 Route”。');
                else if (routeStale) problems.push('Route 基于旧 Compile：重新编译并重建 Route。');
                return problems.length
                  ? <>{problems.map(line => <div className="spx-log error" key={line}>{line}</div>)}</>
                  : <div className="spx-log muted">没有阻塞问题。</div>;
              })()}
              {outputTab === 'provenance' && (provenance
                ? <>
                  {provenance.verifications.length === 0 && <div className="spx-log muted">该测量尚无验证记录。</div>}
                  {provenance.verifications.map(item => <div className="spx-log" key={item.verificationId}>{item.verificationId} · {statusLabel(item.status)} · {item.evidence.methodVersion} · {formatDateTime(item.createdAt)}</div>)}
                </>
                : <div className="spx-log muted">完成测量后显示来源证据。</div>)}
            </div>
          </div>
        </section>

      </div>

      <footer className="spx-statusbar">
        <span>Etch Workbench</span>
        <span>ViennaPS {health?.viennaps ?? '—'}</span>
        <span title={compile?.compileId ?? undefined}>Compile {compile ? (compileStale ? '需重新编译' : '已编译') : '—'}</span>
        <span title={compile?.bindingId ?? undefined}>绑定 {compile?.bindingId ? '已记录' : '—'}</span>
        <span title={segment?.runId ?? undefined}>Run {segment?.runId ? statusLabel(segment.status) : '—'}</span>
        <span className="spx-status-right">{busy ? `${busy} running…` : 'localhost'}</span>
      </footer>
      {settingsOpen && <div className="spx-modal-backdrop" onClick={() => setSettingsOpen(false)}>
        <div className="spx-modal spx-modal-wide" role="dialog" aria-label="设置" onClick={event => event.stopPropagation()}>
          <div className="spx-modal-title">设置</div>
          <div className="spx-modal-body spx-settings-body">
            {settingsLoading && <div className="spx-settings-status">正在读取设置…</div>}
            {settingsError && <div className="spx-settings-error" role="alert">{settingsError}</div>}
            {settingsNotice && <div className="spx-settings-notice" role="status">{settingsNotice}</div>}
            {settingsData && <>
              <section className="spx-settings-section" aria-label="运行默认值">
                <div className="spx-settings-section-title">运行默认值</div>
                <div className={`spx-settings-row${solverTimeoutError ? ' invalid' : ''}`}>
                  <div className="spx-settings-meta">
                    <div className="spx-settings-label">
                      <strong>solver.timeoutSeconds</strong>
                      <span className="spx-settings-hint">s</span>
                    </div>
                    <div className="spx-settings-hint">新工程默认；已有工程在 01 编辑</div>
                    {solverTimeoutError && <em className="spx-field-error">{solverTimeoutError}</em>}
                  </div>
                  <input
                    aria-label="设置 solverTimeoutSeconds"
                    type="number"
                    step="1"
                    min={SOLVER_TIMEOUT_MIN}
                    max={SOLVER_TIMEOUT_MAX}
                    value={settingsDraft.solverTimeoutSeconds}
                    aria-invalid={!!solverTimeoutError}
                    onChange={event => {
                      setSettingsError('');
                      setSettingsNotice('');
                      setSettingsDraft(previous => ({ ...previous, solverTimeoutSeconds: event.target.value }));
                    }}
                  />
                </div>
              </section>

              <section className="spx-settings-section" aria-label="资源上限">
                <div className="spx-settings-section-title">资源上限</div>
                {SETTINGS_LIMIT_KEYS.map(key => {
                  const limit = settingsData.limits[key];
                  if (!limit) return null;
                  const fallback = SETTINGS_LIMIT_FALLBACK[key];
                  const unit = limit.unit || fallback.unit;
                  const fieldError = settingsFieldError(key);
                  const isFloor = key === 'minFreeGiB';
                  const floor = isFloor && limit.envValue != null
                    ? Math.max(limit.minimum, limit.envValue)
                    : limit.minimum;
                  const envBound = limit.envValue ?? (limit.source === 'env' ? limit.value : null);
                  return <div className={`spx-settings-row${fieldError ? ' invalid' : ''}`} key={key}>
                    <div className="spx-settings-meta">
                      <div className="spx-settings-label">
                        <strong>{limit.label || fallback.label}</strong>
                        <span className={`spx-settings-source ${limit.source}`}>{SETTINGS_SOURCE_LABEL[limit.source] ?? '默认'}</span>
                      </div>
                      <div className="spx-settings-hint">
                        当前有效 {limit.value} {unit} · 最小 {floor} {unit}
                        {limit.maximum != null ? ` · 最大 ${limit.maximum} ${unit}` : ''}
                        {limit.envVariable && envBound != null ? ` · ${isFloor ? '环境下限' : '环境上限'} ${envBound} ${unit}` : ''}
                      </div>
                      {limit.envVariable && <div className="spx-settings-env" title={`环境变量 ${limit.envVariable}`}>{limit.envVariable}{limit.envValue != null ? `=${limit.envValue}` : ''}</div>}
                      {fieldError && <em className="spx-field-error">{fieldError}</em>}
                    </div>
                    <input
                      aria-label={`设置 ${key}`}
                      type="number"
                      step="1"
                      min={floor}
                      max={limit.maximum ?? undefined}
                      value={settingsDraft[key]}
                      aria-invalid={!!fieldError}
                      onChange={event => {
                        setSettingsError('');
                        setSettingsNotice('');
                        setSettingsDraft(previous => ({ ...previous, [key]: event.target.value }));
                      }}
                    />
                  </div>;
                })}
              </section>

              <section className="spx-settings-section" aria-label="存储">
                <div className="spx-settings-section-title">存储</div>
                <div className="spx-settings-kv"><span>数据目录</span><code title={settingsData.storage.dataDirectory}>{settingsData.storage.dataDirectory || '—'}</code></div>
                <div className="spx-settings-kv"><span>已用空间</span><strong>{formatBytes(settingsData.storage.usedBytes)}</strong></div>
                <div className="spx-settings-kv"><span>可用空间</span><strong>{formatBytes(settingsData.storage.freeBytes)}</strong></div>
                <div className="spx-settings-kv"><span>总空间</span><strong>{formatBytes(settingsData.storage.totalBytes)}</strong></div>
                <div className="spx-settings-kv"><span>运行</span><strong>{settingsData.storage.runs}</strong></div>
                <div className="spx-settings-kv"><span>工程</span><strong>{settingsData.storage.projects}</strong></div>
                <div className="spx-settings-kv"><span>Domain 状态</span><strong>{settingsData.storage.domainStates}</strong></div>
                <div className="spx-settings-kv"><span>GDS / CSV 资产</span><strong>{settingsData.storage.gdsAssets} / {settingsData.storage.csvAssets}</strong></div>
              </section>

              <section className="spx-settings-section" aria-label="运行时">
                <div className="spx-settings-section-title">运行时</div>
                <div className="spx-settings-kv"><span>ViennaPS</span><code>{settingsData.versions.viennaps || '—'}</code></div>
                <div className="spx-settings-kv"><span>ViennaLS</span><code>{settingsData.versions.viennals || '—'}</code></div>
              </section>
            </>}
          </div>
          <div className="spx-modal-actions spx-settings-actions">
            <button className="wf-button ghost" disabled={!settingsData || settingsLoading || !!settingsSaving}
              onClick={() => void resetSettings()}>{settingsSaving === 'reset' ? '恢复中…' : '恢复默认'}</button>
            <span className="spx-settings-actions-spacer" />
            <button className="wf-button primary" disabled={!settingsData || settingsLoading || !!settingsSaving || !!settingsValidationError}
              onClick={() => void saveSettings()}>{settingsSaving === 'save' ? '保存中…' : '保存设置'}</button>
            <button className="wf-button" onClick={() => setSettingsOpen(false)}>关闭</button>
          </div>
        </div>
      </div>}
      {aboutOpen && <div className="spx-modal-backdrop" onClick={() => setAboutOpen(false)}>
        <div className="spx-modal" role="dialog" aria-label="About Etch Workbench" onClick={event => event.stopPropagation()}>
          <div className="spx-modal-title">About Etch Workbench</div>
          <div className="spx-modal-body">
            <p>Workflow layer for ViennaPS process simulation.</p>
            <div className="spx-prop"><span>Application</span><code>{health?.applicationVersion ?? '0.1.0'}</code></div>
            <div className="spx-prop"><span>ViennaPS</span><code>{health?.viennaps ?? '—'}</code></div>
            <div className="spx-prop"><span>ViennaLS</span><code>{health?.viennals ?? '—'}</code></div>
            <div className="spx-prop"><span>Backend</span><code>{health?.status ?? 'offline'}</code></div>
          </div>
          <div className="spx-modal-actions"><button className="wf-button" onClick={() => setAboutOpen(false)}>Close</button></div>
        </div>
      </div>}
    </div>
  );
}
