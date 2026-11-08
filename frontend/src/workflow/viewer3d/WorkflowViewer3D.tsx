/**
 * Fast, low-jank 3D process-structure viewer for ViennaPS run snapshots.
 *
 * Responsibilities:
 *  - load + parse the snapshot (worker, LRU cache, idle prefetch),
 *  - render 3D snapshots with vtk.js (one actor, cell scalars, LUT),
 *  - render 2D snapshots as a real canvas wireframe,
 *  - material legend with per-material visibility,
 *  - wireframe/surface, projection, clipping plane, reset view,
 *  - explicit loading / empty / error states with retry,
 *  - destroy all vtk resources on unmount.
 *
 * See README.md in this folder for the integration contract.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Viewer2D from './Viewer2D';
import { loadMesh, loadRunStepIds, prefetch, SnapshotUnavailableError } from './snapshotCache';
import { createVtkScene, type StandardView, type VtkScene } from './vtkScene';
import { readWebglRenderer, shortRendererName, type WebglRendererInfo } from './webglRenderer';
import type {
  ClipState,
  DisplayMode,
  ParsedMesh,
  Viewer3DHandle,
  Viewer3DStats,
  WorkflowViewer3DProps,
} from './types';
import './viewer3d.css';

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; mesh: ParsedMesh }
  | { status: 'error'; message: string }
  | { status: 'unavailable'; reason: string };

const AXIS_ORDER: Array<'x' | 'y' | 'z'> = ['x', 'y', 'z'];

/** `?debug=1` exposes the frame-rate counter without opening 性能. */
const DEBUG_FPS = typeof window !== 'undefined'
  && new URLSearchParams(window.location.search).get('debug') === '1';

/** Remember a dismissed software-rendering warning per renderer string. */
const RENDERER_WARNING_KEY = 'wb.viewer3d.dismissedRenderer';

const numberFormat = new Intl.NumberFormat('en-US');

function axisRange(mesh: ParsedMesh, axis: 'x' | 'y' | 'z'): [number, number] {
  const index = axis === 'x' ? 0 : axis === 'y' ? 1 : 2;
  return [mesh.bounds[index], mesh.bounds[index + 3]];
}

function materialIndexSet(mesh: ParsedMesh, hiddenIds: ReadonlySet<number>): Set<number> {
  const hidden = new Set<number>();
  if (hiddenIds.size === 0) return hidden;
  mesh.materials.forEach((material, index) => {
    if (hiddenIds.has(material.id)) hidden.add(index);
  });
  return hidden;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 1 / 2 / 5 × 10ⁿ, the usual choice for a readable scale bar. */
function niceLength(target: number): number {
  if (!Number.isFinite(target) || target <= 0) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(target)));
  const normalized = target / power;
  const factor = normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1;
  return factor * power;
}

function formatLength(value: number): string {
  if (!Number.isFinite(value)) return '—';
  if (value >= 1) return String(Number(value.toPrecision(3)));
  return String(Number(value.toPrecision(2)));
}

export default function WorkflowViewer3D({
  runId,
  stepId,
  dimension,
  snapshot = null,
  height = 420,
  stepIds,
  marker = null,
  onStepUnavailable,
  onStats,
  onSceneReady,
  className,
}: WorkflowViewer3DProps) {
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [retryToken, setRetryToken] = useState(0);
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<number>>(() => new Set<number>());
  const [displayMode, setDisplayMode] = useState<DisplayMode>('surface');
  const [projection, setProjection] = useState<'parallel' | 'perspective'>('parallel');
  const [standardView, setStandardView] = useState<StandardView>('iso');
  const [clip, setClip] = useState<ClipState>({ enabled: false, axis: 'x', position: 0, keep: 'positive' });
  /** Bumped after every clip change so the scene can re-fit the visible half. */
  const [clipFitToken, setClipFitToken] = useState(0);
  /** World units per CSS pixel, reported by the scene for the scale bar. */
  const [viewScale, setViewScale] = useState(0);
  // FPS is opt-in: a headless / software-GL session reports ~5 fps, which reads
  // as a defect instead of a measurement. Triangles / points / parse time stay
  // visible; `?debug=1` forces the FPS counter on.
  const [showStats, setShowStats] = useState(false);
  /** Skip the 500k-triangle preview budget and keep the raw simulation mesh. */
  const [fullResolution, setFullResolution] = useState(() => {
    try {
      return window.localStorage.getItem('wb.viewer3d.fullResolution') === '1';
    } catch {
      return false;
    }
  });
  const [fps, setFps] = useState(0);
  const [sceneVersion, setSceneVersion] = useState(0);
  const [sceneError, setSceneError] = useState<string | null>(null);
  /** WebGL backend of this browser; null when WebGL is unavailable. */
  const [rendererInfo, setRendererInfo] = useState<WebglRendererInfo | null>(null);
  /** Dismissal is remembered per renderer, so a GPU switch shows the hint again. */
  const [dismissedRenderer, setDismissedRenderer] = useState<string>(() => {
    try {
      return window.localStorage.getItem(RENDERER_WARNING_KEY) ?? '';
    } catch {
      return '';
    }
  });

  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<VtkScene | null>(null);
  const attachedRef = useRef<ParsedMesh | null>(null);
  const requestToken = useRef(0);
  const callbacks = useRef({ onStats, onStepUnavailable, onSceneReady });
  callbacks.current = { onStats, onStepUnavailable, onSceneReady };
  const modeRef = useRef({ displayMode, projection, clip });
  modeRef.current = { displayMode, projection, clip };

  const reportStats = useCallback((mesh: ParsedMesh, measuredFps?: number) => {
    const stats: Viewer3DStats = {
      triangles: mesh.triangleCount,
      points: mesh.pointCount,
      fps: measuredFps ?? sceneRef.current?.getFps() ?? 0,
      parseMs: Math.round(mesh.parseMs),
      stepId: mesh.stepId,
      simplified: mesh.simplified,
      sourceTriangles: mesh.sourceTriangles,
    };
    callbacks.current.onStats?.(stats);
  }, []);

  const markUnavailable = useCallback((reason: string) => {
    setState({ status: 'unavailable', reason });
    callbacks.current.onStepUnavailable?.(reason);
  }, []);

  // ------------------------------------------------------------ WebGL backend
  // Read once per mount: the renderer string never changes without a browser
  // restart, and creating a probe context per render would be wasteful.
  useEffect(() => {
    if (dimension !== 3) return;
    setRendererInfo(readWebglRenderer());
  }, [dimension]);

  const dismissRendererWarning = (): void => {
    if (!rendererInfo) return;
    setDismissedRenderer(rendererInfo.renderer);
    try {
      window.localStorage.setItem(RENDERER_WARNING_KEY, rendererInfo.renderer);
    } catch {
      /* private mode: the warning just reappears next time */
    }
  };
  const showRendererWarning = dimension === 3 && !!rendererInfo?.software
    && dismissedRenderer !== rendererInfo.renderer;
  const showRendererDebug = DEBUG_FPS || showStats;

  // ---------------------------------------------------------------- loading
  useEffect(() => {
    const token = ++requestToken.current;
    setState({ status: 'loading' });
    const request = snapshot ? { runId, stepId, snapshot } : { runId, stepId };
    // Full resolution is a 3D-only concept: 2D snapshots are already raw lines.
    const options = dimension === 3 && fullResolution ? { maxTriangles: Number.MAX_SAFE_INTEGER } : undefined;
    loadMesh({ ...request, options })
      .then((mesh) => {
        if (token !== requestToken.current) return;
        if (dimension === 3 && mesh.triangleCount === 0) {
          markUnavailable('该步骤没有三维三角网格，无法用三维视图显示');
          return;
        }
        if (dimension === 2 && mesh.lineCount === 0) {
          markUnavailable('该步骤没有二维界面线，无法用二维视图显示');
          return;
        }
        setState({ status: 'ready', mesh });
        // 3D reports again once the scene has uploaded the mesh (with fps).
        if (dimension === 2) reportStats(mesh);
      })
      .catch((error) => {
        if (token !== requestToken.current) return;
        if (error instanceof SnapshotUnavailableError && error.status === 404) {
          markUnavailable(error.message);
          return;
        }
        setState({ status: 'error', message: messageOf(error) });
      });
    return () => {
      // A newer request wins; stale results are ignored through the token.
      requestToken.current += 1;
    };
    // `snapshot` must be referentially stable; memoise it in the parent.
  }, [runId, stepId, snapshot, dimension, retryToken, markUnavailable, stepIds, reportStats, fullResolution]);

  // ------------------------------------------------------------- prefetching
  useEffect(() => {
    if (snapshot || state.status !== 'ready') return;
    let cancelled = false;
    const warm = async (): Promise<void> => {
      const list = stepIds ?? (await loadRunStepIds(runId));
      if (cancelled || list.length === 0) return;
      const index = list.indexOf(stepId);
      if (index < 0) return;
      for (const neighbour of [list[index - 1], list[index + 1]]) {
        if (neighbour && neighbour !== stepId) prefetch(runId, neighbour);
      }
    };
    void warm();
    return () => {
      cancelled = true;
    };
  }, [runId, stepId, snapshot, stepIds, state.status]);

  // ------------------------------------------------------------- vtk scene
  useEffect(() => {
    if (dimension !== 3) return;
    const container = containerRef.current;
    if (!container) return;
    let cancelled = false;
    let scene: VtkScene | null = null;
    attachedRef.current = null;
    createVtkScene(container)
      .then((created) => {
        if (cancelled) {
          created.destroy();
          return;
        }
        scene = created;
        sceneRef.current = created;
        created.setProjection(modeRef.current.projection);
        created.setDisplayMode(modeRef.current.displayMode);
        created.setClip(modeRef.current.clip);
        created.setScaleListener(setViewScale);
        const handle: Viewer3DHandle = {
          resetView: () => created.resetView(),
          setStandardView: (view: StandardView) => created.setStandardView(view),
          rotate: (azimuth, elevation) => created.rotate(azimuth, elevation),
          renderNow: () => created.renderNow(),
          readPixels: () => created.readPixels(),
          getFps: () => created.getFps(),
          getTriangleCount: () => created.getTriangleCount(),
          getPointCount: () => created.getPointCount(),
          getVisibleBounds: () => created.getVisibleBounds(),
        };
        callbacks.current.onSceneReady?.(handle);
        setSceneVersion((value) => value + 1);
      })
      .catch((error) => {
        if (cancelled) return;
        setSceneError(`三维引擎加载失败：${messageOf(error)}`);
      });
    return () => {
      cancelled = true;
      sceneRef.current = null;
      attachedRef.current = null;
      scene?.destroy();
    };
  }, [dimension]);

  // ----------------------------------------------------------- mesh binding
  useEffect(() => {
    if (dimension !== 3 || state.status !== 'ready') return;
    const scene = sceneRef.current;
    if (!scene) return;
    const mesh = state.mesh;
    const hidden = materialIndexSet(mesh, hiddenIds);
    if (attachedRef.current === mesh) {
      scene.setHidden(hidden);
      return;
    }
    attachedRef.current = mesh;
    scene.attach(mesh, hidden);
    scene.setDisplayMode(modeRef.current.displayMode);
    scene.setProjection(modeRef.current.projection);
    scene.setClip(modeRef.current.clip);
    // Keep the user's camera across steps, but recover if the new geometry no
    // longer fits the frame.
    scene.ensureVisible();
    reportStats(mesh);
    // Keep the clip slider inside the new geometry.
    setClip((previous) => {
      const [low, high] = axisRange(mesh, previous.axis);
      if (previous.position >= low && previous.position <= high) return previous;
      return { ...previous, position: (low + high) / 2 };
    });
  }, [state, hiddenIds, sceneVersion, dimension, reportStats]);

  useEffect(() => {
    sceneRef.current?.setDisplayMode(displayMode);
  }, [displayMode, sceneVersion]);

  useEffect(() => {
    sceneRef.current?.setProjection(projection);
  }, [projection, sceneVersion]);

  useEffect(() => {
    sceneRef.current?.setClip(clip);
  }, [clip, sceneVersion]);

  // Re-fit after a clip change: the visible half is smaller than the full box,
  // and leaving the old framing would shrink the section to a dot. The fit is
  // deferred one frame so it uses the stage size after the toolbar reflow.
  useEffect(() => {
    if (clipFitToken === 0) return;
    const frame = requestAnimationFrame(() => sceneRef.current?.resetView());
    return () => cancelAnimationFrame(frame);
  }, [clipFitToken]);

  useEffect(() => {
    sceneRef.current?.setMarker(marker);
  }, [marker, sceneVersion]);

  // ------------------------------------------------------------------ stats
  useEffect(() => {
    if ((!showStats && !DEBUG_FPS) || dimension !== 3 || state.status !== 'ready') {
      setFps(0);
      return;
    }
    const mesh = state.mesh;
    const timer = setInterval(() => {
      const measured = sceneRef.current?.getFps() ?? 0;
      setFps(measured);
      reportStats(mesh, measured);
    }, 500);
    return () => clearInterval(timer);
  }, [showStats, dimension, state, reportStats]);

  const ready = state.status === 'ready' ? state.mesh : null;
  const coordinateUnit = ready?.coordinateUnit === 'nm' ? 'nm' : 'µm';
  const counts = useMemo(() => {
    if (!ready) return null;
    if (ready.dimension === 3) {
      const perMaterial = new Array<number>(ready.materials.length).fill(0);
      for (let t = 0; t < ready.triangleCount; t++) {
        const index = ready.pointColor[ready.polys[t * 4 + 1]];
        if (index >= 0 && index < perMaterial.length) perMaterial[index] += 1;
      }
      return { perMaterial, unit: '面' };
    }
    const perMaterial = new Array<number>(ready.materials.length).fill(0);
    for (let i = 0; i < ready.lineCount; i++) {
      const index = ready.lineColor[i];
      if (index >= 0 && index < perMaterial.length) perMaterial[index] += 1;
    }
    return { perMaterial, unit: '段' };
  }, [ready]);

  const toggleMaterial = (id: number): void => {
    setHiddenIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allVisible = hiddenIds.size === 0;
  const [clipLow, clipHigh] = ready && ready.dimension === 3 ? axisRange(ready, clip.axis) : [0, 0];
  const busy = state.status === 'loading';
  const overlay = sceneError
    ? { tone: 'error' as const, title: '三维引擎不可用', detail: sceneError, retry: false }
    : state.status === 'loading'
    ? { tone: 'info' as const, title: '正在解析快照…', detail: '几何在 Web Worker 中解析，界面保持可交互。', retry: false }
    : state.status === 'error'
      ? { tone: 'error' as const, title: '无法显示该步骤', detail: state.message, retry: true }
      : state.status === 'unavailable'
        ? { tone: 'warn' as const, title: '该步骤没有可显示的结构', detail: state.reason, retry: true }
        : null;

  const handleRetry = (): void => setRetryToken((value) => value + 1);

  const rootClass = ['wb-viewer3d', className].filter(Boolean).join(' ');

  return (
    <section className={rootClass} style={{ height }} data-testid="workflow-viewer-3d" data-dimension={dimension}>
      <div className="wb-viewer3d__bar">
        {dimension === 3 ? (
          <>
            <button
              type="button"
              className="wb-viewer3d__button"
              disabled={!ready || busy}
              title="回到等轴视角并铺满画布"
              onClick={() => {
                setStandardView('iso');
                sceneRef.current?.setStandardView('iso');
              }}
            >
              重置视角
            </button>
            <button
              type="button"
              className="wb-viewer3d__button wb-viewer3d__button--fit"
              disabled={!ready || busy}
              title="保持当前方向，把当前可见几何铺满画布"
              onClick={() => sceneRef.current?.resetView()}
            >
              适配视图
            </button>
            <label className="wb-viewer3d__field">
              视角
              <select
                aria-label="标准视角"
                value={standardView}
                disabled={!ready || busy}
                onChange={(event) => {
                  const view = event.target.value as StandardView;
                  setStandardView(view);
                  sceneRef.current?.setStandardView(view);
                }}
              >
                <option value="iso">等轴</option>
                <option value="xy">XY</option>
                <option value="xz">XZ</option>
                <option value="yz">YZ</option>
              </select>
            </label>
            <label className="wb-viewer3d__field">
              显示
              <select
                aria-label="显示模式"
                value={displayMode}
                disabled={!ready || busy}
                onChange={(event) => setDisplayMode(event.target.value as DisplayMode)}
              >
                <option value="surface">表面</option>
                <option value="wireframe">线框</option>
              </select>
            </label>
            <label className="wb-viewer3d__field">
              投影
              <select
                aria-label="投影方式"
                value={projection}
                disabled={!ready || busy}
                onChange={(event) => setProjection(event.target.value as 'parallel' | 'perspective')}
              >
                <option value="parallel">正交</option>
                <option value="perspective">透视</option>
              </select>
            </label>
            <label className="wb-viewer3d__toggle"
              title="不简化三角形，显示原始仿真网格；百万面级别可能明显变慢">
              <input
                type="checkbox"
                checked={fullResolution}
                disabled={busy}
                onChange={(event) => {
                  const value = event.target.checked;
                  setFullResolution(value);
                  try { window.localStorage.setItem('wb.viewer3d.fullResolution', value ? '1' : '0'); } catch { /* storage may be full */ }
                }}
              />
              完整分辨率
            </label>
            <label className="wb-viewer3d__toggle">
              <input
                type="checkbox"
                checked={clip.enabled}
                disabled={!ready || busy}
                onChange={(event) => {
                  const enabled = event.target.checked;
                  const [low, high] = ready ? axisRange(ready, clip.axis) : [0, 0];
                  const inside = clip.position >= low && clip.position <= high;
                  setClip({ ...clip, enabled, position: enabled && !inside ? (low + high) / 2 : clip.position });
                  setClipFitToken((value) => value + 1);
                }}
              />
              截面
            </label>
            {clip.enabled && (
              <span className="wb-viewer3d__clip">
                <select
                  aria-label="截面轴"
                  value={clip.axis}
                  onChange={(event) => {
                    const axis = event.target.value as 'x' | 'y' | 'z';
                    const [low, high] = ready ? axisRange(ready, axis) : [0, 0];
                    setClip({ ...clip, axis, position: (low + high) / 2 });
                    setClipFitToken((value) => value + 1);
                  }}
                >
                  {AXIS_ORDER.map((axis) => (
                    <option key={axis} value={axis}>{axis.toUpperCase()}</option>
                  ))}
                </select>
                <input
                  aria-label={`截面位置（${coordinateUnit}）`}
                  type="range"
                  min={clipLow}
                  max={clipLow === clipHigh ? clipLow + 1e-6 : clipHigh}
                  step={(clipHigh - clipLow) / 500 || 1e-6}
                  value={clip.position}
                  title={`${clip.axis.toUpperCase()} = ${clip.position.toFixed(3)} ${coordinateUnit}`}
                  onChange={(event) => setClip({ ...clip, position: Number(event.target.value) })}
                />
                <output className="wb-viewer3d__clip-value">{`${clip.axis.toUpperCase()} = ${clip.position.toFixed(3)} ${coordinateUnit}`}</output>
                <span className="wb-viewer3d__clip-range">{`${clipLow.toFixed(3)} … ${clipHigh.toFixed(3)} ${coordinateUnit}`}</span>
                <select
                  aria-label="截面保留侧"
                  value={clip.keep}
                  onChange={(event) => {
                    setClip({ ...clip, keep: event.target.value as 'positive' | 'negative' });
                    setClipFitToken((value) => value + 1);
                  }}
                >
                  <option value="positive">保留 +{clip.axis.toUpperCase()}</option>
                  <option value="negative">保留 -{clip.axis.toUpperCase()}</option>
                </select>
              </span>
            )}
            <span className="wb-viewer3d__hint">拖动旋转 · 右键/Shift 平移 · 滚轮缩放</span>
          </>
        ) : (
          <>
            <span className="wb-viewer3d__hint">二维界面线 · 滚轮缩放 · 拖动平移 · 双击复位</span>
          </>
        )}
        <label className="wb-viewer3d__toggle wb-viewer3d__toggle--end"
          title="显示帧率（FPS）；无 GPU 时该数字不代表渲染性能">
          <input type="checkbox" checked={showStats} onChange={(event) => setShowStats(event.target.checked)} />
          性能
        </label>
      </div>

      {showRendererWarning && rendererInfo && (
        <div className="wb-viewer3d__renderer-warn" role="alert" aria-label="软件渲染提示">
          <span>
            当前浏览器使用软件渲染（<code title={rendererInfo.renderer}>{shortRendererName(rendererInfo.renderer)}</code>），
            3D 可能卡顿；请在支持 GPU 的桌面浏览器打开或开启硬件加速。
          </span>
          <button type="button" className="wb-viewer3d__button" aria-label="关闭软件渲染提示"
            onClick={dismissRendererWarning}>关闭</button>
        </div>
      )}

      {showRendererDebug && rendererInfo && (
        <div className="wb-viewer3d__renderer" aria-label="WebGL 渲染器" title={rendererInfo.renderer}>
          WebGL：{rendererInfo.renderer}
        </div>
      )}

      <div className="wb-viewer3d__stage">
        {dimension === 3 && <div className="wb-viewer3d__canvas" ref={containerRef} aria-label="三维工艺结构视图" />}
        {dimension === 2 && ready && <Viewer2D mesh={ready} hidden={hiddenIds} />}
        {overlay && (
          <div className={`wb-viewer3d__overlay wb-viewer3d__overlay--${overlay.tone}`} role="status">
            <strong>{overlay.title}</strong>
            <p>{overlay.detail}</p>
            {overlay.retry && (
              <button type="button" className="wb-viewer3d__button" onClick={handleRetry}>
                重试
              </button>
            )}
          </div>
        )}
        {ready && dimension === 3 && hiddenIds.size >= ready.materials.length && ready.materials.length > 0 && (
          <div className="wb-viewer3d__overlay wb-viewer3d__overlay--warn" role="status">
            <strong>所有材料已隐藏</strong>
            <p>在下方图例中重新勾选材料即可恢复显示。</p>
          </div>
        )}
        {ready && dimension === 3 && viewScale > 0 && (() => {
          const nice = niceLength(viewScale * 72);
          return (
            <div className="wb-viewer3d__scalebar" aria-label="比例尺" data-length={nice}>
              <span className="wb-viewer3d__scalebar-label">{`${formatLength(nice)} ${coordinateUnit}`}</span>
              <span className="wb-viewer3d__scalebar-bar" style={{ width: `${Math.max(10, nice / viewScale)}px` }} />
            </div>
          );
        })()}
      </div>

      {ready && counts && (
        <div className="wb-viewer3d__legend" aria-label="材料图例">
          {ready.materials.map((material, index) => {
            const visible = !hiddenIds.has(material.id);
            return (
              <label key={material.id} className="wb-viewer3d__legend-item" data-material-id={material.id}>
                <input
                  type="checkbox"
                  checked={visible}
                  aria-label={`显示 ${material.name}`}
                  onChange={() => toggleMaterial(material.id)}
                />
                <span className="wb-viewer3d__swatch" style={{ background: material.color }} />
                <span className="wb-viewer3d__legend-name">{material.name}</span>
                <span className="wb-viewer3d__legend-count">
                  {numberFormat.format(counts.perMaterial[index] ?? 0)}
                  {counts.unit}
                </span>
              </label>
            );
          })}
          {!allVisible && (
            <button
              type="button"
              className="wb-viewer3d__button wb-viewer3d__button--ghost"
              onClick={() => setHiddenIds(new Set<number>())}
            >
              全部显示
            </button>
          )}
        </div>
      )}

      <div className="wb-viewer3d__status" role="status" aria-live="polite">
        {ready ? (
          <>
            {dimension === 3 ? (
              <span>三角形 {numberFormat.format(ready.triangleCount)}</span>
            ) : (
              <span>线段 {numberFormat.format(ready.lineCount)}</span>
            )}
            <span>点 {numberFormat.format(ready.pointCount)}</span>
            {dimension === 3 && (
              <span className="wb-viewer3d__status-extent">
                {`X 范围 ${formatLength(ready.bounds[3] - ready.bounds[0])} ${coordinateUnit} · Y 范围 ${formatLength(ready.bounds[4] - ready.bounds[1])} ${coordinateUnit} · Z 范围 ${formatLength(ready.bounds[5] - ready.bounds[2])} ${coordinateUnit}`}
              </span>
            )}
            {dimension === 3 && marker && (
              <span className="wb-viewer3d__status-marker">
                {`测量位置：${marker.axis === 'x' ? 'X' : marker.axis === 'z' ? '高度' : 'Y'} = ${formatLength(marker.value)} ${coordinateUnit}`}
              </span>
            )}
            {(showStats || DEBUG_FPS) && dimension === 3 && (
              <span>{fps > 0 ? `${fps.toFixed(1)} fps` : '— fps'}</span>
            )}
            <span>解析 {Math.round(ready.parseMs)} ms{ready.fromCache ? '（缓存）' : ''}</span>
            {ready.simplified && (
              <span className="wb-viewer3d__status-warn">
                顶点聚类简化 {numberFormat.format(ready.sourceTriangles)} → {numberFormat.format(ready.triangleCount)} 面
              </span>
            )}
            <span className="wb-viewer3d__status-muted">{ready.stepName ?? ready.stepId}</span>
          </>
        ) : (
          <span className="wb-viewer3d__status-muted">等待快照…</span>
        )}
      </div>
    </section>
  );
}
