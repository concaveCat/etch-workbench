/** P3 — side-by-side comparison, visual only: no labels, no table.
 *
 * Everything that used to be text is a mark: the step scrubber is a strip of
 * coloured ticks (agreement → colour), the timing comparison is two bars, the run
 * picker is a compact select rendered as a chevron. Titles are tooltips.
 */

import { useEffect, useMemo, useState } from 'react';
import type { CompareRun, MeshFrame } from './api';
import { fetchMesh, frameUrl } from './api';
import { DEFAULT_CAMERA, MeshPane, cameraForView, type Camera } from './MeshPane';
import { SectionView } from './SectionView';

type Props = {
  run: CompareRun | null;
  runId: string | null;
  history: CompareRun[];
  onSelectRun: (runId: string) => void;
};

/** agreement -> colour (1 = teal, 0 = red) */
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

export function ComparePage({ run, runId, history, onSelectRun }: Props) {
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA);
  const [stepIndex, setStepIndex] = useState(0);
  const [mesh, setMesh] = useState<MeshFrame | null>(null);

  const steps = run?.compare?.steps ?? [];
  const step = steps[Math.min(stepIndex, Math.max(0, steps.length - 1))];
  const viennapsFrame = step?.engines.viennaps?.frame ?? null;
  const spxFrame = step?.engines.spx?.frame ?? null;

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

  useEffect(() => {
    let cancelled = false;
    setMesh(null);
    if (!runId || !viennapsFrame || viennapsFrame.kind !== 'mesh') return;
    fetchMesh(runId, viennapsFrame.path)
      .then((payload) => { if (!cancelled) setMesh(payload); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [runId, viennapsFrame?.path]);

  const maxWall = useMemo(() => {
    let max = 1;
    for (const row of steps) {
      for (const engine of Object.values(row.engines)) {
        if (engine.wallMs) max = Math.max(max, engine.wallMs);
      }
    }
    return max;
  }, [steps]);

  if (!run) {
    return (
      <section className="cmp-blank">
        {history.length > 0 ? (
          <select
            className="cmp-ghost-select"
            value=""
            onChange={(event) => event.target.value && onSelectRun(event.target.value)}
          >
            <option value="">▾</option>
            {history.map((row) => (
              <option key={row.runId} value={row.runId}>{row.caseId ?? row.runId}</option>
            ))}
          </select>
        ) : <span className="cmp-dot" />}
      </section>
    );
  }

  return (
    <section className="cmp-stage">
      <div className="cmp-toolbar">
        <select
          className="cmp-ghost-select"
          value={runId ?? ''}
          onChange={(event) => onSelectRun(event.target.value)}
          title="runs"
        >
          {history.map((row) => (
            <option key={row.runId} value={row.runId}>{row.caseId ?? row.runId}</option>
          ))}
        </select>
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
        <MeshPane frame={mesh} camera={camera} height={560} title="" />
        <div className="cmp-raster">
          {spxFrame && runId
            ? <img src={frameUrl(runId, spxFrame.path)} alt="" />
            : <span className="cmp-dot" />}
        </div>
      </div>

      <div className="cmp-scrub" role="tablist">
        {steps.map((row, index) => {
          const agreement = row.probe?.status === 'measured' ? row.probe.agreement ?? null : null;
          return (
            <button
              key={row.stepId}
              type="button"
              className={`cmp-tick ${index === stepIndex ? 'active' : ''}`}
              style={{ background: agreementColor(agreement) }}
              title={`${index} ${row.stepId} · ${agreement === null ? '-' : (agreement * 100).toFixed(0) + '%'}`}
              onClick={() => setStepIndex(index)}
            />
          );
        })}
      </div>

      <div className="cmp-cost">
        {(['viennaps', 'spx'] as const).map((engineId) => {
          const engine = step?.engines?.[engineId];
          const width = engine?.wallMs ? Math.max(2, (engine.wallMs / maxWall) * 100) : 0;
          return (
            <div className={`cmp-cost-row cmp-cost-${engineId}`} key={engineId} title={engineId}>
              <span style={{ width: `${width}%` }} />
            </div>
          );
        })}
      </div>

      <SectionView
        section={step?.section}
        width={640}
        height={150}
        zRange={run.compare?.sectionZ}
        engines={Object.keys(step?.engines ?? {})}
        domainWidth={run.compare?.domain?.width ?? 1}
      />
    </section>
  );
}
