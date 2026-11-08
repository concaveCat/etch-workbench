import { useEffect, useMemo, useRef, useState } from 'react';
import '@kitware/vtk.js/Rendering/Profiles/Geometry';
import vtkGenericRenderWindow from '@kitware/vtk.js/Rendering/Misc/GenericRenderWindow';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane';
import vtkAxesActor from '@kitware/vtk.js/Rendering/Core/AxesActor';
import vtkOrientationMarkerWidget from '@kitware/vtk.js/Interaction/Widgets/OrientationMarkerWidget';
import { axisIndex, cutTriangles } from './section3d';
import type { Axis3D, Surface3D, Vec3, View3D } from './types3d';
import { parseNativeVtu, type NativeVolume3D } from './parseNativeVtu';
import { cutVolumeTetrahedra, volumeMaterialSurfaces } from './volumePreview3d';

const rgb = (hex: string): [number, number, number] => { const h = hex.replace('#', ''); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255) as [number, number, number]; };
type Scene = { window: ReturnType<typeof vtkGenericRenderWindow.newInstance>; plane: ReturnType<typeof vtkPlane.newInstance>; actors: { actor: ReturnType<typeof vtkActor.newInstance>; mapper: ReturnType<typeof vtkMapper.newInstance>; data: ReturnType<typeof vtkPolyData.newInstance>; materialId: number; comparison: boolean }[] };
type VolumeLoad = { status: 'idle' | 'loading' | 'ready' | 'missing' | 'error'; volume?: NativeVolume3D; error?: string };
const volumeKey = (mesh: Surface3D | null) => mesh ? `${mesh.runId}:${mesh.stepId}` : '';
const volumeOutput = (mesh: Surface3D | null) => mesh?.outputs?.find(output => output.kind === 'volumeMesh' && output.format === 'VTU');
function axisBounds(mesh: Surface3D, volume: NativeVolume3D | undefined, axis: number): [number, number] {
  const points = volume?.points ?? mesh.points;
  let min = Infinity, max = -Infinity;
  for (const point of points) { min = Math.min(min, point[axis]); max = Math.max(max, point[axis]); }
  return Number.isFinite(min) && Number.isFinite(max) ? [min, max] : [-5, 5];
}

function SectionPanel({ current, compare, currentVolume, compareVolume, view, catalog }: { current: Surface3D; compare: Surface3D | null; currentVolume?: NativeVolume3D; compareVolume?: NativeVolume3D; view: View3D; catalog: { id: number; name: string; color: string }[] }) {
  const axis = view.section.axis;
  const segments = useMemo(() => [current, compare].map(mesh => mesh ? cutTriangles(mesh, axis, view.section.position) : []), [current, compare, axis, view.section.position]);
  const volumeSections = useMemo(() => [currentVolume, compareVolume].map(volume => volume ? cutVolumeTetrahedra(volume, axisIndex(axis), view.section.position) : []), [currentVolume, compareVolume, axis, view.section.position]);
  const axes = axis === 'x' ? [1, 2] : axis === 'y' ? [0, 2] : [0, 1];
  const extent = useMemo(() => {
    const min = [Infinity, Infinity], max = [-Infinity, -Infinity];
    const sources = [currentVolume?.points ?? current.points, compare ? compareVolume?.points ?? compare.points : []];
    for (const points of sources) for (const p of points) axes.forEach((a, i) => { min[i] = Math.min(min[i], p[a]); max[i] = Math.max(max[i], p[a]); });
    if (!Number.isFinite(min[0]) || !Number.isFinite(min[1])) return { min: [-1, -1], max: [1, 1] };
    return { min, max };
  }, [current, compare, currentVolume, compareVolume, axis]);
  const scale = Math.min(680 / Math.max(.01, extent.max[0] - extent.min[0]), 200 / Math.max(.01, extent.max[1] - extent.min[1]));
  const xy = (p: Vec3) => [400 + (p[axes[0]] - (extent.max[0] + extent.min[0]) / 2) * scale, 130 - (p[axes[1]] - (extent.max[1] + extent.min[1]) / 2) * scale];
  const materialIds = [...new Set([...segments.flat().map(s => s.materialId), ...volumeSections.flat().map(p => p.materialId)])];
  return <div className="section3d" data-testid="section-panel"><strong>源自三维网格 · {axis.toUpperCase()}={view.section.position} µm · 等比例截面</strong>
    <svg viewBox="0 0 800 280" preserveAspectRatio="xMidYMid meet" role="img" aria-label="真实三角网格截面">
      <rect width="800" height="280" fill="#f9fbfd" />
      {volumeSections.map((list, group) => list.filter(p => (current.schemaVersion < 3 && !view.visibleMaterialIds.length) || view.visibleMaterialIds.includes(p.materialId)).map((polygon, index) => <polygon key={`volume-${group}-${index}`} points={polygon.points.map(p => xy(p).join(',')).join(' ')} fill={current.materials?.[String(polygon.materialId)]?.color ?? compare?.materials?.[String(polygon.materialId)]?.color ?? catalog.find(m=>m.id===polygon.materialId)?.color ?? '#8064a2'} opacity={(view.opacityByMaterial[String(polygon.materialId)] ?? 1) * (group ? .3 : .68)} />))}
      {segments.map((list, group) => materialIds.filter(id => (current.schemaVersion < 3 && !view.visibleMaterialIds.length) || view.visibleMaterialIds.includes(id)).map(id => <path key={`${group}-${id}`} data-material={id} fill="none" stroke={current.materials?.[String(id)]?.color ?? catalog.find(m=>m.id===id)?.color ?? '#8064a2'} strokeWidth="1.5" opacity={(view.opacityByMaterial[String(id)] ?? 1) * (group ? .45 : 1)} strokeDasharray={group ? '5 3' : undefined}
        d={list.filter(s => s.materialId === id).map(s => { const a = xy(s.a), b = xy(s.b); return `M${a[0]},${a[1]}L${b[0]},${b[1]}`; }).join('')} />))}
      <text x="15" y="22">{['x', 'y', 'z'][axes[1]]} / µm</text><text x="725" y="270">{['x', 'y', 'z'][axes[0]]} / µm</text>
      <line x1="25" y1="250" x2={25 + scale} y2="250" stroke="#27364b" /><text x="25" y="271">1 µm</text>
    </svg><small>{currentVolume ? '填充区域来自 ViennaPS 原生四面体材料单元；线条来自原生界面网格。' : '当前快照没有原生体网格，仅显示离散表面与平面的交线。'} {segments.flat().length} 条界面交线。</small>
  </div>;
}

export default function Viewer3D({ current, compare, view, onViewChange, onError, catalog = [] }: { current: Surface3D; compare: Surface3D | null; view: View3D; onViewChange: (view: View3D) => void; onError: (message: string) => void; catalog?: { id: number; name: string; color: string }[] }) {
  const container = useRef<HTMLDivElement>(null), scene = useRef<Scene | null>(null);
  const stateRef = useRef(view), changeRef = useRef(onViewChange), errorRef = useRef(onError);
  stateRef.current = view; changeRef.current = onViewChange; errorRef.current = onError;
  const [revision, setRevision] = useState(0), [ready, setReady] = useState(false);
  const [volumes, setVolumes] = useState<Record<string, VolumeLoad>>({});
  const volumesRef = useRef(volumes);
  volumesRef.current = volumes;
  const volumeModeActive = view.displayMode === 'wireframe' || view.displayMode === 'surfaceEdges';
  const currentKey = volumeKey(current), compareKey = volumeKey(compare);
  const currentVolume = volumes[currentKey];
  const compareVolume = compareKey ? volumes[compareKey] : undefined;
  const volumeLoading = volumeModeActive && (
    (!!volumeOutput(current) && currentVolume?.status !== 'ready' && currentVolume?.status !== 'error' && currentVolume?.status !== 'missing') ||
    (!!compare && !!volumeOutput(compare) && compareVolume?.status !== 'ready' && compareVolume?.status !== 'error' && compareVolume?.status !== 'missing')
  );
  const volumeStatusMessage = !volumeModeActive
    ? current.materialSurfaceStatus === 'closed'
      ? '当前显示仿真后处理按原生四面体邻接关系提取的封闭材料壳面，无需读取 VTU。线框或表面带边线模式才按需读取体网格。'
      : `当前表面模式直接显示 ViennaPS 原生界面${current.materialSurfaceError ? `；封闭壳面提取失败：${current.materialSurfaceError}` : ''}。线框或表面带边线模式才按需读取体网格。`
    : currentVolume?.status === 'ready'
      ? '体积预览使用 ViennaPS 原生 VTU 四面体与材料 ID；封闭范围遵从该网格。z 计算边界仍为无限，底部只表示原生体网格的可视化范围；裁剪面不会另造封口。'
      : currentVolume?.status === 'error'
        ? `体积网格读取失败，暂显示${current.materialSurfaceStatus === 'closed' ? '预计算的封闭材料壳面' : '原始界面表面'}：${currentVolume.error}`
        : volumeOutput(current)
          ? '正在读取 ViennaPS 原生体网格…'
          : `此快照未包含 ViennaPS 原生体网格，当前显示${current.materialSurfaceStatus === 'closed' ? '预计算的封闭材料壳面' : '原始界面表面'}。`;
  const materialIds = useMemo(() => [...new Set([
    ...(current.cellData.materialIds ?? []), ...(currentVolume?.volume?.materialIds ?? []),
    ...(compare?.cellData.materialIds ?? []), ...(compareVolume?.volume?.materialIds ?? []),
  ])].sort((a, b) => a - b), [current, compare, currentVolume?.volume, compareVolume?.volume]);
  useEffect(() => {
    const uniqueMeshes = new Map<string, Surface3D>();
    for (const mesh of [current, compare]) if (mesh) uniqueMeshes.set(volumeKey(mesh), mesh);
    const meshes = [...uniqueMeshes.values()];
    const cachedVolumes = volumesRef.current;
    let active = true;
    const controller = new AbortController();
    if (!volumeModeActive) {
      setVolumes(Object.fromEntries(meshes.map(mesh => {
        const key = volumeKey(mesh), cached = cachedVolumes[key];
        return [key, cached?.status === 'ready' ? cached : { status: 'idle' } as VolumeLoad];
      })));
      return () => { active = false; controller.abort(); };
    }
    const requests = meshes.map(async mesh => {
      const key = volumeKey(mesh), artifact = volumeOutput(mesh);
      if (!artifact) return [key, { status: 'missing' } as VolumeLoad] as const;
      const cached = cachedVolumes[key];
      if (cached?.status === 'ready' && cached.volume) return [key, cached] as const;
      try {
        const response = await fetch(artifact.url, { signal: controller.signal });
        if (!response.ok) throw new Error(`体网格读取失败（HTTP ${response.status}）`);
        const volume = await parseNativeVtu(await response.text());
        return [key, { status: 'ready', volume } as VolumeLoad] as const;
      } catch (error) {
        return [key, { status: 'error', error: error instanceof Error ? error.message : String(error) } as VolumeLoad] as const;
      }
    });
    const nextLoads = Object.fromEntries(meshes.map(mesh => {
      const key = volumeKey(mesh), artifact = volumeOutput(mesh), cached = cachedVolumes[key];
      return [key, cached?.status === 'ready' && cached.volume
        ? cached
        : { status: artifact ? 'loading' : 'missing' } as VolumeLoad];
    }));
    setVolumes(nextLoads);
    Promise.all(requests).then(rows => {
      if (!active) return;
      setVolumes(previous => ({ ...previous, ...Object.fromEntries(rows) }));
    });
    return () => { active = false; controller.abort(); };
  }, [current, compare, volumeModeActive]);
  const captureCamera = () => {
    const camera = scene.current?.window.getRenderer().getActiveCamera();
    if (camera) changeRef.current({ ...stateRef.current, camera: { position: [...camera.getPosition()] as Vec3, focalPoint: [...camera.getFocalPoint()] as Vec3, viewUp: [...camera.getViewUp()] as Vec3, parallelScale: camera.getParallelScale() } });
  };
  useEffect(() => {
    if (!container.current) return;
    const win = vtkGenericRenderWindow.newInstance({ background: [.96, .975, .99] });
    win.setContainer(container.current);
    const plane = vtkPlane.newInstance(), axes = vtkAxesActor.newInstance();
    const widget = vtkOrientationMarkerWidget.newInstance({ actor: axes, interactor: win.getInteractor(), parentRenderer: win.getRenderer() });
    widget.setEnabled(true); widget.setViewportSize(.13); widget.setMinPixelSize(70); widget.setMaxPixelSize(120);
    scene.current = { window: win, plane, actors: [] };
    const subscription = win.getInteractor().onEndAnimation(captureCamera);
    const resize = new ResizeObserver(() => { win.resize(); win.getRenderWindow().render(); }); resize.observe(container.current);
    win.resize(); setReady(true);
    return () => {
      subscription.unsubscribe(); resize.disconnect(); widget.setEnabled(false); widget.delete(); axes.delete();
      for (const { actor, mapper, data } of scene.current?.actors ?? []) { win.getRenderer().removeActor(actor); actor.delete(); mapper.delete(); data.delete(); }
      plane.delete();
      const renderer = win.getRenderer(), renderWindow = win.getRenderWindow(), interactor = win.getInteractor(), style = interactor.getInteractorStyle();
      win.delete(); interactor.delete(); style.delete(); renderer.delete(); renderWindow.delete(); scene.current = null;
    };
  }, []);
  useEffect(() => {
    const s = scene.current; if (!s || !ready) return;
    for (const { actor, mapper, data } of s.actors) { s.window.getRenderer().removeActor(actor); actor.delete(); mapper.delete(); data.delete(); }
    s.actors = [];
    const loadingCurrentVolume = volumeModeActive && !!volumeOutput(current) && currentVolume?.status !== 'ready' && currentVolume?.status !== 'error';
    const loadingCompareVolume = volumeModeActive && !!compare && !!volumeOutput(compare) && compareVolume?.status !== 'ready' && compareVolume?.status !== 'error';
    if (loadingCurrentVolume || loadingCompareVolume) return;
    try {
      for (const [index, mesh] of [current, compare].entries()) {
        if (!mesh) continue;
        const volume = volumeModeActive ? (index === 0 ? currentVolume?.volume : compareVolume?.volume) : undefined;
        const sourcePoints: number[][] = volume?.points ?? mesh.points;
        const materialSurfaces = volume ? volumeMaterialSurfaces(volume) : null;
        if (!materialSurfaces && (!mesh.triangles || mesh.cellData.materialIds.length !== mesh.triangles.length)) throw new Error('三维交互网格材料数据不匹配');
        const cellsByMaterial = materialSurfaces ?? new Map<number, number[][]>();
        if (!materialSurfaces) {
          mesh.triangles!.forEach((triangle, triangleIndex) => {
            const materialId = mesh.cellData.materialIds[triangleIndex];
            const cells = cellsByMaterial.get(materialId) ?? [];
            cells.push(triangle);
            cellsByMaterial.set(materialId, cells);
          });
        }
        const pointBuffer = new Float64Array(sourcePoints.length * 3);
        sourcePoints.forEach((point, pointIndex) => pointBuffer.set(point, pointIndex * 3));
        for (const [id, triangles] of cellsByMaterial) {
          const data = vtkPolyData.newInstance();
          data.getPoints().setData(pointBuffer, 3);
          const cells: number[] = [];
          triangles.forEach(triangle => cells.push(3, ...triangle));
          data.getPolys().setData(new Uint32Array(cells));
          const mapper = vtkMapper.newInstance(); mapper.setInputData(data); mapper.setScalarVisibility(false);
          const actor = vtkActor.newInstance(); actor.setMapper(mapper); actor.getProperty().setColor(...rgb(mesh.materials?.[String(id)]?.color ?? catalog.find(m=>m.id===id)?.color ?? '#8064a2'));
          s.window.getRenderer().addActor(actor); s.actors.push({ actor, mapper, data, materialId: id, comparison: index === 1 });
        }
      }
      if (!stateRef.current.camera) {
        const c = s.window.getRenderer().getActiveCamera(); c.setPosition(8, -10, 8); c.setFocalPoint(0, 0, 0); c.setViewUp(0, 0, 1);
        s.window.getRenderer().resetCamera(); captureCamera();
      }
      setRevision(n => n + 1);
    } catch (error) { errorRef.current(error instanceof Error ? error.message : String(error)); }
  }, [current, compare, currentVolume, compareVolume, ready, catalog, volumeModeActive]);
  useEffect(() => {
    const s = scene.current; if (!s) return;
    const c = s.window.getRenderer().getActiveCamera(); c.setParallelProjection(view.projection === 'parallel');
    if (view.camera) { c.setPosition(...view.camera.position); c.setFocalPoint(...view.camera.focalPoint); c.setViewUp(...view.camera.viewUp); c.setParallelScale(view.camera.parallelScale); }
    const normal: Vec3 = [0, 0, 0]; normal[axisIndex(view.clip.axis)] = view.clip.keep === 'positive' ? 1 : -1;
    const origin: Vec3 = [0, 0, 0]; origin[axisIndex(view.clip.axis)] = view.clip.position;
    s.plane.setNormal(...normal); s.plane.setOrigin(...origin);
    for (const { actor, mapper, materialId, comparison } of s.actors) {
      actor.setVisibility((current.schemaVersion < 3 && !view.visibleMaterialIds.length) || view.visibleMaterialIds.includes(materialId));
      actor.getProperty().setOpacity((view.opacityByMaterial[String(materialId)] ?? 1) * (comparison ? .35 : 1));
      actor.getProperty().setRepresentation(view.displayMode === 'wireframe' ? 1 : 2);
      actor.getProperty().setEdgeVisibility(comparison || view.displayMode === 'surfaceEdges');
      mapper.removeAllClippingPlanes(); if (view.clip.enabled) mapper.addClippingPlane(s.plane);
    }
    s.window.getRenderer().resetCameraClippingRange(); s.window.getRenderWindow().render();
  }, [view, revision]);
  const standardView = (axis?: Axis3D) => {
    const s = scene.current; if (!s) return;
    const renderer = s.window.getRenderer(), c = renderer.getActiveCamera();
    c.setFocalPoint(0, 0, 0); c.setPosition(...(axis === 'z' ? [0, 0, 10] : axis === 'y' ? [0, -10, 0] : axis === 'x' ? [10, 0, 0] : [8, -10, 8]) as Vec3);
    c.setViewUp(...(axis === 'z' ? [0, 1, 0] : [0, 0, 1]) as Vec3);
    renderer.resetCamera(); renderer.resetCameraClippingRange(); s.window.getRenderWindow().render(); captureCamera();
  };
  const png = async () => {
    try {
      const s = scene.current; if (!s) return;
      const image = s.window.getApiSpecificRenderWindow().captureNextImage('image/png'); s.window.getRenderWindow().render();
      const uri = image ? await image : null; if (!uri) throw new Error('当前渲染后端未返回 PNG');
      const a = document.createElement('a'); a.href = uri; a.download = `viennaps-3d-${current.runId}-${current.stepId}.png`; a.click();
    } catch (e) { onError(String(e)); }
  };
  const update = (part: Partial<View3D>) => onViewChange({ ...view, ...part });
  const planeControl = (key: 'clip' | 'section') => {
    const value = view[key], index = axisIndex(value.axis);
    const bounds = [axisBounds(current, currentVolume?.volume, index), ...(compare ? [axisBounds(compare, compareVolume?.volume, index)] : [])];
    const min = Math.min(...bounds.map(row => row[0]));
    const max = Math.max(...bounds.map(row => row[1]));
    return <div className="plane-control"><label><input type="checkbox" checked={value.enabled} onChange={e => update({ [key]: { ...value, enabled: e.target.checked } })} />{key === 'clip' ? '裁剪面' : '正交截面'}</label>
      <select aria-label={`${key}轴`} value={value.axis} onChange={e => update({ [key]: { ...value, axis: e.target.value } })}>{['x', 'y', 'z'].map(a => <option key={a}>{a}</option>)}</select>
      <input aria-label={`${key}位置滑块`} type="range" min={min} max={max === min ? min + .01 : max} step="any" value={value.position} onChange={e => update({ [key]: { ...value, position: +e.target.value } })} />
      <input aria-label={`${key}位置`} type="number" value={value.position} step="0.05" onChange={e => { const p = +e.target.value; if (Number.isFinite(p)) update({ [key]: { ...value, position: p } }); }} /><span>µm</span>
      {key === 'clip' && <select aria-label="保留侧" value={view.clip.keep} onChange={e => update({ clip: { ...view.clip, keep: e.target.value as 'positive' | 'negative' } })}><option value="positive">保留正侧</option><option value="negative">保留负侧</option></select>}
    </div>;
  };
  return <div className="viewer3d" data-testid="viewer-3d">
    <div className="view3d-controls"><button className="button" onClick={() => standardView()}>重置视图</button>{(['z', 'y', 'x'] as Axis3D[]).map((axis, i) => <button className="button" key={axis} onClick={() => standardView(axis)}>{['XY', 'XZ', 'YZ'][i]}</button>)}
      <select aria-label="投影" value={view.projection} onChange={e => update({ projection: e.target.value as View3D['projection'] })}><option value="parallel">正交投影</option><option value="perspective">透视投影</option></select>
      <select aria-label="显示模式" value={view.displayMode} onChange={e => update({ displayMode: e.target.value as View3D['displayMode'] })}><option value="surface">表面</option><option value="wireframe">线框</option><option value="surfaceEdges">表面带边线</option></select>
      {volumeLoading && <span className="volume-load-status" role="status" aria-live="polite">正在读取体网格…</span>}
      <button className="button" onClick={() => void png()}>⇩ PNG</button><small>拖动旋转 · Shift 拖动平移 · 滚轮缩放{compare ? ' · 比较使用共同坐标、相机与裁剪' : ''}</small>
    </div>
    <div className="vtk-canvas" ref={container} aria-label="ViennaPS 三维材料体积查看器" />
    <div className="material3d-controls">{materialIds.map(id => { const m=current.materials?.[String(id)] ?? compare?.materials?.[String(id)]; const name=m?.name ?? catalog.find(x=>x.id===id)?.name ?? `Material ${id}`; const checked=(current.schemaVersion < 3 && !view.visibleMaterialIds.length) || view.visibleMaterialIds.includes(id); return <label key={id}><input type="checkbox" aria-label={`显示 ${name}`} checked={checked} onChange={e => update({ visibleMaterialIds: e.target.checked ? [...new Set([...view.visibleMaterialIds, id])] : (view.visibleMaterialIds.length ? view.visibleMaterialIds : [...new Set(current.cellData.materialIds)]).filter(x => x !== id) })} />{name}<input aria-label={`${name} 透明度`} type="range" min="0" max="1" step=".05" value={view.opacityByMaterial[String(id)] ?? 1} onChange={e => update({ opacityByMaterial: { ...view.opacityByMaterial, [id]: +e.target.value } })} /></label>; })}</div>
    {planeControl('clip')}{planeControl('section')}<small className="clip-note">{volumeStatusMessage}</small>
    {view.section.enabled && <SectionPanel current={current} compare={compare} currentVolume={currentVolume?.volume} compareVolume={compareVolume?.volume} view={view} catalog={catalog} />}
  </div>;
}
