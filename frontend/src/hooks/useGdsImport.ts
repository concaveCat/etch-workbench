import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { api, prettyError } from '../api/client';
import type { GDSAsset, GDSGeometry, Layer, MaterialCatalogItem, Project, StackGeometry } from '../api/types';
import type { Activity } from '../app/types';
import { gdsLayerIds, gdsMaskPairs, gdsPairs } from '../lib/gds';
import { defaultProject, initialVisibleMaterialIds, promoteProjectToV7 } from '../project/defaults';
import { defaultView3D } from '../types3d';

// GDS import popover: the uploaded asset, the pending (not yet imported)
// mask settings, and the handlers that read/write them. `project` stays the
// source of truth for the destination project, so every write goes through
// the App-owned setters passed in here.
export type GdsImportDeps = {
  project: Project;
  materials: MaterialCatalogItem[];
  setProject: Dispatch<SetStateAction<Project>>;
  setProjectId: Dispatch<SetStateAction<string | null>>;
  setSelectedStep: Dispatch<SetStateAction<string | null>>;
  setNotice: Dispatch<SetStateAction<string>>;
  setLoading: Dispatch<SetStateAction<boolean>>;
  setActivity: Dispatch<SetStateAction<Activity>>;
};

export function useGdsImport({ project, materials, setProject, setProjectId, setSelectedStep, setNotice, setLoading, setActivity }: GdsImportDeps) {
  const [gdsAsset, setGdsAsset] = useState<GDSAsset | null>(null);
  const [gdsInspection, setGdsInspection] = useState<Record<string, unknown> | null>(null);
  const [gdsImportOpen, setGdsImportOpen] = useState(false);
  const [gdsImportPosition, setGdsImportPosition] = useState<{ top: number; left: number; side: 'right' | 'left' } | null>(null);
  const [pendingGdsAsset, setPendingGdsAsset] = useState<GDSAsset | null>(null);
  const [pendingGdsStructure, setPendingGdsStructure] = useState('');
  const [pendingGdsLayerDatatype, setPendingGdsLayerDatatype] = useState('');
  const [gdsImportMode, setGdsImportMode] = useState<'etch-mask' | 'material-geometry'>('etch-mask');
  const [pendingMaskName, setPendingMaskName] = useState('GDS 刻蚀掩膜');
  const [pendingMaskMaterial, setPendingMaskMaterial] = useState('Mask');
  const [pendingMaskThickness, setPendingMaskThickness] = useState(0.3);
  const [pendingGdsScale, setPendingGdsScale] = useState(1);
  const [pendingMaskOffset, setPendingMaskOffset] = useState<[number, number]>([0, 0]);
  const [pendingMaskPolarity, setPendingMaskPolarity] = useState<'inside' | 'outside'>('inside');
  const [pendingProtectsEtch, setPendingProtectsEtch] = useState(true);
  const [gdsImportError, setGdsImportError] = useState('');
  const gdsInputRef = useRef<HTMLInputElement>(null);
  const gdsImportButtonRef = useRef<HTMLButtonElement>(null);
  const gdsImportCardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!gdsImportOpen) return undefined;
    const updatePosition = () => {
      const anchor = gdsImportButtonRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const cardWidth = Math.min(360, Math.max(280, window.innerWidth - 24));
      const openRight = anchor.right + 12 + cardWidth <= window.innerWidth - 12;
      const left = openRight ? anchor.right + 12 : Math.max(12, anchor.left - cardWidth - 12);
      const maxTop = Math.max(12, window.innerHeight - 480);
      setGdsImportPosition({ top: Math.max(12, Math.min(anchor.top - 4, maxTop)), left, side: openRight ? 'right' : 'left' });
    };
    updatePosition();
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true);
    return () => {
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [gdsImportOpen]);

  const loadGdsAsset = async (assetId: string) => {
    const asset = await api<GDSAsset>(`/api/gds-assets/${assetId}`);
    setGdsAsset(asset);
    return asset;
  };

  const uploadGdsFile = async (file: File) => {
    setLoading(true); setNotice(''); setGdsImportError('');
    try {
      const response = await fetch(`/api/gds-assets?file_name=${encodeURIComponent(file.name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
      });
      const result = await response.json() as GDSAsset | { detail?: unknown };
      if (!response.ok) {
        const detail = 'detail' in result ? result.detail : undefined;
        throw new Error(typeof detail === 'string' ? detail : `GDS 上传失败：HTTP ${response.status}`);
      }
      const asset = result as GDSAsset;
      const firstUsableStructure = asset.structures.find(item => gdsMaskPairs(asset, item.name).length > 0)?.name
        ?? asset.structures.find(item => gdsLayerIds(asset, item.name).length > 0)?.name;
      if (!firstUsableStructure) throw new Error('GDS 文件不包含可用的结构单元或图层。');
      const firstPair = (gdsImportMode === 'etch-mask' ? gdsMaskPairs(asset, firstUsableStructure) : gdsPairs(asset, firstUsableStructure))[0];
      setPendingGdsScale(1);
      setPendingMaskOffset([0, 0]);
      setPendingGdsAsset(asset);
      setPendingGdsStructure(firstUsableStructure);
      setPendingGdsLayerDatatype(firstPair ? `${firstPair[0]}/${firstPair[1]}` : '');
      setGdsImportError('');
      setNotice(gdsMaskPairs(asset, firstUsableStructure).length
        ? '已读取 GDS 文件。掩膜列表只显示含闭合边界多边形的 layer/datatype 组合。'
        : '已读取 GDS 文件，但没有发现闭合边界多边形；请切换到高级材料几何导入，或选择其他 GDS 文件。');
    } catch (error) { setGdsImportError(prettyError(error)); }
    finally { setLoading(false); if (gdsInputRef.current) gdsInputRef.current.value = ''; }
  };

  const makeGdsMaskProject = (
    baseProject: Project,
    source: GDSGeometry,
    pair: [number, number],
    settings: { maskName: string; maskMaterial: string; maskThickness: number; polarity: 'inside' | 'outside'; protectsEtch: boolean },
  ): Project => {
    const existingStack = baseProject.geometry.kind === 'stack'
      ? baseProject.geometry
      : { kind: 'stack', substrateMaterial: 'Si', baseHeight: 0, layers: [], pattern: { kind: 'none' } } as StackGeometry;
    const layers = existingStack.layers.filter(layer => layer.source !== 'gds-mask');
    const maskLayer: Layer = {
      id: `gds_mask_${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`,
      name: settings.maskName.trim() || 'GDS 掩膜', material: settings.maskMaterial,
      thickness: settings.maskThickness, role: 'mask', source: 'gds-mask',
    };
    const stack: StackGeometry = {
      ...existingStack,
      layers: [...layers, maskLayer],
      pattern: { kind: 'gds' },
      gdsMask: {
        name: maskLayer.name,
        assetId: source.assetId, fileName: source.fileName, structure: source.structure,
        layer: pair[0], datatype: pair[1], unitScale: source.unitScale, position: source.position,
        baseHeight: existingStack.baseHeight + layers.reduce((sum, layer) => sum + layer.thickness, 0),
        height: maskLayer.thickness, material: maskLayer.material,
        retainedRegion: settings.polarity, protectsEtch: settings.protectsEtch,
      },
    };
    const next: Project = {
      ...baseProject,
      schemaVersion: 7,
      dimension: 3,
      domain: { ...baseProject.domain, yExtent: baseProject.domain.yExtent ?? baseProject.domain.xExtent, boundaryXY: baseProject.domain.boundaryXY ?? 'reflective' },
      geometry: stack,
      outputSettings: { ...(baseProject.outputSettings ?? defaultProject.outputSettings!), volumeMesh: true },
      view3D: { ...structuredClone(defaultView3D), visibleMaterialIds: initialVisibleMaterialIds(materials, stack) },
    };
    delete next.view;
    return next;
  };

  const importPendingGds = async () => {
    if (!pendingGdsAsset || !pendingGdsStructure) return;
    if (gdsImportMode === 'etch-mask' && project.dimension !== 3) {
      setGdsImportError('GDS 掩膜刻蚀需要 3D 工程。请先将工程切换到 3D 模式，再导入掩膜。');
      return;
    }
    const layerIds = gdsLayerIds(pendingGdsAsset, pendingGdsStructure);
    if (!layerIds.length) {
      setGdsImportError('所选结构没有可映射的图层，请换一个结构单元。');
      return;
    }
    const defaultMaterial = materials.find(item => item.name === 'SiO2')?.name ?? materials[0]?.name ?? 'Si';
    const projectV7 = promoteProjectToV7(project);
    const gdsGeometry: GDSGeometry = {
      kind: 'gds', assetId: pendingGdsAsset.assetId, fileName: pendingGdsAsset.fileName, structure: pendingGdsStructure,
      position: gdsImportMode === 'etch-mask' ? [pendingMaskOffset[0], pendingMaskOffset[1], 0] : [0, 0, 0],
      unitScale: gdsImportMode === 'etch-mask' ? pendingGdsScale : 1,
      layers: layerIds.slice(0, 64).map(layer => ({
        layer, material: defaultMaterial,
        ...(project.dimension === 3 ? { baseHeight: 0, height: Math.max(0.5, 2 * project.domain.gridDelta), mask: false } : {}),
      })),
    };
    let importedProject: Project = { ...projectV7, geometry: gdsGeometry };
    let nativeMaskInspection: Record<string, unknown> | null = null;
    if (gdsImportMode === 'etch-mask') {
      const pair = gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).find(([layer, datatype]) => `${layer}/${datatype}` === pendingGdsLayerDatatype);
      if (!pair) {
        setGdsImportError('请选择一个有效的 GDS 图层 / datatype 组合。');
        return;
      }
      if (!pendingMaskName.trim() || !Number.isFinite(pendingGdsScale) || pendingGdsScale <= 0 || pendingGdsScale > 1_000_000
          || !Number.isFinite(pendingMaskThickness) || pendingMaskThickness < 2 * project.domain.gridDelta || pendingMaskThickness > 100
          || pendingMaskOffset.some(value => !Number.isFinite(value) || Math.abs(value) > 40)) {
        setGdsImportError('请检查版图比例、偏移和掩膜厚度。掩膜厚度需至少跨越两个网格间距。');
        return;
      }
      importedProject = makeGdsMaskProject(projectV7, gdsGeometry, pair, {
        maskName: pendingMaskName,
        maskMaterial: pendingMaskMaterial, maskThickness: pendingMaskThickness,
        polarity: pendingMaskPolarity, protectsEtch: pendingProtectsEtch,
      });
    }
    setLoading(true); setGdsImportError('');
    try {
      if (gdsImportMode === 'etch-mask') {
        const pair = gdsMaskPairs(pendingGdsAsset, pendingGdsStructure).find(([layer, datatype]) => `${layer}/${datatype}` === pendingGdsLayerDatatype)!;
        const result = await api<{ inspection: Record<string, unknown> }>(
          `/api/gds-assets/${pendingGdsAsset.assetId}/inspect`, { method: 'POST', body: JSON.stringify({
            structure: pendingGdsStructure, selectedLayers: [pair[0]], selectedLayerDatatypes: [[pair[0], pair[1]]],
            dimension: 2, gridDelta: project.domain.gridDelta, xExtent: project.domain.xExtent,
            boundary: 'reflective', unitScale: pendingGdsScale, position: pendingMaskOffset,
          }) });
        const selectedNativeLayers = result.inspection.selectedNativeLayers as number[] | undefined;
        const pointCounts = result.inspection.levelSetPointCounts as Record<string, number> | undefined;
        if (!selectedNativeLayers?.includes(pair[0]) || !Number.isFinite(pointCounts?.[String(pair[0])]) || Number(pointCounts?.[String(pair[0])]) <= 0) {
          throw new Error(`Layer ${pair[0]} · datatype ${pair[1]} 没有生成可用的 ViennaPS 掩膜网格，请选择含闭合版图图形的组合。`);
        }
        nativeMaskInspection = result.inspection;
      }
      await api('/api/projects/validate', { method: 'POST', body: JSON.stringify(importedProject) });
    } catch (error) {
      const message = prettyError(error);
      setGdsImportError(message.includes('native GDS layer readback') || message.includes('level-set')
        ? `ViennaPS 无法从所选 layer/datatype 读取掩膜图形，请换一个含闭合版图图形的组合。${message}`
        : message);
      return;
    } finally { setLoading(false); }
    setGdsAsset(pendingGdsAsset); setGdsInspection(null); setProject(importedProject); setProjectId(null);
    if (gdsImportMode === 'etch-mask') { setSelectedStep(importedProject.steps[0]?.id ?? null); setActivity('structure'); }
    setGdsImportOpen(false); setPendingGdsAsset(null); setPendingGdsStructure(''); setGdsImportError('');
    setPendingGdsLayerDatatype('');
    setNotice(gdsImportMode === 'etch-mask'
      ? `已导入 ${pendingGdsAsset.fileName} · ${pendingGdsStructure}，并将“${pendingMaskName.trim()}”加入“多层”顶层掩膜。ViennaPS 原生检查通过：所选掩膜网格点数 ${JSON.stringify(nativeMaskInspection?.levelSetPointCounts)}。${projectV7.geometry.kind === 'stack' ? '现有多层和工艺步骤已保留。' : '原工程没有多层结构，已建立最小 Si 基底；请在“多层”中确认基底并添加需要刻蚀的薄膜。'} 请生成结构预览并核对。`
      : `已导入 ${pendingGdsAsset.fileName} · ${pendingGdsStructure}。请为可用图层指定材料，再运行 ViennaPS 原生预览。${layerIds.length > 64 ? ' 默认只选前 64 个图层，最多映射 64 层。' : ''}`);
  };

  const startGdsMaskTransfer = () => {
    if (project.geometry.kind !== 'gds' || !gdsAsset) return;
    const structure = project.geometry.structure;
    const pair = gdsMaskPairs(gdsAsset, structure)[0] ?? gdsPairs(gdsAsset, structure)[0];
    if (!pair) { setNotice('所选结构没有可用的 layer/datatype 图形。'); return; }
    setPendingGdsAsset(gdsAsset);
    setPendingGdsStructure(structure);
    setPendingGdsScale(project.geometry.unitScale);
    setPendingMaskOffset([project.geometry.position[0], project.geometry.position[1]]);
    setPendingGdsLayerDatatype(`${pair[0]}/${pair[1]}`);
    setGdsImportMode('etch-mask');
    setGdsImportError('');
    setGdsImportOpen(true);
    setNotice('请选择 GDS 掩膜图层。导入后掩膜会作为“多层”的顶层图案；基底、薄膜和刻蚀步骤由当前项目设置。');
  };

  const inspectGds = async () => {
    if (project.geometry.kind !== 'gds') return;
    const geometry = project.geometry;
    setLoading(true); setGdsInspection(null); setNotice('正在 bounded_child.py 保护下读取 ViennaPS GDSGeometry…');
    try {
      const result = await api<{ inspection: Record<string, unknown>; boundedChild: Record<string, unknown> }>(
        `/api/gds-assets/${geometry.assetId}/inspect`, { method: 'POST', body: JSON.stringify({
          structure: geometry.structure, selectedLayers: geometry.layers.map(layer => layer.layer),
          dimension: project.dimension, gridDelta: project.domain.gridDelta,
          xExtent: project.domain.xExtent, ...(project.dimension === 3 ? { yExtent: project.domain.yExtent } : {}),
          boundary: project.dimension === 3 ? project.domain.boundaryXY : project.domain.xBoundary,
          unitScale: geometry.unitScale, position: geometry.position.slice(0, 2),
          probeHeight: Math.max(project.domain.gridDelta * 2, geometry.layers.find(layer => layer.height)?.height ?? 1),
        }) });
      setGdsInspection(result.inspection);
      setNotice(`ViennaPS 原生结构检查完成：${geometry.structure}，已选图层 ${JSON.stringify(result.inspection.selectedNativeLayers)}。`);
    } catch (error) { setNotice(prettyError(error)); }
    finally { setLoading(false); }
  };

  const gdsAvailableLayers = project.geometry.kind === 'gds' && gdsAsset
    ? gdsLayerIds(gdsAsset, project.geometry.structure) : [];

  return {
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
  };
}
