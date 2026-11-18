/** P3 — multi-engine side-by-side comparison.
 *
 * Supports any N engines (mesh vs raster, mesh vs mesh, raster vs raster),
 * dynamic left/right engine selectors, honest solver timing bars, and active-stack
 * accuracy tooltips.
 */

import { useEffect, useMemo, useState } from 'react';
import type { CompareRun, CompareStep, EngineRow, MeshFrame, PairComparison } from './api';
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

function agreementColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return '#d8dee5';
  const clamped = Math.max(0, Math.min(1, value));
  const red = Math.round(233 + (20 - 233) * clamped);
  const green = Math.round(90 + (184 - 90) * clamped);
  const blue = Math.round(84 + (166 - 84) * clamped);
  return `rgb(${red},${green},${blue})`;
}

const CAMERA_ICONS = {
  azimuth: (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="8" /><path d="M12 4v16M4 12h16" />
    </svg>
  ),
  elevation: (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M4 18h16M6 18l6-9 6 9" />
    </svg>
  ),
  zoom: (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
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

function EngineViewport({
  runId,
  engineId,
  row,
  camera,
}: {
  runId: string | null;
  engineId: string;
  row: EngineRow | undefined;
  camera: Camera;
}) {
  const [mesh, setMesh] = useState<MeshFrame | null>(null);
  const frame = row?.frame ?? null;

  useEffect(() => {
    let cancelled = false;
    setMesh(null);
    if (!runId || !frame || frame.kind !== 'mesh') return;
    fetchMesh(runId, frame.path)
      .then((payload) => { if (!cancelled) setMesh(payload); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [runId, frame?.kind, frame?.path]);

  if (!frame || !runId) {
    return (
      <div className="cmp-raster" title={`${engineId}: ${row?.status ?? 'missing'}`}>
        <span className="cmp-dot" />
      </div>
    );
  }
  if (frame.kind === 'mesh') {
    return <MeshPane frame={mesh} camera={camera} height={560} title={engineId} />;
  }
  return (
    <div className="cmp-raster" title={engineId}>
      <img src={frameUrl(runId, frame.path)} alt={engineId} />
    </div>
  );
}

export function ComparePage({ run, runId, history, onSelectRun }: Props) {
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA);
  const [stepIndex, setStepIndex] = useState(0);
  const [leftEngine, setLeftEngine] = useState<string>('viennaps');
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
  const step = steps[Math.min(stepIndex, Math.max(0, steps.length - 1))];

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

  // Total solver / process time bars across all participating engines
  const engineTimings = useMemo(() => {
    const meta = run?.compare?.engines ?? {};
    const stepCount = Math.max(1, steps.length);
    return engineIds.map((eid) => {
      const info = meta[eid];
      const stepRow = step?.engines?.[eid];
      // Prefer per-step solveMs; for batch engines (SPX), show average per-step solveWallMs
      const stepSolve = stepRow?.solveMs ?? (
        info?.solveWallMs != null ? info.solveWallMs / stepCount : stepRow?.wallMs ?? 0
      );
      const totalSolve = info?.solveWallMs ?? info?.processWallMs ?? 0;
      const speedup = run?.compare?.summary?.speedupVsReference?.[eid];
      return { eid, stepMs: stepSolve || 0, totalSolveMs: totalSolve, speedup };
    });
  }, [engineIds, run?.compare?.engines, run?.compare?.summary?.speedupVsReference, step, steps.length]);

  const maxStepMs = useMemo(
    () => Math.max(1, ...engineTimings.map((t) => t.stepMs)),
    [engineTimings],
  );

  if (!run) {
    return (
      <section className="cmp-blank">
        <RunHistory runs={history} current={null} onSelect={onSelectRun} />
      </section>
    );
  }

  const activePair = resolvePair(step, leftEngine, rightEngine);

  return (
    <section className="cmp-stage">
      <div className="cmp-toolbar">
        <RunHistory runs={history} current={runId} onSelect={onSelectRun} />
        {engineIds.length >= 2 ? (
          <>
            <select
              className="cmp-ghost-select"
              aria-label="left engine"
              value={leftEngine}
              onChange={(e) => setLeftEngine(e.target.value)}
              title="Left viewport engine"
            >
              {engineIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
            <span aria-hidden>⇄</span>
            <select
              className="cmp-ghost-select"
              aria-label="right engine"
              value={rightEngine}
              onChange={(e) => setRightEngine(e.target.value)}
              title="Right viewport engine"
            >
              {engineIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </>
        ) : null}
        <span className="cmp-spacer" />
        <label className="cmp-icon-slider" title="azimuth">
          {CAMERA_ICONS.azimuth}
          <input type="range" min="0" max="360" value={camera.azimuth}
                 onChange={(event) => setCamera({ ...camera, azimuth: Number(event.target.value) })} />
        </label>
        <label className="cmp-icon-slider" title="elevation">
          {CAMERA_ICONS.elevation}
          <input type="range" min="-85" max="85" value={camera.elevation}
                 onChange={(event) => setCamera({ ...camera, elevation: Number(event.target.value) })} />
        </label>
        <label className="cmp-icon-slider" title="zoom">
          {CAMERA_ICONS.zoom}
          <input type="range" min="0.4" max="3" step="0.05" value={camera.zoom}
                 onChange={(event) => setCamera({ ...camera, zoom: Number(event.target.value) })} />
        </label>
      </div>

      <div className="cmp-duo">
        <EngineViewport
          runId={runId}
          engineId={leftEngine}
          row={step?.engines?.[leftEngine]}
          camera={camera}
        />
        <EngineViewport
          runId={runId}
          engineId={rightEngine}
          row={step?.engines?.[rightEngine]}
          camera={camera}
        />
      </div>

      <div className="cmp-scrub" role="tablist">
        {steps.map((row, index) => {
          const pr = resolvePair(row, leftEngine, rightEngine).probe;
          const agreement = pr?.status === 'measured' ? (pr.activeAgreement ?? pr.agreement ?? null) : null;
          const fullAgr = pr?.status === 'measured' && pr.agreement != null
            ? `${(pr.agreement * 100).toFixed(1)}%`
            : '-';
          const actAgr = pr?.status === 'measured' && pr.activeAgreement != null
            ? ` · active ${(pr.activeAgreement * 100).toFixed(1)}%`
            : '';
          return (
            <button
              key={row.stepId}
              type="button"
              className={`cmp-tick ${index === stepIndex ? 'active' : ''}`}
              style={{ background: agreementColor(agreement) }}
              title={`${index} ${row.stepId} · all ${fullAgr}${actAgr}`}
              onClick={() => setStepIndex(index)}
            />
          );
        })}
      </div>

      <div className="cmp-cost">
        {engineTimings.map(({ eid, stepMs, totalSolveMs, speedup }) => {
          const width = stepMs > 0 ? Math.max(2, (stepMs / maxStepMs) * 100) : 0;
          const spLabel = speedup != null ? ` · ${speedup.toFixed(2)}x vs ${referenceEngine}` : '';
          return (
            <div
              className={`cmp-cost-row cmp-cost-${eid}`}
              key={eid}
              title={`${eid}: step solve ~${stepMs.toFixed(1)} ms · total solve ${totalSolveMs.toFixed(0)} ms${spLabel}`}
            >
              <span style={{ width: `${width}%` }} />
            </div>
          );
        })}
      </div>

      <SectionView
        section={activePair.section}
        width={640}
        height={150}
        zRange={run.compare?.sectionZ}
        engines={[leftEngine, rightEngine]}
        domainWidth={run.compare?.domain?.width ?? 1}
      />
    </section>
  );
}
