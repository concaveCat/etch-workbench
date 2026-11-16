/** P3 - side-by-side comparison: shared camera, aligned timeline, per-step cost. */

import { useEffect, useMemo, useState } from 'react';
import type { CompareRun, MeshFrame } from './api';
import { fetchMesh, frameUrl } from './api';
import { DEFAULT_CAMERA, MeshView, type Camera } from './MeshView';

type Props = {
  run: CompareRun | null;
  runId: string | null;
  history: CompareRun[];
  onSelectRun: (runId: string) => void;
};

const STATUS_COLOR: Record<string, string> = {
  ok: '#35d6c2', missing: '#8a8f98', unsupported: '#e0a030', failed: '#ff5c5c',
  out_of_domain: '#e0a030', skipped: '#8a8f98',
};

export function ComparePage({ run, runId, history, onSelectRun }: Props) {
  const [camera, setCamera] = useState<Camera>(DEFAULT_CAMERA);
  const [stepIndex, setStepIndex] = useState(0);
  const [mesh, setMesh] = useState<MeshFrame | null>(null);
  const [meshError, setMeshError] = useState<string | null>(null);

  const steps = run?.compare?.steps ?? [];
  const step = steps[Math.min(stepIndex, Math.max(0, steps.length - 1))];
  const viennapsFrame = step?.engines.viennaps?.frame ?? null;
  const spxFrame = step?.engines.spx?.frame ?? null;

  useEffect(() => {
    let cancelled = false;
    setMesh(null);
    setMeshError(null);
    if (!runId || !viennapsFrame || viennapsFrame.kind !== 'mesh') return;
    fetchMesh(runId, viennapsFrame.path)
      .then((payload) => { if (!cancelled) setMesh(payload); })
      .catch((error: unknown) => { if (!cancelled) setMeshError(String(error)); });
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
    return <section className="cmp-page cmp-empty">还没有对比运行。回到第 2 页点“开始对比运行”。</section>;
  }
  if (run.state !== 'succeeded') {
    return (
      <section className="cmp-page cmp-empty">
        <p>运行 {run.runId} 状态：<strong>{run.state}</strong></p>
        {run.detail ? <pre className="cmp-json">{run.detail}</pre> : null}
      </section>
    );
  }

  return (
    <section className="cmp-page cmp-page-compare">
      <header className="cmp-compare-head">
        <h2>3 · 左右对比</h2>
        <select value={runId ?? ''} onChange={(event) => onSelectRun(event.target.value)}>
          {history.map((row) => (
            <option key={row.runId} value={row.runId}>{row.caseId ?? row.runId} · {row.runId.slice(0, 6)}</option>
          ))}
        </select>
        <span className="cmp-spacer" />
        <label className="cmp-check">
          方位 {camera.azimuth}°
          <input type="range" min="0" max="360" value={camera.azimuth}
                 onChange={(event) => setCamera({ ...camera, azimuth: Number(event.target.value) })} />
        </label>
        <label className="cmp-check">
          仰角 {camera.elevation}°
          <input type="range" min="-80" max="80" value={camera.elevation}
                 onChange={(event) => setCamera({ ...camera, elevation: Number(event.target.value) })} />
        </label>
        <label className="cmp-check">
          缩放 {camera.zoom.toFixed(2)}
          <input type="range" min="0.4" max="3" step="0.05" value={camera.zoom}
                 onChange={(event) => setCamera({ ...camera, zoom: Number(event.target.value) })} />
        </label>
      </header>

      <div className="cmp-panes">
        <MeshView frame={mesh} camera={camera} width={520} height={380} title="ViennaPS（水平集参考）"
                  note={meshError ?? (viennapsFrame ? `${viennapsFrame.kind} · ${step?.engines.viennaps.status}` : '无帧')} />
        <div className="cmp-pane">
          <figcaption>
            <strong>SPX（几何模拟）</strong>
            <span className="cmp-note">相机由 SPX 固定；同 bbox、同视角预设</span>
          </figcaption>
          {spxFrame && runId
            ? <img src={frameUrl(runId, spxFrame.path)} width={520} height={380} alt="SPX frame"
                   style={{ borderRadius: 8, background: '#0b1620' }} />
            : <div className="cmp-missing">无帧：{step?.engines.spx.status ?? 'missing'}</div>}
        </div>
      </div>

      <div className="cmp-timeline">
        {steps.map((row, index) => (
          <button type="button" key={row.stepId}
                  className={`cmp-tl-step ${index === stepIndex ? 'active' : ''} ${row.aligned ? '' : 'unaligned'}`}
                  onClick={() => setStepIndex(index)}>
            <span>{row.stepId}</span>
            <span className={`cmp-badge ${row.comparability}`}>{row.comparability}</span>
            {row.probe?.status === 'measured' && row.probe.agreement !== undefined
              ? <span className="cmp-note">一致 {(row.probe.agreement * 100).toFixed(0)}%</span>
              : null}
            {Object.entries(row.engines).map(([engineId, engine]) => (
              <span key={engineId} className="cmp-bar-wrap" title={`${engineId}: ${engine.wallMs ?? '—'} ms`}>
                <span className="cmp-bar" style={{
                  width: `${Math.max(2, ((engine.wallMs ?? 0) / maxWall) * 100)}%`,
                  background: STATUS_COLOR[engine.status] ?? '#8a8f98',
                }} />
              </span>
            ))}
          </button>
        ))}
      </div>

      <table className="cmp-table">
        <thead>
          <tr>
            <th>步骤</th><th>机制</th><th>可比性</th>
            <th>ViennaPS ms</th><th>SPX ms</th><th>逐点一致率</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((row) => (
            <tr key={row.stepId} className={row.aligned ? '' : 'unaligned'}>
              <td>{row.stepId}</td>
              <td>{row.kind}/{row.mechanism}</td>
              <td>{row.comparability}</td>
              <td>{row.engines.viennaps?.wallMs?.toFixed(1) ?? '—'}</td>
              <td>{row.engines.spx?.wallMs?.toFixed(1) ?? '—'}</td>
              <td>{row.probe?.status === 'measured' && row.probe.agreement !== undefined
                ? `${(row.probe.agreement * 100).toFixed(1)}%`
                : (row.probe?.status ?? '—')}</td>
              <td>
                {Object.entries(row.engines).map(([engineId, engine]) => (
                  <span key={engineId} className="cmp-status" style={{ color: STATUS_COLOR[engine.status] }}>
                    {engineId}:{engine.status}
                  </span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="cmp-hint">
        探针 {run.compare?.probePoints ?? 0} 点 · 一致率按材料标签逐一比对 ·
        总耗时 ViennaPS {run.compare?.engines.viennaps?.processWallMs?.toFixed(0)} ms ·
        SPX {run.compare?.engines.spx?.processWallMs?.toFixed(0)} ms ·
        完全对齐 {run.compare?.summary.fullyAlignedSteps}/{run.compare?.summary.stepCount} 步 ·
        digest {run.compare?.jobDigest}
      </p>
    </section>
  );
}
