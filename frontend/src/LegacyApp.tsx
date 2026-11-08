import Viewer3D from './Viewer3D';
import NativeIntermediatePanel from './NativeIntermediatePanel';
import { RunErrorDialog } from './RunErrorDialog';
import { defaultView3D } from './types3d';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { TopBar } from './app/TopBar';
import { Rail } from './app/Rail';
import { ContextPanel } from './app/ContextPanel';
import { Inspector } from './app/Inspector';
import { Dock } from './app/Dock';
import { StatusBar } from './app/StatusBar';
import type { Activity, ArtifactEstimate, DockTab, Selection } from './app/types';
import { Icon } from './ui/Icon';
import { Menu } from './ui/Menu';
import { StructurePanel } from './panels/StructurePanel';
import { RecipePanel } from './panels/RecipePanel';
import { MaterialsPanel } from './panels/MaterialsPanel';
import { ResultsPanel } from './panels/ResultsPanel';
import { SettingsPanel } from './panels/SettingsPanel';
import { StepInspector } from './inspector/StepInspector';
import { MaterialInspector } from './inspector/MaterialInspector';
import { RunInspector } from './inspector/RunInspector';
import { ArtifactPreflightCard } from './components/ArtifactPreflightCard';
import { DeleteMaterialDialog } from './components/DeleteMaterialDialog';
import { MaterialPropertyInspector } from './components/MaterialPropertyInspector';
import { NumericField, SelectField } from './components/fields';
import { SvgViewer } from './viewer/SvgViewer';
import { convertProjectUnits, lengthUnitLabel, lengthUnitsPerUm, rateUnitLabel, type LengthUnit } from './project/units';
import { api, effectiveArtifactCapBytes, formatArtifactCapMiB, prettyError, saveBlob } from './api/client';
import type { AdvectionSettings, CsvRateAsset, DirectionalMaterialRate, Domain, FluxResult, GDSGeometry, GDSMaskPattern, Geometry, GpuCapabilityRow, GpuProfileFailure, Layer, MaterialCatalogItem, OutputSettings, ParticleSpecies, Project, RayTracingSettings, RunInfo, Solver, StackGeometry, Step } from './api/types';
import { gdsAssetIdFromProject, gdsLayerIds, gdsMaskPairs, gdsPairs } from './lib/gds';
import { fitPanelWidths, maxPanelWidth, panelWidthLimits } from './lib/panelLayout';
import { MATERIAL_CATEGORY_OPTIONS, configuredMaterialRates, directionalMaterialRates, geometryMaterialNames, isDirectionalMaterialMode, isGeometryStep, materialCategoryLabel, materialRateModeActive, materialRateModeAvailable, scalarMaterialRates, specializedDefaults, specializedDimensionLabel, specializedModelAvailable, stepKindLabel, stripDirectionalRateTable, stripDirectionalScalarRates, stripIsotropicMethod, usesFlux } from './lib/projectRules';
import { ARTIFACT_RISK_LABELS, NOT_APPLICABLE_ENGINE, artifactEstimateFromError, engineTone, errorRunStates, formatEstimateMiB, formatMicron, formatThickness, humanStatus, runEngineLabel, runEnginesOf, runFailureDialogData, terminalStates, type RunFailureDialogData } from './lib/runPresentation';
import { usePanelLayout } from './hooks/usePanelLayout';
import { useGdsImport } from './hooks/useGdsImport';
import { useWorkbenchBootstrap } from './hooks/useWorkbenchBootstrap';
import { useSnapshotViewer } from './hooks/useSnapshotViewer';
import { useRunLogPolling } from './hooks/useRunLogPolling';
import { defaultDirection, defaultIonSpecies, defaultMultiParticleFields, defaultNeutralSpecies, defaultProject, initialVisibleMaterialIds, normalizeGdsMaskLayer, promoteProjectToV5, promoteProjectToV7 } from './project/defaults';

// Ray count is the dominant cost knob for stochastic flux steps: measured on
// the RTX 3060 host, 8/64/256 rays per point cost 1.42/2.26/4.87 s of flux
// engine time for a six-step 3D deposition. The numeric field stays available
// for fine tuning; the presets give the common trade-off points a name.
const RAY_PRESETS = [
  { value: 16, label: '预览', detail: '每点 16 条射线：最快，统计噪声明显，适合看趋势或快速试几何' },
  { value: 64, label: '标准', detail: '每点 64 条射线：射线耗时约为 256 的三分之一，噪声可接受' },
  { value: 256, label: '精确', detail: '每点 256 条射线：默认精度，统计噪声最低，单步耗时最长' },
] as const;

const NATIVE_MAKEFIN_SIGSEGV_MODELS = new Set(['sf6o2', 'hbro2', 'sf6c4f8', 'neutralTransport']);
// Mirrors backend.models.MIN_DOMAIN_EXTENT_3D: a 22 nm FinFET cell is only
// 0.48 x 0.50 um, so the old 1 um 3D floor blocked real layouts. The 2D domain
// keeps its 1 um contract. Domain3D also requires at least two grid intervals
// per lateral axis, so the UI floor follows gridDelta.
const MIN_DOMAIN_EXTENT_3D = 0.01;
const minThreeDExtent = (gridDelta: number) => Math.max(MIN_DOMAIN_EXTENT_3D, 2 * gridDelta);

function newStepId(): string { return `step_${crypto.randomUUID().replaceAll('-', '').slice(0, 24)}`; }

export default function App() {
  const [project, setProject] = useState<Project>(() => structuredClone(defaultProject));
  const projectUnits: LengthUnit = project.units?.length === 'nm' ? 'nm' : 'um';
  const lengthUnit = lengthUnitLabel(projectUnits);
  const rateUnit = rateUnitLabel(projectUnits);
  const unitsPerUm = lengthUnitsPerUm(projectUnits);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [savedProjectJson, setSavedProjectJson] = useState<string | null>(null);
  const [savedSelection, setSavedSelection] = useState('');
  const {
    projectRows,
    runs,
    capabilities,
    gpuInventory,
    engineCatalog,
    materials, setMaterials,
    specializedCatalog,
    newSpecializedModel, setNewSpecializedModel,
    health, setHealth,
    notice, setNotice,
    refreshLists,
  } = useWorkbenchBootstrap();
  const [materialDraft, setMaterialDraft] = useState({ name: '', color: '#8b5cf6', category: 'Generic', densityGcm3: '', conductive: false, description: '' });
  const [materialBusy, setMaterialBusy] = useState(false);
  const [deletingMaterial, setDeletingMaterial] = useState<string | null>(null);
  const [deleteMaterialCandidate, setDeleteMaterialCandidate] = useState<MaterialCatalogItem | null>(null);
  const [deleteMaterialError, setDeleteMaterialError] = useState('');
  const [csvAsset, setCsvAsset] = useState<CsvRateAsset | null>(null);
  const [csvUploadMode, setCsvUploadMode] = useState<'new' | 'replace'>('new');
  const [slicePosition, setSlicePosition] = useState(0);
  const [rateRowMaterial, setRateRowMaterial] = useState('');
  const [directionalRateRowMaterial, setDirectionalRateRowMaterial] = useState('');
  const [multiRateMaterial, setMultiRateMaterial] = useState('');
  const [multiStickingMaterial, setMultiStickingMaterial] = useState('');
  const [inputHash, setInputHash] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]);
  const [artifactEstimate, setArtifactEstimate] = useState<ArtifactEstimate | null>(null);
  const [validationMessage, setValidationMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [latestRunId, setLatestRunId] = useState<string | null>(null);
  const [runInfo, setRunInfo] = useState<RunInfo | null>(null);
  const [displayInfo, setDisplayInfo] = useState<RunInfo | null>(null);
  const [runErrorDialog, setRunErrorDialog] = useState<RunFailureDialogData | null>(null);
  const [fluxResult, setFluxResult] = useState<FluxResult | null>(null);
  const [fluxResultRunId, setFluxResultRunId] = useState<string | null>(null);
  const [selectedFluxField, setSelectedFluxField] = useState('');
  const [displayRunId, setDisplayRunId] = useState<string | null>(null);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  const [activity, setActivity] = useState<Activity>('structure');
  const [selection, setSelection] = useState<Selection>({ kind: 'none' });
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [inspectorTab, setInspectorTab] = useState<'basic' | 'physics' | 'native'>('basic');
  const [dockTab, setDockTab] = useState<DockTab>('log');
  const [dockOpen, setDockOpen] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<'structure' | 'viewer' | 'settings'>('viewer');
  const { panelWidths, setPanelWidths, isResizingPanels, workspaceRef, beginPanelResize, movePanelResize, endPanelResize, adjustPanelWidth } = usePanelLayout();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const csvInputRef = useRef<HTMLInputElement>(null);
  const shownRunErrorRef = useRef<string | null>(null);
  const validationSequence = useRef(0);

  const updateProject = useCallback((updater: (previous: Project) => Project) => setProject(previous => {
    const next = updater(previous);
    if ((next.geometry.kind === 'fin' || next.geometry.kind === 'gds') && next.schemaVersion < 7) return { ...promoteProjectToV7(next), geometry: next.geometry };
    if (previous.schemaVersion < 3 && (JSON.stringify(previous.geometry) !== JSON.stringify(next.geometry) || JSON.stringify(previous.steps) !== JSON.stringify(next.steps))) return { ...next, schemaVersion: Math.max(3, next.schemaVersion) as Project["schemaVersion"], solver: { ...next.solver, enginePreference: 'cpu' }, ...(next.dimension === 3 ? { view3D: { ...next.view3D!, visibleMaterialIds: materials.map(m => m.id) } } : {}) };
    return next;
  }), [materials]);

  const changeProjectUnits = (target: LengthUnit) => {
    if (projectUnits === target) return;
    const result = convertProjectUnits(project as unknown as Record<string, unknown>, target);
    if (result.blockedReason) { setNotice(result.blockedReason); return; }
    setProject(result.project as unknown as Project);
    setNotice(`长度单位已切换为 ${lengthUnitLabel(target)}；长度与速率已按比例换算。`);
  };

  const fetchRunInfo = useCallback(async (runId: string, cursor = 0) => {
    return api<RunInfo>(`/api/runs/${runId}?cursor=${cursor}`);
  }, []);

  const fetchFluxResult = useCallback(async (runId: string, expectedMeshIdentity?: string) => {
    const result = await api<FluxResult>(`/api/runs/${runId}/flux-result`);
    if (expectedMeshIdentity && result.meshIdentity !== expectedMeshIdentity) throw new Error('通量数据的网格身份与运行记录不一致。');
    setFluxResult(result); setFluxResultRunId(runId);
    setSelectedFluxField(previous => result.fields.some(field => `${field.association}:${field.name}` === previous) ? previous : result.fields[0] ? `${result.fields[0].association}:${result.fields[0].name}` : '');
  }, []);

  const {
    selectedSnapshot, setSelectedSnapshot,
    compareRunId, setCompareRunId,
    compareInfo, setCompareInfo,
    compareSnapshot, setCompareSnapshot,
    selectedStepId, setSelectedStepId,
    historyView2D, setHistoryView2D,
    historyView3D, setHistoryView3D,
    snapshotSequence, comparisonSequence,
    svgRef,
    fetchSnapshot, loadComparison, changeView, changeView3D, exportSvg,
    view2D, view3D, displayDimension, compatibleCompare,
  } = useSnapshotViewer({ project, updateProject, displayRunId, latestRunId, fetchRunInfo, setDisplayRunId, setNotice });

  const { runLog, setRunLog, logCursor, setLogCursor, cursorRef } = useRunLogPolling({
    latestRunId,
    runInfo,
    displayRunId,
    autoFollow,
    selectedStepId,
    fluxResultRunId,
    fetchRunInfo,
    fetchSnapshot,
    fetchFluxResult,
    refreshLists,
    setRunInfo,
    setDisplayInfo,
    setHealth,
    setNotice,
  });

  const {
    gdsAsset, setGdsAsset,
    gdsInspection, setGdsInspection,
    gdsImportOpen, setGdsImportOpen,
    gdsImportPosition,
    pendingGdsAsset,
    pendingGdsStructure, setPendingGdsStructure,
    pendingGdsLayerDatatype, setPendingGdsLayerDatatype,
    gdsImportMode, setGdsImportMode,
    pendingMaskName, setPendingMaskName,
    pendingMaskMaterial, setPendingMaskMaterial,
    pendingMaskThickness, setPendingMaskThickness,
    pendingGdsScale, setPendingGdsScale,
    pendingMaskOffset, setPendingMaskOffset,
    pendingMaskPolarity, setPendingMaskPolarity,
    pendingProtectsEtch, setPendingProtectsEtch,
    gdsImportError, setGdsImportError,
    gdsInputRef, gdsImportButtonRef, gdsImportCardRef,
    gdsAvailableLayers,
    loadGdsAsset, uploadGdsFile, importPendingGds, startGdsMaskTransfer, inspectGds,
  } = useGdsImport({ project, materials, setProject, setProjectId, setSelectedStep, setNotice, setLoading, setActivity });

  useEffect(() => {
    const closeProjectMenusOnMobile = () => {
      if (!window.matchMedia('(max-width: 900px)').matches) return;
      document.querySelectorAll<HTMLDetailsElement>('.topbar-shell details.ui-menu[open]')
        .forEach(menu => { menu.open = false; });
    };
    closeProjectMenusOnMobile();
    window.addEventListener('resize', closeProjectMenusOnMobile);
    return () => window.removeEventListener('resize', closeProjectMenusOnMobile);
  }, []);

  useEffect(() => {
    if (selectedStep) {
      setSelection({ kind: 'step', id: selectedStep });
      setInspectorOpen(true);
      setInspectorTab('basic');
    } else {
      setSelection(previous => previous.kind === 'step' ? { kind: 'none' } : previous);
    }
  }, [selectedStep]);

  useEffect(() => {
    if (selection.kind !== 'none') setInspectorOpen(true);
  }, [selection]);

  useEffect(() => {
    const selectionExists = selection.kind === 'none'
      || selection.kind === 'step' && project.steps.some(step => step.id === selection.id)
      || selection.kind === 'material' && materials.some(material => material.name === selection.name)
      || selection.kind === 'run' && (displayRunId === selection.id || runs.some(run => run.runId === selection.id))
      || selection.kind === 'artifact' && !!selectedSnapshot?.outputs?.some(artifact => artifact.fileName === selection.key);
    if (!selectionExists) setSelection({ kind: 'none' });
  }, [selection, project.steps, materials, runs, selectedSnapshot, displayRunId]);

  const editAdvection = (key: keyof AdvectionSettings, value: AdvectionSettings[keyof AdvectionSettings]) => setProject(previous => {
    const promoted = promoteProjectToV7(previous);
    return { ...promoted, solver: { ...promoted.solver, advection: { ...promoted.solver.advection!, [key]: value } as AdvectionSettings } };
  });
  const editRayTracing = (key: keyof RayTracingSettings, value: RayTracingSettings[keyof RayTracingSettings]) => setProject(previous => {
    const promoted = promoteProjectToV7(previous);
    return { ...promoted, solver: { ...promoted.solver, rayTracing: { ...promoted.solver.rayTracing!, [key]: value } as RayTracingSettings } };
  });
  const editOutputSetting = (key: keyof OutputSettings, value: boolean) => setProject(previous => {
    const promoted = promoteProjectToV7(previous);
    return { ...promoted, outputSettings: { ...promoted.outputSettings!, [key]: value } };
  });
  const editGdsGeometry = (updater: (geometry: GDSGeometry) => GDSGeometry) => updateProject(previous =>
    previous.geometry.kind === 'gds' ? { ...previous, geometry: updater(previous.geometry) } : previous);
  const syncGdsMaskLayer = (geometry: StackGeometry): StackGeometry => {
    const index = geometry.layers.findIndex(layer => layer.source === 'gds-mask');
    if (index < 0 || !geometry.gdsMask) return geometry;
    const layer = geometry.layers[index];
    const baseHeight = geometry.baseHeight + geometry.layers.slice(0, index).reduce((sum, item) => sum + item.thickness, 0);
    return { ...geometry, gdsMask: { ...geometry.gdsMask, name: layer.name, material: layer.material, height: layer.thickness, baseHeight } };
  };
  const editStackGdsMask = (updater: (mask: GDSMaskPattern) => GDSMaskPattern) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack' || !previous.geometry.gdsMask) return previous;
    return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, gdsMask: updater(previous.geometry.gdsMask) }) };
  });
  const active = !!runInfo && !terminalStates.has(runInfo.status);
  const changeActivity = (next: Activity) => {
    setActivity(next);
    setMobilePanel('structure');
  };
  useEffect(() => {
    const panel = document.getElementById(`${activity}-context`);
    if (panel) panel.scrollIntoView({ block: 'start' });
  }, [activity]);
  useEffect(() => {
    if (selection.kind === 'none') return;
    const selectionBelongsToActivity =
      activity === 'recipe' && selection.kind === 'step' && project.steps.some(step => step.id === selection.id)
      || activity === 'materials' && selection.kind === 'material' && materials.some(material => material.name === selection.name)
      || activity === 'results' && selection.kind === 'run' && (displayRunId === selection.id || runs.some(run => run.runId === selection.id))
      || activity === 'results' && selection.kind === 'artifact' && !!selectedSnapshot?.outputs?.some(artifact => artifact.fileName === selection.key);
    if (!selectionBelongsToActivity) {
      if (selection.kind === 'step') setSelectedStep(null);
      setSelection({ kind: 'none' });
    }
  }, [activity, selection, project.steps, materials, displayRunId, runs, selectedSnapshot]);
  const particleSteps = project.steps.filter(usesFlux).length;
  const processSteps = project.steps.filter(step => !isGeometryStep(step));
  const selectedStepObject = project.steps.find(step => step.id === selectedStep) ?? null;
  const selectedMaterial = selection.kind === 'material' ? materials.find(item => item.name === selection.name) : undefined;
  const selectedRunHash = displayRunId === latestRunId ? runInfo?.requestedInputHash : runs.find(item => item.runId === displayRunId)?.requestedInputHash;
  const isStale = !!selectedRunHash && !!inputHash && selectedRunHash !== inputHash;
  const visibleLog = displayRunId === latestRunId ? runLog : displayInfo?.log.text ?? '';
  const selectedExecution = displayInfo?.actualExecution?.find(item => item.stepId === selectedSnapshot?.stepId);
  const selectedExecutionGpu = selectedExecution?.actualParameters?.cudaDeviceReadback as {
    uuid?: string; name?: string; pciBusId?: string;
    afterProcessApply?: { readbackStatus?: string; synchronizeStatus?: number };
    process?: { processListed?: boolean; processListStatus?: string };
  } | undefined;
  const directionalActual = selectedExecution?.model === 'directional' ? selectedExecution.actualParameters ?? {} : null;
  const multiParticleActual = selectedExecution?.model === 'multiParticle' ? selectedExecution.actualParameters ?? {} : null;
  const specializedActual = selectedExecution?.model === 'specialized' ? selectedExecution.actualParameters ?? {} : null;
  const geometryActual = selectedExecution && ['removeMaterial', 'planarize', 'boolean', 'gdsBoolean'].includes(selectedExecution.model ?? '') ? selectedExecution.actualParameters ?? {} : null;
  const selectedV2 = displayInfo?.v2Steps?.find(item => item.stepId === selectedSnapshot?.stepId);
  const measuredChange = selectedSnapshot?.measuredRemoval
    ? { kind: 'removal' as const, record: selectedSnapshot.measuredRemoval }
    : selectedSnapshot?.measuredAdded ? { kind: 'addition' as const, record: selectedSnapshot.measuredAdded } : null;
  const hasMeasuredChange = measuredChange !== null;
  const displayFlux = fluxResultRunId === displayRunId ? fluxResult : null;
  const fluxField = displayFlux?.fields.find(item => `${item.association}:${item.name}` === selectedFluxField) ?? displayFlux?.fields[0];
  const fluxValues = displayFlux && fluxField
    ? fluxField.components === 1 ? displayFlux[fluxField.association].scalars[fluxField.name] : displayFlux[fluxField.association].vectors[fluxField.name]
    : undefined;

  useEffect(() => {
    if (displayInfo?.kind === 'flux') { setDockTab('flux'); setDockOpen(true); }
    else if ((displayInfo?.intermediateOutputs?.length ?? 0) > 0) { setDockTab('intermediate'); setDockOpen(true); }
    else if ((selectedSnapshot?.outputs?.length ?? 0) > 0) { setDockTab('artifacts'); setDockOpen(true); }
    else if (directionalActual || multiParticleActual || specializedActual || geometryActual || selectedV2 || hasMeasuredChange) { setDockTab('native'); setDockOpen(true); }
    else if (visibleLog) { setDockTab('log'); setDockOpen(true); }
  }, [displayInfo?.kind, displayInfo?.intermediateOutputs?.length, selectedSnapshot?.outputs?.length, directionalActual, multiParticleActual, specializedActual, geometryActual, selectedV2, hasMeasuredChange, visibleLog]);

  useEffect(() => {
    const sequence = ++validationSequence.current;
    const timeout = window.setTimeout(async () => {
      try {
        const result = await api<{ valid: boolean; warnings: string[]; requestedInputHash: string; artifactEstimate?: ArtifactEstimate | null }>('/api/projects/validate', { method: 'POST', body: JSON.stringify(project) });
        if (sequence === validationSequence.current) {
          setInputHash(result.requestedInputHash); setWarnings(result.warnings); setArtifactEstimate(result.artifactEstimate ?? null); setValidationMessage('输入通过校验');
        }
      } catch (error) {
        if (sequence === validationSequence.current) { setInputHash(''); setWarnings([]); setArtifactEstimate(null); setValidationMessage(prettyError(error)); }
      }
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [project]);

  useEffect(() => {
    if (!displayInfo || !errorRunStates.has(displayInfo.status)) return;
    const key = `${displayInfo.runId}:${displayInfo.status}`;
    if (shownRunErrorRef.current === key) return;
    shownRunErrorRef.current = key;
    setRunErrorDialog(runFailureDialogData(displayInfo));
  }, [displayInfo]);

  const loadRun = useCallback(async (runId: string, preferLatest = false) => {
    try {
      const info = await fetchRunInfo(runId, 0);
      if (runId === latestRunId) setRunInfo(info);
      setDisplayInfo(info);
      setDisplayRunId(runId); setAutoFollow(false);
      if (info.kind === 'flux' && info.status === 'succeeded' && info.fluxResult) await fetchFluxResult(runId, info.fluxResult.meshIdentity);
      else if (fluxResultRunId !== runId) { setFluxResult(null); setFluxResultRunId(null); }
      const row = preferLatest ? info.snapshots.at(-1) : info.snapshots[0];
      if (row) await fetchSnapshot(runId, row.stepId);
      else setSelectedSnapshot(null);
    } catch (error) { setNotice(prettyError(error)); }
  }, [fetchRunInfo, fetchSnapshot, fetchFluxResult, latestRunId, fluxResultRunId]);

  const deepLinkLoaded = useRef(false);
  useEffect(() => {
    if (deepLinkLoaded.current) return;
    const runId = new URLSearchParams(window.location.search).get('run')?.trim() ?? '';
    if (!/^[a-f0-9]{24}$/.test(runId)) return;
    deepLinkLoaded.current = true;
    void (async () => {
      try {
        const replay = await api<{ normalizedProject?: Project }>(`/api/runs/${runId}/replay-project`);
        if (replay.normalizedProject) {
          setProject(replay.normalizedProject);
          setProjectId(null);
          setSavedProjectJson('');
          setSelectedStep(null);
          setSelection({ kind: 'run', id: runId });
          setActivity('results');
        }
      } catch {
        // Keep the default project; the run inspector still loads below.
      }
      await loadRun(runId, true);
    })();
  }, [loadRun]);

  const startRun = async (kind: 'preview' | 'simulate') => {
    if (kind === 'simulate' && artifactEstimate?.risk === 'reject') {
      setNotice(`运行产物预估约 ${formatEstimateMiB(artifactEstimate.estimatedMiB)}，超过 ${artifactEstimate.capMiB} MiB 上限；工作台会在启动前拒绝该运行。请按“运行产物预估”中的建议调整网格、步骤或输出后再试。`);
      return;
    }
    setLoading(true); setNotice(''); setFluxResult(null); setFluxResultRunId(null);
    try {
      await api('/api/projects/validate', { method: 'POST', body: JSON.stringify(project) });
      const started = await api<{ runId: string; status: string; requestedInputHash: string }>('/api/runs', { method: 'POST', body: JSON.stringify({ kind, project }) });
      const initialInfo: RunInfo = { runId: started.runId, kind, status: started.status, currentStepId: null, completedStepIds: [], elapsedSeconds: 0, requestedInputHash: started.requestedInputHash, stepTotal: project.steps.filter(step => step.enabled).length, log: { cursor: 0, nextCursor: 0, truncated: false, text: '' }, snapshots: [] };
      setLatestRunId(started.runId); setDisplayRunId(started.runId); setRunInfo(initialInfo); setDisplayInfo(initialInfo);
      setRunLog(''); cursorRef.current = 0; setLogCursor(0); setSelectedSnapshot(null); setSelectedStepId(null); setAutoFollow(true);
      setNotice(kind === 'preview' ? '初始结构预览已提交。' : '工艺仿真已提交。');
      void refreshLists();
    } catch (error) {
      const rejected = artifactEstimateFromError(error);
      if (rejected) setArtifactEstimate(rejected);
      setNotice(prettyError(error));
    }
    finally { setLoading(false); }
  };

  const startNativeSlice = async () => {
    if (project.dimension !== 3 || project.schemaVersion !== 7) return;
    setLoading(true); setNotice(''); setFluxResult(null); setFluxResultRunId(null);
    try {
      const started = await api<{ runId: string; status: string; requestedInputHash: string }>('/api/runs/slice', {
        method: 'POST', body: JSON.stringify({ project, sliceRequest: { axis: 1, position: slicePosition } }),
      });
      const initialInfo: RunInfo = { runId: started.runId, kind: 'slice', status: started.status, currentStepId: null,
        completedStepIds: [], elapsedSeconds: 0, requestedInputHash: started.requestedInputHash, stepTotal: 0,
        log: { cursor: 0, nextCursor: 0, truncated: false, text: '' }, snapshots: [] };
      setLatestRunId(started.runId); setDisplayRunId(started.runId); setRunInfo(initialInfo); setDisplayInfo(initialInfo);
      setRunLog(''); cursorRef.current = 0; setLogCursor(0); setSelectedSnapshot(null); setSelectedStepId(null); setAutoFollow(true);
      setNotice('ViennaPS Slice 真实切片已提交；运行记录将同时保留 3D 源结构和输出 2D 网格。'); void refreshLists();
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); }
  };

  const startFlux = async (stepId: string) => {
    setLoading(true); setNotice(''); setFluxResult(null); setFluxResultRunId(null);
    try {
      await api('/api/projects/validate', { method: 'POST', body: JSON.stringify(project) });
      const started = await api<{ runId: string; status: string; requestedInputHash: string }>('/api/runs/flux', { method: 'POST', body: JSON.stringify({ project, stepId }) });
      const initialInfo: RunInfo = { runId: started.runId, kind: 'flux', status: started.status, currentStepId: null, completedStepIds: [], elapsedSeconds: 0,
        requestedInputHash: started.requestedInputHash, stepTotal: 0, log: { cursor: 0, nextCursor: 0, truncated: false, text: '' }, snapshots: [] };
      setLatestRunId(started.runId); setDisplayRunId(started.runId); setRunInfo(initialInfo); setDisplayInfo(initialInfo);
      setRunLog(''); cursorRef.current = 0; setLogCursor(0); setSelectedSnapshot(null); setSelectedStepId(null); setAutoFollow(true);
      setNotice('ViennaPS Process.calculateFlux 单次通量计算已提交；不会推进几何。'); void refreshLists();
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); }
  };

  const exportFluxCsv = () => {
    if (!fluxResult) return;
    const field = fluxResult.fields.find(item => `${item.association}:${item.name}` === selectedFluxField) ?? fluxResult.fields[0];
    if (!field) return;
    const values = field.components === 1 ? fluxResult[field.association].scalars[field.name] : fluxResult[field.association].vectors[field.name];
    if (!values) return;
    const quote = (value: unknown) => `"${String(value).replaceAll('"', '""')}"`;
    const heading = field.nativeUnit ? `${field.name} (${field.nativeUnit})` : `${field.name} (native unit not declared)`;
    const rows = [['tupleIndex', heading], ...values.map((value, index) => [index, Array.isArray(value) ? value.join(';') : value])];
    saveBlob(`viennaps-flux-${fluxResult.execution.stepId}-${field.name}.csv`, new Blob([rows.map(row => row.map(quote).join(',')).join('\n') + '\n'], { type: 'text/csv;charset=utf-8' }));
  };

  const cancelRun = async () => {
    if (!latestRunId) return;
    try { const result = await api<{ status: string }>(`/api/runs/${latestRunId}/cancel`, { method: 'POST' }); setRunInfo(previous => previous ? { ...previous, status: result.status } : previous); }
    catch (error) { setNotice(prettyError(error)); }
  };

  const saveProject = async () => {
    setLoading(true); setNotice('');
    try {
      const result = projectId
        ? await api<{ id: string; project: Project }>(`/api/projects/${projectId}`, { method: 'PUT', body: JSON.stringify(project) })
        : await api<{ id: string; project: Project }>('/api/projects', { method: 'POST', body: JSON.stringify(project) });
      setProject(result.project); setProjectId(result.id); setSavedProjectJson(JSON.stringify(result.project)); setNotice('项目已保存到本机工作台。'); await refreshLists();
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); }
  };

  const createCustomMaterial = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setMaterialBusy(true);
    try {
      const result = await api<MaterialCatalogItem>('/api/materials', { method: 'POST', body: JSON.stringify({
        name: materialDraft.name, color: materialDraft.color, category: materialDraft.category,
        densityGcm3: materialDraft.densityGcm3.trim() ? Number(materialDraft.densityGcm3) : null,
        conductive: materialDraft.conductive, description: materialDraft.description,
      }) });
      setMaterials(previous => [...previous, result]);
      setProject(previous => previous.dimension === 3 && previous.view3D
        ? { ...previous, view3D: { ...previous.view3D, visibleMaterialIds: initialVisibleMaterialIds(
          [...materials, result], previous.geometry, [result.id, ...previous.view3D.visibleMaterialIds]) } }
        : previous);
      setMaterialDraft({ name: '', color: '#8b5cf6', category: 'Generic', densityGcm3: '', conductive: false, description: '' });
      setNotice(`已保存自定义材料 ${result.name}（原生 ID ${result.id}）。材料名称和元数据不会自动建立工艺物理模型。`);
    } catch (error) { setNotice(prettyError(error)); }
    finally { setMaterialBusy(false); }
  };

  const saveCustomMaterialMetadata = async (name: string, event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setMaterialBusy(true);
    const data = new FormData(event.currentTarget);
    const density = String(data.get('densityGcm3') ?? '').trim();
    try {
      const result = await api<MaterialCatalogItem>(`/api/materials/${encodeURIComponent(name)}`, { method: 'PATCH', body: JSON.stringify({
        color: String(data.get('color') ?? '#ffffff'), category: String(data.get('category') ?? 'Generic'), densityGcm3: density ? Number(density) : null,
        conductive: data.has('conductive'), description: String(data.get('description') ?? ''),
      }) });
      setMaterials(previous => previous.map(item => item.name === name ? result : item));
      setNotice(`已更新 ${name} 的元数据。该材料的工艺行为仍由配方模型和速率参数决定。`);
    } catch (error) { setNotice(prettyError(error)); }
    finally { setMaterialBusy(false); }
  };

  const deleteCustomMaterial = async (material: MaterialCatalogItem) => {
    setDeletingMaterial(material.name); setDeleteMaterialError(''); setNotice('');
    try {
      await api(`/api/materials/${encodeURIComponent(material.name)}`, { method: 'DELETE', body: JSON.stringify({ project }) });
      setMaterials(previous => previous.filter(item => item.name !== material.name));
      setProject(previous => previous.view3D
        ? { ...previous, view3D: { ...previous.view3D, visibleMaterialIds: previous.view3D.visibleMaterialIds.filter(id => id !== material.id) } }
        : previous);
      setSelection(previous => previous.kind === 'material' && previous.name === material.name ? { kind: 'none' } : previous);
      setDeleteMaterialCandidate(null);
      setNotice(`已删除自定义材料 ${material.name}；原生 ID ${material.id} 已保留，不会重新分配。`);
    } catch (error) { setDeleteMaterialError(prettyError(error)); }
    finally { setDeletingMaterial(null); }
  };

  const openProject = async () => {
    if (!savedSelection) return;
    try {
      const result = await api<{ id: string; project: Project }>(`/api/projects/${savedSelection}`);
      const loadedProject = normalizeGdsMaskLayer(result.project);
      const firstStep = loadedProject.steps[0] ?? null;
      setProject(loadedProject); setProjectId(result.id); setSavedProjectJson(JSON.stringify(loadedProject)); setSelectedStep(firstStep?.id ?? null); setSelection(firstStep ? { kind: 'step', id: firstStep.id } : { kind: 'none' }); setActivity(firstStep ? 'recipe' : 'structure'); setMobilePanel(firstStep ? 'settings' : 'structure'); setInspectorOpen(true); setGdsInspection(null);
      const projectAsset = gdsAssetIdFromProject(loadedProject); if (projectAsset) await loadGdsAsset(projectAsset); else setGdsAsset(null);
      setNotice('项目已从本机工作台打开。');
    } catch (error) { setNotice(prettyError(error)); }
  };

  const downloadContinuationPackage = async (value: { project: Project; replay?: unknown }, filename: string) => {
    const response = await fetch('/api/continuation-package/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
    if (!response.ok) {
      const text = await response.text();
      let message = `导出状态包失败：HTTP ${response.status}`;
      try { const data = JSON.parse(text) as { detail?: unknown }; if (typeof data.detail === 'string') message = data.detail; } catch { /* keep HTTP status */ }
      throw new Error(message);
    }
    saveBlob(filename, await response.blob());
  };

  const exportProject = async () => {
    try {
      const validated = await api<{ normalizedProject: Project }>('/api/projects/validate', { method: 'POST', body: JSON.stringify(project) });
      const exportReady = validated.normalizedProject;
      if (exportReady.continuationState || exportReady.geometry.kind === 'gds') await downloadContinuationPackage({ project: exportReady }, `${exportReady.name || 'project'}.zip`);
      else saveBlob(`${exportReady.name || 'project'}.json`, new Blob([JSON.stringify(exportReady, null, 2)], { type: 'application/json' }));
    } catch (error) { setNotice(prettyError(error)); }
  };

  const startReplay = async (replay: unknown) => {
    const started = await api<{ runId: string; status: string; requestedInputHash: string }>('/api/runs/replay', { method: 'POST', body: JSON.stringify(replay) });
    const replayData = replay as { normalizedProject: Project; analysisMode?: 'process' | 'flux' | 'slice' };
    const replayProject = replayData.normalizedProject;
    setProject(replayProject); setProjectId(null); setSelectedStep(null); setSelection({ kind: 'run', id: started.runId }); setActivity('results'); setMobilePanel('structure'); setInspectorOpen(true);
    setGdsInspection(null);
    const replayAsset = gdsAssetIdFromProject(replayProject); if (replayAsset) await loadGdsAsset(replayAsset); else setGdsAsset(null);
    const replayKind = replayData.analysisMode === 'flux' ? 'flux' : replayData.analysisMode === 'slice' ? 'slice' : replayProject.steps.some(step => step.enabled) ? 'simulate' : 'preview';
    const initialInfo: RunInfo = { runId: started.runId, kind: replayKind, status: started.status, currentStepId: null, completedStepIds: [], elapsedSeconds: 0, requestedInputHash: started.requestedInputHash, stepTotal: replayKind === 'simulate' ? replayProject.steps.filter(step => step.enabled).length : 0, log: { cursor: 0, nextCursor: 0, truncated: false, text: '' }, snapshots: [] };
    setFluxResult(null); setFluxResultRunId(null);
    setLatestRunId(started.runId); setDisplayRunId(started.runId); setRunInfo(initialInfo); setDisplayInfo(initialInfo);
    setRunLog(''); cursorRef.current = 0; setSelectedSnapshot(null); setSelectedStepId(null); setAutoFollow(true); setNotice('重放任务已提交；页面显示其起点是初始结构还是保存的 ViennaPS Domain 状态。');
  };

  const importFile = async (file: File) => {
    try {
      setLoading(true);
      if (file.name.toLowerCase().endsWith('.zip')) {
        const maxPackageBytes = effectiveArtifactCapBytes(capabilities);
        if (file.size > maxPackageBytes) throw new Error(`状态包不能超过 ${formatArtifactCapMiB(maxPackageBytes)}。`);
        const imported = await api<{ project: Project; replay?: unknown | null }>('/api/continuation-package/import', { method: 'POST', body: file, headers: { 'Content-Type': 'application/zip' } });
        if (imported.replay) await startReplay(imported.replay);
        else {
          const importedProject = normalizeGdsMaskLayer(imported.project);
          setProject(importedProject); setProjectId(null); setSelectedStep(null); setSelection({ kind: 'none' }); setActivity('structure'); setMobilePanel('structure'); setInspectorOpen(true); setGdsInspection(null);
          const projectAsset = gdsAssetIdFromProject(importedProject); if (projectAsset) await loadGdsAsset(projectAsset); else setGdsAsset(null);
          setNotice(projectAsset ? 'GDS 项目包和源布局已校验并导入。' : 'ViennaPS Domain 状态包已校验并导入，可将该状态用作新工艺的起点。');
        }
        return;
      }
      const parsed: unknown = JSON.parse(await file.text());
      if (typeof parsed === 'object' && parsed !== null && 'format' in parsed && (parsed as { format?: unknown }).format === 'viennaps-replay') {
        await startReplay(parsed);
      } else {
        const valid = await api<{ normalizedProject: Project }>('/api/projects/validate', { method: 'POST', body: JSON.stringify(parsed) });
        const importedProject = normalizeGdsMaskLayer(valid.normalizedProject);
        setProject(importedProject); setProjectId(null); setSelectedStep(null); setSelection({ kind: 'none' }); setActivity('structure'); setMobilePanel('structure'); setInspectorOpen(true); setGdsInspection(null);
        const projectAsset = gdsAssetIdFromProject(importedProject); if (projectAsset) await loadGdsAsset(projectAsset); else setGdsAsset(null);
        setNotice('项目 JSON 已导入。');
      }
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); if (fileInputRef.current) fileInputRef.current.value = ''; }
  };

  const createContinuation = async (runId: string, stepId: string) => {
    try {
      setLoading(true);
      const result = await api<{ project: Project; continuation: { sourceRunId: string; sourceStepId: string; engineVersions: { viennaps: string; viennals: string } } }>(`/api/runs/${runId}/continuation-project/${encodeURIComponent(stepId)}`);
      const continuationProject = result.project.continuationState
        ? { ...result.project, continuationState: { ...result.project.continuationState, sourceRunId: result.continuation.sourceRunId, sourceStepId: result.continuation.sourceStepId } }
        : result.project;
      setProject(continuationProject); setProjectId(null); setSavedSelection(''); setSelectedStep(null); setSelection({ kind: 'none' }); setActivity('structure'); setMobilePanel('structure'); setInspectorOpen(true); clearDisplayedRun();
      setNotice('已从选中快照创建新工艺；起始 Domain 来自保存的 ViennaPS 状态。');
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); }
  };

  const discardContinuationState = () => {
    if (!project.continuationState) return;
    const { continuationState: _state, ...fromInitial } = project;
    setProject({ ...fromInitial, schemaVersion: project.schemaVersion >= 7 ? 7 : 6 }); setNotice('已移除保存状态引用；后续运行将从项目中的初始结构开始。');
  };

  const downloadRun = async (format: 'zip' | 'replay') => {
    if (!displayRunId) return;
    try {
      if (format === 'replay') {
        const replay = await api<Record<string, unknown>>(`/api/runs/${displayRunId}/replay-project`);
        const normalizedReplay = (replay as { normalizedProject?: Project }).normalizedProject;
        if (normalizedReplay?.continuationState) await downloadContinuationPackage({ project: normalizedReplay, replay }, `replay-project-${displayRunId}.zip`);
        else saveBlob(`replay-project-${displayRunId}.json`, new Blob([JSON.stringify(replay, null, 2)], { type: 'application/json' }));
      } else {
        const response = await fetch(`/api/runs/${displayRunId}/download`);
        if (!response.ok) throw new Error(`下载失败：HTTP ${response.status}`);
        saveBlob(`viennaps-run-${displayRunId}.zip`, await response.blob());
      }
    } catch (error) { setNotice(prettyError(error)); }
  };

  const addStep = (model: Step['model'] = 'isotropic') => {
    const id = newStepId();
    if (model === 'specialized') {
      const spec = specializedCatalog?.models.find(row => row.id === newSpecializedModel
        && specializedModelAvailable(row, project.dimension)) ?? availableSpecializedModels[0];
      if (!spec) { setNotice('ViennaPS 专用模型目录尚未加载完成。'); return; }
      const step: Step = { id, name: `步骤 ${project.steps.length + 1}`, enabled: true, model: 'specialized',
        nativeModel: spec.id, operation: spec.operation, nativeParameters: specializedDefaults(spec, project.geometry),
        ...(spec.id === 'oxidation' ? {} : { duration: 0.2 }),
        ...(spec.depositMaterial ? { depositMaterial: spec.depositMaterial } : spec.depositMaterialSelectable ? { depositMaterial: materials.find(item => item.name === 'Al2O3')?.name ?? materials[0]?.name ?? 'Al2O3' } : {}) };
      setProject(previous => { const promoted = promoteProjectToV7(previous); return { ...promoted, steps: [...promoted.steps, step] }; });
      setSelectedStep(id); return;
    }
    if (model === 'multiParticle') {
      const step: Step = { id, name: `步骤 ${project.steps.length + 1}`, enabled: true, operation: 'etch', model,
        duration: 0.2, ...defaultMultiParticleFields() };
      setProject(previous => {
        const promoted = promoteProjectToV7(previous);
        return { ...promoted, steps: [...promoted.steps, step] };
      });
      setSelectedStep(id);
      return;
    }
    if (model === 'directional') {
      const step: Step = { id, name: `步骤 ${project.steps.length + 1}`, enabled: true, operation: 'etch', model,
        direction: defaultDirection(project.dimension), directionalRateMagnitude: 0.2, isotropicRateMagnitude: 0,
        calculateVisibility: true, duration: 0.2 };
      setProject(previous => {
        const promoted = promoteProjectToV5(previous);
        return { ...promoted, steps: [...promoted.steps, step] };
      });
      setSelectedStep(id);
      return;
    }
    const step: Step = { id, name: `步骤 ${project.steps.length + 1}`, enabled: true, operation: 'etch', model, rateMagnitude: 0.2, duration: 0.2,
      ...(project.schemaVersion >= 4 ? { rateMode: 'uniform' as const } : {}),
      ...(project.schemaVersion >= 4 && model === 'isotropic' ? { isotropicMethod: 'auto' as const } : {}),
      ...(model === 'singleParticle' ? { stickingProbability: 0.5, sourceExponent: 1 } : {}) };
    updateProject(previous => ({ ...previous, steps: [...previous.steps, step] })); setSelectedStep(id);
  };
  const addCsvStep = (asset: CsvRateAsset) => {
    const id = newStepId();
    const step: Step = {
      id, name: `步骤 ${project.steps.length + 1}`, enabled: true, operation: 'native', model: 'csvProcess',
      duration: 0.2, assetId: asset.assetId, fileName: asset.fileName,
      direction: defaultDirection(project.dimension), offset: [0, 0],
      isotropicComponent: 0, directionalComponent: 1,
      interpolationMode: asset.linearCompatible ? 'LINEAR' : 'IDW',
      ...(asset.linearCompatible ? {} : { idwNeighbors: Math.min(4, asset.rowCount) }),
      calculateVisibility: true,
    };
    setProject(previous => {
      const promoted = promoteProjectToV7(previous);
      return { ...promoted, steps: [...promoted.steps, step] };
    });
    setSelectedStep(id);
  };
  const uploadCsvFile = async (file: File, mode: 'new' | 'replace') => {
    setLoading(true); setNotice('');
    try {
      const response = await fetch(`/api/csv-assets?file_name=${encodeURIComponent(file.name)}&dimension=${project.dimension}`, {
        method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: file,
      });
      const result = await response.json() as CsvRateAsset | { detail?: unknown };
      if (!response.ok) {
        const detail = 'detail' in result ? result.detail : undefined;
        throw new Error(typeof detail === 'string' ? detail : `CSV 上传失败：HTTP ${response.status}`);
      }
      const asset = result as CsvRateAsset;
      setCsvAsset(asset);
      if (mode === 'replace' && selectedStep) {
        setProject(previous => ({ ...previous, steps: previous.steps.map(step => {
          if (step.id !== selectedStep || step.model !== 'csvProcess') return step;
          const nextMode = asset.linearCompatible ? step.interpolationMode ?? 'LINEAR' : step.interpolationMode === 'LINEAR' ? 'IDW' : step.interpolationMode ?? 'IDW';
          return { ...step, assetId: asset.assetId, fileName: asset.fileName, interpolationMode: nextMode,
            ...(nextMode === 'IDW' ? { idwNeighbors: Math.min(step.idwNeighbors ?? 4, asset.rowCount) } : { idwNeighbors: undefined }) };
        }) }));
        setNotice(`已替换为 ${asset.fileName}（${asset.rowCount} 行，${asset.linearCompatible ? '支持 LINEAR' : '仅 IDW'}）。`);
      } else {
        addCsvStep(asset);
        setNotice(`已上传 ${asset.fileName}（${asset.dimension}D，${asset.rowCount} 行）。`);
      }
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); if (csvInputRef.current) csvInputRef.current.value = ''; }
  };
  const changeSpecializedModel = (stepId: string, nativeModel: string) => {
    const spec = specializedCatalog?.models.find(row => row.id === nativeModel);
    if (!spec) return;
    if (!specializedModelAvailable(spec, project.dimension)) { setNotice(`${spec.label} ${specializedDimensionLabel(spec) ?? '不支持当前维度'}。`); return; }
    setProject(previous => {
      const promoted = promoteProjectToV7(previous);
      return { ...promoted, steps: promoted.steps.map(step => {
        if (step.id !== stepId) return step;
        return { id: step.id, name: step.name, enabled: step.enabled, model: 'specialized', nativeModel: spec.id,
          operation: spec.operation, nativeParameters: specializedDefaults(spec, previous.geometry),
          ...(spec.id === 'oxidation' ? {} : { duration: step.duration ?? 0.2 }),
          ...(spec.depositMaterial ? { depositMaterial: spec.depositMaterial } : spec.depositMaterialSelectable ? { depositMaterial: step.depositMaterial ?? materials.find(item => item.name === 'Al2O3')?.name ?? 'Al2O3' } : {}) };
      }) };
    });
  };
  const availableSpecializedModels = (specializedCatalog?.models ?? []).filter(spec => specializedModelAvailable(spec, project.dimension));
  const editSpecializedParameter = (stepId: string, key: string, value: unknown) => editStep(stepId, step => step.model === 'specialized'
    ? { ...step, nativeParameters: { ...(step.nativeParameters ?? {}), [key]: value } } : step);
  const addGeometryStep = (model: 'removeMaterial' | 'planarize' | 'boolean' | 'gdsBoolean') => {
    const id = newStepId();
    const name = model === 'removeMaterial' ? '移除材料' : model === 'planarize' ? '平坦化' : model === 'gdsBoolean' ? 'GDS 布尔' : '布尔几何';
    if (model === 'gdsBoolean') {
      const geometry = project.geometry.kind === 'gds' ? project.geometry : null;
      const pairs = geometry && gdsAsset ? gdsMaskPairs(gdsAsset, geometry.structure) : [];
      const [layer, datatype] = pairs[0] ?? [geometry?.layers[0]?.layer ?? 1, 0];
      const first = geometry?.layers.find(row => row.layer === layer) ?? geometry?.layers[0];
      const step: Step = { id, name, enabled: true, model, booleanOperation: 'difference', applyToAll: true,
        gdsMask: { layer, datatype, unitScale: 1, position: [0, 0, 0],
          baseHeight: first?.baseHeight ?? 0,
          height: Math.max(2 * project.domain.gridDelta, first?.height ?? 0.5),
          retainedRegion: 'inside' } };
      setProject(previous => {
        const promoted = promoteProjectToV7(previous);
        return { ...promoted, steps: [...promoted.steps, step] };
      });
      setSelectedStep(id);
      return;
    }
    const step: Step = model === 'removeMaterial'
      ? { id, name, enabled: true, model, material: materials.find(item => item.name === 'SiO2')?.name ?? materials[0]?.name ?? 'SiO2' }
      : model === 'planarize'
        ? { id, name, enabled: true, model, cutoffHeight: 0.6 }
        : { id, name, enabled: true, model, booleanOperation: 'difference', position: [0, 0, project.dimension === 2 ? 0 : 0.2], width: 1, height: 0.8, ...(project.dimension === 3 ? { depth: 1 } : {}), angle: 0, applyToAll: true };
    setProject(previous => {
      const promoted = promoteProjectToV5(previous);
      return { ...promoted, steps: [...promoted.steps, step] };
    });
    setSelectedStep(id);
  };
  const editStep = (stepId: string, transform: (step: Step) => Step) => updateProject(previous => ({ ...previous, steps: previous.steps.map(step => step.id === stepId ? transform(step) : step) }));
  const editParticleSpecies = (stepId: string, index: number, transform: (species: ParticleSpecies) => ParticleSpecies) => updateProject(previous => ({
    ...previous,
    steps: previous.steps.map(step => {
      if (step.id !== stepId || step.model !== 'multiParticle') return step;
      const species = [...(step.species ?? [])];
      const oldLabel = species[index]?.label;
      if (!species[index] || !step.rateCoefficients) return step;
      species[index] = transform(species[index]);
      const newLabel = species[index].label;
      const rateCoefficients = structuredClone(step.rateCoefficients);
      if (oldLabel !== newLabel) {
        rateCoefficients.defaultBySpecies[newLabel] = rateCoefficients.defaultBySpecies[oldLabel] ?? 0.025;
        delete rateCoefficients.defaultBySpecies[oldLabel];
        for (const row of Object.values(rateCoefficients.byMaterial)) {
          if (Object.hasOwn(row, oldLabel)) { row[newLabel] = row[oldLabel]; delete row[oldLabel]; }
        }
      }
      return { ...step, species, rateCoefficients };
    }),
  }));
  const addParticleSpecies = (stepId: string, kind: ParticleSpecies['kind']) => updateProject(previous => ({
    ...previous,
    steps: previous.steps.map(step => {
      if (step.id !== stepId || step.model !== 'multiParticle' || !step.rateCoefficients || (step.species?.length ?? 0) >= 8) return step;
      const used = new Set((step.species ?? []).map(species => species.label));
      const prefix = kind === 'ion' ? 'ion' : 'neutral';
      let ordinal = 1; while (used.has(`${prefix}${ordinal}`)) ordinal++;
      const label = `${prefix}${ordinal}`;
      const species = kind === 'ion' ? defaultIonSpecies(label) : defaultNeutralSpecies(label);
      const rateCoefficients = structuredClone(step.rateCoefficients);
      rateCoefficients.defaultBySpecies[label] = 0.025;
      for (const row of Object.values(rateCoefficients.byMaterial)) row[label] = 0.025;
      return { ...step, species: [...(step.species ?? []), species], rateCoefficients };
    }),
  }));
  const removeParticleSpecies = (stepId: string, index: number) => updateProject(previous => ({
    ...previous,
    steps: previous.steps.map(step => {
      if (step.id !== stepId || step.model !== 'multiParticle' || (step.species?.length ?? 0) <= 1 || !step.rateCoefficients) return step;
      const label = step.species?.[index]?.label;
      if (!label) return step;
      const rateCoefficients = structuredClone(step.rateCoefficients);
      delete rateCoefficients.defaultBySpecies[label];
      for (const row of Object.values(rateCoefficients.byMaterial)) delete row[label];
      return { ...step, species: step.species?.filter((_, position) => position !== index), rateCoefficients };
    }),
  }));
  const changeStepModel = (stepId: string, model: Step['model']) => setProject(previous => {
    const current = previous.steps.find(step => step.id === stepId);
    if (!current || isGeometryStep(current) || isGeometryStep({ model, id: '', name: '', enabled: true }) || current.model === model) return previous;
    if (current.model === 'multiParticle') {
      const promoted = promoteProjectToV7(previous);
      return { ...promoted, steps: promoted.steps.map(step => {
        if (step.id !== stepId) return step;
        const rate = Math.min(2, Object.values(step.rateCoefficients?.defaultBySpecies ?? {}).reduce((sum, value) => sum + value, 0));
        const { species: _species, rateCoefficients: _rateCoefficients, ...common } = step;
        if (model === 'directional') return { ...stripIsotropicMethod(stripDirectionalRateTable(common)), model, direction: defaultDirection(previous.dimension), directionalRateMagnitude: rate, isotropicRateMagnitude: 0, calculateVisibility: true };
        const converted: Step = { ...stripIsotropicMethod(stripDirectionalRateTable(common)), model, rateMagnitude: Math.max(rate, 0.2), rateMode: 'uniform' };
        if (model === 'singleParticle') { converted.stickingProbability = 0.5; converted.sourceExponent = 1; }
        else { delete converted.stickingProbability; delete converted.sourceExponent; }
        return converted;
      }) };
    }
    if (model === 'multiParticle') {
      return { ...promoteProjectToV7(previous), steps: promoteProjectToV7(previous).steps.map(step => {
        if (step.id !== stepId) return step;
        const { rateMagnitude: _rate, rateMode: _mode, materialRates: _materials, selectivityReference: _reference,
          stickingProbability: _sticking, sourceExponent: _exponent, direction: _direction,
          directionalRateMagnitude: _directional, isotropicRateMagnitude: _isotropic,
          isotropicMethod: _isotropicMethod, isotropicTimeStepRatio: _isotropicTimeStepRatio, defaultMaterialRate: _defaultMaterialRate,
          calculateVisibility: _visibility, ...common } = step;
        return { ...common, model, ...defaultMultiParticleFields() };
      }) };
    }
    if (model === 'directional') {
      const promoted = promoteProjectToV5(previous);
      return { ...promoted, steps: promoted.steps.map(step => {
        if (step.id !== stepId) return step;
        const availableRates = [step.rateMagnitude ?? 0, ...Object.values(scalarMaterialRates(step))];
        const rate = Math.max(...availableRates);
        const { rateMagnitude: _rateMagnitude, rateMode: _rateMode, materialRates: _materialRates, selectivityReference: _selectivityReference,
          stickingProbability: _stickingProbability, sourceExponent: _sourceExponent,
          isotropicMethod: _isotropicMethod, isotropicTimeStepRatio: _isotropicTimeStepRatio, ...common } = step;
        return { ...stripDirectionalRateTable(common), model: 'directional', direction: defaultDirection(previous.dimension), directionalRateMagnitude: rate,
          isotropicRateMagnitude: 0, calculateVisibility: true };
      }) };
    }
    if (current.model === 'directional') {
      const promoted = promoteProjectToV5(previous);
      return { ...promoted, steps: promoted.steps.map(step => {
        if (step.id !== stepId) return step;
        const combinedRate = Math.min(2, (step.directionalRateMagnitude ?? 0) + (step.isotropicRateMagnitude ?? 0));
        const { direction: _direction, directionalRateMagnitude: _directionalRate, isotropicRateMagnitude: _isotropicRate,
          calculateVisibility: _visibility, ...common } = step;
        const converted: Step = { ...stripDirectionalRateTable(common), model, rateMagnitude: combinedRate, rateMode: 'uniform' };
        if (model === 'singleParticle') { converted.stickingProbability = 0.5; converted.sourceExponent = 1; }
        else { delete converted.stickingProbability; delete converted.sourceExponent; }
        return converted;
      }) };
    }
    return { ...previous, steps: previous.steps.map(step => {
      if (step.id !== stepId) return step;
      if (model === 'singleParticle') return { ...stripIsotropicMethod(step), model, rateMagnitude: step.rateMagnitude ?? 0.2, stickingProbability: 0.5, sourceExponent: 1 };
      const converted: Step = { ...step, model, rateMagnitude: step.rateMagnitude ?? 0.2 };
      delete converted.stickingProbability; delete converted.sourceExponent;
      return converted;
    }) };
  });
  const editRateProfile = (stepId: string, transform: (step: Step, project: Project) => Step) => updateProject(previous => ({
    ...previous,
    schemaVersion: previous.schemaVersion >= 7 ? 7 : previous.schemaVersion === 6 ? 6 : previous.schemaVersion >= 5 ? 5 : 4,
    solver: { ...previous.solver, enginePreference: 'cpu' },
    steps: previous.steps.map(step => step.model === 'directional' || step.model === 'specialized' ? step : step.id === stepId ? transform({ ...step, rateMode: step.rateMode ?? 'uniform' }, previous) : { ...step, rateMode: step.rateMode ?? 'uniform' }),
  }));
  const moveStep = (stepId: string, offset: -1 | 1) => updateProject(previous => {
    const index = previous.steps.findIndex(step => step.id === stepId); const nextIndex = index + offset;
    if (index < 0 || nextIndex < 0 || nextIndex >= previous.steps.length) return previous;
    const items = [...previous.steps]; [items[index], items[nextIndex]] = [items[nextIndex], items[index]];
    return { ...previous, steps: items };
  });
  const copyStep = (step: Step) => {
    const duplicate = { ...step, id: newStepId(), name: `${step.name} 副本` };
    updateProject(previous => { const index = previous.steps.findIndex(item => item.id === step.id); const items = [...previous.steps]; items.splice(index + 1, 0, duplicate); return { ...previous, steps: items }; });
    setSelectedStep(duplicate.id);
  };
  const setRateMode = (stepId: string, mode: 'uniform' | 'material') => editRateProfile(stepId, (step, source) => {
    if (mode === 'material' && step.model !== 'directional' && step.model !== 'specialized' && materialRateModeAvailable(step)) return {
      ...step, rateMode: 'material',
      materialRates: step.rateMode === 'material' ? { ...scalarMaterialRates(step) } : configuredMaterialRates(source, step.rateMagnitude ?? 0.2),
    };
    const next = { ...step, rateMode: 'uniform' as const };
    delete next.materialRates; delete next.selectivityReference;
    return next;
  });
  const setMaterialRate = (stepId: string, name: string, rate: number) => editRateProfile(stepId, step => ({ ...step, rateMode: 'material', materialRates: { ...scalarMaterialRates(step), [name]: rate } }));
  const removeMaterialRate = (stepId: string, name: string) => editRateProfile(stepId, step => {
    const materialRates = { ...scalarMaterialRates(step) }; delete materialRates[name];
    return { ...step, rateMode: 'material', materialRates };
  });
  const addMaterialRate = (stepId: string, name: string) => {
    if (!name) return;
    editRateProfile(stepId, step => ({ ...step, rateMode: 'material', materialRates: { ...scalarMaterialRates(step), [name]: step.rateMagnitude ?? 0.2 } }));
    setRateRowMaterial('');
  };
  const setSelectivityReference = (stepId: string, name: string) => editRateProfile(stepId, step => {
    const next = { ...step, rateMode: 'material' as const };
    if (name) next.selectivityReference = name; else delete next.selectivityReference;
    return next;
  });
  // Directional material-rate table. Scalar rates and the visibility flag stay
  // out of the object while this mode is active (the native RateMap has no
  // visibility control and the schema rejects mixed scalar/material steps).
  const setDirectionalMaterialRate = (stepId: string, name: string, patch: Partial<DirectionalMaterialRate>) => editStep(stepId, step => {
    const table = directionalMaterialRates(step);
    return { ...step, materialRates: { ...table, [name]: { ...(table[name] ?? { directional: 0, isotropic: 0 }), ...patch } } };
  });
  const removeDirectionalMaterialRate = (stepId: string, name: string) => editStep(stepId, step => {
    const table = directionalMaterialRates(step); delete table[name];
    return { ...step, materialRates: table };
  });
  const addDirectionalMaterialRate = (stepId: string, name: string) => {
    if (!name) return;
    editStep(stepId, step => ({ ...step, materialRates: { ...directionalMaterialRates(step), [name]: { ...(step.defaultMaterialRate ?? { directional: 0, isotropic: 0 }) } } }));
    setDirectionalRateRowMaterial('');
  };
  const setDirectionalDefaultMaterialRate = (stepId: string, patch: Partial<DirectionalMaterialRate>) => editStep(stepId, step => ({
    ...step, defaultMaterialRate: { ...(step.defaultMaterialRate ?? { directional: 0, isotropic: 0 }), ...patch },
  }));
  const setDirectionalRateMode = (stepId: string, mode: 'scalar' | 'material') => editStep(stepId, step => {
    if (mode === 'material') {
      if (isDirectionalMaterialMode(step)) return step;
      return {
        ...stripDirectionalScalarRates(step),
        materialRates: {},
        defaultMaterialRate: { directional: step.directionalRateMagnitude ?? 0.2, isotropic: step.isotropicRateMagnitude ?? 0 },
      };
    }
    if (!isDirectionalMaterialMode(step)) return step;
    return {
      ...stripDirectionalRateTable(step),
      directionalRateMagnitude: step.defaultMaterialRate?.directional ?? 0.2,
      isotropicRateMagnitude: step.defaultMaterialRate?.isotropic ?? 0,
      calculateVisibility: true,
    };
  });
  const updatePattern = (part: Partial<Exclude<StackGeometry['pattern'], { kind: 'none' }>>) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack' || previous.geometry.pattern.kind === 'none') return previous;
    return { ...previous, geometry: { ...previous.geometry, pattern: { ...previous.geometry.pattern, ...part } as StackGeometry['pattern'] } };
  });
  const editLayer = (id: string, patch: Partial<Layer>) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack') return previous;
    const layers = previous.geometry.layers.map(layer => layer.id === id ? { ...layer, ...patch } : layer);
    if (previous.geometry.pattern.kind === 'gds') return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, layers }) };
    if (previous.geometry.pattern.kind !== 'none' && layers.at(-1)?.role !== 'mask') setNotice('已关闭顶层图案，因为新的顶层不是掩膜层；已有层的材料和角色保持不变。');
    return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, layers, pattern: previous.geometry.pattern.kind !== 'none' && layers.at(-1)?.role !== 'mask' ? { kind: 'none' } : previous.geometry.pattern }) };
  });
  const addLayer = () => updateProject(previous => {
    if (previous.geometry.kind !== 'stack' || previous.geometry.layers.length >= 16) return previous;
    const oldLayers = previous.geometry.layers;
    const layers = [...oldLayers];
    const gdsIndex = layers.findIndex(layer => layer.source === 'gds-mask');
    const newLayer = { id: `layer_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, name: `新层 ${oldLayers.length + 1}`, material: materials.find(m => m.name === 'SiO2')?.name ?? 'SiO2', thickness: Math.max(.6, 2 * previous.domain.gridDelta), role: 'film' as const };
    if (gdsIndex >= 0) layers.splice(gdsIndex, 0, newLayer); else layers.push(newLayer);
    if (previous.geometry.pattern.kind !== 'none' && previous.geometry.pattern.kind !== 'gds') setNotice('已关闭顶层图案，因为新增薄膜后顶层不再是原掩膜；已有层的材料和角色保持不变。');
    return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, layers, pattern: previous.geometry.pattern.kind === 'gds' ? previous.geometry.pattern : { kind: 'none' } }) };
  });
  const removeLayer = (id: string) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack') return previous;
    const removingGdsMask = previous.geometry.layers.some(layer => layer.id === id && layer.source === 'gds-mask');
    const layers = previous.geometry.layers.filter(layer => layer.id !== id);
    const closePattern = previous.geometry.pattern.kind !== 'none' && (layers.at(-1)?.role !== 'mask' || !layers.length);
    if (closePattern) setNotice('已关闭顶层图案，因为删除层后顶层不再是掩膜层；其他已有层的材料和角色保持不变。');
    const geometry = { ...previous.geometry, layers, pattern: removingGdsMask || closePattern ? { kind: 'none' as const } : previous.geometry.pattern, ...(removingGdsMask ? { gdsMask: undefined } : {}) };
    return { ...previous, geometry: syncGdsMaskLayer(geometry) };
  });
  const moveLayer = (id: string, offset: -1 | 1) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack') return previous;
    const layers = [...previous.geometry.layers], index = layers.findIndex(layer => layer.id === id), target = index + offset;
    if (index < 0 || target < 0 || target >= layers.length) return previous;
    const gdsIndex = layers.findIndex(layer => layer.source === 'gds-mask');
    if (index === gdsIndex || (gdsIndex >= 0 && target >= gdsIndex)) return previous;
    [layers[index], layers[target]] = [layers[target], layers[index]];
    if (previous.geometry.pattern.kind !== 'none' && layers.at(-1)?.role !== 'mask') setNotice('已关闭顶层图案，因为换序后顶层不再是掩膜层；已有层的材料和角色保持不变。');
    return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, layers, pattern: previous.geometry.pattern.kind !== 'none' && previous.geometry.pattern.kind !== 'gds' && layers.at(-1)?.role !== 'mask' ? { kind: 'none' } : previous.geometry.pattern }) };
  });
  const copyLayer = (id: string) => updateProject(previous => {
    if (previous.geometry.kind !== 'stack' || previous.geometry.layers.length >= 16) return previous;
    const index = previous.geometry.layers.findIndex(layer => layer.id === id), original = previous.geometry.layers[index];
    if (!original || original.source === 'gds-mask') return previous;
    const layers = [...previous.geometry.layers];
    layers.splice(index + 1, 0, { ...original, id: `layer_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, name: `${original.name} 副本` });
    const closePattern = previous.geometry.pattern.kind !== 'none' && previous.geometry.pattern.kind !== 'gds' && layers.at(-1)?.role !== 'mask';
    if (closePattern) setNotice('已关闭顶层图案，因为复制后顶层不再是掩膜层；已有层的材料和角色保持不变。');
    return { ...previous, geometry: syncGdsMaskLayer({ ...previous.geometry, layers, pattern: closePattern ? { kind: 'none' } : previous.geometry.pattern }) };
  });

  const loadServerRun = async (runId: string, preferLatest: boolean) => {
    setSelectedStep(null);
    setSelection({ kind: 'run', id: runId });
    setActivity('results'); setMobilePanel('structure'); setInspectorOpen(true);
    await loadRun(runId, preferLatest);
  };
  const layerCount = project.geometry.kind === 'stack' ? project.geometry.layers.length : 0;
  const selectedGds = project.geometry.kind === 'gds' ? project.geometry : null;
  const clearDisplayedRun = () => {
    snapshotSequence.current++; comparisonSequence.current++;
    setLatestRunId(null); setRunInfo(null); setRunLog(''); setLogCursor(0);
    setDisplayInfo(null); setDisplayRunId(null); setSelectedSnapshot(null); setSelectedStepId(null);
    setFluxResult(null); setFluxResultRunId(null); setSelectedFluxField('');
    setCompareRunId(''); setCompareInfo(null); setCompareSnapshot(null); setAutoFollow(false);
  };
  const newProject = (dimension: 2 | 3) => {
    const next: Project = dimension === 2 ? structuredClone(defaultProject) : {
      ...structuredClone(defaultProject), name: '三维工艺项目', schemaVersion: 7, dimension: 3,
      domain: { gridDelta: .2, xExtent: 6, yExtent: 6, boundaryXY: 'reflective' },
      outputSettings: { ...structuredClone(defaultProject.outputSettings!), volumeMesh: true },
      geometry: { kind: 'stack', substrateMaterial: 'Si', baseHeight: 0, layers: [
        { id: 'layer_sio2', name: 'SiO₂ 薄膜', material: 'SiO2', thickness: .6, role: 'film' },
        { id: 'layer_si3n4', name: 'Si₃N₄ 掩膜', material: 'Si3N4', thickness: .6, role: 'mask' },
      ], pattern: { kind: 'hole', radius: 1, taperAngle: 0 } }, view3D: { ...structuredClone(defaultView3D), visibleMaterialIds: initialVisibleMaterialIds(materials, defaultProject.geometry) },
    };
    if (dimension === 3) delete next.view;
    setProject(next); setProjectId(null); setSavedSelection(''); setSelectedStep(null); clearDisplayedRun();
    setNotice(`已创建新的${dimension === 3 ? '三' : '二'}维工程，原工程和运行记录仍保留。`);
  };
  const switchProjectDimension = (dimension: 2 | 3) => {
    if (dimension === project.dimension || loading || active) return;
    const source = structuredClone(project);
    const sourceBaseName = source.name.replace(/（[23]D 副本）$/, '');
    const targetName = projectId || source.name !== sourceBaseName ? `${sourceBaseName}（${dimension}D 副本）` : source.name;
    let next: Project;
    let convertedHole = false;
    let directionFallback = false;
    let geometry: Geometry = source.geometry;
    if (source.geometry.kind === 'gds') {
      const value = source.geometry;
      geometry = dimension === 3
        ? { ...value, position: [value.position[0], 0, value.position[1]], layers: value.layers.map(layer => ({
            layer: layer.layer, material: layer.material, baseHeight: layer.baseHeight ?? 0,
            height: layer.height ?? Math.max(0.5, 2 * source.domain.gridDelta), mask: layer.mask ?? false,
          })) }
        : { ...value, position: [value.position[0], value.position[2], 0], layers: value.layers.map(layer => ({ layer: layer.layer, material: layer.material })) };
    } else if (source.geometry.kind === 'fin' && dimension === 2 && source.geometry.extrusion) {
      const { extrusion: _extrusion, ...fin } = source.geometry;
      geometry = fin;
    }
    const droppedGdsBoolean = dimension === 2 && source.steps.some(step => step.model === 'gdsBoolean');
    const droppedSpecialized = source.steps.filter(step => {
      if (step.model !== 'specialized') return false;
      const spec = specializedCatalog?.models.find(item => item.id === step.nativeModel);
      return !!spec?.dimensions?.length && !spec.dimensions.includes(dimension);
    });
    const droppedSpecializedIds = new Set(droppedSpecialized.map(step => step.id));
    const droppedCsv = source.steps.filter(step => step.model === 'csvProcess');
    const droppedCsvIds = new Set(droppedCsv.map(step => step.id));
    const droppedNotice = [
      droppedSpecialized.length
        ? `已移除不支持的专用模型：${droppedSpecialized.map(step => step.nativeModel).join('、')}。`
        : '',
      droppedGdsBoolean ? '已移除 GDS 布尔步骤（仅支持 3D GDS 项目）。' : '',
      droppedCsv.length
        ? `已移除 CSV 速率表步骤：${droppedCsv.map(step => step.fileName ?? '未命名').join('、')}（速率表维度、方向与 offset 在 2D/3D 之间不兼容，请重新上传并添加）。`
        : '',
    ].filter(Boolean).join(' ');
    const convertedSteps = source.steps
      .filter(step => !(dimension === 2 && step.model === 'gdsBoolean') && !droppedSpecializedIds.has(step.id) && !droppedCsvIds.has(step.id))
      .map(step => {
      if (step.model === 'boolean') {
        const position = step.position ?? [0, 0, 0];
        if (dimension === 3) return { ...step, position: [position[0], 0, position[1]] as [number, number, number], depth: step.depth ?? step.width ?? 1 };
        const converted = { ...step, position: [position[0], position[2], 0] as [number, number, number] };
        delete converted.depth;
        return converted;
      }
      if (step.model !== 'directional') return step;
      const direction = step.direction ?? defaultDirection(source.dimension);
      if (dimension === 3) return { ...step, direction: [direction[0], 0, direction[1]] as [number, number, number] };
      const mapped: [number, number, number] = [direction[0], direction[2], 0];
      if (Math.hypot(mapped[0], mapped[1]) <= 1e-12) {
        directionFallback = true;
        return { ...step, direction: defaultDirection(2) };
      }
      return { ...step, direction: mapped };
    });
    if (dimension === 3) {
      next = {
        ...source,
        name: targetName,
        schemaVersion: source.schemaVersion >= 7 ? 7 : source.schemaVersion === 6 ? 6 : source.schemaVersion === 5 ? 5 : source.schemaVersion >= 4 ? 4 : 3,
        dimension: 3,
        steps: convertedSteps,
        geometry,
        outputSettings: { ...(source.outputSettings ?? defaultProject.outputSettings!), volumeMesh: true },
        domain: { gridDelta: source.domain.gridDelta, xExtent: source.domain.xExtent, yExtent: source.domain.yExtent ?? source.domain.xExtent, boundaryXY: source.domain.boundaryXY ?? source.domain.xBoundary ?? 'reflective' },
        view3D: {
          ...(source.view3D ? structuredClone(source.view3D) : structuredClone(historyView3D)),
          visibleMaterialIds: initialVisibleMaterialIds(materials, geometry, source.view3D?.visibleMaterialIds ?? historyView3D.visibleMaterialIds),
        },
      };
      delete next.view;
      setHistoryView2D(structuredClone(source.view ?? defaultProject.view!));
    } else {
      const boundary = source.domain.boundaryXY ?? source.domain.xBoundary ?? 'reflective';
      geometry = source.geometry.kind === 'hole' ? {
        kind: 'trench' as const, width: source.geometry.radius * 2, depth: source.geometry.depth,
        taperAngle: source.geometry.taperAngle, maskHeight: source.geometry.maskHeight,
        maskTaperAngle: source.geometry.maskTaperAngle, substrateMaterial: source.geometry.substrateMaterial,
      } : source.geometry.kind === 'gds' || source.geometry.kind === 'fin' ? geometry : source.geometry.kind === 'stack' && source.geometry.pattern.kind === 'hole'
        ? { ...source.geometry, pattern: { kind: 'trench' as const, width: source.geometry.pattern.radius * 2, taperAngle: source.geometry.pattern.taperAngle } }
        : source.geometry;
      convertedHole = source.geometry.kind === 'hole' || (source.geometry.kind === 'stack' && source.geometry.pattern.kind === 'hole');
      next = {
        ...source,
        name: targetName,
        schemaVersion: source.schemaVersion >= 7 ? 7 : source.schemaVersion === 6 ? 6 : source.schemaVersion === 5 ? 5 : source.schemaVersion >= 4 ? 4 : 3,
        dimension: 2,
        steps: convertedSteps,
        domain: { gridDelta: source.domain.gridDelta, xExtent: source.domain.xExtent, xBoundary: boundary },
        geometry,
        view: structuredClone(source.view ?? historyView2D),
      };
      delete next.view3D;
      setHistoryView3D(structuredClone(source.view3D ?? historyView3D));
    }
    setProject(next); setProjectId(null); setSavedSelection(''); clearDisplayedRun();
    const baseNotice = directionFallback
      ? '已转换到 2D：3D 方向若只有 y 分量则没有对应二维方向，已改为 [0, -1, 0]（向下）。'
      : convertedHole
      ? '已新建对应 2D 工程：圆孔按 x 截面转换为宽度等于孔径的沟槽；原工程和历史结果保持不变。'
      : `已新建对应 ${dimension}D 工程；原工程和历史结果保持不变。`;
    setNotice(`${baseNotice}${droppedNotice ? ` ${droppedNotice}` : ''}`);
  };
  const snapshotRows = displayInfo?.runId === displayRunId ? displayInfo.snapshots : [];
  const gpuStatus = capabilities?.capabilities?.engines?.gpu?.status ?? 'unverified-outside-sandbox';
  const gpuRows3D = capabilities?.capabilities?.threeDimensional?.rows?.filter(r => r.status === 'verified' && r.dimension === 3 && r.engine.startsWith('GPU_')) ?? [];
  const enabledFluxSteps = project.steps.filter(usesFlux);
  const finFallbackModels = project.geometry.kind === 'fin' && (project.geometry.construction ?? 'native') === 'native'
    ? [...new Set(enabledFluxSteps.filter(step => step.model === 'specialized' && NATIVE_MAKEFIN_SIGSEGV_MODELS.has(step.nativeModel ?? '')).map(step => step.nativeModel!))].sort()
    : [];
  const masked3DFinFallbackModels = project.geometry.kind === 'fin' && project.dimension === 3
    && project.geometry.maskHeight > 0
    && (project.geometry.construction ?? 'native') === 'native'
    ? [...new Set(enabledFluxSteps.filter(step => step.model === 'singleParticle' || step.model === 'multiParticle').map(step => step.model))].sort()
    : [];
  const finExtrusionFallbackModels = project.geometry.kind === 'fin' && project.dimension === 3
    && !!project.geometry.extrusion && (project.domain.boundaryXY ?? project.domain.xBoundary) === 'periodic'
    ? [...new Set(enabledFluxSteps.filter(step =>
      (step.model === 'specialized' && NATIVE_MAKEFIN_SIGSEGV_MODELS.has(step.nativeModel ?? ''))
      || (project.geometry.kind === 'fin' && project.geometry.maskHeight > 0
        && (step.model === 'singleParticle' || step.model === 'multiParticle'))
    ).map(step => step.model === 'specialized' ? step.nativeModel! : step.model))].sort()
    : [];
  const effectiveFinConstruction = project.geometry.kind === 'fin'
    ? (project.geometry.construction === 'boolean' || finFallbackModels.length > 0 || masked3DFinFallbackModels.length > 0 ? 'boolean' : 'native')
    : 'native';
  const boundary = project.domain.boundaryXY ?? project.domain.xBoundary;
  const schemaGpuRows = (capabilities?.capabilities?.gpuModelRows ?? []).filter(r => r.status === 'verified' && r.dimension === project.dimension && r.schemaVersions?.includes(project.schemaVersion));
  const gpuMaterialRoles = () => {
    const roles = new Map<string, Set<string>>();
    const add = (name: string | undefined, role: string) => { if (name) { const set = roles.get(name) ?? new Set<string>(); set.add(role); roles.set(name, set); } };
    const geometry = project.geometry;
    if (geometry.kind === 'gds') geometry.layers.forEach(layer => add(layer.material, layer.mask ? 'mask' : 'film'));
    else {
      add(geometry.substrateMaterial, 'bulk');
      if (geometry.kind === 'stack') geometry.layers.forEach(layer => add(layer.material, layer.role));
      else if ((geometry.kind === 'trench' || geometry.kind === 'hole' || geometry.kind === 'fin') && geometry.maskHeight > 0) add(geometry.maskMaterial ?? 'Mask', 'mask');
    }
    return Object.fromEntries([...roles.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, values]) => [name, [...values].sort()]));
  };
  const gpuMaterialSignature = (step: Step) => {
    const names = new Set(geometryMaterialNames(project.geometry));
    const addKeys = (value: unknown) => {
      if (value && typeof value === 'object' && !Array.isArray(value)) Object.keys(value).forEach(name => names.add(name));
    };
    addKeys(step.materialRates);
    addKeys(step.rateCoefficients?.byMaterial);
    (step.species ?? []).forEach(species => { if (species.kind === 'neutral') addKeys(species.stickingProbabilityByMaterial); });
    const native = step.nativeParameters ?? {};
    ['materialRates', 'materialParametersByMaterial', 'materialPlaneWaferRateByMaterial'].forEach(key => addKeys(native[key]));
    if (step.depositMaterial) names.add(step.depositMaterial);
    return [...names].filter(Boolean).sort();
  };
  const gpuMaterialBindings = (step: Step) => {
    const bindings: Record<string, string> = {};
    if (step.depositMaterial) bindings.depositMaterial = step.depositMaterial;
    const native = step.nativeParameters ?? {};
    ['maskMaterial', 'siliconMaterial', 'oxideMaterial', 'etchFrontMaterial'].forEach(key => {
      const value = native[key];
      if (typeof value === 'string' && value) bindings[key] = value;
    });
    return Object.fromEntries(Object.entries(bindings).sort(([a], [b]) => a.localeCompare(b)));
  };
  const gpuSpeciesSignature = (step: Step) => step.model === 'multiParticle'
    ? { species: step.species ?? [], rateCoefficients: step.rateCoefficients ?? { defaultBySpecies: {}, byMaterial: {} } }
    : undefined;
  const stableJson = (value: unknown) => JSON.stringify(value, (_key, child) => {
    if (child && typeof child === 'object' && !Array.isArray(child)) return Object.fromEntries(Object.entries(child).sort(([a], [b]) => a.localeCompare(b)));
    return child;
  });
  const gpuSolverProfile = () => ({
    gridDelta: project.domain.gridDelta,
    timeStepRatio: project.solver.timeStepRatio,
    raysPerPoint: project.solver.raysPerPoint,
    randomMode: project.solver.random.mode,
    advection: project.solver.advection,
    rayTracing: project.solver.rayTracing,
  });
  const matchesKnownGpuProfileFailure = (failure: GpuProfileFailure, step: Step) => {
    const model = step.model === 'specialized' ? `specialized:${step.nativeModel ?? ''}` : step.model;
    const profile = gpuSolverProfile() as Record<string, unknown>;
    return failure.dimension === project.dimension && failure.model === model
      && failure.operation === step.operation && failure.boundaryXY === boundary
      && failure.engine === 'GPU_DISK' && failure.geometryKind === project.geometry.kind
      && stableJson(failure.materials) === stableJson([...gpuMaterialSignature(step)].sort())
      && stableJson(failure.materialRoles) === stableJson(gpuMaterialRoles())
      && stableJson(failure.materialBindings) === stableJson(gpuMaterialBindings(step))
      && stableJson(failure.speciesSignature ?? null) === stableJson(gpuSpeciesSignature(step) ?? null)
      && Object.entries(failure.solverProfile ?? {}).every(([key, value]) => profile[key] === value);
  };
  const knownGpuProfileForStep = (step: Step) =>
    (capabilities?.capabilities?.knownGpuProfileFailures ?? []).find(failure => matchesKnownGpuProfileFailure(failure, step));
  const gpuMaterialScopeMatches = (row: GpuCapabilityRow, step: Step) => {
    const scope = row.materialScope;
    if (!scope || scope.kind !== 'viennaps-native-material-registry-v1' || scope.engineVersion !== '4.6.2'
      || !boundary || !scope.boundaries.includes(boundary)) return false;
    const geometryKind = project.geometry.kind;
    const roleKinds = [...new Set(Object.values(gpuMaterialRoles()).flat())].sort();
    const allowedRoleKinds = scope.roleKindsByGeometry[geometryKind] ?? [];
    if (!allowedRoleKinds.some(candidate => stableJson([...candidate].sort()) === stableJson(roleKinds))) return false;
    const expectedBindings = scope.bindingKeysByOperation[step.operation ?? 'etch'];
    const bindings = gpuMaterialBindings(step);
    if (!expectedBindings || stableJson([...expectedBindings].sort()) !== stableJson(Object.keys(bindings).sort())) return false;
    const materialByName = new Map(materials.map(material => [material.name, material]));
    const names = gpuMaterialSignature(step);
    const selected = names.map(name => materialByName.get(name));
    if (selected.some(material => !material)) return false;
    const ids = selected.map(material => material!.id);
    if (new Set(ids).size !== ids.length) return false;
    const builtinIds = new Set(scope.builtInLegacyIds);
    const [customMin, customMax] = scope.customLegacyIdRange;
    if (ids.some((id, index) => {
      const material = selected[index]!;
      return material.isCustom ? id < customMin || id > customMax : !builtinIds.has(id);
    })) return false;
    if (Object.keys(gpuMaterialRoles()).some(name => !names.includes(name))) return false;
    return Object.values(bindings).every(name => names.includes(name));
  };
  const gpuSpeciesTableScopeMatches = (row: GpuCapabilityRow, step: Step) => {
    if (row.speciesScope?.kind !== 'material-tables-v1' || !row.speciesSignature) return false;
    const normalize = (signature: NonNullable<GpuCapabilityRow['speciesSignature']>) => {
      const species = signature.species ?? [];
      const labels = species.map(item => item.label);
      const defaults = signature.rateCoefficients?.defaultBySpecies ?? {};
      if (!species.length || new Set(labels).size !== labels.length || labels.some(label => !Object.hasOwn(defaults, label))) return null;
      return {
        species: species.map(item => {
          if (item.kind === 'neutral') {
            const { label: _label, stickingProbabilityByMaterial: _sticking, ...native } = item;
            return native;
          }
          const { label: _label, ...native } = item;
          return native;
        }),
        defaultRatesByOrder: labels.map(label => defaults[label]),
      };
    };
    const baseline = normalize(row.speciesSignature);
    const candidate = normalize(gpuSpeciesSignature(step)!);
    return baseline !== null && candidate !== null && stableJson(baseline) === stableJson(candidate);
  };
  const rowMatchesStep = (row: GpuCapabilityRow, step: Step) => {
    if (row.operation !== step.operation || row.boundaryXY !== boundary ||
        row.model !== (step.model === 'specialized' ? `specialized:${step.nativeModel ?? ''}` : step.model)) return false;
    if (knownGpuProfileForStep(step)) return false;
    // Stage 9 rows are scoped to the geometry and material set actually
    // compared against ViennaPS. Legacy rows keep their earlier host scope.
    if (row.geometryKinds && !row.geometryKinds.includes(project.geometry.kind)) return false;
    if (project.geometry.kind === 'gds') {
      const scope = row.gdsScope;
      const has3dMask = project.dimension === 3 && project.geometry.layers.some(layer => layer.mask ?? false);
      if (!scope || scope.kind !== 'viennaps-gds-process-v1' || scope.engineVersion !== '4.6.2'
        || !scope.dimensions.includes(project.dimension)
        || project.geometry.layers.length < 1 || project.geometry.layers.length > scope.maxLayerMappings
        || (has3dMask && !scope.supports3DMask)) return false;
    }
    if (row.finExtrusionMode && (project.geometry.kind !== 'fin' || row.finExtrusionMode !== (project.geometry.extrusion ? 'extruded' : 'direct'))) return false;
    if (project.geometry.kind === 'fin' && row.geometryKinds?.includes('fin') &&
        (row.finExtrusionMode !== (project.geometry.extrusion ? 'extruded' : 'direct') ||
         row.finConstruction !== effectiveFinConstruction)) return false;
    if (row.patternKinds) {
      const pattern = project.geometry.kind === 'stack' ? project.geometry.pattern.kind : null;
      if (!pattern || !row.patternKinds.includes(pattern)) return false;
    }
    if (row.materialScope) {
      if (!gpuMaterialScopeMatches(row, step)) return false;
    } else {
      if (row.materials && row.materials.join('\u0000') !== gpuMaterialSignature(step).join('\u0000')) return false;
      if (row.materialRoles && stableJson(row.materialRoles) !== stableJson(gpuMaterialRoles())) return false;
      if (row.materialBindings && stableJson(row.materialBindings) !== stableJson(gpuMaterialBindings(step))) return false;
    }
    if (step.model === 'multiParticle' && (row.speciesScope?.kind === 'material-tables-v1'
      ? !gpuSpeciesTableScopeMatches(row, step)
      : (!row.speciesSignature || stableJson(row.speciesSignature) !== stableJson(gpuSpeciesSignature(step))))) return false;
    return true;
  };
  const nativeKnownFailure = (capabilities?.capabilities?.knownNativeFailures ?? []).find(failure =>
    failure.failure === 'native-viennaps-process-apply-segfault'
    && failure.engineVersion === '4.6.2'
    && enabledFluxSteps.some(step => failure.model === (step.model === 'specialized' ? `specialized:${step.nativeModel ?? ''}` : step.model)
      && failure.dimension === project.dimension
      && failure.operation === step.operation
      && failure.boundaryXY === boundary
      && failure.geometryKind === project.geometry.kind));
  const nativeFailureNotice = nativeKnownFailure
    ? `该模型和几何组合曾触发 ViennaPS 4.6.2 原生崩溃；运行失败时可在历史记录中查看错误。`
    : null;
  const schemaGpuStepsVerified = enabledFluxSteps.length > 0 && enabledFluxSteps.every(step => schemaGpuRows.some(row => rowMatchesStep(row, step)));
  const legacyGpuRows = project.dimension === 3 ? gpuRows3D.filter(row => row.dimension === 3 && row.boundaryXY === boundary) : [];
  const gpuCoverageVerified = project.schemaVersion < 3
    ? (project.dimension === 2 ? capabilities?.capabilities?.engines?.gpu?.exposed === true : enabledFluxSteps.length > 0 && enabledFluxSteps.every(step => legacyGpuRows.some(row => rowMatchesStep(row, step))))
    : schemaGpuStepsVerified;
  const gpuDeviceSelectable = (gpuInventory?.devices.length ?? 0) > 0;
  const gpuExposed = gpuDeviceSelectable;
  const defaultGpuDevice = gpuInventory?.devices.find(device => device.uuid === gpuInventory.preferredGpuDeviceId);
  const selectedEngine = project.solver.engineId ? engineCatalog.find(engine => engine.engineId === project.solver.engineId) : undefined;
  // Host capability (gpuExposed) answers "can this machine run CUDA flux at
  // all"; the run's engine comes from actualExecution and may still be CPU_DISK
  // (for example geometry or explicitly CPU steps in a mixed recipe).
  const runEngines = runEnginesOf(displayInfo);
  const runEngine = runEngineLabel(runEngines);
  const runEngineTitle = runEngines.length > 1
    ? `本次运行混合使用：${runEngines.join(' + ')}（RunInfo.actualExecution）`
    : runEngines.length === 1
      ? `本次运行实际使用 ${runEngines[0]}（RunInfo.actualExecution）`
      : '所选运行的 actualExecution 中没有实际引擎：可能尚未执行、只有几何操作或全部为零速率 no-op。';
  const selectedStepEngine = selectedExecution?.actualEngine && selectedExecution.actualEngine !== NOT_APPLICABLE_ENGINE ? selectedExecution.actualEngine : null;
  const loadedRunSelection = selection.kind === 'run' && displayInfo?.runId === selection.id;
  const artifactReject = artifactEstimate?.risk === 'reject';
  const artifactRejectNotice = artifactReject && artifactEstimate
    ? `运行产物预估约 ${formatEstimateMiB(artifactEstimate.estimatedMiB)}（上限 ${artifactEstimate.capMiB} MiB）已超限；工作进程启动前会返回 422，请按“运行产物预估”的建议调整。`
    : '';

  return <div className={`app-shell ${isResizingPanels ? 'columns-resizing' : ''}`}>
    <input ref={csvInputRef} data-testid="csv-asset-input" type="file" accept=".csv,text/csv" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void uploadCsvFile(file, csvUploadMode); }} />
    <TopBar>
      <header className="topbar topbar-shell">
        <div className="brand-block"><div className="brand-mark">V<span>PS</span></div><div className="brand-copy"><strong>ViennaPS 工艺工作台</strong><span>二维 / 三维本机仿真 · {lengthUnit} · s</span></div></div>
        <div className="topbar-project"><span className="muted-label">项目</span><input value={project.name} aria-label="项目名称" onChange={event => updateProject(previous => ({ ...previous, name: event.target.value }))} /><span className="save-state">{!projectId ? '未保存' : savedProjectJson === JSON.stringify(project) ? '已保存' : '有未保存修改'}</span></div>
        <nav className="toolbar" aria-label="项目操作">
          <Menu label="项目">
            <button className="button" onClick={() => newProject(project.dimension)} disabled={loading || active}>新建工程</button>
            <button className="button primary" onClick={saveProject} disabled={loading || health !== 'ready'}>保存项目</button>
            <select aria-label="已保存项目" value={savedSelection} onChange={event => setSavedSelection(event.target.value)}>
              <option value="">打开已保存项目…</option>{projectRows.map(row => <option key={row.id} value={row.id}>{row.name} · {new Date(row.updatedAt).toLocaleString()}</option>)}
            </select>
            <button className="button" onClick={openProject} disabled={!savedSelection}>打开</button>
            <button className="button" onClick={() => fileInputRef.current?.click()}>导入项目 / 状态包</button>
            <button className="button" onClick={exportProject}>导出项目</button>
            <input ref={fileInputRef} type="file" accept="application/json,.json,application/zip,.zip" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void importFile(file); }} />
          </Menu>
          <div className="toolbar-group toolbar-run">
            <button className="button" onClick={() => void startRun('preview')} disabled={loading || health !== 'ready' || active}>生成结构预览</button>
            {active
              ? <button className="button danger" onClick={() => void cancelRun()}>取消运行</button>
              : <button className="button primary run-button" onClick={() => void startRun('simulate')} disabled={loading || health !== 'ready' || !project.steps.some(step => step.enabled) || artifactReject} title={artifactRejectNotice || undefined}>运行工艺</button>}
            {project.schemaVersion >= 7 && selectedStepObject && usesFlux(selectedStepObject) && <button className="button" onClick={() => void startFlux(selectedStepObject.id)} disabled={loading || health !== 'ready' || active}>单次计算所选粒子过程通量</button>}
          </div>
        </nav>
        <div className="topbar-status"><div className={`server-pill ${health}`} aria-label={health === 'ready' ? '本机服务已连接' : health === 'checking' ? '正在连接' : '本机服务离线'} title={health === 'ready' ? '本机服务已连接' : health === 'checking' ? '正在连接' : '本机服务离线'}><i /></div><div className={`run-pill ${runInfo?.status ?? ''}`}><i />{humanStatus(runInfo?.status)}</div><button className="inspector-toggle" aria-label={inspectorOpen ? '关闭属性检查器' : '打开属性检查器'} title={inspectorOpen ? '关闭属性检查器' : '打开属性检查器'} onClick={() => setInspectorOpen(value => !value)}><Icon name="settings"/></button></div>
      </header>
    </TopBar>

    <div className="mobile-tabs">
      {([{ id: 'structure', label: '结构' }, { id: 'recipe', label: '配方' }, { id: 'materials', label: '材料' }, { id: 'results', label: '结果' }, { id: 'settings', label: '设置' }] as const).map(item => <button key={item.id} className={activity === item.id ? 'selected' : ''} onClick={() => changeActivity(item.id)}>{item.label}</button>)}
      <button className={`mobile-canvas-toggle ${mobilePanel === 'viewer' ? 'selected' : ''}`} onClick={() => setMobilePanel('viewer')}>画布</button>
    </div>

    <main className={`workspace ${inspectorOpen ? '' : 'inspector-hidden'}`} ref={workspaceRef}>
      <Rail activity={activity} onChange={changeActivity} gpuStatus={gpuExposed ? 'GPU_DISK · 可选择' : gpuStatus === 'unverified-outside-sandbox' ? '设备不可用' : gpuStatus} />
      <aside id="structure-panel" className={`side-panel structure-panel ${mobilePanel === 'structure' ? 'mobile-visible' : ''}`}>
        <ContextPanel>
        {activity === 'materials' && <MaterialsPanel><section id="materials-context" className="panel-section material-admin-panel" aria-label="材料目录与自定义材料">
          <div className="section-heading"><div><h2>材料目录</h2></div><span className="tag">{materials.length} 种</span></div>
          <div className="panel-card material-registration-card">
            <form className="field-grid one-column material-registration-form" onSubmit={event => void createCustomMaterial(event)}>
              <label className="field"><span className="field-label">新材料名称</span><input aria-label="新材料名称" type="text" required maxLength={64} value={materialDraft.name} onChange={event => setMaterialDraft(previous => ({ ...previous, name: event.target.value }))} /></label>
              <SelectField label="材料类型" value={materialDraft.category} options={MATERIAL_CATEGORY_OPTIONS.map(([value, label]) => ({ value, label }))} onChange={value => setMaterialDraft(previous => ({ ...previous, category: value }))} />
              <label className="field"><span className="field-label">显示颜色</span><input aria-label="新材料颜色" type="color" value={materialDraft.color} onChange={event => setMaterialDraft(previous => ({ ...previous, color: event.target.value }))} /></label>
              <label className="field"><span className="field-label">密度</span><input aria-label="新材料密度" type="number" min="0" max="100" step="0.01" placeholder="g/cm³，可留空" value={materialDraft.densityGcm3} onChange={event => setMaterialDraft(previous => ({ ...previous, densityGcm3: event.target.value }))} /></label>
              <label className="field"><span className="field-label">是否导电</span><span className="field-checkbox"><input aria-label="新材料是否导电" type="checkbox" checked={materialDraft.conductive} onChange={event => setMaterialDraft(previous => ({ ...previous, conductive: event.target.checked }))} /></span></label>
              <label className="field"><span className="field-label">备注</span><textarea aria-label="新材料备注" maxLength={500} value={materialDraft.description} onChange={event => setMaterialDraft(previous => ({ ...previous, description: event.target.value }))} /></label>
              <button className="button primary" type="submit" disabled={materialBusy || health !== 'ready'}>注册自定义材料</button>
            </form>
          </div>
          {[{ label: '预设材料', rows: materials.filter(item => !item.isCustom) }, { label: '自定义材料', rows: materials.filter(item => item.isCustom) }].map(group => <div className="material-catalog-group" key={group.label}>
            <div className="field-label material-catalog-heading">{group.label}</div>
            <div className="material-catalog-list">{group.rows.map(item => <div key={item.id} className={`material-catalog-item ${item.isCustom ? 'custom' : ''}`}>
              <button type="button" className={`material-catalog-row ${selection.kind === 'material' && selection.name === item.name ? 'selected' : ''}`} onClick={() => { setSelection({ kind: 'material', name: item.name }); setActivity('materials'); }}>
                <i className="material-swatch" style={{ background: item.color }} />
                <span className="material-catalog-name">{item.name}</span>
                <span className="material-catalog-meta">{materialCategoryLabel(item.category)} · ID {item.id}</span>
              </button>
              {item.isCustom && <button type="button" className="material-catalog-delete" aria-label={`删除自定义材料 ${item.name}`} title="删除自定义材料" disabled={deletingMaterial !== null || materialBusy} onClick={() => { setDeleteMaterialCandidate(item); setDeleteMaterialError(''); }}>×</button>}
            </div>)}</div>
          </div>)}
        </section></MaterialsPanel>}
        {activity === 'structure' && <StructurePanel><section id="structure-context" className="panel-section">
          <div className="section-heading structure-section-heading"><div><h2>结构与网格</h2></div>
            <div className="segmented dimension-switch" role="group" aria-label="工程维度">
              <button type="button" aria-label="切换到 2D" aria-pressed={project.dimension === 2} className={project.dimension === 2 ? 'active' : ''} disabled={loading || active || !!project.continuationState} onClick={() => switchProjectDimension(2)}>2D</button>
              <button type="button" aria-label="切换到 3D" aria-pressed={project.dimension === 3} className={project.dimension === 3 ? 'active' : ''} disabled={loading || active || !!project.continuationState} onClick={() => switchProjectDimension(3)}>3D</button>
            </div>
          </div>
          {project.continuationState && <div className="inline-note warning continuation-note">
            <div className="continuation-heading"><strong>新工艺起始状态 · {project.continuationState.sourceStepId ?? '导入状态'}</strong><button className="button" aria-label="移除起始 Domain 状态" onClick={discardContinuationState}>移除</button></div>
            <details><summary>兼容性要求</summary><p>新工艺从该原生 Domain 状态开始。网格、边界、材料映射及 ViennaPS/ViennaLS 版本需匹配；修改结构参数后将无法运行。</p></details>
          </div>}
          <div className="field-grid one-column">
            <div className="field-grid two-column">
              <SelectField label="长度单位" value={projectUnits} options={[{ value: 'um', label: 'µm' }, { value: 'nm', label: 'nm' }]} onChange={value => changeProjectUnits(value as LengthUnit)} />
            </div>
            <NumericField label="网格间距" unit={lengthUnit} value={project.domain.gridDelta} max={0.5 * unitsPerUm} step={String(0.001 * unitsPerUm)} onChange={value => updateProject(previous => ({ ...previous, domain: { ...previous.domain, gridDelta: value } }))} />
            <NumericField label="x 横向总宽度" unit={lengthUnit} value={project.domain.xExtent} min={project.dimension === 3 ? minThreeDExtent(project.domain.gridDelta) : unitsPerUm} max={40 * unitsPerUm} step={String(0.1 * unitsPerUm)} hint={project.dimension === 3 ? '至少 2 个网格间距' : undefined} onChange={value => updateProject(previous => ({ ...previous, domain: { ...previous.domain, xExtent: value } }))} />
            {project.dimension === 3 && <NumericField label="y 横向总宽度" unit={lengthUnit} value={project.domain.yExtent ?? 6} min={minThreeDExtent(project.domain.gridDelta)} max={40 * unitsPerUm} step={String(.2 * unitsPerUm)} hint="至少 2 个网格间距" onChange={value => updateProject(previous => ({ ...previous, domain: { ...previous.domain, yExtent: value } }))} />}
            <SelectField label="横向边界" value={project.dimension === 3 ? project.domain.boundaryXY ?? 'reflective' : project.domain.xBoundary ?? 'reflective'} options={[{ value: 'reflective', label: '反射' }, { value: 'periodic', label: '周期' }]} onChange={value => updateProject(previous => ({ ...previous, domain: previous.dimension === 3 ? { ...previous.domain, boundaryXY: value as 'reflective' | 'periodic' } : { ...previous.domain, xBoundary: value as Domain['xBoundary'] } }))} />
          </div>
          <div className="structure-select">
            <span className="field-label">初始结构</span>
            <div className="segmented">
              <button className={project.geometry.kind === 'stack' ? 'active' : ''} onClick={() => updateProject(previous => ({ ...previous, geometry: structuredClone(defaultProject.geometry) }))}>多层</button>
              <button className={project.geometry.kind === 'plane' ? 'active' : ''} onClick={() => updateProject(previous => ({ ...previous, geometry: { kind: 'plane', height: 0, substrateMaterial: 'Si' } }))}>平面</button>
              <button className={project.geometry.kind === 'trench' ? 'active' : ''} onClick={() => updateProject(previous => ({ ...previous, geometry: { kind: 'trench', width: 2, depth: 2.2, taperAngle: 0, maskHeight: 0, maskTaperAngle: 0, substrateMaterial: 'Si', maskMaterial: 'Mask' } }))}>沟槽</button>
              {project.dimension === 3 && <button className={project.geometry.kind === 'hole' ? 'active' : ''} onClick={() => updateProject(previous => ({ ...previous, geometry: { kind: 'hole', radius: 1, depth: 2, taperAngle: 0, maskHeight: 0, maskTaperAngle: 0, substrateMaterial: 'Si', maskMaterial: 'Mask' } }))}>圆孔</button>}
              <button className={project.geometry.kind === 'fin' ? 'active' : ''} onClick={() => updateProject(previous => ({ ...previous, geometry: { kind: 'fin', width: 0.8, height: 1.2, taperAngle: 0, maskHeight: 0, maskTaperAngle: 0, substrateMaterial: 'Si', maskMaterial: 'Mask', construction: 'boolean' } }))}>Fin</button>
              <button
                ref={gdsImportButtonRef}
                type="button"
                className={gdsImportOpen || project.geometry.kind === 'gds' ? 'active' : ''}
                aria-haspopup="dialog"
                aria-expanded={gdsImportOpen}
                aria-controls="gds-import-popover"
                onClick={() => { setGdsImportError(''); setGdsImportOpen(open => !open); }}
              >导入 GDS</button>
            </div>
            <input ref={gdsInputRef} type="file" accept=".gds,.gdsii,application/octet-stream" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void uploadGdsFile(file); }} />
            {gdsImportOpen && gdsImportPosition && createPortal(
              <div
                ref={gdsImportCardRef}
                id="gds-import-popover"
                role="dialog"
                aria-label="导入 GDS 版图"
                className={`gds-import-popover ${gdsImportPosition.side === 'left' ? 'opens-left' : ''}`}
                style={{ top: gdsImportPosition.top, left: gdsImportPosition.left }}
              >
                <div className="gds-import-card-header">
                  <div><strong>导入 GDS 版图</strong><span>选择单元和掩膜层，将其加入当前多层结构。</span></div>
                  <button type="button" className="icon-button" aria-label="关闭 GDS 导入卡片" onClick={() => setGdsImportOpen(false)}>×</button>
                </div>
                <button type="button" className="button gds-import-file-button" disabled={loading} onClick={() => gdsInputRef.current?.click()}>
                  {loading ? '正在读取文件…' : pendingGdsAsset ? '更换 GDS 文件' : '选择 GDS 文件'}
                </button>
                {pendingGdsAsset && <>
                  <div className="gds-import-file-summary">
                    <strong title={pendingGdsAsset.fileName}>{pendingGdsAsset.fileName}</strong>
                    <span>{pendingGdsAsset.structures.length} 个结构单元 · {pendingGdsAsset.fileSizeBytes.toLocaleString()} 字节</span>
                    <span>原生单位：1 user unit = {pendingGdsAsset.units.micrometersPerNativeUserUnit.toPrecision(5)} µm</span>
                  </div>
                  <div className="gds-import-mode" role="radiogroup" aria-label="GDS 导入用途">
                    <label className={gdsImportMode === 'etch-mask' ? 'selected' : ''}>
                      <input type="radio" name="gds-import-mode" value="etch-mask" checked={gdsImportMode === 'etch-mask'} onChange={() => {
                        setGdsImportMode('etch-mask');
                        if (pendingGdsAsset) {
                          const structure = pendingGdsAsset.structures.find(item => gdsMaskPairs(pendingGdsAsset, item.name).length > 0)?.name ?? pendingGdsStructure;
                          setPendingGdsStructure(structure);
                          const pair = gdsMaskPairs(pendingGdsAsset, structure)[0];
                          setPendingGdsLayerDatatype(pair ? `${pair[0]}/${pair[1]}` : '');
                        }
                      }} />
                      <span><strong>作为刻蚀掩膜</strong><small>把所选图形加入当前多层结构的顶层。</small></span>
                    </label>
                    <label className={gdsImportMode === 'material-geometry' ? 'selected' : ''}>
                      <input type="radio" name="gds-import-mode" value="material-geometry" checked={gdsImportMode === 'material-geometry'} onChange={() => {
                        setGdsImportMode('material-geometry');
                        if (pendingGdsAsset) {
                          const pair = gdsPairs(pendingGdsAsset, pendingGdsStructure)[0];
                          setPendingGdsLayerDatatype(pair ? `${pair[0]}/${pair[1]}` : '');
                        }
                      }} />
                      <span><strong>导入为材料几何（高级）</strong><small>每个 GDS 图层都要指定仿真材料。</small></span>
                    </label>
                  </div>
                  <label className="gds-import-field">
                    <span>1 · 选择版图单元（Cell）</span>
                    <select value={pendingGdsStructure} onChange={event => {
                      const structure = event.target.value;
                      const pair = (gdsImportMode === 'etch-mask' ? gdsMaskPairs(pendingGdsAsset, structure) : gdsPairs(pendingGdsAsset, structure))[0];
                      setPendingGdsStructure(structure);
                      setPendingGdsLayerDatatype(pair ? `${pair[0]}/${pair[1]}` : '');
                      setGdsImportError('');
                    }}>
                      {pendingGdsAsset.structures.filter(item => gdsLayerIds(pendingGdsAsset, item.name).length > 0).map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
                    </select>
                  </label>
                  <div className="gds-import-layer-summary">
                    {gdsLayerIds(pendingGdsAsset, pendingGdsStructure).length} 个图层 · {gdsImportMode === 'etch-mask' ? gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).length : gdsPairs(pendingGdsAsset, pendingGdsStructure).length} 种{gdsImportMode === 'etch-mask' ? '闭合多边形掩膜' : 'layer/datatype'}组合
                  </div>
                  {gdsImportMode === 'etch-mask' && !gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).length && <div className="gds-import-help">当前 Cell 没有闭合边界多边形可用作掩膜。可换 Cell 或切换到高级材料几何导入。</div>}
                  {gdsImportMode === 'etch-mask' && <label className="gds-import-field">
                    <span>2 · 作为掩膜的闭合 GDS 图层</span>
                    <select value={pendingGdsLayerDatatype} onChange={event => { setPendingGdsLayerDatatype(event.target.value); setGdsImportError(''); }}>
                      {gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).length === 0 && <option value="">无闭合掩膜图形</option>}
                      {gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).map(([layer, datatype]) => <option key={`${layer}/${datatype}`} value={`${layer}/${datatype}`}>Layer {layer} · datatype {datatype}</option>)}
                    </select>
                  </label>}
                  {gdsImportMode === 'etch-mask' && <>
                    <div className="gds-import-section-heading"><span>3</span><div><strong>掩膜图层</strong><small>这些属性会填入“多层”中的新顶层掩膜；基底与薄膜保持当前项目设置。</small></div></div>
                    <label className="gds-import-field"><span>掩膜名称</span><input type="text" maxLength={80} value={pendingMaskName} onChange={event => setPendingMaskName(event.target.value)} /></label>
                    <div className="gds-import-data-grid">
                      <label className="gds-import-field"><span>掩膜材料</span><select value={pendingMaskMaterial} onChange={event => setPendingMaskMaterial(event.target.value)}>{materials.map(item => <option key={item.id} value={item.name}>{item.name}</option>)}</select></label>
                      <label className="gds-import-field"><span>掩膜厚度 · µm</span><input type="number" min={2 * project.domain.gridDelta} max="100" step="0.05" value={pendingMaskThickness} onChange={event => setPendingMaskThickness(Number(event.target.value))} /></label>
                      <label className="gds-import-field"><span>额外尺寸比例（自动换算后 ×）</span><input type="number" min="0.000001" max="1000000" step="0.1" value={pendingGdsScale} onChange={event => setPendingGdsScale(Number(event.target.value))} /></label>
                      <label className="gds-import-field"><span>GDS X 偏移 · µm</span><input type="number" min="-40" max="40" step="0.05" value={pendingMaskOffset[0]} onChange={event => setPendingMaskOffset([Number(event.target.value), pendingMaskOffset[1]])} /></label>
                      <label className="gds-import-field"><span>GDS Y 偏移 · µm</span><input type="number" min="-40" max="40" step="0.05" value={pendingMaskOffset[1]} onChange={event => setPendingMaskOffset([Number(event.target.value), pendingMaskOffset[1]])} /></label>
                    </div>
                    <label className="gds-import-field"><span>保留版图区域</span><select value={pendingMaskPolarity} onChange={event => setPendingMaskPolarity(event.target.value as 'inside' | 'outside')}><option value="inside">GDS 图形内部</option><option value="outside">GDS 图形外部</option></select></label>
                    <label className="gds-import-protection"><input type="checkbox" checked={pendingProtectsEtch} onChange={event => setPendingProtectsEtch(event.target.checked)} /><span><strong>刻蚀时保护掩膜材料</strong><small>关闭后，掩膜材料会按工艺步骤的材料速率参与刻蚀。</small></span></label>
                    <div className="gds-import-help">沿用当前计算域和已有多层结构。若原工程只有裸 GDS，导入后会显示最小起始结构；请在“多层”中确认基底并添加所需薄膜，再到工艺步骤区设置刻蚀参数。</div>
                  </>}
                  <div className="gds-import-help">
                    {gdsImportMode === 'etch-mask'
                      ? '确认后只会把所选 GDS 图形作为一个顶层掩膜加入“多层”；已有基底、薄膜和工艺步骤会保留。导入前会运行 ViennaPS 原生图层检查。'
                      : 'GDS 图层编号本身不代表材料；导入后需要把图层映射为硅、氧化物等仿真材料。'}
                  </div>
                </>}
                {gdsImportError && <div className="gds-import-error" role="alert">{gdsImportError}</div>}
                <div className="gds-import-card-actions">
                  <button type="button" className="button" onClick={() => setGdsImportOpen(false)}>稍后</button>
                  <button
                    type="button"
                    className="button primary"
                    disabled={loading || !pendingGdsAsset || !gdsLayerIds(pendingGdsAsset, pendingGdsStructure).length || (gdsImportMode === 'etch-mask' && !gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).length)}
                    onClick={() => void importPendingGds()}
                  >{loading ? '正在检查…' : '导入并准备预览'}</button>
                </div>
              </div>,
              document.body,
            )}
          </div>
          {project.geometry.kind === 'stack' ? <div className="field-grid one-column geometry-fields">
            <SelectField label="基底材料" value={project.geometry.substrateMaterial} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => updateProject(previous => previous.geometry.kind === 'stack' ? { ...previous, geometry: { ...previous.geometry, substrateMaterial: value } } : previous)} />
            <NumericField label="基底表面高度" unit={lengthUnit} value={project.geometry.baseHeight} onChange={value => updateProject(previous => previous.geometry.kind === 'stack' ? { ...previous, geometry: { ...previous.geometry, baseHeight: value } } : previous)} />
            {project.geometry.layers.map((layer, index) => layer.source === 'gds-mask' && project.geometry.kind === 'stack' && project.geometry.gdsMask ? <div className="layer-card gds-mask-card" key={layer.id}>
              <div className="section-heading gds-mask-section-heading"><div className="gds-mask-heading-copy"><span className="field-label">第 {index + 1} 层 · {layer.name}</span><span className="gds-mask-heading-hint">GDS 掩膜 · 顶层图案直接使用此版图图形</span></div><button type="button" className="button danger-quiet" onClick={() => removeLayer(layer.id)}>移除</button></div>
              <label className="field"><span className="field-label">掩膜名称</span><input type="text" maxLength={80} value={layer.name} onChange={event => editLayer(layer.id, { name: event.target.value })} /></label>
              <SelectField label="掩膜材料" value={layer.material} options={materials.map(item => ({ value: item.name, label: item.name }))} onChange={value => editLayer(layer.id, { material: value })} />
              <NumericField label="掩膜厚度" unit={lengthUnit} value={layer.thickness} min={2 * project.domain.gridDelta} max={100} step=".05" onChange={value => editLayer(layer.id, { thickness: value })} />
              {gdsAsset && <>
                <SelectField label="版图结构" value={project.geometry.gdsMask.structure} options={gdsAsset.structures.filter(item => gdsMaskPairs(gdsAsset, item.name).length > 0).map(item => ({ value: item.name, label: item.name }))} onChange={value => {
                  const pair = gdsMaskPairs(gdsAsset, value)[0]; if (!pair) return;
                  editStackGdsMask(mask => ({ ...mask, structure: value, layer: pair[0], datatype: pair[1] }));
                }} />
                <SelectField label="GDS 掩膜图层 / datatype" value={`${project.geometry.gdsMask.layer}/${project.geometry.gdsMask.datatype}`} options={gdsMaskPairs(gdsAsset, project.geometry.gdsMask.structure).map(([gdsLayer, datatype]) => ({ value: `${gdsLayer}/${datatype}`, label: `Layer ${gdsLayer} · datatype ${datatype}` }))} onChange={value => { const [gdsLayer, datatype] = value.split('/').map(Number); editStackGdsMask(mask => ({ ...mask, layer: gdsLayer, datatype })); }} />
                <NumericField label="版图尺寸比例" value={project.geometry.gdsMask.unitScale} min={.000001} max={1_000_000} step="0.1" onChange={value => editStackGdsMask(mask => ({ ...mask, unitScale: value }))} />
                <SelectField label="保留图形区域" value={project.geometry.gdsMask.retainedRegion} options={[{ value: 'inside', label: 'GDS 图形内部' }, { value: 'outside', label: 'GDS 图形外部' }]} onChange={value => editStackGdsMask(mask => ({ ...mask, retainedRegion: value as 'inside' | 'outside' }))} />
                <NumericField label="GDS X 偏移" unit={lengthUnit} value={project.geometry.gdsMask.position[0]} min={-40} max={40} step="0.05" onChange={value => editStackGdsMask(mask => { const position: [number, number, number] = [...mask.position]; position[0] = value; return { ...mask, position }; })} />
                <NumericField label="GDS Y 偏移" unit={lengthUnit} value={project.geometry.gdsMask.position[1]} min={-40} max={40} step="0.05" onChange={value => editStackGdsMask(mask => { const position: [number, number, number] = [...mask.position]; position[1] = value; return { ...mask, position }; })} />
                <label className="visibility-option"><input type="checkbox" checked={project.geometry.gdsMask.protectsEtch} onChange={event => editStackGdsMask(mask => ({ ...mask, protectsEtch: event.target.checked }))} /><span><strong>保护掩膜材料</strong><small>关闭后，掩膜材料按工艺步骤中的材料速率参与刻蚀。</small></span></label>
                <div className="inline-note">来源：{project.geometry.gdsMask.fileName} · {project.geometry.gdsMask.layer}/{project.geometry.gdsMask.datatype}。掩膜底面自动贴合其下方多层的顶部。</div>
              </>}
            </div> : <div className="layer-card" key={layer.id}>
              <label className="field"><span className="field-label">第 {index + 1} 层名称</span><input type="text" value={layer.name} onChange={event => editLayer(layer.id, { name: event.target.value })} /></label>
              <SelectField label="材料" value={layer.material} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => editLayer(layer.id, { material: value })} />
              <NumericField label="厚度" unit={lengthUnit} value={layer.thickness} min={.01} max={100} step=".05" onChange={value => editLayer(layer.id, { thickness: value })} />
              <SelectField label="角色" value={layer.role} options={[{ value: 'film', label: '薄膜' }, { value: 'mask', label: '掩膜' }]} onChange={value => editLayer(layer.id, { role: value as Layer['role'] })} />
              <div className="layer-actions"><button className="button" disabled={index === 0} onClick={() => moveLayer(layer.id, -1)}>↑</button><button className="button" disabled={index === layerCount - 1} onClick={() => moveLayer(layer.id, 1)}>↓</button><button className="button" onClick={() => copyLayer(layer.id)}>复制</button><button className="button danger-quiet" onClick={() => removeLayer(layer.id)}>删除</button></div>
            </div>)}
            <button className="button" disabled={project.geometry.layers.length >= 16} onClick={addLayer}>＋ 添加层</button>
            <SelectField label="顶层图案" value={project.geometry.pattern.kind} options={project.geometry.gdsMask ? [{ value: 'gds', label: 'GDS 掩膜图案' }] : [{ value: 'none', label: '无' }, { value: 'trench', label: '沟槽' }, ...(project.dimension === 3 ? [{ value: 'hole', label: '圆孔' }] : [])]} hint={project.geometry.gdsMask ? '由上方 GDS 掩膜层提供图案。' : undefined} onChange={value => updateProject(previous => { if (previous.geometry.kind !== 'stack' || previous.geometry.gdsMask) return previous; const pattern: StackGeometry['pattern'] = value === 'trench' ? { kind: 'trench', width: 2, taperAngle: 0 } : value === 'hole' && previous.dimension === 3 ? { kind: 'hole', radius: 1, taperAngle: 0 } : { kind: 'none' }; return { ...previous, geometry: { ...previous.geometry, pattern } }; })} />
            {project.geometry.pattern.kind === 'trench' && <NumericField label="沟槽宽度" unit={lengthUnit} value={project.geometry.pattern.width} min={.05} onChange={value => updatePattern({ width: value })} />}
            {project.geometry.pattern.kind === 'hole' && <NumericField label="孔半径" unit={lengthUnit} value={project.geometry.pattern.radius} min={.05} onChange={value => updatePattern({ radius: value })} />}
            {(project.geometry.pattern.kind === 'trench' || project.geometry.pattern.kind === 'hole') && <NumericField label="图案锥角" unit="°" value={project.geometry.pattern.taperAngle} min={-45} max={45} onChange={value => updatePattern({ taperAngle: value })} />}
            {project.geometry.gdsMask && <div className="inline-note">当前为 GDS 图形转移模式。运行前可先用“生成结构预览”检查掩膜、膜层和基底的相对位置。</div>}
          </div> : project.geometry.kind === 'gds' ? <div className="field-grid one-column geometry-fields gds-fields">
            <div className="gds-file-summary"><div><strong>{project.geometry.fileName}</strong><span>{gdsAsset ? `${gdsAsset.structures.length} 个结构单元 · ${gdsAvailableLayers.length} 个图层` : '正在读取布局…'}</span></div>
              <details><summary>文件与坐标信息</summary><div className="gds-file-details"><span>SHA-256：{project.geometry.assetId}</span>
                {gdsAsset && <span>文件单位：{gdsAsset.units.metersPerDatabaseUnit} m/数据库单位；{gdsAsset.units.userUnitsPerDatabaseUnit} 数据库单位/用户单位；1 原生单位 = {gdsAsset.units.micrometersPerNativeUserUnit} µm。</span>}
                <span>ViennaPS 将 XY 映射为 {project.dimension === 2 ? 'x/h' : 'x/y'}；3D 图层高度沿 z。</span>
              </div></details>
            </div>
            <button type="button" className="button primary" disabled={!gdsAsset || loading} onClick={startGdsMaskTransfer}>从此版图建立刻蚀流程…</button>
            <SelectField label="GDS 结构单元" value={project.geometry.structure} options={(gdsAsset?.structures ?? []).map(item => ({ value: item.name, label: item.name }))} onChange={value => {
              if (!gdsAsset) return;
              const ids = gdsLayerIds(gdsAsset, value); const material = materials.find(item => item.name === 'SiO2')?.name ?? materials[0]?.name ?? 'Si';
              editGdsGeometry(current => ({ ...current, structure: value, layers: ids.slice(0, 64).map(layer => ({ layer, material, ...(project.dimension === 3 ? { baseHeight: 0, height: Math.max(0.5, 2 * project.domain.gridDelta), mask: false } : {}) })) }));
              setGdsInspection(null);
            }} />
            <NumericField label="声明单位到 µm 的手动比例" value={project.geometry.unitScale} min={0.000001} max={1000000} step="0.1" hint="自动换算后再乘此比例，默认 1。" onChange={value => editGdsGeometry(current => ({ ...current, unitScale: value }))} />
            {(project.dimension === 2 ? [{ index: 0, label: 'GDS x 位置', unit: 'µm' }, { index: 1, label: 'GDS h 位置', unit: 'µm' }] : [{ index: 0, label: 'GDS x 位置', unit: 'µm' }, { index: 1, label: 'GDS y 位置', unit: 'µm' }, { index: 2, label: '层高附加偏移 z', unit: 'µm' }]).map(axis => <NumericField key={axis.label} label={axis.label} unit={axis.unit} value={selectedGds?.position[axis.index] ?? 0} min={-40} max={40} step="0.05" onChange={value => editGdsGeometry(current => { const position: [number, number, number] = [...current.position]; position[axis.index] = value; if (project.dimension === 2) position[2] = 0; return { ...current, position }; })} />)}
            <div className="gds-layer-list"><span className="field-label">结构中可用图层与材料映射</span>
              {gdsAvailableLayers.map(layerId => {
                const mapping = selectedGds?.layers.find(item => item.layer === layerId);
                return <div className="layer-card" key={layerId}>
                  <label className="visibility-option"><input type="checkbox" aria-label={`包含 GDS 图层 ${layerId}`} checked={!!mapping} onChange={event => editGdsGeometry(current => {
                    if (event.target.checked) {
                      if (current.layers.length >= 64 || current.layers.some(item => item.layer === layerId)) return current;
                      const material = materials.find(item => item.name === 'SiO2')?.name ?? materials[0]?.name ?? 'Si';
                      return { ...current, layers: [...current.layers, { layer: layerId, material, ...(project.dimension === 3 ? { baseHeight: 0, height: Math.max(0.5, 2 * project.domain.gridDelta), mask: false } : {}) }].sort((a,b) => a.layer-b.layer) };
                    }
                    return { ...current, layers: current.layers.filter(item => item.layer !== layerId) };
                  })} /><span><strong>GDS 图层 {layerId}</strong></span></label>
                  {mapping && <>
                    <SelectField label={`图层 ${layerId} 的材料`} value={mapping.material} options={materials.map(item => ({ value: item.name, label: item.name }))} onChange={value => editGdsGeometry(current => ({ ...current, layers: current.layers.map(item => item.layer === layerId ? { ...item, material: value } : item) }))} />
                    {project.dimension === 3 && <>
                      <NumericField label={`图层 ${layerId} 底面高度`} unit={lengthUnit} value={mapping.baseHeight ?? 0} step="0.05" onChange={value => editGdsGeometry(current => ({ ...current, layers: current.layers.map(item => item.layer === layerId ? { ...item, baseHeight: value } : item) }))} />
                      <NumericField label={`图层 ${layerId} 厚度`} unit={lengthUnit} value={mapping.height ?? Math.max(0.5, 2 * project.domain.gridDelta)} min={2 * project.domain.gridDelta} step="0.05" onChange={value => editGdsGeometry(current => ({ ...current, layers: current.layers.map(item => item.layer === layerId ? { ...item, height: value } : item) }))} />
                      <label className="visibility-option"><input type="checkbox" aria-label={`GDS 图层 ${layerId} 是掩膜`} checked={mapping.mask ?? false} onChange={event => editGdsGeometry(current => ({ ...current, layers: current.layers.map(item => item.layer === layerId ? { ...item, mask: event.target.checked } : item) }))} /><span><strong>作为掩膜材料</strong><small>图层厚度由上方高度设置。</small></span></label>
                    </>}
                  </>}
                </div>;
              })}
              {gdsAvailableLayers.length > 64 && <div className="inline-note warning">schema 7 最多允许映射 64 个 GDS 图层；请勾选要保留的层。</div>}
            </div>
            <button className="button gds-check-button" aria-label="检查 ViennaPS 原生 GDS 读入" disabled={loading || !project.geometry.layers.length || !gdsAsset} onClick={() => void inspectGds()}>检查原生 GDS 布局</button>
            {gdsInspection && <div className="inline-note gds-inspection-result"><strong>GDSGeometry 布局读取边界</strong>
              {gdsInspection.supportPurpose === 'align-native-periodic-gds-bounds-with-project-domain' && <span>周期边界使用未映射的隐藏 GDS 支持图层 {String(gdsInspection.supportLayer)} 对齐 ViennaPS 周期范围。</span>}
              <details className="gds-inspection"><summary>诊断详情</summary><div className="gds-inspection-content"><span>边界 {JSON.stringify(gdsInspection.nativeBounds)}；ViennaPS 读取 {String(gdsInspection.nativeStructureCount)} 个单元。</span><span>已选原生图层 {JSON.stringify(gdsInspection.selectedNativeLayers)} · 隐藏支持图层 {JSON.stringify(gdsInspection.supportLayers)} · Level-set 点数 {JSON.stringify(gdsInspection.levelSetPointCounts)}</span>{gdsInspection.supportPurpose === 'align-native-periodic-gds-bounds-with-project-domain' && <span>支持图层不会进入材料 Level-set 或工艺结果。</span>}<span>未选结构仅用于初始化布局边界，不会插入仿真 Domain。结构预览显示所选图层的实际网格。</span></div></details>
            </div>}
          </div> : project.geometry.kind === 'fin' ? <div className="field-grid one-column geometry-fields">
            <SelectField label="Fin 材料" value={project.geometry.substrateMaterial} options={materials.map(item => ({ value: item.name, label: item.name }))} onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, substrateMaterial: value } } : previous)} />
            <SelectField label="Fin 构造方式" value={project.geometry.construction ?? 'native'} options={[{ value: 'boolean', label: 'ViennaPS 布尔几何（推荐）' }, { value: 'native', label: '原生 MakeFin（旧项目兼容）' }]} onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, construction: value as 'native' | 'boolean' } } : previous)} />
            <NumericField label="Fin 宽度" unit={lengthUnit} value={project.geometry.width} min={2 * project.domain.gridDelta} max={40} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, width: value } } : previous)} />
            <NumericField label="Fin 高度" unit={lengthUnit} value={project.geometry.height} min={2 * project.domain.gridDelta} max={100} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, height: value } } : previous)} />
            <NumericField label="Fin 锥角" unit="°" value={project.geometry.taperAngle} min={-45} max={45} step="1" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, taperAngle: value } } : previous)} />
            <SelectField label="掩膜材料" value={project.geometry.maskMaterial} options={materials.map(item => ({ value: item.name, label: item.name }))} onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, maskMaterial: value } } : previous)} />
            <NumericField label="掩膜厚度" unit={lengthUnit} value={project.geometry.maskHeight} min={0} max={20} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, maskHeight: value } } : previous)} />
            {project.geometry.maskHeight > 0 && <NumericField label="掩膜锥角" unit="°" value={project.geometry.maskTaperAngle} min={-45} max={45} step="1" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, maskTaperAngle: value } } : previous)} />}
            {project.dimension === 3 && <>
              <label className="visibility-option"><input type="checkbox" aria-label="使用 ViennaPS Fin 挤出" checked={!!project.geometry.extrusion} onChange={event => updateProject(previous => previous.geometry.kind === 'fin' ? { ...previous, geometry: { ...previous.geometry, extrusion: event.target.checked ? { extent: [-1, 1], axis: 1 } : undefined } } : previous)} /><span><strong>用原生 Extrude 沿 y 方向挤出 2D Fin</strong><small>关闭时直接调用 d3.MakeFin；打开时调用 d2.MakeFin + ViennaPS Extrude。</small></span></label>
              {project.geometry.extrusion && <>
                <NumericField label="挤出 y 最小位置" unit={lengthUnit} value={project.geometry.extrusion.extent[0]} min={-(project.domain.yExtent ?? 6)/2} max={(project.domain.yExtent ?? 6)/2} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' && previous.geometry.extrusion ? { ...previous, geometry: { ...previous.geometry, extrusion: { ...previous.geometry.extrusion, extent: [value, previous.geometry.extrusion.extent[1]] } } } : previous)} />
                <NumericField label="挤出 y 最大位置" unit={lengthUnit} value={project.geometry.extrusion.extent[1]} min={-(project.domain.yExtent ?? 6)/2} max={(project.domain.yExtent ?? 6)/2} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'fin' && previous.geometry.extrusion ? { ...previous, geometry: { ...previous.geometry, extrusion: { ...previous.geometry.extrusion, extent: [previous.geometry.extrusion.extent[0], value] } } } : previous)} />
              </>}
            </>}
            {finFallbackModels.length > 0 || masked3DFinFallbackModels.length > 0 || finExtrusionFallbackModels.length > 0
              ? <div className="inline-note warning"><strong>已为已安装引擎启用安全构造回退</strong>
                  {finFallbackModels.length > 0 && <span>{finFallbackModels.join('、')} 与 ViennaPS 4.6.2 原生 MakeFin 组合会导致 ViennaPS 进程崩溃。运行时将使用经 GPU_DISK 与 CPU_DISK 独立参考验证的 ViennaPS 布尔 Fin；已保存工程仍保留原始构造选择。</span>}
                  {masked3DFinFallbackModels.length > 0 && <span>三维带遮罩 Fin 的原生构造与通用粒子过程会产生不一致的最终网格。运行时改用经产品与独立 ViennaPS GPU_DISK 参考对照的布尔构造；保存的项目仍保留原构造选择，初始快照记录实际路径。</span>}
                  {finExtrusionFallbackModels.length > 0 && <span>周期边界下，{finExtrusionFallbackModels.join('、')} 的 3D 挤出 Fin 改用 ViennaPS 3D 布尔盒体构造，保留所设 y 范围。该输入已与独立 ViennaPS 参考核对，初始快照记录实际构造路径。</span>}
                </div>
              : <div className="inline-note">布尔构造使用 ViennaPS 平面、盒体、BooleanOperation 和材料 level set 生成 Fin 及顶部遮罩；原生 MakeFin 保留既有项目结果。3D 平面切片可在 ViennaPS Slice 中另行计算。</div>}
          </div> : project.geometry.kind !== 'plane' ? <div className="field-grid one-column geometry-fields">
            <SelectField label="基底材料" value={project.geometry.substrateMaterial} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => updateProject(previous => previous.geometry.kind !== 'stack' ? { ...previous, geometry: { ...previous.geometry, substrateMaterial: value } } : previous)} />
            <SelectField label="掩膜材料" value={project.geometry.maskMaterial ?? 'Mask'} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => updateProject(previous => previous.geometry.kind === 'trench' || previous.geometry.kind === 'hole' ? { ...previous, geometry: { ...previous.geometry, maskMaterial: value } } : previous)} />
            {project.geometry.kind === 'trench' && <NumericField label="沟槽宽度" unit={lengthUnit} value={project.geometry.width} min={0.05} max={40} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'trench' ? { ...previous, geometry: { ...previous.geometry, width: value } } : previous)} />}
            {project.geometry.kind === 'hole' && <NumericField label="孔半径" unit={lengthUnit} value={project.geometry.radius} min={.05} step=".05" onChange={value => updateProject(previous => previous.geometry.kind === 'hole' ? { ...previous, geometry: { ...previous.geometry, radius: value } } : previous)} />}

            <NumericField label="深度" unit={lengthUnit} value={project.geometry.depth} min={0.05} max={100} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind !== 'plane' ? { ...previous, geometry: { ...previous.geometry, depth: value } } : previous)} />
            <NumericField label="锥角" unit="°" value={project.geometry.taperAngle} min={-45} max={45} step="1" hint="正值表示底部收窄" onChange={value => updateProject(previous => previous.geometry.kind !== 'plane' ? { ...previous, geometry: { ...previous.geometry, taperAngle: value } } : previous)} />
            <NumericField label="掩膜厚度" unit={lengthUnit} value={project.geometry.maskHeight} min={0} max={20} step="0.05" hint="0 表示不建立掩膜" onChange={value => updateProject(previous => previous.geometry.kind !== 'plane' ? { ...previous, geometry: { ...previous.geometry, maskHeight: value } } : previous)} />
            {project.geometry.maskHeight > 0 && <NumericField label="掩膜锥角" unit="°" value={project.geometry.maskTaperAngle} min={-45} max={45} step="1" onChange={value => updateProject(previous => previous.geometry.kind !== 'plane' ? { ...previous, geometry: { ...previous.geometry, maskTaperAngle: value } } : previous)} />}
          </div> : <div className="field-grid one-column geometry-fields"><SelectField label="基底材料" value={project.geometry.substrateMaterial} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => updateProject(previous => previous.geometry.kind === 'plane' ? { ...previous, geometry: { ...previous.geometry, substrateMaterial: value } } : previous)} /><NumericField label="平面高度" unit={lengthUnit} value={project.geometry.height} step="0.05" onChange={value => updateProject(previous => previous.geometry.kind === 'plane' ? { ...previous, geometry: { ...previous.geometry, height: value } } : previous)} /></div>}
          {project.dimension === 3 && project.schemaVersion === 7 && (project.geometry.kind === 'fin' || project.geometry.kind === 'gds') && <div className="field-grid one-column geometry-fields">
            <NumericField label="真实切片位置 y" unit={lengthUnit} value={slicePosition} min={-(project.domain.yExtent ?? 6)/2} max={(project.domain.yExtent ?? 6)/2} step="0.05" onChange={setSlicePosition} />
            <button className="button" disabled={loading || health !== 'ready' || active} onClick={() => void startNativeSlice()}>运行 ViennaPS Slice 并生成 2D 网格</button>
            <details className="compact-help"><summary>切片说明</summary><p>从 3D Domain 生成真实几何切片，并保存为独立 2D 快照。查看器裁剪不会生成几何。</p></details>
          </div>}
        </section></StructurePanel>}

        {activity === 'recipe' && <RecipePanel><section id="recipe-context" className="panel-section process-section">
          <div className="section-heading"><div><h2>工艺步骤</h2></div><span className="step-count">{project.steps.length} 步</span></div>
          <div className="panel-card recipe-add-card">
            <div className="recipe-add-heading">添加工艺步骤</div>
            <div className="add-step-groups">
              <div className="add-step-group"><strong>工艺过程</strong><div className="add-step-row">
                <button className="button add-step" onClick={() => addStep('isotropic')}>＋ 添加各向同性步骤</button>
                <button className="button add-step" onClick={() => addStep('singleParticle')}>＋ 添加单粒子步骤</button>
                <button className="button add-step" onClick={() => addStep('multiParticle')}>＋ 添加多粒子步骤</button>
                <button className="button add-step" onClick={() => addStep('directional')}>＋ 添加定向步骤</button>
              </div></div>
              <div className="add-step-group"><strong>ViennaPS 专用模型</strong><div className="specialized-add-row"><select aria-label="选择 ViennaPS 专用模型" value={availableSpecializedModels.some(spec => spec.id === newSpecializedModel) ? newSpecializedModel : availableSpecializedModels[0]?.id ?? ''} onChange={event => setNewSpecializedModel(event.target.value)}>{availableSpecializedModels.map(spec => <option key={spec.id} value={spec.id}>{spec.label}</option>)}</select><button className="button add-step" disabled={!specializedCatalog} onClick={() => addStep('specialized')}>＋ 添加专用模型步骤</button></div></div>
              <div className="add-step-group"><strong>CSV 速率表</strong><div className="specialized-add-row">
                <button className="button add-step" disabled={loading} onClick={() => { setCsvUploadMode('new'); csvInputRef.current?.click(); }}>＋ 上传 CSV 并添加步骤</button>
                <span className="inline-note">{csvAsset ? `已上传 ${csvAsset.fileName}（${csvAsset.dimension}D，${csvAsset.rowCount} 行，${csvAsset.linearCompatible ? 'LINEAR 可用' : '仅 IDW'}）` : `${project.dimension === 2 ? 'x,rate' : 'x,y,rate'} · ≤16 MiB`}</span>
              </div></div>
              <div className="add-step-group"><strong>几何操作</strong><div className="add-step-row">
                <button className="button add-step" onClick={() => addGeometryStep('removeMaterial')}>＋ 移除材料</button>
                <button className="button add-step" onClick={() => addGeometryStep('planarize')}>＋ 平坦化</button>
                <button className="button add-step" onClick={() => addGeometryStep('boolean')}>＋ 布尔几何</button>
                <button className="button add-step" disabled={project.dimension !== 3 || project.geometry.kind !== 'gds'}
                  title={project.dimension === 3 && project.geometry.kind === 'gds' ? '用项目 GDS 版图的某个图层多边形作为布尔操作数' : '仅三维 GDS 项目可用'}
                  onClick={() => addGeometryStep('gdsBoolean')}>＋ GDS 布尔</button>
              </div></div>
            </div>
          </div>
          <div className="step-list">
            {project.steps.map((step, index) => <div key={step.id} className={`step-card ${selectedStep === step.id ? 'selected' : ''} ${!step.enabled ? 'disabled-step' : ''}`}>
              <button className="step-main" onClick={() => { setSelectedStep(step.id); setSelection({ kind: 'step', id: step.id }); setActivity('recipe'); setInspectorOpen(true); setMobilePanel('settings'); }}>
                <span className="step-number">{String(index + 1).padStart(2, '0')}</span>
                <span className={`step-symbol ${isGeometryStep(step) ? 'geometry' : step.model === 'csvProcess' ? 'native' : step.operation}`}>{isGeometryStep(step) ? '◇' : step.model === 'csvProcess' ? '≈' : step.operation === 'etch' ? '−' : '+'}</span>
                <span className="step-title"><strong>{step.name}</strong><small>{stepKindLabel(step)}</small></span>
              </button>
              <label className="toggle" aria-label="启用步骤"><input type="checkbox" checked={step.enabled} onChange={event => editStep(step.id, value => ({ ...value, enabled: event.target.checked }))} /><span /></label>
            </div>)}
            {!project.steps.length && <div className="steps-empty"><span>＋</span><strong>还没有工艺步骤</strong><small>可以先预览结构，也可以添加第一步工艺。</small></div>}
          </div>
        </section></RecipePanel>}

        {activity === 'results' && <ResultsPanel><section id="results-context" className="panel-section history-section">
          <div className="section-heading"><div><h2>运行记录</h2></div><button className="icon-button" title="刷新记录" onClick={() => void refreshLists()}>↻</button></div>
          {runs.length ? <div className="history-list">{runs.map(row => <button key={row.runId} className={`history-item ${displayRunId === row.runId ? 'selected' : ''}`} onClick={() => void loadServerRun(row.runId, true)}>
            <span className={`history-status ${row.status}`} /><span className="history-copy"><strong>{row.kind === 'preview' ? '结构预览' : row.kind === 'flux' ? '单次通量' : row.kind === 'slice' ? 'ViennaPS 真实切片' : '工艺运行'}</strong><small>{new Date(row.createdAt ?? '').toLocaleString()} · {row.runId.slice(0, 8)}</small></span><span className="history-state">{humanStatus(row.status)}</span>
          </button>)}</div> : <div className="inline-note">运行记录会保存在本机工作台。</div>}
        </section></ResultsPanel>}
        {activity === 'settings' && <SettingsPanel><section id="settings-context" className="panel-section settings-context">
          <div className="section-heading"><div><h2>求解器与输出</h2></div></div>
          <div className="field-grid one-column solver-fields">
            {processSteps.length > 0 && <NumericField label="时间步比例" value={project.solver.timeStepRatio} min={0.05} max={0.5} step="0.01" hint="显式传入 AdvectionParameters" onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, timeStepRatio: value } }))} />}
            {particleSteps > 0 ? <>
              <div className="field">
                <span className="field-label">射线档位<small>决定统计噪声与单步耗时；同一种子仍可复现</small></span>
                <div className="segmented ray-presets" role="group" aria-label="射线档位">
                  {RAY_PRESETS.map(preset => <button key={preset.value} type="button" title={preset.detail}
                    aria-label={`射线档位 ${preset.label} ${preset.value}`}
                    aria-pressed={project.solver.raysPerPoint === preset.value}
                    className={project.solver.raysPerPoint === preset.value ? 'active' : ''}
                    onClick={() => updateProject(previous => ({ ...previous, solver: { ...previous.solver, raysPerPoint: preset.value } }))}>
                    {preset.label} · {preset.value}
                  </button>)}
                </div>
              </div>
              <NumericField label="每点射线数" value={project.solver.raysPerPoint} min={16} max={4096} step="16" hint="16–4096，可微调" onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, raysPerPoint: Math.trunc(value) } }))} />
              <SelectField label="随机策略" value={project.solver.random.mode} options={[{ value: 'fixed', label: '固定种子（可重放）' }, { value: 'random', label: '每次运行生成' }]} onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, random: value === 'fixed' ? { mode: 'fixed', seed: previous.solver.random.mode === 'fixed' ? previous.solver.random.seed : 314159 } : { mode: 'random' } } }))} />
              {project.solver.random.mode === 'fixed' && <NumericField label="随机种子" value={project.solver.random.seed} min={0} max={4294967295} step="1" onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, random: { mode: 'fixed', seed: Math.trunc(value) } } }))} />}
              <SelectField label="引擎偏好" value={project.solver.enginePreference} options={[{ value: 'auto', label: '自动 · GPU 优先' }, { value: 'cpu', label: 'CPU · CPU_DISK' }, { value: 'gpu', label: 'GPU · GPU_DISK', disabled: !gpuDeviceSelectable }]} onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, enginePreference: value as Solver['enginePreference'] } }))} />
              <SelectField label="引擎插件" value={project.solver.engineId ?? 'native'} options={[
                { value: 'native', label: 'ViennaPS · 原生策略' },
                ...engineCatalog.filter(engine => engine.engineId !== 'viennaps').map(engine => ({ value: engine.engineId, label: `${engine.engineId} · ${engine.version} · ${engine.kind}` })),
              ]} onChange={value => updateProject(previous => {
                const solver = { ...previous.solver };
                delete solver.engineId;
                delete solver.engineKernel;
                delete solver.engineBackend;
                if (value !== 'native') {
                  const engine = engineCatalog.find(item => item.engineId === value);
                  if (engine) {
                    solver.engineId = engine.engineId;
                    solver.engineKernel = engine.kernels[0] ?? 'CPU_DISK';
                    solver.engineBackend = engine.backends.includes('cpu') ? 'cpu' : 'gpu';
                  }
                }
                return { ...previous, solver };
              })} />
              {selectedEngine && selectedEngine.kernels.length > 0 && <SelectField label="引擎 Kernel" value={project.solver.engineKernel ?? selectedEngine.kernels[0]} options={selectedEngine.kernels.map(kernel => ({ value: kernel, label: kernel }))} onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, engineKernel: value } }))} />}
              {selectedEngine && <div className="inline-note">插件提供能力：{selectedEngine.capabilities.length ? selectedEngine.capabilities.join(' · ') : '未声明'}。缺少 process 所需能力时运行前会 fail closed。</div>}
              <div className="capability-note"><i />{gpuExposed
                ? nativeFailureNotice ?? `自动模式优先使用所选 CUDA GPU${defaultGpuDevice ? `（默认 ${defaultGpuDevice.name}）` : ''}；GPU 运行失败会保留原生错误。${gpuCoverageVerified ? ' 当前类别已有代表性运行记录。' : ''}`
                : '未发现可用的 CUDA 计算设备。'}</div>
            </> : <div className="not-applicable"><strong>射线 / 随机种子 / GPU 通量：不适用</strong><span>当前启用步骤没有非零速率的单粒子模型。</span></div>}
            {project.schemaVersion >= 7 && <>
              <SelectField label="计算 GPU" value={project.solver.gpuDeviceId ?? 'auto'} options={[
                { value: 'auto', label: gpuInventory?.devices.find(device => device.uuid === gpuInventory.preferredGpuDeviceId)?.name ? `自动 · 优先 ${gpuInventory.devices.find(device => device.uuid === gpuInventory.preferredGpuDeviceId)!.name}` : gpuInventory?.status === 'unavailable' ? '自动 · 未发现 CUDA GPU' : '自动 · 正在检测 CUDA GPU' },
                ...(gpuInventory?.devices ?? []).map(device => ({ value: device.uuid, label: `${device.name} · ${device.memoryTotalMiB ?? '未知'} MiB · ${device.pciBusId}` })),
              ]} onChange={value => updateProject(previous => { const solver = { ...previous.solver }; if (value === 'auto') delete solver.gpuDeviceId; else solver.gpuDeviceId = value; return { ...previous, solver }; })} />
              <div className="inline-note">该选择只作用于使用 ViennaPS CUDA 通量引擎的步骤；各向同性、定向及几何操作不使用 CUDA 通量引擎。三维查看器通过浏览器 WebGL 请求高性能适配器，具体渲染卡由浏览器和驱动选择。</div>
              {(gpuInventory?.devices.length ?? 0) === 0 && <div className="inline-note warning">本机没有检测到可用于 ViennaPS CUDA 计算的 NVIDIA GPU；显式 GPU 运行会失败，不会静默切换设备。</div>}
            </>}
            {project.schemaVersion < 7 && <div className="not-applicable"><strong>高级原生求解器参数</strong><span>旧项目格式仍可运行。升级后可编辑 ViennaLS 离散、自适应步进、射线跟踪和单次通量参数。</span><button className="button" onClick={() => setProject(promoteProjectToV7(project))}>升级到扩展项目格式 7</button></div>}
            {project.schemaVersion >= 7 && <details className="solver-advanced">
              <summary>ViennaLS 离散与自适应参数</summary>
              {processSteps.length > 0 ? <>
                <SelectField label="空间离散" value={project.solver.advection?.spatialScheme ?? 'ENGQUIST_OSHER_1ST_ORDER'} options={[
                  ['ENGQUIST_OSHER_1ST_ORDER','Engquist–Osher 1 阶'],['ENGQUIST_OSHER_2ND_ORDER','Engquist–Osher 2 阶'],
                  ['LAX_FRIEDRICHS_1ST_ORDER','Lax–Friedrichs 1 阶'],['LAX_FRIEDRICHS_2ND_ORDER','Lax–Friedrichs 2 阶'],
                  ['LOCAL_LAX_FRIEDRICHS_ANALYTICAL_1ST_ORDER','解析 Local Lax–Friedrichs 1 阶'],
                  ['LOCAL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER','Local Local Lax–Friedrichs 1 阶'],['LOCAL_LOCAL_LAX_FRIEDRICHS_2ND_ORDER','Local Local Lax–Friedrichs 2 阶'],
                  ['LOCAL_LAX_FRIEDRICHS_1ST_ORDER','Local Lax–Friedrichs 1 阶'],['LOCAL_LAX_FRIEDRICHS_2ND_ORDER','Local Lax–Friedrichs 2 阶'],
                  ['STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER','Stencil Local Lax–Friedrichs 1 阶'],['WENO_3RD_ORDER','WENO 3 阶'],['WENO_5TH_ORDER','WENO 5 阶'],
                ].map(([value,label]) => ({ value, label }))} onChange={value => editAdvection('spatialScheme', value)} />
                <SelectField label="时间离散" value={project.solver.advection?.temporalScheme ?? 'FORWARD_EULER'} options={[
                  { value: 'FORWARD_EULER', label: 'Forward Euler' },{ value: 'RUNGE_KUTTA_2ND_ORDER', label: 'Runge–Kutta 2 阶' },{ value: 'RUNGE_KUTTA_3RD_ORDER', label: 'Runge–Kutta 3 阶' },
                ]} onChange={value => editAdvection('temporalScheme', value)} />
                <label className="visibility-option"><input type="checkbox" checked={project.solver.advection?.adaptiveTimeStepping ?? false} onChange={event => editAdvection('adaptiveTimeStepping', event.target.checked)} /><span><strong>自适应时间步</strong><small>传入 AdvectionParameters.adaptiveTimeStepping</small></span></label>
                {project.solver.advection?.adaptiveTimeStepping && <NumericField label="自适应细分次数" value={project.solver.advection?.adaptiveTimeStepSubdivisions ?? 20} min={1} max={256} step="1" onChange={value => editAdvection('adaptiveTimeStepSubdivisions', Math.trunc(value))} />}
                <label className="visibility-option"><input type="checkbox" checked={project.solver.advection?.checkDissipation ?? true} onChange={event => editAdvection('checkDissipation', event.target.checked)} /><span><strong>检查数值耗散</strong><small>ViennaLS 原生 AdvectionParameters 开关</small></span></label>
                {project.solver.advection?.checkDissipation && <NumericField label="耗散系数 α" value={project.solver.advection?.dissipationAlpha ?? 1} min={0} max={10} step="0.05" onChange={value => editAdvection('dissipationAlpha', value)} />}
                <label className="visibility-option"><input type="checkbox" checked={project.solver.advection?.ignoreVoids ?? false} onChange={event => editAdvection('ignoreVoids', event.target.checked)} /><span><strong>忽略空洞</strong><small>传入 AdvectionParameters.ignoreVoids</small></span></label>
                <label className="visibility-option"><input type="checkbox" checked={project.solver.advection?.calculateIntermediateVelocities ?? false} onChange={event => editAdvection('calculateIntermediateVelocities', event.target.checked)} /><span><strong>计算中间速度</strong><small>原生求解器开关；不表示一定生成单独文件</small></span></label>
                <label className="visibility-option"><input type="checkbox" checked={project.solver.advection?.velocityOutput ?? false} onChange={event => editAdvection('velocityOutput', event.target.checked)} /><span><strong>启用速度输出</strong><small>ViennaLS 5.8.5 将原生速度与耗散数组写入 VTP；单位按原生数据保留</small></span></label>
              </> : <div className="not-applicable"><strong>不适用</strong><span>当前配方没有工艺过程步骤。</span></div>}
            </details>}
            {project.schemaVersion >= 7 && particleSteps > 0 && <details className="solver-advanced">
              <summary>单粒子射线跟踪参数</summary>
              <NumericField label="Disk 半径" value={project.solver.rayTracing?.diskRadius ?? 0} min={0} max={100} step="0.01" hint="ViennaPS 绑定没有声明此参数单位" onChange={value => editRayTracing('diskRadius', value)} />
              <NumericField label="最大边界命中次数" value={project.solver.rayTracing?.maxBoundaryHits ?? 1000} min={1} max={1000000} step="1" onChange={value => editRayTracing('maxBoundaryHits', Math.trunc(value))} />
              <NumericField label="最大反射次数" value={project.solver.rayTracing?.maxReflections ?? 4294967295} min={0} max={4294967295} step="1" onChange={value => editRayTracing('maxReflections', Math.trunc(value))} />
              <NumericField label="最小节点距离比例" value={project.solver.rayTracing?.minNodeDistanceFactor ?? 0.05} min={0} max={1} step="0.01" onChange={value => editRayTracing('minNodeDistanceFactor', value)} />
              <NumericField label="平滑邻居数" value={project.solver.rayTracing?.smoothingNeighbors ?? 1} min={0} max={1024} step="1" onChange={value => editRayTracing('smoothingNeighbors', Math.trunc(value))} />
              <SelectField label="通量归一化" value={project.solver.rayTracing?.normalizationType ?? 'SOURCE'} options={[{ value: 'SOURCE', label: 'SOURCE' },{ value: 'MAX', label: 'MAX' }]} onChange={value => editRayTracing('normalizationType', value as RayTracingSettings['normalizationType'])} />
              <label className="visibility-option"><input type="checkbox" checked={project.solver.rayTracing?.ignoreFluxBoundaries ?? false} onChange={event => editRayTracing('ignoreFluxBoundaries', event.target.checked)} /><span><strong>忽略通量边界</strong><small>传入 RayTracingParameters.ignoreFluxBoundaries</small></span></label>
            </details>}
            <NumericField label="CPU 线程数" value={project.solver.cpuThreads} min={capabilities?.resourcePolicy?.run?.cpuThreads?.minimum ?? 1} max={capabilities?.resourcePolicy?.run?.cpuThreads?.maximum ?? 1} step="1" disabled={!capabilities?.resourcePolicy?.run?.cpuThreads?.maximum} hint={capabilities?.resourcePolicy?.run?.cpuThreads ? `本机可用上限 ${capabilities.resourcePolicy.run.cpuThreads.maximum} 线程` : '正在读取本机 CPU 上限'} onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, cpuThreads: Math.trunc(value) } }))} />
            <NumericField label="运行超时" unit="s" value={project.solver.timeoutSeconds} min={0} max={600} step="1" hint="0 表示不限制运行时长；用户可自行观察是否卡住。" onChange={value => updateProject(previous => ({ ...previous, solver: { ...previous.solver, timeoutSeconds: Math.trunc(value) } }))} />
          </div>
        <section className="panel-section output-settings-panel" aria-label="原生输出设置">
          <div className="section-heading"><div><span className="eyebrow">输出文件</span><h2>结果文件</h2></div></div>
          {project.schemaVersion < 7
            ? <div className="not-applicable"><strong>扩展项目格式 7</strong><span>升级后可以选择额外的 ViennaPS Domain 输出。查看器所需的表面网格始终保存。</span><button className="button" onClick={() => setProject(promoteProjectToV7(project))}>升级并设置输出</button></div>
            : <div className="field-grid one-column">
              <label className="visibility-option"><input aria-label="输出原生 level-set" type="checkbox" checked={project.outputSettings?.levelSets ?? false} onChange={event => editOutputSetting('levelSets', event.target.checked)} /><span><strong>Level-set 数据</strong><small>保存 ViennaPS 每个 level set 的原生 .lvst 文件</small></span></label>
              <label className="visibility-option"><input aria-label="输出 level-set 网格" type="checkbox" checked={project.outputSettings?.levelSetMeshes ?? false} onChange={event => editOutputSetting('levelSetMeshes', event.target.checked)} /><span><strong>Level-set 网格</strong><small>按 level-set 分文件保存原生 VTP 网格</small></span></label>
              <label className="visibility-option"><input aria-label="输出原生表面 VTK" type="checkbox" checked={project.outputSettings?.surfaceMeshVtk ?? false} onChange={event => editOutputSetting('surfaceMeshVtk', event.target.checked)} /><span><strong>原生表面 VTK</strong><small>默认关闭；仅下载表面网格或交给 ParaView 等外部工具时启用，工作台预览使用快照数据</small></span></label>
              {project.dimension === 3 && <label className="visibility-option"><input aria-label="输出体网格" type="checkbox" checked={project.outputSettings?.volumeMesh ?? false} onChange={event => editOutputSetting('volumeMesh', event.target.checked)} /><span><strong>体网格 / 3D 体积预览</strong><small>使用 ViennaPS 原生 VTU 四面体；新建 3D 项目默认启用，会增加结果文件体积</small></span></label>}
              <label className="visibility-option"><input aria-label="输出 ViennaPS 元数据" type="checkbox" checked={project.outputSettings?.nativeMetadata ?? false} onChange={event => editOutputSetting('nativeMetadata', event.target.checked)} /><span><strong>Domain 元数据</strong><small>保留原生键和值；未声明的单位和点/单元关联保持未指定</small></span></label>
              <div className="inline-note">表面网格始终作为查看器结果保存；所选原生文件附带 SHA-256、尺寸和 level-set 材料映射，可逐个下载或打包导出。</div>
            </div>}
        </section>
        <ArtifactPreflightCard estimate={artifactEstimate} />
        </section></SettingsPanel>}
      </ContextPanel>
      </aside>

      <div className="column-resizer" role="separator" aria-orientation="vertical" aria-label="调整结构栏宽度"
        aria-controls="structure-panel" aria-valuemin={panelWidthLimits.structure.min} aria-valuemax={maxPanelWidth('structure', workspaceRef.current?.clientWidth ?? 1200)}
        aria-valuenow={Math.round(panelWidths.structure)} tabIndex={0} onKeyDown={event => adjustPanelWidth(event, 'structure')}
        onPointerDown={event => beginPanelResize(event, 'structure')} onPointerMove={movePanelResize} onPointerUp={endPanelResize}
        onPointerCancel={endPanelResize} onLostPointerCapture={endPanelResize}
        onDoubleClick={() => setPanelWidths(previous => fitPanelWidths({ ...previous, structure: 280 }, workspaceRef.current?.clientWidth ?? 1000))} />

      <section id="viewer-panel" className={`viewer-panel ${mobilePanel === 'viewer' ? 'mobile-visible' : ''}`}>
        <div className="viewer-header">
          <div className="viewer-title"><div><span className="eyebrow">网格查看</span><h1>结构结果</h1></div>
            {selectedSnapshot && <div className="snapshot-chip"><span>{selectedSnapshot.stepName}</span><small>{selectedSnapshot.sliceReadback ? 'ViennaPS 真实 3D 切片' : selectedSnapshot.stepId === 'initial' ? '初始结构' : `第 ${selectedSnapshot.stepIndex} 步`}</small></div>}
          </div>
          <div className="viewer-actions">
            <label className="compare-select"><span>对比</span><select value={compareRunId} onChange={event => void loadComparison(event.target.value)}><option value="">关闭对比</option>{runs.filter(row => row.status === 'succeeded' || row.status === 'failed').map(row => <option key={row.runId} value={row.runId}>{row.runId.slice(0, 8)} · {humanStatus(row.status)}</option>)}</select></label>
            {compareInfo && <select aria-label="对比步骤" value={compareSnapshot?.stepId ?? ''} onChange={event => void loadComparison(compareInfo.runId, event.target.value)}>{compareInfo.snapshots.map(row => <option key={row.stepId} value={row.stepId}>{row.name}</option>)}</select>}
            {snapshotRows.length > 0 && <select aria-label="当前结果步骤" value={selectedStepId ?? ''} onChange={event => { setAutoFollow(false); if (displayRunId) void fetchSnapshot(displayRunId, event.target.value); }}>{snapshotRows.map(row => <option key={row.stepId} value={row.stepId}>{row.name}</option>)}</select>}
            {displayDimension === 2 && <>
            <button className="icon-button" title="缩小" onClick={() => changeView({ zoom: Math.max(0.1, view2D.zoom / 1.2) })}>−</button>
            <button className="icon-button" title="放大" onClick={() => changeView({ zoom: Math.min(20, view2D.zoom * 1.2) })}>＋</button>
            <button className="icon-button" title="显示网格" onClick={() => changeView({ showGrid: !view2D.showGrid })}>{view2D.showGrid ? '▦' : '□'}</button>
            </>}
            {displayDimension === 2 && <button className="icon-button export-svg" title="导出 SVG" onClick={exportSvg}>⇩ SVG</button>}
          </div>
        </div>
        <div className="viewer-meta">
          <span className="meta-item"><i className="meta-dot" />{selectedSnapshot ? `${selectedSnapshot.mesh.pointCount.toLocaleString()} 点 · ${(selectedSnapshot.mesh.triangleCount ?? selectedSnapshot.mesh.lineCount ?? 0).toLocaleString()} ${displayDimension === 3 ? '三角面' : '界面段'}` : '二维网格界面'}</span>
          <span className="meta-item">坐标：{displayDimension === 3 ? 'x / y / z' : 'x / h'} · {lengthUnit}</span>
          <span className={`meta-item engine-chip ${engineTone(runEngines)}`} title={runEngineTitle}>本次运行引擎 {runEngine}</span>
          {selectedExecution && <span className="meta-item engine-chip step" title="所选步骤在 RunInfo.actualExecution 中记录的 actualEngine">本步 {selectedStepEngine ?? '不适用'}</span>}
          {selectedExecutionGpu?.uuid && <span className="meta-item" title={`CUDA Runtime 设备/同步回读：${selectedExecutionGpu.pciBusId ?? '未知'}；nvidia-smi 进程清单：${selectedExecutionGpu.process?.processListed ? '已列出 worker' : '未列出 worker'}`}>
            计算设备：{selectedExecutionGpu.name ?? selectedExecutionGpu.uuid}
            {selectedExecutionGpu.afterProcessApply?.readbackStatus === 'verified' ? ' · CUDA 已同步核验' : ''}
            {selectedExecutionGpu.process?.processListStatus === 'not-observed' ? ' · 系统未列出进程' : ''}
          </span>}
          {selectedSnapshot && <span className="meta-item">网格 Δ={selectedSnapshot.gridDelta} {lengthUnit}</span>}
          {measuredChange && <span className="meta-item measured-change-chip">
            {measuredChange.kind === 'removal' ? '实测去除' : '实测增加'} {measuredChange.record.material} {formatThickness(measuredChange.kind === 'removal' ? measuredChange.record.removedThickness : measuredChange.record.addedThickness)}
          </span>}
          {selectedSnapshot?.sliceReadback && <div className="inline-note">原生操作：{selectedSnapshot.sliceReadback.nativeOperation}，y 法向位置 {selectedSnapshot.sliceReadback.position} {lengthUnit}；输出是 2D 几何网格，未使用查看器裁剪。</div>}
          {selectedSnapshot?.geometryReadback && <details className="solver-advanced"><summary>ViennaPS 初始几何原生回读</summary><pre>{JSON.stringify(selectedSnapshot.geometryReadback, null, 2)}</pre></details>}
          {selectedSnapshot?.noOp && <span className="tag no-op-tag">零速率 no-op</span>}
          {isStale && <span className="tag stale-tag">对应其他输入配置</span>}
          {displayRunId && <span className="run-id">RUN {displayRunId.slice(0, 12)}</span>}
          {displayInfo?.runMode && <span className="tag">{displayInfo.runMode === 'from-domain-state' ? '起始于保存的 Domain 状态' : '从初始结构运行'}</span>}
        </div>
        {selectedSnapshot && compareSnapshot && !compatibleCompare && <div className="inline-note warning">二维 / 三维直接叠加比较已禁用，请选择相同维度的快照。</div>}
        {displayDimension === 3 && selectedSnapshot ? <Viewer3D current={selectedSnapshot} compare={compatibleCompare} view={view3D} onViewChange={changeView3D} onError={setNotice} catalog={materials} /> : <SvgViewer current={selectedSnapshot} compare={compatibleCompare} view={view2D} onViewChange={changeView} svgRef={svgRef} catalog={materials} />}
        <div className={`dock-shell ${dockOpen ? 'expanded' : 'collapsed'}`}>
          <div className="dock-header">
            <button type="button" className="dock-toggle" aria-expanded={dockOpen} onClick={() => setDockOpen(value => !value)}>{dockOpen ? '▾' : '▸'} 日志</button>
            <Dock tab={dockTab} onChange={tab => { setDockTab(tab); setDockOpen(true); }} counts={{ log: runInfo ? 1 : 0, flux: displayFlux?.fields.length ?? 0, intermediate: displayInfo?.intermediateOutputs?.length ?? 0, native: directionalActual || multiParticleActual || specializedActual || geometryActual || selectedV2 || hasMeasuredChange ? 1 : 0, artifacts: selectedSnapshot?.outputs?.length ?? 0 }} />
          </div>
          {dockOpen && <div className="dock-scroll">
        <div id="dock-flux" className="dock-block">
        {displayInfo?.kind === 'flux' && <section className="flux-result-panel" aria-label="ViennaPS 通量场结果">
          <div className="flux-result-heading"><div><span className="eyebrow">原生通量网格</span><h2>单次通量结果</h2></div>{displayFlux && <button className="button" onClick={exportFluxCsv} disabled={!fluxField}>导出所选字段 CSV</button>}</div>
          {!displayFlux ? <div className="inline-note">{displayInfo.status === 'succeeded' ? '正在读取 ViennaPS 通量网格…' : '通量计算运行后，ViennaPS 返回的网格字段会显示在此处。'}</div> : <>
            <div className="flux-result-meta"><span>来源：{displayFlux.source}</span><span>引擎：{displayFlux.execution.actualEngine}</span><span>种子：{displayFlux.execution.actualSeed}</span><span>ViennaPS {displayFlux.versions.viennaps} / ViennaLS {displayFlux.versions.viennals}</span></div>
            <div className="flux-result-meta"><span>网格 SHA-256：{displayFlux.meshIdentity}</span><span>{displayFlux.mesh.pointCount.toLocaleString()} 点</span><span>字段元组：{fluxField?.tuples.toLocaleString() ?? 0}</span><span>{fluxField?.association ?? '无字段'} · {fluxField?.nativeUnit ?? '原生接口未声明单位'}</span></div>
            {displayFlux.fields.length > 0 ? <>
              <label className="flux-field-select"><span>原生字段</span><select aria-label="通量字段" value={selectedFluxField} onChange={event => setSelectedFluxField(event.target.value)}>{displayFlux.fields.map(field => <option key={`${field.association}:${field.name}`} value={`${field.association}:${field.name}`}>{field.association} · {field.name} ({field.components} 分量，{field.tuples} 元组)</option>)}</select></label>
              <div className="inline-note">字段数组按 ViennaLS Mesh 原样保存在 {fluxField?.association} 容器。该通量返回网格有 {Object.values(displayFlux.mesh.cellCounts).reduce((sum, count) => sum + count, 0)} 个显式拓扑单元；表格只显示原生元组序号和值，不将数据映射到其他表面网格。{fluxField?.nativeUnit ? ` 原生单位：${fluxField.nativeUnit}。` : ' ViennaPS/ViennaLS 网格接口没有为此字段提供单位。'}</div>
              <div className="flux-table-wrap"><table className="flux-table"><thead><tr><th>tupleIndex</th><th>{fluxField?.name}</th></tr></thead><tbody>{(fluxValues ?? []).slice(0, 200).map((value, index) => <tr key={index}><td>{index}</td><td>{Array.isArray(value) ? value.map(item => Number(item).toPrecision(7)).join(', ') : Number(value).toPrecision(7)}</td></tr>)}</tbody></table>{(fluxValues?.length ?? 0) > 200 && <small>显示前 200 / {fluxValues?.length} 个元组；CSV 和运行 ZIP 包含完整原生数组。</small>}</div>
            </> : <div className="inline-note">ViennaPS 没有为此运行返回标量或向量字段数组。</div>}
          </>}
        </section>}
        </div>
        <div id="dock-intermediate" className="dock-block">
        {displayRunId && displayInfo?.kind === 'simulate' && (displayInfo.intermediateOutputs?.length ?? 0) > 0 &&
          <NativeIntermediatePanel runId={displayRunId} outputs={displayInfo.intermediateOutputs ?? []} />}
        </div>
        <div id="dock-artifacts" className="dock-block">
        <div className="result-footer">
          <div className="result-readout"><span>运行状态</span><strong className={`status-text ${runInfo?.status ?? ''}`}>{displayRunId === latestRunId ? humanStatus(runInfo?.status) : humanStatus(runs.find(row => row.runId === displayRunId)?.status)}</strong>
            {displayRunId === latestRunId && runInfo && <><span>已用时间</span><strong>{runInfo.elapsedSeconds.toFixed(1)} s</strong><span>步骤</span><strong>{runInfo.completedStepIds.length}/{runInfo.stepTotal ?? 0}</strong></>}</div>
          <div className="result-downloads"><button className="button" disabled={!displayRunId} onClick={() => void downloadRun('replay')}>导出重放配置</button><button className="button" disabled={!displayRunId || !terminalStates.has(displayInfo?.status ?? runs.find(row => row.runId === displayRunId)?.status ?? '')} onClick={() => void downloadRun('zip')}>下载运行结果 ZIP</button>{displayRunId && selectedSnapshot && <button className="button primary" disabled={loading || !terminalStates.has(displayInfo?.status ?? runs.find(row => row.runId === displayRunId)?.status ?? '') || !snapshotRows.find(row => row.stepId === selectedSnapshot.stepId)?.nativeState} title={snapshotRows.find(row => row.stepId === selectedSnapshot.stepId)?.nativeState ? '以该快照的 ViennaPS Domain 状态开始新工艺' : '该历史快照没有原生 Domain 状态'} onClick={() => void createContinuation(displayRunId, selectedSnapshot.stepId)}>以此状态开始新工艺</button>}</div>
        </div>
                  {!!selectedSnapshot?.outputs?.length && <details className="solver-advanced native-artifact-list" onToggle={event => { if (event.currentTarget.open && selectedSnapshot?.outputs?.[0]) setSelection({ kind: 'artifact', key: selectedSnapshot.outputs[0].fileName }); }}><summary>ViennaPS 原生输出 · {selectedSnapshot.outputs.length} 个文件</summary>
            {selectedSnapshot.outputs.map(artifact => <div className="native-artifact-row" key={`${artifact.fileName}:${artifact.sha256}`}>
              <span><strong>{({ surfaceMesh: '表面网格', levelSet: 'Level-set 数据', levelSetMesh: 'Level-set 网格', volumeMesh: '体网格', nativeMetadata: 'Domain 元数据' } as Record<string, string>)[artifact.kind] ?? artifact.kind}</strong>
                <small>{artifact.levelSetIndex !== undefined ? `Level-set ${artifact.levelSetIndex} · 材料 ID ${artifact.materialId} · ` : ''}{artifact.format} · {(artifact.sizeBytes / 1024).toFixed(1)} KiB</small></span>
              <a className="button" href={artifact.url} download={artifact.fileName.split('/').at(-1)} aria-label={`下载原生输出 ${artifact.fileName}`}>下载</a>
            </div>)}
          </details>}

        </div>
        <div id="dock-log" className="dock-block">
        <div className="log-panel">
          <div className="log-heading"><div><span className="eyebrow">运行输出</span><strong>运行日志</strong></div><span>{runInfo?.log.truncated ? '已达到 4 MiB 日志上限' : `游标 ${logCursor} B`}</span></div>
          <pre>{visibleLog || (displayRunId === latestRunId ? runInfo?.failureReason : displayInfo?.failureReason) || '提交运行后，ViennaPS 的标准输出和错误信息会显示在这里。'}</pre>
        </div>
        </div>
        <div id="dock-native" className="dock-block">
        {directionalActual && <div className="directional-history">
          <strong>DirectionalProcess 原生参数 · 历史记录</strong>
          <span>方向：{Array.isArray(directionalActual.requestedDirection) ? `[${directionalActual.requestedDirection.join(', ')}]` : '—'} → 单位向量 {Array.isArray(directionalActual.nativeUnitDirection) ? `[${directionalActual.nativeUnitDirection.join(', ')}]` : '—'}</span>
          <span>原生定向速度：{String(directionalActual.nativeDirectionalVelocity ?? '—')} {rateUnit} · 原生各向同性速度：{String(directionalActual.nativeIsotropicVelocity ?? '—')} {rateUnit}</span>
          <span>遮挡可见性：{String(directionalActual.calculateVisibility ?? '未计算')} · 掩膜材料：{Array.isArray(directionalActual.maskMaterials) ? directionalActual.maskMaterials.join(', ') || '无' : '—'}</span>
          <span>射线数 / 随机种子：不适用 · 运行引擎：{selectedExecution?.actualEngine ?? 'not-applicable'}</span>
          {selectedExecution?.noOp && <span>零速率步骤：未调用 ViennaPS Process，结果沿用前一步网格。</span>}
        </div>}
        {multiParticleActual && <div className="directional-history">
          <strong>MultiParticleProcess 原生配置与回调 · 历史记录</strong>
          <span>引擎：{selectedExecution?.actualEngine ?? '—'} · 种子：{String(selectedExecution?.actualSeed ?? '—')} · CPU 线程：{String(multiParticleActual.cpuThreads ?? '—')}</span>
          <pre>{JSON.stringify(multiParticleActual, null, 2)}</pre>
        </div>}
        {specializedActual && <div className="directional-history">
          <strong>{String((specializedActual.nativeModel as Record<string, unknown> | undefined)?.nativeClass ?? 'ViennaPS 专用模型')} 原生执行与历史参数</strong>
          <span>模型：{String((specializedActual.nativeModel as Record<string, unknown> | undefined)?.nativeModel ?? selectedExecution?.stepId)} · 引擎：{selectedExecution?.actualEngine ?? '—'} · 种子：{String(selectedExecution?.actualSeed ?? '不适用')} · CPU 线程：{String(specializedActual.cpuThreads ?? '—')}</span>
          <pre>{JSON.stringify(specializedActual, null, 2)}</pre>
        </div>}
        {geometryActual && <div className="directional-history">
          <strong>ViennaPS 几何操作 · 历史记录</strong>
          {geometryActual.geometryOperation === 'planarize' && geometryActual.stopMaterial != null
            ? <span>材质感知 CMP：停止材料 {String(geometryActual.stopMaterial)} · 顶面 {String(geometryActual.stopHeightMethod ?? 'area-weighted-p90')} {formatMicron(Number(geometryActual.measuredStopTopHeight))} − 过抛光 {formatMicron(Number(geometryActual.overPolish))} → 有效截断高度 {formatMicron(Number(geometryActual.cutoffHeight))}（中位面 {formatMicron(Number(geometryActual.stopMaterialMedianTopHeight))}，最高面 {formatMicron(Number(geometryActual.stopMaterialHighestTopHeight))}，上表面积 {Number(geometryActual.stopMaterialUpFacingArea ?? 0).toFixed(4)} µm²）。原生 <code>Planarize</code> 仍是全局高度切割，不模拟横向选择比。</span>
            : null}
          <pre>{JSON.stringify(geometryActual, null, 2)}</pre>
        </div>}
        {selectedV2 && <div className="directional-history">
          <strong>v2 执行记录 · {String(selectedV2.plan?.processId ?? selectedV2.stepId)}</strong>
          <span>process {String(selectedV2.plan?.processVersion ?? '—')} · variant {String(selectedV2.plan?.variantId ?? '—')} · engine {String(selectedV2.plan?.engineId ?? '—')} {String(selectedV2.plan?.engineKernel ?? '')}</span>
          {selectedV2.state && <span>fields {selectedV2.state.hasFields ? (selectedV2.state.fieldsPersisted ? '已持久化' : '仅内存/边界省略') : '无'} · materials {Array.isArray((selectedV2.state.materials as { order?: unknown[] } | null)?.order) ? ((selectedV2.state.materials as { order?: unknown[] }).order ?? []).length : '—'}</span>}
          <pre>{JSON.stringify({ plan: selectedV2.plan, outputs: selectedV2.outputs, state: selectedV2.state }, null, 2)}</pre>
        </div>}
        {measuredChange && <div className="directional-history measured-change">
          <strong>实测材料变化 · {measuredChange.kind === 'removal' ? '去除' : '增加'}（{measuredChange.record.material}）</strong>
          <span>顶面高度（面积加权中位）：{formatMicron(measuredChange.record.medianHeightBefore)} → {formatMicron(measuredChange.record.medianHeightAfter)} · 实测{measuredChange.kind === 'removal' ? '去除' : '增加'}厚度 {formatThickness(measuredChange.kind === 'removal' ? measuredChange.record.removedThickness : measuredChange.record.addedThickness)}</span>
          <span>最高顶面（参考）：{formatMicron(measuredChange.record.topHeightBefore)} → {formatMicron(measuredChange.record.topHeightAfter)}（变化 {formatThickness(measuredChange.record.topHeightDelta)}）。厚度不用最高点：高台阶上的覆盖层会先掉最高处，最高点差值描述“膜曾经在哪里”，不是去掉了多少。</span>
          {measuredChange.record.removedCompletely && <span>该材料在这一步被完全移除，没有剩余上表面可比对；该步后最高表面为 {formatMicron(measuredChange.record.remainingTopHeight)}。</span>}
          <span>测量方法：{measuredChange.record.method ?? '表面网格上表面单元'}；厚度基准 {measuredChange.record.thicknessBasis ?? 'area-weighted-median'}，不生成体网格。名义 rate × duration 只是标定通量，不代表实际厚度。</span>
          {(measuredChange.record.materials?.length ?? 0) > 1 && <div className="flux-table-wrap"><table className="flux-table"><thead><tr><th>材料</th><th>中位面：前 → 后</th><th>最高面：前 → 后</th><th>实测去除</th><th>实测增加</th></tr></thead><tbody>
            {(measuredChange.record.materials ?? []).map(row => <tr key={row.material}><td>{row.material}</td><td>{formatMicron(row.medianHeightBefore)} → {formatMicron(row.medianHeightAfter)}</td><td>{formatMicron(row.topHeightBefore)} → {formatMicron(row.topHeightAfter)}{row.removedCompletely ? '（已移除）' : ''}</td><td>{formatThickness(row.removedThickness)}</td><td>{formatThickness(row.addedThickness)}</td></tr>)}
          </tbody></table></div>}
        </div>}
        </div>
          </div>}
        </div>
      </section>

      <div className="column-resizer" role="separator" aria-orientation="vertical" aria-label="调整参数栏宽度"
        aria-controls="settings-panel" aria-valuemin={panelWidthLimits.settings.min} aria-valuemax={maxPanelWidth('settings', workspaceRef.current?.clientWidth ?? 1200)}
        aria-valuenow={Math.round(panelWidths.settings)} tabIndex={0} onKeyDown={event => adjustPanelWidth(event, 'settings')}
        onPointerDown={event => beginPanelResize(event, 'settings')} onPointerMove={movePanelResize} onPointerUp={endPanelResize}
        onPointerCancel={endPanelResize} onLostPointerCapture={endPanelResize}
        onDoubleClick={() => setPanelWidths(previous => fitPanelWidths({ ...previous, settings: 275 }, workspaceRef.current?.clientWidth ?? 1000))} />

      <Inspector className={`${mobilePanel === 'settings' ? 'mobile-visible' : ''} ${inspectorOpen ? '' : 'inspector-collapsed'}`}>
        <section className="panel-section">
          <div className="section-heading"><div><span className="eyebrow">{selectedStepObject ? '步骤编辑' : '项目概要'}</span><h2>{selectedStepObject ? '步骤参数' : '属性检查器'}</h2></div><button type="button" className="icon-button" aria-label="关闭属性检查器" title="关闭属性检查器" onClick={() => setInspectorOpen(false)}><Icon name="close"/></button></div>
          {selectedStepObject ? <>
            <div className="inspector-tabs" role="tablist" aria-label="步骤参数分组">
              <button id="step-tab-basic" type="button" role="tab" aria-selected={inspectorTab === 'basic'} aria-controls="step-parameter-panel" tabIndex={inspectorTab === 'basic' ? 0 : -1} onClick={() => setInspectorTab('basic')}>基本</button>
              <button id="step-tab-physics" type="button" role="tab" aria-selected={inspectorTab === 'physics'} aria-controls="step-parameter-panel" tabIndex={inspectorTab === 'physics' ? 0 : -1} onClick={() => setInspectorTab('physics')}>物理</button>
              <button id="step-tab-native" type="button" role="tab" aria-selected={inspectorTab === 'native'} aria-controls="step-parameter-panel" tabIndex={inspectorTab === 'native' ? 0 : -1} onClick={() => setInspectorTab('native')}>原生参数</button>
            </div>
            <StepInspector activeTab={inspectorTab}>
            <div className="field-grid one-column step-inspector-basics">
              <div className="step-tab-panel" data-step-tab-panel="basic">
              <label className="field"><span className="field-label">步骤名称</span><input type="text" value={selectedStepObject.name} maxLength={80} onChange={event => editStep(selectedStepObject.id, step => ({ ...step, name: event.target.value }))} /></label>
              </div>
              {!isGeometryStep(selectedStepObject) ? <>
              <div className="step-tab-panel" data-step-tab-panel="basic">
              {selectedStepObject.model !== 'specialized' && selectedStepObject.model !== 'csvProcess' && <SelectField label="工艺操作" value={selectedStepObject.operation ?? 'etch'} options={[{ value: 'etch', label: '刻蚀' }, { value: 'deposit', label: '沉积' }]} onChange={value => editStep(selectedStepObject.id, step => {
                const next = { ...step, operation: value as Step['operation'] };
                if (next.operation === 'deposit') next.depositMaterial = materials.find(m => m.name === 'SiO2')?.name ?? 'SiO2'; else delete next.depositMaterial;
                // Material tables are valid for both operations now: the worker applies the
                // operation sign. Only scalar-rate steps lose the table on an operation switch.
                const keepsMaterialTable = isDirectionalMaterialMode(step) || (step.model === 'isotropic' && step.rateMode === 'material');
                if (!keepsMaterialTable) { delete next.materialRates; delete next.selectivityReference; }
                if (next.model !== 'directional' && next.model !== 'multiParticle' && project.schemaVersion >= 4) next.rateMode = keepsMaterialTable && step.model === 'isotropic' ? 'material' : 'uniform'; else delete next.rateMode;
                return next;
              })} />}
              {selectedStepObject.model !== 'specialized' && selectedStepObject.model !== 'csvProcess' && <SelectField label="过程模型" value={selectedStepObject.model} options={[{ value: 'isotropic', label: '各向同性过程' }, { value: 'singleParticle', label: '单粒子过程' }, { value: 'multiParticle', label: '多粒子过程' }, { value: 'directional', label: '定向过程' }]} onChange={value => changeStepModel(selectedStepObject.id, value as Step['model'])} />}
              {selectedStepObject.model !== 'specialized' && selectedStepObject.model !== 'csvProcess' && selectedStepObject.operation === 'deposit' && <SelectField label="沉积材料" value={selectedStepObject.depositMaterial ?? materials[0]?.name ?? 'SiO2'} options={materials.map(m => ({ value: m.name, label: m.name }))} onChange={value => editStep(selectedStepObject.id, step => ({ ...step, depositMaterial: value }))} hint="材料不改变通用全局速率" />}
              {selectedStepObject.model === 'specialized' && <SelectField label="ViennaPS 4.6.2 专用模型" value={selectedStepObject.nativeModel ?? ''} options={availableSpecializedModels.map(item => ({ value: item.id, label: item.label }))} onChange={value => changeSpecializedModel(selectedStepObject.id, value)} />}
              </div>
              {selectedStepObject.model === 'specialized' ? <div className="step-tab-panel" data-step-tab-panel="native">
                {(() => {
                  const spec = specializedCatalog?.models.find(item => item.id === selectedStepObject.nativeModel);
                  if (!spec) return <div className="inline-note warning">安装版 ViennaPS 专用模型目录不可用。</div>;
                  const values = selectedStepObject.nativeParameters ?? {};
                  const geometryNames = geometryMaterialNames(project.geometry);
                  return <>
                    <div className="inline-note">参数直接传给安装的 {spec.nativeClass}。{specializedCatalog?.unitsNote} 原生模型规定的工艺速率由该模型内部计算，工作台不叠加通用速率。</div>
                    {specializedDimensionLabel(spec) && <div className="inline-note warning">安装版 {spec.nativeClass} {specializedDimensionLabel(spec)}：{spec.dimensions?.[0] === 2 ? '3D 绑定在受控探测中崩溃' : '2D 绑定在受控探测中不返回'}，因此其他维度的项目会被验证拒绝。</div>}
                    {spec.id === 'oxideRegrowth' && <div className="inline-note warning">本机 ViennaPS 4.6.2 的 OxideRegrowth 已验证条件是含 SiO₂ / Si₃N₄ 的平坦多层结构。请将“顶层图案”设为“无”；此版本在带沟槽或圆孔的 Domain 上可能崩溃。{project.geometry.kind === 'stack' && project.geometry.pattern.kind !== 'none' && <button className="button" onClick={() => updateProject(previous => previous.geometry.kind === 'stack' ? { ...previous, geometry: { ...previous.geometry, pattern: { kind: 'none' } } } : previous)}>将顶层图案改为无</button>}</div>}
                    {spec.depositMaterial && <div className="not-applicable"><strong>原生沉积材料</strong><span>{spec.depositMaterial}（由模型固定）</span></div>}
                    {spec.depositMaterialSelectable && <SelectField label="沉积材料" value={selectedStepObject.depositMaterial ?? 'Al2O3'} options={materials.map(item => ({ value: item.name, label: item.name }))} onChange={value => editStep(selectedStepObject.id, step => ({ ...step, depositMaterial: value }))} />}
                    {spec.fields.map(field => {
                      const value = values[field.key];
                      const write = (next: unknown) => editSpecializedParameter(selectedStepObject.id, field.key, next);
                      if (field.kind === 'number') return <NumericField key={field.key} label={field.label} unit={field.nativeUnit ?? undefined} value={typeof value === 'number' ? value : Number(field.default ?? 0)} min={field.minimum} max={field.maximum} step={String(field.step ?? 0.1)} onChange={write} />;
                      if (field.kind === 'boolean') return <label key={field.key} className="visibility-option"><input type="checkbox" aria-label={field.label} checked={value === true} onChange={event => write(event.target.checked)} /><span><strong>{field.label}</strong><small>传入 ViennaPS {spec.nativeClass} 布尔参数</small></span></label>;
                      if (field.kind === 'enum') return <SelectField key={field.key} label={field.label} value={String(value ?? field.default ?? '')} options={(field.options ?? []).map(option => ({ value: option.value, label: option.label }))} onChange={write} />;
                      if (field.kind === 'material' || field.kind === 'materialOptional') return <SelectField key={field.key} label={field.label} value={String(value ?? '')} options={[...(field.kind === 'materialOptional' ? [{ value: '', label: '不指定' }] : []), ...materials.map(item => ({ value: item.name, label: item.name }))]} onChange={write} />;
                      if (field.kind === 'text') return <label className="field" key={field.key}><span className="field-label">{field.label}</span><input aria-label={field.label} value={String(value ?? '')} maxLength={80} onChange={event => write(event.target.value)} /></label>;
                      if (field.kind === 'vector3') return <div className="special-vector" key={field.key}><strong>{field.label}</strong>{([0, 1, 2] as const).map(axis => <NumericField key={`${field.key}-${axis}`} label={`${field.label} · ${['x', 'y', 'z'][axis]}`} value={Array.isArray(value) ? Number(value[axis] ?? 0) : Number((field.default as number[] | undefined)?.[axis] ?? 0)} step="0.1" onChange={next => { const vector = Array.isArray(value) ? [...value] as number[] : [...((field.default as number[] | undefined) ?? [0, 0, 0])]; vector[axis] = next; write(vector); }} />)}</div>;
                      if (field.kind === 'materialRates') {
                        const table = value && typeof value === 'object' ? value as Record<string, number> : {};
                        const required = spec.id === 'wetEtch' ? geometryNames : spec.id === 'selectiveEpitaxy' ? ['Si', 'SiGe'] : [];
                        const rowNames = [...new Set([...Object.keys(table), ...required])].sort();
                        return <div className="rate-table specialized-table" key={field.key}><strong>{field.label}</strong>{rowNames.map(name => <div className="rate-row" key={name}><strong>{name}</strong><NumericField label={`${name} · ${field.label}`} value={Number(table[name] ?? field.default ?? 0)} min={field.minimum} max={field.maximum} step={String(field.step ?? 0.01)} onChange={next => write({ ...table, [name]: next })} />{!required.includes(name) && <button className="icon-button" aria-label={`移除 ${name} 参数行`} onClick={() => { const next = { ...table }; delete next[name]; write(next); }}>×</button>}</div>)}{required.length === 0 && <div className="rate-add"><select aria-label={`${field.label}材料`} value={rateRowMaterial} onChange={event => setRateRowMaterial(event.target.value)}><option value="">选择材料…</option>{geometryNames.filter(name => !Object.hasOwn(table, name)).map(name => <option key={name} value={name}>{name}</option>)}</select><button className="button" disabled={!rateRowMaterial} onClick={() => { const name = rateRowMaterial; setRateRowMaterial(''); write({ ...table, [name]: Number(field.default ?? 0) }); }}>＋ 添加材料行</button></div>}</div>;
                      }
                      const table = value && typeof value === 'object' ? value as Record<string, Record<string, number>> : {};
                      return <fieldset className="special-fluorocarbon-table" key={field.key}><legend>{field.label}</legend>{Object.entries(table).sort(([a], [b]) => a.localeCompare(b)).map(([materialName, row]) => <div className="fluorocarbon-material-row" key={materialName}><strong>{materialName}</strong><div>{Object.entries(field.defaults ?? {}).map(([parameter, defaultValue]) => <NumericField key={parameter} label={`${materialName} · ${parameter}`} value={Number(row?.[parameter] ?? defaultValue)} min={field.minimum} max={field.maximum} step={String(field.step ?? 0.01)} onChange={next => write({ ...table, [materialName]: { ...row, [parameter]: next } })} />)}</div></div>)}</fieldset>;
                    })}
                    {spec.id !== 'oxidation' && <NumericField label="持续时间" unit="s" value={selectedStepObject.duration ?? 0.2} min={0.01} max={600} step="0.01" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, duration: value }))} />}
                    {spec.id === 'oxidation' && <div className="not-applicable"><strong>时间输入</strong><span>使用原生参数中的小时数与时间步；不再添加通用过程持续时间。</span></div>}
                    {spec.usesFlux && <div className="inline-note">此模型使用真实 ViennaPS 射线/通量路径。引擎、随机种子与线程会记录在本次运行历史中。</div>}
                  </>;
                })()}
              </div> : selectedStepObject.model === 'csvProcess' ? <div className="step-tab-panel" data-step-tab-panel="native">
                {(() => {
                  const asset = csvAsset && csvAsset.assetId === selectedStepObject.assetId ? csvAsset : null;
                  const direction = selectedStepObject.direction ?? defaultDirection(project.dimension);
                  const offset = selectedStepObject.offset ?? [0, 0];
                  return <>
                    <div className="inline-note">原生 ps.CSVFileProcess + RateGrid。{project.dimension === 2 ? '二维表格为 x,rate' : '三维表格为 x,y,rate'}，没有 z 列：速率只随横向坐标变化。上传时已检查有限数值、列数和行结构；缺失或不兼容的表现在会被验证拒绝。</div>
                    <div className="not-applicable"><strong>速率表</strong><span>{selectedStepObject.fileName ?? '未选择'} · {selectedStepObject.assetId?.slice(0, 12) ?? '—'}…{asset ? ` · ${asset.rowCount} 行 · ${asset.linearCompatible ? 'LINEAR 可用' : '仅 IDW'}` : ''}</span></div>
                    <button className="button" disabled={loading} onClick={() => { setCsvUploadMode('replace'); csvInputRef.current?.click(); }}>重新上传并替换速率表</button>
                    <div className="inline-note warning">操作固定为原生：正速率配合向下方向使表面外移（沉积），负速率刻蚀。原生模型没有独立的 etch/deposit 参数，因此不显示工艺操作选择。自定义 Python 插值器不可达。</div>
                    <SelectField label="插值模式（必填）" value={selectedStepObject.interpolationMode ?? 'LINEAR'} options={[{ value: 'LINEAR', label: 'LINEAR · 原生线性/双线性' }, { value: 'IDW', label: 'IDW · 反距离权重' }]} onChange={value => editStep(selectedStepObject.id, step => {
                      const next = { ...step, interpolationMode: value as 'LINEAR' | 'IDW' };
                      if (value === 'IDW') next.idwNeighbors = step.idwNeighbors ?? Math.min(4, asset?.rowCount ?? 4); else delete next.idwNeighbors;
                      return next;
                    })} />
                    {selectedStepObject.interpolationMode === 'IDW' && <NumericField label="IDW 邻居数" value={selectedStepObject.idwNeighbors ?? 4} min={1} max={64} step="1" hint="必须不超过表行数；原生 setIDWNeighbors" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, idwNeighbors: Math.round(value) }))} />}
                    <div className="directional-axis-fields">
                      {(project.dimension === 2 ? [0, 1] as const : [0, 1, 2] as const).map(axis => {
                        const label = project.dimension === 2 ? (axis === 0 ? '方向 x 分量' : '方向 h 分量') : ['方向 x 分量', '方向 y 分量', '方向 z 分量'][axis];
                        return <NumericField key={label} label={label} value={direction[axis]} min={-1} max={1} step="0.05" onChange={value => editStep(selectedStepObject.id, step => {
                          const next: [number, number, number] = [...(step.direction ?? defaultDirection(project.dimension))];
                          next[axis] = value;
                          if (project.dimension === 2) next[2] = 0;
                          return { ...step, direction: next };
                        })} />;
                      })}
                    </div>
                    <NumericField label="速率表 offset x" unit={lengthUnit} value={offset[0]} min={-1000000} max={1000000} step="0.05" hint="原生在 coordinate+offset 处查表" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, offset: [value, (step.offset ?? [0, 0])[1]] }))} />
                    {project.dimension === 3 && <NumericField label="速率表 offset y" unit={lengthUnit} value={offset[1]} min={-1000000} max={1000000} step="0.05" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, offset: [(step.offset ?? [0, 0])[0], value] }))} />}
                    <NumericField label="各向同性分量" value={selectedStepObject.isotropicComponent ?? 0} min={0} max={100} step="0.01" hint="原生 isotropicComponent；沿法向的速率倍数" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, isotropicComponent: value }))} />
                    <NumericField label="定向分量" value={selectedStepObject.directionalComponent ?? 1} min={0} max={100} step="0.01" hint="原生 directionalComponent；沿方向向量的速率倍数" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, directionalComponent: value }))} />
                    <label className="visibility-option">
                      <input type="checkbox" aria-label="计算方向可见性" checked={selectedStepObject.calculateVisibility ?? true}
                        onChange={event => editStep(selectedStepObject.id, step => ({ ...step, calculateVisibility: event.target.checked }))} />
                      <span><strong>计算遮挡可见性</strong><small>原生 calculateVisibility；关闭后不计算方向遮挡</small></span>
                    </label>
                    <NumericField label="持续时间" unit="s" value={selectedStepObject.duration ?? 0.2} min={0.01} max={600} step="0.01" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, duration: value }))} />
                  </>;
                })()}
              </div> : selectedStepObject.model === 'directional' ? <div className="step-tab-panel" data-step-tab-panel="physics">
                <div className="inline-note directional-map-note">方向输入会先单位化。二维坐标为 [x, h, 0]；三维为 [x, y, z]，其中 z 轴垂直。ViennaPS 原生刻蚀速度传负值、沉积速度传正值；方向和速率符号由工作台按操作映射。</div>
                <div className="directional-axis-fields">
                  {(project.dimension === 2 ? [0, 1] as const : [0, 1, 2] as const).map(axis => {
                    const direction = selectedStepObject.direction ?? defaultDirection(project.dimension);
                    const label = project.dimension === 2 ? (axis === 0 ? '方向 x 分量' : '方向 h 分量') : (['方向 x 分量', '方向 y 分量', '方向 z 分量'][axis]);
                    return <NumericField key={label} label={label} value={direction[axis]} min={-1} max={1} step="0.05" onChange={value => editStep(selectedStepObject.id, step => {
                      const next: [number, number, number] = [...(step.direction ?? defaultDirection(project.dimension))];
                      next[axis] = value;
                      if (project.dimension === 2) next[2] = 0;
                      return { ...step, direction: next };
                    })} />;
                  })}
                </div>
                {(() => {
                  const materialMode = isDirectionalMaterialMode(selectedStepObject);
                  const table = directionalMaterialRates(selectedStepObject);
                  const defaultRate = selectedStepObject.defaultMaterialRate ?? { directional: 0, isotropic: 0 };
                  return <>
                    <SelectField label="速率输入模式" value={materialMode ? 'material' : 'scalar'} options={[{ value: 'scalar', label: '标量速率' }, { value: 'material', label: '材料速率表' }]} onChange={value => setDirectionalRateMode(selectedStepObject.id, value as 'scalar' | 'material')} hint="材料速率表与标量速率、遮挡可见性互斥" />
                    {materialMode ? <>
                      <div className="rate-profile-note">每种材料分别给出定向与各向同性分量（{rateUnit}，0–2）。未列出的材料使用默认速率，0 表示不动。原生 RateMap 没有遮挡可见性开关，因此材料模式不写 calculateVisibility。</div>
                      <NumericField label="默认定向速率" unit={rateUnit} value={defaultRate.directional} min={0} max={2} step="0.01" hint="未列出材料的定向分量；0 表示保护" onChange={value => setDirectionalDefaultMaterialRate(selectedStepObject.id, { directional: value })} />
                      <NumericField label="默认各向同性速率" unit={rateUnit} value={defaultRate.isotropic} min={0} max={2} step="0.01" hint="未列出材料的各向同性分量；0 表示不添加" onChange={value => setDirectionalDefaultMaterialRate(selectedStepObject.id, { isotropic: value })} />
                      <div className="rate-table" role="table" aria-label="定向材料速率表">
                        <div className="rate-table-head" role="row"><span>材料 / 定向 / 各向同性</span><span>{rateUnit}</span><span /></div>
                        {Object.entries(table).sort(([a], [b]) => a.localeCompare(b)).map(([materialName, row]) => <div className="rate-row" role="row" key={materialName}>
                          <div>
                            <strong>{materialName}</strong>
                            <NumericField label={`${materialName} 定向速率`} unit={rateUnit} value={row.directional ?? 0} min={0} max={2} step="0.01" onChange={value => setDirectionalMaterialRate(selectedStepObject.id, materialName, { directional: value })} />
                            <NumericField label={`${materialName} 各向同性速率`} unit={rateUnit} value={row.isotropic ?? 0} min={0} max={2} step="0.01" onChange={value => setDirectionalMaterialRate(selectedStepObject.id, materialName, { isotropic: value })} />
                          </div>
                          <span className="rate-ratio">{(row.directional ?? 0) === 0 && (row.isotropic ?? 0) === 0 ? '0 · 不动' : `${row.directional ?? 0} / ${row.isotropic ?? 0}`}</span>
                          <button className="icon-button" type="button" aria-label={`移除 ${materialName} 定向速率行`} title="移除材料行" onClick={() => removeDirectionalMaterialRate(selectedStepObject.id, materialName)}>×</button>
                        </div>)}
                        <div className="rate-add"><select aria-label="新增定向材料速率行" value={directionalRateRowMaterial} onChange={event => setDirectionalRateRowMaterial(event.target.value)}><option value="">选择材料…</option>{materials.filter(m => !Object.hasOwn(table, m.name)).map(m => <option key={m.name} value={m.name}>{m.name}</option>)}</select><button className="button" disabled={!directionalRateRowMaterial} onClick={() => addDirectionalMaterialRate(selectedStepObject.id, directionalRateRowMaterial)}>＋ 添加材料行</button></div>
                      </div>
                    </> : <>
                      <NumericField label="定向速率大小" unit={rateUnit} value={selectedStepObject.directionalRateMagnitude ?? 0} min={0} max={2} step="0.01" hint="刻蚀/沉积操作自动应用原生符号" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, directionalRateMagnitude: value }))} />
                      <NumericField label="各向同性速率分量" unit={rateUnit} value={selectedStepObject.isotropicRateMagnitude ?? 0} min={0} max={2} step="0.01" hint="与定向分量叠加；0 表示不添加该分量" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, isotropicRateMagnitude: value }))} />
                      <label className={`visibility-option ${(selectedStepObject.directionalRateMagnitude ?? 0) === 0 ? 'disabled' : ''}`}>
                        <input type="checkbox" aria-label="计算方向可见性" checked={selectedStepObject.calculateVisibility ?? true} disabled={(selectedStepObject.directionalRateMagnitude ?? 0) === 0}
                          onChange={event => editStep(selectedStepObject.id, step => ({ ...step, calculateVisibility: event.target.checked }))} />
                        <span><strong>计算遮挡可见性</strong><small>仅定向速率非零时适用；关闭后不计算遮挡</small></span>
                      </label>
                    </>}
                  </>;
                })()}
              </div> : selectedStepObject.model === 'multiParticle' ? <>
                <div className="step-tab-panel" data-step-tab-panel="native">
                <div className="inline-note">参数直接传给 ViennaPS 4.6.2 MultiParticleProcess。安装版绑定没有声明离子角度与能量单位，工作台不换算单位。速率关系是受控表格：按材料选择各粒子通量系数，刻蚀返回负速度、沉积返回正速度；原生通量单位未声明，系数负责换算到工作台使用的 µm/s。</div>
                <div className="multi-species-list" aria-label="多粒子种类">
                  {(selectedStepObject.species ?? []).map((particle, particleIndex) => <fieldset className="multi-species-card" key={`${particle.label}-${particleIndex}`}>
                    <legend>{particle.kind === 'ion' ? '离子' : '中性粒子'} · {particle.label}</legend>
                    <label className="field"><span className="field-label">粒子标签</span><input aria-label={`粒子标签 ${particleIndex + 1}`} value={particle.label} maxLength={40} onChange={event => editParticleSpecies(selectedStepObject.id, particleIndex, current => ({ ...current, label: event.target.value }))} /></label>
                    <button type="button" className="icon-button" aria-label={`移除粒子 ${particle.label}`} disabled={(selectedStepObject.species?.length ?? 0) <= 1} onClick={() => removeParticleSpecies(selectedStepObject.id, particleIndex)}>移除</button>
                    {particle.kind === 'ion' ? <>
                      <div className="multi-native-fields">
                        {([
                          { key: 'sourcePower', label: 'sourcePower', min: 0, max: 20, step: '0.1' },
                          { key: 'thetaRMin', label: 'thetaRMin', min: 0, max: 90, step: '1' },
                          { key: 'thetaRMax', label: 'thetaRMax', min: 0, max: 90, step: '1' },
                          { key: 'minAngle', label: 'minAngle', min: 0, max: 90, step: '1' },
                          { key: 'B_sp', label: 'B_sp', min: -1, max: 20, step: '0.1' },
                          { key: 'meanEnergy', label: 'meanEnergy', min: 0, max: 1000000, step: '1' },
                          { key: 'sigmaEnergy', label: 'sigmaEnergy', min: 0, max: 1000000, step: '1' },
                          { key: 'thresholdEnergy', label: 'thresholdEnergy', min: 0, max: 1000000, step: '1' },
                          { key: 'inflectAngle', label: 'inflectAngle', min: 0, max: 90, step: '1' },
                          { key: 'n', label: 'n', min: 1, max: 100, step: '1' },
                        ] as const).map(parameter => <NumericField key={parameter.key} label={parameter.label} value={particle[parameter.key]} min={parameter.min} max={parameter.max} step={parameter.step}
                          onChange={value => editParticleSpecies(selectedStepObject.id, particleIndex, current => current.kind === 'ion' ? { ...current, [parameter.key]: parameter.key === 'n' ? Math.round(value) : value } : current)} />)}
                      </div>
                    </> : <>
                      <NumericField label="默认黏附概率" value={particle.defaultStickingProbability} min={0} max={1} step="0.01" onChange={value => editParticleSpecies(selectedStepObject.id, particleIndex, current => current.kind === 'neutral' ? { ...current, defaultStickingProbability: value } : current)} />
                      <div className="multi-material-table">
                        {Object.entries(particle.stickingProbabilityByMaterial).map(([materialName, probability]) => <div className="rate-row" key={materialName}>
                          <strong>{materialName}</strong>
                          <NumericField label={`${materialName} 黏附概率`} value={probability} min={0} max={1} step="0.01" onChange={value => editParticleSpecies(selectedStepObject.id, particleIndex, current => current.kind === 'neutral' ? { ...current, stickingProbabilityByMaterial: { ...current.stickingProbabilityByMaterial, [materialName]: value } } : current)} />
                          <button type="button" className="icon-button" aria-label={`移除 ${materialName} 黏附覆盖`} onClick={() => editParticleSpecies(selectedStepObject.id, particleIndex, current => {
                            if (current.kind !== 'neutral') return current;
                            const stickingProbabilityByMaterial = { ...current.stickingProbabilityByMaterial }; delete stickingProbabilityByMaterial[materialName];
                            return { ...current, stickingProbabilityByMaterial };
                          })}>×</button>
                        </div>)}
                        <div className="rate-add"><select aria-label={`${particle.label} 新增材料黏附覆盖`} value={multiStickingMaterial} onChange={event => setMultiStickingMaterial(event.target.value)}><option value="">选择材料…</option>{materials.filter(material => !Object.hasOwn(particle.stickingProbabilityByMaterial, material.name)).map(material => <option key={material.name} value={material.name}>{material.name}</option>)}</select><button className="button" disabled={!multiStickingMaterial} onClick={() => {
                          const materialName = multiStickingMaterial; setMultiStickingMaterial('');
                          editParticleSpecies(selectedStepObject.id, particleIndex, current => current.kind === 'neutral' ? { ...current, stickingProbabilityByMaterial: { ...current.stickingProbabilityByMaterial, [materialName]: current.defaultStickingProbability } } : current);
                        }}>＋ 按材料设置黏附</button></div>
                      </div>
                    </>}
                  </fieldset>)}
                  <div className="rate-add"><button className="button" disabled={(selectedStepObject.species?.length ?? 0) >= 8} onClick={() => addParticleSpecies(selectedStepObject.id, 'ion')}>＋ 添加离子</button><button className="button" disabled={(selectedStepObject.species?.length ?? 0) >= 8} onClick={() => addParticleSpecies(selectedStepObject.id, 'neutral')}>＋ 添加中性粒子</button></div>
                </div>
                </div>
                <div className="step-tab-panel" data-step-tab-panel="physics">
                <div className="multi-rate-table" role="table" aria-label="多粒子速率关系">
                  <strong>速率关系系数表</strong>
                  {(selectedStepObject.species ?? []).map(particle => <NumericField key={particle.label} label={`默认系数 · ${particle.label}`} value={selectedStepObject.rateCoefficients?.defaultBySpecies[particle.label] ?? 0} min={0} max={2} step="0.001" onChange={value => editStep(selectedStepObject.id, step => ({
                    ...step, rateCoefficients: { defaultBySpecies: { ...(step.rateCoefficients?.defaultBySpecies ?? {}), [particle.label]: value }, byMaterial: step.rateCoefficients?.byMaterial ?? {} },
                  }))} />)}
                  {Object.entries(selectedStepObject.rateCoefficients?.byMaterial ?? {}).sort(([a], [b]) => a.localeCompare(b)).map(([materialName, coefficients]) => <div className="multi-material-rate-row" key={materialName}>
                    <strong>{materialName}</strong>
                    {(selectedStepObject.species ?? []).map(particle => <NumericField key={particle.label} label={`${materialName} · ${particle.label} 系数`} value={coefficients[particle.label] ?? selectedStepObject.rateCoefficients?.defaultBySpecies[particle.label] ?? 0} min={0} max={2} step="0.001" onChange={value => editStep(selectedStepObject.id, step => ({
                      ...step, rateCoefficients: { defaultBySpecies: step.rateCoefficients?.defaultBySpecies ?? {}, byMaterial: { ...(step.rateCoefficients?.byMaterial ?? {}), [materialName]: { ...(step.rateCoefficients?.byMaterial[materialName] ?? {}), [particle.label]: value } } },
                    }))} />)}
                    <button type="button" className="icon-button" aria-label={`移除 ${materialName} 速率系数覆盖`} onClick={() => editStep(selectedStepObject.id, step => {
                      const byMaterial = { ...(step.rateCoefficients?.byMaterial ?? {}) }; delete byMaterial[materialName];
                      return { ...step, rateCoefficients: { defaultBySpecies: step.rateCoefficients?.defaultBySpecies ?? {}, byMaterial } };
                    })}>×</button>
                  </div>)}
                  <div className="rate-add"><select aria-label="多粒子新增速率材料" value={multiRateMaterial} onChange={event => setMultiRateMaterial(event.target.value)}><option value="">选择材料…</option>{materials.filter(material => !Object.hasOwn(selectedStepObject.rateCoefficients?.byMaterial ?? {}, material.name)).map(material => <option key={material.name} value={material.name}>{material.name}</option>)}</select><button className="button" disabled={!multiRateMaterial} onClick={() => {
                    const materialName = multiRateMaterial; setMultiRateMaterial('');
                    editStep(selectedStepObject.id, step => ({ ...step, rateCoefficients: { defaultBySpecies: step.rateCoefficients?.defaultBySpecies ?? {}, byMaterial: { ...(step.rateCoefficients?.byMaterial ?? {}), [materialName]: { ...(step.rateCoefficients?.defaultBySpecies ?? {}) } } } }));
                  }}>＋ 添加材料系数行</button></div>
                </div>
                </div>
              </> : <>
              <div className="step-tab-panel" data-step-tab-panel="physics">
              {selectedStepObject.model === 'isotropic' && <SelectField label="各向同性算法" value={selectedStepObject.isotropicMethod ?? ''} options={[
                { value: '', label: 'Level-set · 旧项目默认（不写字段）' },
                { value: 'auto', label: '自动（2D 几何 / 3D level-set）' },
                { value: 'levelSet', label: 'Level-set' },
                { value: 'geometric', label: '几何偏移' },
              ]} onChange={value => editStep(selectedStepObject.id, step => {
                const next = { ...step };
                if (value) next.isotropicMethod = value as NonNullable<Step['isotropicMethod']>; else delete next.isotropicMethod;
                if (value === 'geometric') delete next.isotropicTimeStepRatio;
                return next;
              })} hint="自动只在 2D、统一速率且几何安全时改用几何偏移；3D 自动始终走 level-set" />}
              {selectedStepObject.model === 'isotropic' && selectedStepObject.isotropicMethod === 'geometric' && <div className="inline-note warning">几何偏移只支持统一速率{materialRateModeActive(selectedStepObject) ? '（当前是按材料速率，请切回统一速率）' : ''}，并且厚度必须 ≥ 一个网格；3D 显式几何通常比 level-set 慢 3–30 倍，只适合作为对照。</div>}
              {selectedStepObject.model === 'isotropic' && selectedStepObject.isotropicMethod !== 'geometric' && <>
                <label className="visibility-option">
                  <input type="checkbox" aria-label="覆盖 level-set 时间步比例" checked={selectedStepObject.isotropicTimeStepRatio !== undefined}
                    onChange={event => editStep(selectedStepObject.id, step => {
                      const next = { ...step };
                      if (event.target.checked) next.isotropicTimeStepRatio = step.isotropicTimeStepRatio ?? project.solver.timeStepRatio;
                      else delete next.isotropicTimeStepRatio;
                      return next;
                    })} />
                  <span><strong>覆盖 level-set 时间步比例</strong><small>仅影响 level-set 路径；关闭时沿用求解器值 {project.solver.timeStepRatio}</small></span>
                </label>
                {selectedStepObject.isotropicTimeStepRatio !== undefined && <NumericField label="level-set 时间步比例" value={selectedStepObject.isotropicTimeStepRatio} min={0.05} max={0.5} step="0.01" hint="范围 0.05–0.5；0.5 更快，但需要自行验证稳定性" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, isotropicTimeStepRatio: value }))} />}
              </>}
              {materialRateModeAvailable(selectedStepObject) && <SelectField label="速率模式" value={selectedStepObject.rateMode ?? 'uniform'} options={[{ value: 'uniform', label: '统一速率' }, { value: 'material', label: '按材料设置' }]} onChange={value => setRateMode(selectedStepObject.id, value as 'uniform' | 'material')} hint={selectedStepObject.operation === 'deposit' ? '沉积与刻蚀都可用材料表；未列出的材料使用“速率大小”' : '可为每种材料分别指定刻蚀速率'} />}
              <NumericField label={materialRateModeActive(selectedStepObject) ? '未列出材料默认速率' : '速率大小'} unit={rateUnit} value={selectedStepObject.rateMagnitude ?? 0} min={0} max={2} step="0.01" hint={materialRateModeActive(selectedStepObject) ? '材料表中的显式值优先；未列出的材料使用此值，0 表示保护' : "刻蚀传入负速率，沉积传入正速率"} onChange={value => materialRateModeActive(selectedStepObject) ? editRateProfile(selectedStepObject.id, step => ({ ...step, rateMagnitude: value, rateMode: 'material' })) : editStep(selectedStepObject.id, step => ({ ...step, rateMagnitude: value }))} />
              {materialRateModeActive(selectedStepObject) && <>
                <div className="rate-profile-note">表格中的速率优先；未列出的材料使用默认速率。0 {rateUnit} 表示保护。掩膜也按此表执行，同一种材料的所有区域共用速率。</div>
                <SelectField label="选择性参考材料" value={selectedStepObject.selectivityReference ?? ''} options={[{ value: '', label: '不显示参考比值' }, ...materials.map(m => ({ value: m.name, label: m.name }))]} onChange={value => setSelectivityReference(selectedStepObject.id, value)} hint="仅显示参考比值，不改变仿真" />
                <div className="rate-table" role="table" aria-label={selectedStepObject.operation === 'etch' ? '材料相关刻蚀速率' : '材料相关沉积速率'}>
                  <div className="rate-table-head" role="row"><span>材料 / 速率</span><span>参考速率 ÷ 本材料</span><span /></div>
                  {Object.entries(scalarMaterialRates(selectedStepObject)).sort(([a], [b]) => a.localeCompare(b)).map(([materialName, rate]) => {
                    const referenceRate = scalarMaterialRates(selectedStepObject)[selectedStepObject.selectivityReference ?? ''] ?? selectedStepObject.rateMagnitude ?? 0;
                    const ratio = rate === 0 ? referenceRate === 0 ? '— · 0/0 未定义' : '∞ · 保护' : `${(referenceRate / rate).toPrecision(4)}×`;
                    return <div className="rate-row" role="row" key={materialName}>
                      <div><strong>{materialName}</strong>{rate === 0 && <small className="protected-mark">0 · 保护</small>}<NumericField label={selectedStepObject.operation === 'etch' ? '刻蚀速率' : '沉积速率'} unit={rateUnit} value={rate} min={0} max={2} step=".01" onChange={value => setMaterialRate(selectedStepObject.id, materialName, value)} /></div>
                      <span className="rate-ratio">{selectedStepObject.selectivityReference ? ratio : '—'}</span>
                      <button className="icon-button" type="button" aria-label={`移除 ${materialName} 速率覆盖`} title="移除覆盖" onClick={() => removeMaterialRate(selectedStepObject.id, materialName)}>×</button>
                    </div>;
                  })}
                  <div className="rate-add"><select aria-label="新增材料速率行" value={rateRowMaterial} onChange={event => setRateRowMaterial(event.target.value)}><option value="">选择材料…</option>{materials.filter(m => !Object.hasOwn(scalarMaterialRates(selectedStepObject), m.name)).map(m => <option key={m.name} value={m.name}>{m.name}</option>)}</select><button className="button" disabled={!rateRowMaterial} onClick={() => addMaterialRate(selectedStepObject.id, rateRowMaterial)}>＋ 添加材料行</button></div>
                </div>
              </>}
              </div></>}
              <div className="step-tab-panel" data-step-tab-panel="physics">
              {selectedStepObject.model !== 'specialized' && <NumericField label="持续时间" unit="s" value={selectedStepObject.duration ?? 0.2} min={0.01} max={600} step="0.01" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, duration: value }))} />}
              {selectedStepObject.model === 'singleParticle' && <>
                <NumericField label="黏附概率" value={selectedStepObject.stickingProbability ?? 0.5} min={0} max={1} step="0.01" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, stickingProbability: value }))} />
                <NumericField label="源角分布指数" value={selectedStepObject.sourceExponent ?? 1} min={0} max={20} step="0.1" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, sourceExponent: value }))} />
              </>}
              </div>
              </> : <div className="step-tab-panel" data-step-tab-panel="basic">
                {selectedStepObject.model === 'removeMaterial' && <SelectField label="要移除的材料" value={selectedStepObject.material ?? materials[0]?.name ?? ''} options={materials.map(material => ({ value: material.name, label: material.name }))} onChange={value => editStep(selectedStepObject.id, step => ({ ...step, material: value }))} hint="调用 ViennaPS Domain.removeMaterial" />}
                {selectedStepObject.model === 'planarize' && <>
                  <SelectField label="截断高度来源" value={selectedStepObject.stopMaterial ? `material:${selectedStepObject.stopMaterial}` : 'height'} options={[{ value: 'height', label: '固定高度 · cutoffHeight' }, ...materials.map(material => ({ value: `material:${material.name}`, label: `按材料停止 · ${material.name} 顶面` }))]} onChange={value => editStep(selectedStepObject.id, step => {
                    if (value === 'height') { const next = { ...step, cutoffHeight: step.cutoffHeight ?? 0 }; delete next.stopMaterial; delete next.overPolish; return next; }
                    const next = { ...step, stopMaterial: value.slice('material:'.length) }; delete next.cutoffHeight;
                    if (next.overPolish === undefined) next.overPolish = 0;
                    return next;
                  })} hint="按材料停止时，运行阶段用该材料上表面的面积加权 90 分位高度推导截断高度" />
                  {!selectedStepObject.stopMaterial
                    ? <NumericField label="平坦化截断高度" unit={lengthUnit} value={selectedStepObject.cutoffHeight ?? 0} min={-100} max={100} step="0.05" hint="调用 ViennaPS Planarize(domain, cutoffHeight)" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, cutoffHeight: value }))} />
                    : <NumericField label="过抛光量" unit={lengthUnit} value={selectedStepObject.overPolish ?? 0} min={0} max={100} step="0.005" hint="有效截断高度 = 该材料上表面面积加权 90 分位高度 − 过抛光量" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, overPolish: value }))} />}
                  {selectedStepObject.stopMaterial && <div className="inline-note">材质感知 CMP：原生 <code>Planarize</code> 只接受一个高度，因此按“{selectedStepObject.stopMaterial}”上表面在运行时的<b>面积加权 90 分位（p90）</b>高度推导该高度，再调用原生平坦化。用 p90 而不是中位数：双峰停止面（例如 22 nm FinFET 的 STI 0.036 与 ILD 0.095）中位数会选到更大的下方平台，p90 选上方平台；只有上方平台占比 ≤10% 时才会选中下方，窄而高的毛刺不影响结果。它仍是一刀切的全局高度，不模拟横向选择比，多级且无主导上方平台时会被过切或欠切。实测值、方法与有效截断高度会写入该步结果。</div>}
                </>}
                {selectedStepObject.model === 'boolean' && <>
                  <SelectField label="布尔操作" value={selectedStepObject.booleanOperation ?? 'difference'} options={[{ value: 'union', label: '并集 · UNION' }, { value: 'intersection', label: '交集 · INTERSECT' }, { value: 'difference', label: '差集 · RELATIVE_COMPLEMENT' }]} onChange={value => editStep(selectedStepObject.id, step => ({ ...step, booleanOperation: value as Step['booleanOperation'] }))} />
                  <div className="inline-note">ViennaPS 使用真实盒体 stencil。位置是盒体底面坐标，宽度沿 x；二维高度沿 h，三维高度沿 z、深度沿 y。</div>
                  {(project.dimension === 2 ? [{ index: 0, label: '盒体 x 位置', unit: lengthUnit }, { index: 1, label: '盒体 h 起始位置', unit: lengthUnit }] : [{ index: 0, label: '盒体 x 位置', unit: lengthUnit }, { index: 1, label: '盒体 y 位置', unit: lengthUnit }, { index: 2, label: '盒体 z 起始位置', unit: lengthUnit }]).map(axis => <NumericField key={axis.label} label={axis.label} unit={axis.unit} value={(selectedStepObject.position ?? [0, 0, 0])[axis.index]} min={-40} max={40} step="0.05" onChange={value => editStep(selectedStepObject.id, step => {
                    const position: [number, number, number] = [...(step.position ?? [0, 0, 0])];
                    position[axis.index] = value;
                    if (project.dimension === 2) position[2] = 0;
                    return { ...step, position };
                  })} />)}
                  <NumericField label="盒体宽度（x）" unit={lengthUnit} value={selectedStepObject.width ?? 1} min={project.domain.gridDelta * 2} max={40} step="0.05" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, width: value }))} />
                  <NumericField label={project.dimension === 2 ? '盒体高度（h）' : '盒体高度（z）'} unit={lengthUnit} value={selectedStepObject.height ?? 0.8} min={project.domain.gridDelta * 2} max={100} step="0.05" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, height: value }))} />
                  {project.dimension === 3 && <NumericField label="盒体深度（y）" unit={lengthUnit} value={selectedStepObject.depth ?? 1} min={project.domain.gridDelta * 2} max={40} step="0.05" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, depth: value }))} />}
                  <NumericField label="盒体旋转角" unit="°" value={selectedStepObject.angle ?? 0} min={-45} max={45} step="1" onChange={value => editStep(selectedStepObject.id, step => ({ ...step, angle: value }))} />
                  <label className="visibility-option"><input type="checkbox" aria-label="布尔操作应用到所有材料界面" checked={selectedStepObject.applyToAll ?? true} onChange={event => editStep(selectedStepObject.id, step => ({ ...step, applyToAll: event.target.checked }))} /><span><strong>应用到所有材料界面</strong><small>传入 ViennaPS Domain.applyBooleanOperation 的 applyToAll 参数</small></span></label>
                </>}
                {selectedStepObject.model === 'gdsBoolean' && <>
                  {project.geometry.kind !== 'gds'
                    ? <div className="inline-note">GDS 布尔步骤只适用于 geometry.kind=gds 的三维项目。</div>
                    : <>
                      <SelectField label="布尔操作" value={selectedStepObject.booleanOperation ?? 'difference'} options={[{ value: 'difference', label: '差集 · RELATIVE_COMPLEMENT（挖孔/开槽）' }, { value: 'union', label: '并集 · UNION（在掩膜区补加材料）' }, { value: 'intersection', label: '交集 · INTERSECT（只保留掩膜区）' }]} onChange={value => editStep(selectedStepObject.id, step => ({ ...step, booleanOperation: value as Step['booleanOperation'] }))} />
                      <div className="inline-note">操作数来自当前版图结构 <code>{project.geometry.structure}</code>：所选图层多边形按保留区域竖直拉伸成真实三维掩膜体，再调用原生 <code>Domain.applyBooleanOperation</code>。位置相对版图放置，默认 0 即与版图对齐。这是几何操作，不包含光刻胶、光学邻近/偏置、侧壁角或材料选择比。</div>
                      {!gdsAsset && <div className="inline-note">正在读取版图图层…</div>}
                      {gdsAsset && gdsMaskPairs(gdsAsset, project.geometry.structure).length === 0 && <div className="inline-note">该结构没有闭合的多边形图层，无法作为布尔掩膜。</div>}
                      {gdsAsset && gdsMaskPairs(gdsAsset, project.geometry.structure).length > 0 && <SelectField label="掩膜图层 / datatype" value={`${selectedStepObject.gdsMask?.layer ?? 0}/${selectedStepObject.gdsMask?.datatype ?? 0}`} options={gdsMaskPairs(gdsAsset, project.geometry.structure).map(([gdsLayer, datatype]) => ({ value: `${gdsLayer}/${datatype}`, label: `Layer ${gdsLayer} · datatype ${datatype}` }))} onChange={value => { const [gdsLayer, datatype] = value.split('/').map(Number); editStep(selectedStepObject.id, step => ({ ...step, gdsMask: { ...(step.gdsMask ?? { layer: gdsLayer, datatype, unitScale: 1, position: [0, 0, 0], baseHeight: 0, height: Math.max(2 * project.domain.gridDelta, 0.5), retainedRegion: 'inside' }), layer: gdsLayer, datatype } })); }} />}
                      <SelectField label="保留区域" value={selectedStepObject.gdsMask?.retainedRegion ?? 'inside'} options={[{ value: 'inside', label: '保留多边形内部（正向掩膜）' }, { value: 'outside', label: '保留多边形外部（反向掩膜）' }]} onChange={value => editStep(selectedStepObject.id, step => step.gdsMask ? { ...step, gdsMask: { ...step.gdsMask, retainedRegion: value as 'inside' | 'outside' } } : step)} hint="保留区域决定掩膜体覆盖多边形内部还是外部；差集使用反向掩膜会移除几乎全部材料" />
                      <NumericField label="掩膜底面高度（z）" unit={lengthUnit} value={selectedStepObject.gdsMask?.baseHeight ?? 0} min={0} max={100} step="0.01" hint="相对版图 z 框架；与版图图层底面配合可只切上层材料（必须 ≥ 0）" onChange={value => editStep(selectedStepObject.id, step => step.gdsMask ? { ...step, gdsMask: { ...step.gdsMask, baseHeight: value } } : step)} />
                      <NumericField label="掩膜厚度（z）" unit={lengthUnit} value={selectedStepObject.gdsMask?.height ?? Math.max(2 * project.domain.gridDelta, 0.5)} min={2 * project.domain.gridDelta} max={100} step="0.01" hint={`至少两个网格间距（${(2 * project.domain.gridDelta).toFixed(4)} µm）`} onChange={value => editStep(selectedStepObject.id, step => step.gdsMask ? { ...step, gdsMask: { ...step.gdsMask, height: value } } : step)} />
                      {[{ index: 0, label: '掩膜 x 偏移', unit: 'µm' }, { index: 1, label: '掩膜 y 偏移', unit: 'µm' }, { index: 2, label: '掩膜 z 偏移', unit: 'µm' }].map(axis => <NumericField key={axis.label} label={axis.label} unit={axis.unit} value={(selectedStepObject.gdsMask?.position ?? [0, 0, 0])[axis.index]} min={-40} max={40} step="0.01" onChange={value => editStep(selectedStepObject.id, step => {
                        const mask = step.gdsMask; if (!mask) return step;
                        const position: [number, number, number] = [...mask.position]; position[axis.index] = value;
                        return { ...step, gdsMask: { ...mask, position } };
                      })} />)}
                      <NumericField label="版图缩放系数" value={selectedStepObject.gdsMask?.unitScale ?? 1} min={0.000001} max={1000000} step="0.01" hint="与项目 GDS 放置的单位缩放相乘" onChange={value => editStep(selectedStepObject.id, step => step.gdsMask ? { ...step, gdsMask: { ...step.gdsMask, unitScale: value } } : step)} />
                      <label className="visibility-option"><input type="checkbox" aria-label="GDS 布尔应用到所有材料界面" checked={selectedStepObject.applyToAll ?? true} onChange={event => editStep(selectedStepObject.id, step => ({ ...step, applyToAll: event.target.checked }))} /><span><strong>应用到所有材料界面</strong><small>关闭时只修改顶层材料，可能造成下层超过上层；仅在你确认要这样做时关闭</small></span></label>
                    </>}
                </>}
                <div className="not-applicable"><strong>几何操作</strong><span>没有工艺速率、物理持续时间、随机种子或通量引擎。此步骤直接修改前一步的 ViennaPS Domain。</span></div>
              </div>}
              {(isGeometryStep(selectedStepObject) || selectedStepObject.model === 'specialized') && <div className="step-tab-panel" data-step-tab-panel="physics"><div className="inline-note">{isGeometryStep(selectedStepObject) ? '几何操作没有工艺物理参数。' : '此模型的时间条件和计算参数位于“原生参数”页签。'}</div></div>}
            </div>
            {selectedStepObject.model !== 'specialized' && selectedStepObject.model !== 'multiParticle' && <div className="step-tab-panel" data-step-tab-panel="native"><div className="inline-note">此步骤没有额外的 ViennaPS 原生参数。</div></div>}
            <div className="step-tab-panel" data-step-tab-panel="basic"><div className="step-edit-actions"><button className="button" onClick={() => moveStep(selectedStepObject.id, -1)}>↑ 上移</button><button className="button" onClick={() => moveStep(selectedStepObject.id, 1)}>↓ 下移</button><button className="button" onClick={() => copyStep(selectedStepObject)}>复制</button><button className="button danger-quiet" onClick={() => { updateProject(previous => ({ ...previous, steps: previous.steps.filter(step => step.id !== selectedStepObject.id) })); setSelectedStep(null); }}>删除</button></div>
            <div className="section-divider" /></div>
          </StepInspector></> : selection.kind === 'run'
            ? <RunInspector>
                <div className="project-summary"><strong>运行 {selection.id.slice(0, 12)}</strong><span>{humanStatus(runs.find(row => row.runId === selection.id)?.status ?? displayInfo?.status)}</span><span>{runs.find(row => row.runId === selection.id)?.kind ?? displayInfo?.kind ?? '运行'}</span></div>
                <div className="run-engine-summary" aria-label="本次运行引擎">
                  <span className="field-label">本次运行引擎<small>来自 RunInfo.actualExecution 的实际引擎，与主机 GPU 能力无关</small></span>
                  <span className={`engine-badge ${loadedRunSelection ? engineTone(runEngines) : 'idle'}`}>{loadedRunSelection ? runEngine : '未载入'}</span>
                  <small>{loadedRunSelection ? runEngineTitle : '打开该运行记录后显示实际引擎。'}</small>
                  {loadedRunSelection && selectedStepEngine && <small>当前所选步骤：{selectedStepEngine}</small>}
                </div>
              </RunInspector>
            : selection.kind === 'material'
              ? <MaterialInspector>{selectedMaterial ? <MaterialPropertyInspector material={selectedMaterial} busy={materialBusy} onSave={saveCustomMaterialMetadata} /> : <div className="project-summary"><strong>{selection.name}</strong><span>材料目录中找不到该材料。</span></div>}</MaterialInspector>
              : selection.kind === 'artifact'
                ? <div className="project-summary"><strong>{selection.key}</strong><span>原生产物来自所选快照。</span></div>
                : <div className="project-summary"><strong>{project.name}</strong><span>{project.dimension}D · {project.steps.length} 个步骤 · {project.geometry.kind}</span><span>选择结构、配方、材料、结果或设置活动查看该域输入。</span></div>}

        </section>

        <section className="validation-card">
          <div className="validation-head"><span className={`validation-mark ${inputHash ? 'ok' : ''}`}>{inputHash ? '✓' : '!'}</span><div><strong>{inputHash ? '参数检查' : '需要修正输入'}</strong><small>{validationMessage || '等待工作台校验…'}</small></div></div>
          {warnings.map((warning, index) => <p key={index} className="warning-line">{warning}</p>)}
          <div className="validation-foot"><span>工艺步骤上限</span><strong>{capabilities?.resourcePolicy?.run?.maximumSteps == null ? '不限' : `${capabilities.resourcePolicy.run.maximumSteps} 步`}</strong></div>
          <div className="validation-foot" title="主机 GPU 能力来自 /api/gpu-devices，表示本机能否运行 CUDA 通量；不代表所选运行使用的引擎。"><span>主机 GPU 能力</span><strong>{gpuExposed ? 'GPU_DISK · 可选择' : gpuStatus === 'unverified-outside-sandbox' ? '设备不可用' : gpuStatus}</strong></div>
          <div className="validation-foot" title={runEngineTitle}><span>本次运行引擎</span><strong>{runEngine}{selectedStepEngine ? ` · 本步 ${selectedStepEngine}` : ''}</strong></div>
          {artifactEstimate && (artifactEstimate.estimatedMiB > 0 || artifactEstimate.risk !== 'ok') && <div className={`validation-foot artifact-foot ${artifactEstimate.risk}`} title={`运行产物预检估算；当前配置上限 ${artifactEstimate.capMiB} MiB，超限的运行会在启动前被拒绝。`}><span>运行产物预估</span><strong>{formatEstimateMiB(artifactEstimate.estimatedMiB)} / {artifactEstimate.capMiB} MiB · {ARTIFACT_RISK_LABELS[artifactEstimate.risk]}</strong></div>}
        </section>
      </Inspector>
    </main>

    <StatusBar health={health} validationMessage={validationMessage} hash={inputHash} gpu={gpuExposed ? 'GPU_DISK · 可选择' : gpuStatus === 'unverified-outside-sandbox' ? '设备不可用' : gpuStatus} engine={runEngine} stepEngine={selectedStepEngine} stepCount={runInfo?.completedStepIds.length ?? project.steps.length} stepLimit={capabilities?.resourcePolicy?.run?.maximumSteps ?? null} elapsed={runInfo ? `${runInfo.elapsedSeconds.toFixed(1)} s` : '—'} runId={displayRunId ?? ''} warnings={warnings} />

    {notice && <div className="toast" role="status"><span>{notice}</span><button aria-label="关闭提示" onClick={() => setNotice('')}>×</button></div>}
    {deleteMaterialCandidate && <DeleteMaterialDialog material={deleteMaterialCandidate} busy={deletingMaterial === deleteMaterialCandidate.name} error={deleteMaterialError}
      onClose={() => { if (!deletingMaterial) { setDeleteMaterialCandidate(null); setDeleteMaterialError(''); } }}
      onConfirm={() => void deleteCustomMaterial(deleteMaterialCandidate)} />}
    {runErrorDialog && <RunErrorDialog {...runErrorDialog}
      onClose={() => setRunErrorDialog(null)}
      onViewLog={() => { setRunErrorDialog(null); setDockTab('log'); setDockOpen(true); }} />}
  </div>;
}
