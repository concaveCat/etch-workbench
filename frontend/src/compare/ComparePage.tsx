/** P3 — multi-engine side-by-side comparison stage:
 *  1) Top toolbar + full-width 88-step scrubber bar;
 *  2) Pixel-aligned left/right 3D/raster viewports (identical header + 520px stage);
 *  3) Bottom dashboard comparing per-step render/solve times, consistency & IoU, and cross-section.
 */

import { useEffect, useMemo, useState } from 'react';
import type {
  CompareRun,
  CompareStep,
  EngineRow,
  EngineSummaryInfo,
  MeshFrame,
  PairComparison,
} from './api';
import { fetchMesh, frameUrl } from './api';
import { DEFAULT_CAMERA, MeshPane, cameraForView, type Camera } from './MeshPane';
import { RunHistory } from './RunHistory';
import { SectionView } from './SectionView';

type Props = {
  run: CompareRun | null;
  runId: string | null;
  history: CompareRun[];
  onSelectRun: (runId: string) => void;
};

const VIEWPORT_HEIGHT = 500;

function agreementColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return '#d8dee5';
  const clamped = Math.max(0, Math.min(1, value));
  const red = Math.round(233 + (20 - 233) * clamped);
  const green = Math.round(90 + (184 - 90) * clamped);
  const blue = Math.round(84 + (166 - 84) * clamped);
  return `rgb(${red},${green},${blue})`;
}

function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${value.toFixed(1)} ms`;
}

function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(2)}%`;
}

const CAMERA_ICONS = {
  azimuth: (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="8" /><path d="M12 4v16M4 12h16" />
    </svg>
  ),
  elevation: (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M4 18h16M6 18l6-9 6 9" />
    </svg>
  ),
  zoom: (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="11" cy="11" r="6" /><path d="M15.5 15.5 20 20M11 8v6M8 11h6" />
    </svg>
  ),
};

function resolvePair(step: CompareStep | undefined, left: string, right: string): PairComparison {
  if (!step) return {};
  const comps = step.comparisons ?? {};
  const direct = comps[`${left}|${right}`];
  if (direct) return direct;
  const reverse = comps[`${right}|${left}`];
  if (reverse) return reverse;
  return { probe: step.probe, section: step.section };
}

function stepFrameMs(row: EngineRow | undefined): number {
  if (!row) return 0;
  if (typeof row.frameMs === 'number') return row.frameMs;
  const m = row.metrics ?? {};
  const raw = m.frameMs ?? m.frame_ms;
  return typeof raw === 'number' ? raw : 0;
}

function stepProbeMs(row: EngineRow | undefined): number {
  if (!row) return 0;
  if (typeof row.probeMs === 'number') return row.probeMs;
  const m = row.metrics ?? {};
  const raw = m.probeMs ?? m.probe_ms;
  return typeof raw === 'number' ? raw : 0;
}

function stepSolveMs(
  row: EngineRow | undefined,
  meta: EngineSummaryInfo | undefined,
  stepCount: number,
): { ms: number; amortized: boolean } {
  if (row && typeof row.solveMs === 'number') {
    return { ms: row.solveMs, amortized: false };
  }
  if (meta && typeof meta.solveWallMs === 'number' && stepCount > 0) {
    return { ms: meta.solveWallMs / stepCount, amortized: true };
  }
  return { ms: row?.wallMs ?? 0, amortized: false };
}

function EngineViewportCard({
  runId,
  engineId,
  meta,
  row,
  stepCount,
  camera,
}: {
  runId: string | null;
  engineId: string;
  meta: EngineSummaryInfo | undefined;
  row: EngineRow | undefined;
  stepCount: number;
  camera: Camera;
}) {
  const [mesh, setMesh] = useState<MeshFrame | null>(null);
  const [loadingMesh, setLoadingMesh] = useState(false);
  const frame = row?.frame ?? null;

  useEffect(() => {
    let cancelled = false;
    setMesh(null);
    if (!runId || !frame || frame.kind !== 'mesh') {
      setLoadingMesh(false);
      return;
    }
    setLoadingMesh(true);
    fetchMesh(runId, frame.path)
      .then((payload) => {
        if (!cancelled) {
          setMesh(payload);
          setLoadingMesh(false);
        }
      })
      .catch(() => {
        if (!cancelled) setLoadingMesh(false);
      });
    return () => { cancelled = true; };
  }, [runId, frame?.kind, frame?.path]);

  const fMs = stepFrameMs(row);
  const solve = stepSolveMs(row, meta, stepCount);
  const kindBadge = frame?.kind === 'mesh' ? '3D Mesh' : frame?.kind === 'raster' ? 'Raster PNG' : (row?.status ?? '无帧');

  return (
    <div className="cmp-viewport-card">
      <div className="cmp-viewport-head">
        <span className={`cmp-engine-dot cmp-engine-dot-${engineId}`} />
        <strong className="cmp-viewport-title">{engineId}</strong>
        <span className="cmp-viewport-kind">{kindBadge}</span>
        <span className="cmp-spacer" />
        <span className="cmp-viewport-stat" title="本步渲染 / 出帧时间">
          渲染 {fmtMs(fMs)}
        </span>
        <span className="cmp-viewport-stat" title={solve.amortized ? '批处理总求解均摊耗时' : '本步物理/几何求解耗时'}>
          {solve.amortized ? '均摊求解 ' : '求解 '}
          {fmtMs(solve.ms)}
        </span>
      </div>
      <div className="cmp-viewport-body" style={{ height: VIEWPORT_HEIGHT }}>
        {!frame || !runId ? (
          <div className="cmp-missing">{row?.reason ?? `本步无帧 (${row?.status ?? 'missing'})`}</div>
        ) : frame.kind === 'mesh' ? (
          <MeshPane
            frame={mesh}
            loading={loadingMesh}
            camera={camera}
            height={VIEWPORT_HEIGHT}
            title=""
          />
        ) : (
          <div className="cmp-raster">
            <img src={frameUrl(runId, frame.path)} alt={engineId} />
          </div>
        )}
      </div>
    </div>
  );
}

export function ComparePage({ run, runId, history, onSelectRun }: Props) {
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA);
  const [stepIndex, setStepIndex] = useState(0);
  const [leftEngine, setLeftEngine] = useState<string>('voxel');
  const [rightEngine, setRightEngine] = useState<string>('spx');

  const engineIds = useMemo(
    () => Object.keys(run?.compare?.engines ?? {}),
    [run?.compare?.engines],
  );
  const referenceEngine = run?.compare?.referenceEngine ?? engineIds[engineIds.length - 1] ?? 'spx';

  useEffect(() => {
    if (engineIds.length === 0) return;
    setRightEngine((prev) => (engineIds.includes(prev) ? prev : referenceEngine));
    setLeftEngine((prev) => {
      if (engineIds.includes(prev) && (engineIds.length === 1 || prev !== referenceEngine)) {
        return prev;
      }
      return engineIds.find((id) => id !== referenceEngine) ?? engineIds[0];
    });
  }, [engineIds, referenceEngine]);

  const steps = run?.compare?.steps ?? [];
  const safeIndex = Math.min(stepIndex, Math.max(0, steps.length - 1));
  const step = steps[safeIndex];

  // Keyboard Left/Right arrow navigation across the 88 steps
  useEffect(() => {
    if (steps.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        setStepIndex((idx) => Math.max(0, idx - 1));
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        setStepIndex((idx) => Math.min(steps.length - 1, idx + 1));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [steps.length]);

  const runView = run?.compare?.view;
  const runCamera = run?.compare?.camera;
  useEffect(() => {
    const azimuth = Number(runCamera?.azimuth);
    const elevation = Number(runCamera?.elevation);
    const zoom = Number(runCamera?.zoom);
    if (Number.isFinite(azimuth) && Number.isFinite(elevation)) {
      setCamera({ azimuth, elevation, zoom: Number.isFinite(zoom) && zoom > 0 ? zoom : 1 });
    } else {
      setCamera(cameraForView(runView));
    }
  }, [runView, runCamera?.azimuth, runCamera?.elevation, runCamera?.zoom]);

  const milestoneIndices = useMemo(() => {
    const n = steps.length;
    if (n <= 1) return [0];
    const marks = new Set<number>([0, n - 1]);
    const stride = n > 40 ? 10 : n > 16 ? 5 : 2;
    for (let i = stride; i < n - 2; i += stride) marks.add(i);
    return Array.from(marks).sort((a, b) => a - b);
  }, [steps.length]);

  if (!run) {
    return (
      <section className="cmp-blank">
        <RunHistory runs={history} current={null} onSelect={onSelectRun} />
      </section>
    );
  }

  const enginesMeta = run.compare?.engines ?? {};
  const activePair = resolvePair(step, leftEngine, rightEngine);
  const probe = activePair.probe;
  const comparedIds = leftEngine === rightEngine ? [leftEngine] : [leftEngine, rightEngine];

  const timingRows = comparedIds.map((eid) => {
    const row = step?.engines?.[eid];
    const meta = enginesMeta[eid];
    const fMs = stepFrameMs(row);
    const solve = stepSolveMs(row, meta, steps.length);
    const pMs = stepProbeMs(row);
    const totalSolve = meta?.solveWallMs ?? null;
    const totalFrame = meta?.frameWallMs ?? null;
    const speedup = run.compare?.summary?.speedupVsReference?.[eid] ?? null;
    return { eid, fMs, solveMs: solve.ms, amortized: solve.amortized, pMs, totalSolve, totalFrame, speedup };
  });

  const maxFrameMs = Math.max(1, ...timingRows.map((r) => r.fMs));
  const maxSolveMs = Math.max(1, ...timingRows.map((r) => r.solveMs));

  const iouEntries = Object.entries(probe?.iou ?? {}).sort((a, b) => a[1] - b[1]);

  return (
    <section className="cmp-stage">
      {/* 1. Top Control Toolbar */}
      <div className="cmp-toolbar">
        <RunHistory runs={history} current={runId} onSelect={(id) => { setStepIndex(0); onSelectRun(id); }} />
        {engineIds.length >= 2 ? (
          <div className="cmp-pair-picker">
            <select
              className="cmp-ghost-select"
              aria-label="left engine"
              value={leftEngine}
              onChange={(e) => setLeftEngine(e.target.value)}
              title="左侧视图引擎"
            >
              {engineIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
            <span className="cmp-swap-icon" aria-hidden>⇄</span>
            <select
              className="cmp-ghost-select"
              aria-label="right engine"
              value={rightEngine}
              onChange={(e) => setRightEngine(e.target.value)}
              title="右侧视图引擎"
            >
              {engineIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </div>
        ) : null}
        <span className="cmp-spacer" />
        <label className="cmp-icon-slider" title={`方位角 ${camera.azimuth}°`}>
          {CAMERA_ICONS.azimuth}
          <input type="range" min="0" max="360" value={camera.azimuth}
                 onChange={(event) => setCamera({ ...camera, azimuth: Number(event.target.value) })} />
        </label>
        <label className="cmp-icon-slider" title={`仰角 ${camera.elevation}°`}>
          {CAMERA_ICONS.elevation}
          <input type="range" min="-85" max="85" value={camera.elevation}
                 onChange={(event) => setCamera({ ...camera, elevation: Number(event.target.value) })} />
        </label>
        <label className="cmp-icon-slider" title={`缩放 ${camera.zoom.toFixed(2)}x`}>
          {CAMERA_ICONS.zoom}
          <input type="range" min="0.4" max="3" step="0.05" value={camera.zoom}
                 onChange={(event) => setCamera({ ...camera, zoom: Number(event.target.value) })} />
        </label>
      </div>

      {/* 2. TOP 88-Step Navigation Bar */}
      {steps.length > 0 ? (
        <div className="cmp-stepbar">
          <div className="cmp-stepbar-head">
            <div className="cmp-stepbar-nav">
              <button
                type="button"
                className="cmp-nav-btn"
                disabled={safeIndex <= 0}
                onClick={() => setStepIndex((i) => Math.max(0, i - 1))}
                title="上一步 (←)"
              >‹</button>
              <span className="cmp-step-counter">
                步骤 <strong>{safeIndex + 1}</strong> / {steps.length}
              </span>
              <button
                type="button"
                className="cmp-nav-btn"
                disabled={safeIndex >= steps.length - 1}
                onClick={() => setStepIndex((i) => Math.min(steps.length - 1, i + 1))}
                title="下一步 (→)"
              >›</button>
            </div>
            <code className="cmp-step-name">{`#${safeIndex} · ${step?.stepId ?? ''}`}</code>
            {step ? (
              <span className="cmp-step-mech">
                {`${step.kind}/${step.mechanism}${step.model ? ` · ${step.model}` : ''}`}
              </span>
            ) : null}
            <span className="cmp-spacer" />
            {probe?.status === 'measured' ? (
              <span className="cmp-step-summary-pill">
                全空间一致率 <strong>{fmtPct(probe.agreement)}</strong>
                {probe.activeAgreement != null ? (
                  <> · 有效叠层 <strong>{fmtPct(probe.activeAgreement)}</strong></>
                ) : null}
              </span>
            ) : null}
          </div>

          <div
            className="cmp-scrub"
            role="tablist"
            style={{
              gridTemplateColumns: steps.length <= 120
                ? `repeat(${steps.length}, minmax(0, 1fr))`
                : `repeat(${steps.length}, minmax(10px, 1fr))`,
            }}
          >
            {steps.map((row, index) => {
              const pr = resolvePair(row, leftEngine, rightEngine).probe;
              const agreement = pr?.status === 'measured' ? (pr.activeAgreement ?? pr.agreement ?? null) : null;
              const fullAgr = pr?.status === 'measured' ? fmtPct(pr.agreement) : '-';
              const actAgr = pr?.status === 'measured' && pr.activeAgreement != null
                ? ` · 有效叠层 ${fmtPct(pr.activeAgreement)}`
                : '';
              return (
                <button
                  key={row.stepId}
                  type="button"
                  role="tab"
                  aria-selected={index === safeIndex}
                  className={`cmp-tick ${index === safeIndex ? 'active' : ''}`}
                  style={{ background: agreementColor(agreement) }}
                  title={`#${index} ${row.stepId} · 全空间 ${fullAgr}${actAgr}`}
                  onClick={() => setStepIndex(index)}
                />
              );
            })}
          </div>

          <div className="cmp-scrub-ruler">
            {milestoneIndices.map((idx) => (
              <button
                key={idx}
                type="button"
                className={`cmp-ruler-mark ${idx === safeIndex ? 'active' : ''}`}
                style={{ left: `${((idx + 0.5) / steps.length) * 100}%` }}
                onClick={() => setStepIndex(idx)}
              >
                {idx}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {/* 3. Pixel-Aligned Side-by-Side Left & Right Viewports */}
      <div className="cmp-duo">
        <EngineViewportCard
          runId={runId}
          engineId={leftEngine}
          meta={enginesMeta[leftEngine]}
          row={step?.engines?.[leftEngine]}
          stepCount={steps.length}
          camera={camera}
        />
        <EngineViewportCard
          runId={runId}
          engineId={rightEngine}
          meta={enginesMeta[rightEngine]}
          row={step?.engines?.[rightEngine]}
          stepCount={steps.length}
          camera={camera}
        />
      </div>

      {/* 4. BOTTOM Comparison Panel: Render/Solve Timing + Consistency Metrics */}
      <div className="cmp-bottom-grid">
        {/* Bottom-Left: Step Render & Solve Timing Comparison */}
        <div className="cmp-metric-card">
          <div className="cmp-metric-head">
            <h4>本步渲染时间与计算耗时对比</h4>
            <span className="cmp-metric-sub">{`${leftEngine} vs ${rightEngine}`}</span>
          </div>

          <div className="cmp-timing-group">
            <div className="cmp-timing-title">本步渲染 / 出帧耗时 (frameMs)</div>
            {timingRows.map((r) => (
              <div className="cmp-bar-line" key={`frame-${r.eid}`}>
                <span className="cmp-bar-label">{r.eid}</span>
                <div className="cmp-bar-track">
                  <span
                    className={`cmp-bar-fill cmp-bar-${r.eid}`}
                    style={{ width: `${r.fMs > 0 ? Math.max(2, (r.fMs / maxFrameMs) * 100) : 0}%` }}
                  />
                </div>
                <span className="cmp-bar-val">{fmtMs(r.fMs)}</span>
              </div>
            ))}
          </div>

          <div className="cmp-timing-group">
            <div className="cmp-timing-title">本步物理 / 几何求解耗时 (solveMs)</div>
            {timingRows.map((r) => (
              <div className="cmp-bar-line" key={`solve-${r.eid}`}>
                <span className="cmp-bar-label">{r.eid}</span>
                <div className="cmp-bar-track">
                  <span
                    className={`cmp-bar-fill cmp-bar-${r.eid}`}
                    style={{ width: `${r.solveMs > 0 ? Math.max(2, (r.solveMs / maxSolveMs) * 100) : 0}%` }}
                  />
                </div>
                <span className="cmp-bar-val">
                  {fmtMs(r.solveMs)}
                  {r.amortized ? ' (均摊)' : ''}
                </span>
              </div>
            ))}
          </div>

          <div className="cmp-timing-footer">
            {timingRows.map((r) => (
              <span key={`tot-${r.eid}`} className="cmp-total-chip">
                <strong>{r.eid}</strong> 全流程：求解 {fmtMs(r.totalSolve)} · 渲染 {fmtMs(r.totalFrame)}
                {r.speedup != null && r.eid !== referenceEngine ? ` · 求解加速 ${r.speedup.toFixed(2)}x` : ''}
              </span>
            ))}
          </div>
        </div>

        {/* Bottom-Right: Step Consistency & Material IoU */}
        <div className="cmp-metric-card">
          <div className="cmp-metric-head">
            <h4>本步几何一致性对比</h4>
            <span className="cmp-metric-sub">
              {probe?.status === 'measured'
                ? `公共探针格 ${probe.points ?? run.compare?.probePoints ?? 0} 点`
                : `状态：${probe?.status ?? '未启用'}`}
            </span>
          </div>

          {probe?.status === 'measured' ? (
            <>
              <div className="cmp-kpi-row">
                <div className="cmp-kpi">
                  <span className="cmp-kpi-label">全空间一致率</span>
                  <strong className="cmp-kpi-val">{fmtPct(probe.agreement)}</strong>
                </div>
                <div className="cmp-kpi">
                  <span className="cmp-kpi-label">有效叠层一致率 (去空气)</span>
                  <strong className="cmp-kpi-val">{fmtPct(probe.activeAgreement)}</strong>
                </div>
                <div className="cmp-kpi">
                  <span className="cmp-kpi-label">特征区一致率 (z≥0)</span>
                  <strong className="cmp-kpi-val">{fmtPct(probe.featureAgreement)}</strong>
                </div>
                <div className="cmp-kpi">
                  <span className="cmp-kpi-label">差异探针点数</span>
                  <strong className="cmp-kpi-val">
                    {`${probe.mismatchCount ?? 0} / ${probe.points ?? run.compare?.probePoints ?? 0}`}
                  </strong>
                </div>
              </div>

              {iouEntries.length > 0 ? (
                <div className="cmp-iou-list">
                  {iouEntries.map(([mat, score]) => (
                    <span
                      key={mat}
                      className="cmp-iou-chip"
                      style={{ borderColor: agreementColor(score) }}
                      title={`${mat} IoU: ${(score * 100).toFixed(1)}%`}
                    >
                      <i style={{ background: agreementColor(score) }} />
                      {mat} {(score * 100).toFixed(1)}%
                    </span>
                  ))}
                </div>
              ) : null}

              {probe.topConfusions && probe.topConfusions.length > 0 ? (
                <div className="cmp-confusions">
                  主要差异材料对：
                  {probe.topConfusions.slice(0, 3).map((item, i) => {
                    const lMat = String(item[leftEngine] ?? item[probe.leftEngine ?? ''] ?? '?');
                    const rMat = String(item[rightEngine] ?? item[probe.rightEngine ?? ''] ?? '?');
                    return (
                      <span key={i} className="cmp-confusion-tag">
                        {`${leftEngine}:${lMat} ≠ ${rightEngine}:${rMat} (${item.count}点)`}
                      </span>
                    );
                  })}
                </div>
              ) : (
                <div className="cmp-confusions cmp-confusions-ok">本步全部采样点 100% 完全一致</div>
              )}
            </>
          ) : (
            <div className="cmp-confusions">{probe?.reason ?? '当前步骤无探针比对数据'}</div>
          )}
        </div>
      </div>

      {/* 5. Cross-Section Interface Overlay */}
      <SectionView
        section={activePair.section}
        width={960}
        height={170}
        zRange={run.compare?.sectionZ}
        engines={[leftEngine, rightEngine]}
        domainWidth={run.compare?.domain?.width ?? 1}
      />
    </section>
  );
}
