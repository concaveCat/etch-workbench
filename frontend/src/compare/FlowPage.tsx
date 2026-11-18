/** P2 - compiled StepIR flow editor with dynamic multi-engine selection. */

import { useMemo } from 'react';
import type { EngineDescriptor } from './api';
import type { Draft } from './draft';
import { MATERIALS, gradeOf, toJob } from './draft';

type Props = {
  draft: Draft;
  onChange: (next: Draft) => void;
  catalog: EngineDescriptor[];
  engines: string[];
  sequential: boolean;
  onToggleSequential: (next: boolean) => void;
  onToggleEngine: (engineId: string) => void;
  onRun: () => void;
  busy: boolean;
  error: string | null;
};

const KIND_OPTIONS: { kind: Draft['steps'][number]['kind']; mechanism: Draft['steps'][number]['mechanism']; label: string }[] = [
  { kind: 'etch', mechanism: 'isotropic', label: '各向同性刻蚀' },
  { kind: 'deposit', mechanism: 'isotropic', label: '各向同性沉积' },
  { kind: 'etch', mechanism: 'anisotropic', label: '各向异性刻蚀' },
  { kind: 'deposit', mechanism: 'anisotropic', label: '各向异性沉积' },
  { kind: 'deposit', mechanism: 'fill', label: '沟槽填充 (fill)' },
  { kind: 'strip', mechanism: 'strip', label: '材料剥离 (strip)' },
  { kind: 'planarize', mechanism: 'cmp', label: '平坦化 / CMP' },
];

const BADGE: Record<string, string> = { exact: '可比', approximate: '近似可比', no_counterpart: '无可比实现' };

export function FlowPage({
  draft,
  onChange,
  catalog,
  engines,
  sequential,
  onToggleSequential,
  onToggleEngine,
  onRun,
  busy,
  error,
}: Props) {
  const job = useMemo(() => toJob(draft), [draft]);
  const availableEngines = catalog.length > 0
    ? catalog
    : [
        { engineId: 'voxel', label: 'voxel', adapter: '', mechanisms: [], frameKinds: ['mesh'], rationale: '' },
        { engineId: 'viennaps', label: 'viennaps', adapter: '', mechanisms: [], frameKinds: ['mesh'], rationale: '' },
        { engineId: 'spx', label: 'spx', adapter: '', mechanisms: [], frameKinds: ['raster'], rationale: '' },
      ];

  const patchStep = (index: number, patch: Partial<Draft['steps'][number]>) => {
    onChange({ ...draft, steps: draft.steps.map((step, i) => (i === index ? { ...step, ...patch } : step)) });
  };
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 1 || target >= draft.steps.length) return;
    const steps = [...draft.steps];
    const [row] = steps.splice(index, 1);
    steps.splice(target, 0, row);
    onChange({ ...draft, steps });
  };

  return (
    <section className="cmp-page">
      <div className="cmp-form">
        <h2>2 · 工艺流程与多引擎选择</h2>
        <p className="cmp-hint">支持任意数量已注册引擎同时对拍，也可为单步绑定自定义模型 ID（<code>step.model</code>）。</p>

        {draft.steps.map((step, index) => {
          const grade = gradeOf(step);
          if (index === 0) {
            return (
              <div className="cmp-step cmp-step-initial" key={step.step_id}>
                <code>{step.step_id}</code>
                <span>初始结构（来自第 1 页）</span>
                <span className="cmp-badge exact">{BADGE.exact}</span>
              </div>
            );
          }
          return (
            <div className={`cmp-step ${grade === 'no_counterpart' ? 'cmp-step-bad' : ''}`} key={step.step_id}>
              <div className="cmp-step-head">
                <code>{step.step_id}</code>
                <span className={`cmp-badge ${grade}`}>{BADGE[grade]}</span>
                <span className="cmp-spacer" />
                <button type="button" onClick={() => move(index, -1)} title="上移">↑</button>
                <button type="button" onClick={() => move(index, 1)} title="下移">↓</button>
                <button type="button" onClick={() => onChange({ ...draft, steps: draft.steps.filter((_, i) => i !== index) })}>删除</button>
              </div>
              <div className="cmp-grid">
                <label>
                  机制
                  <select
                    value={`${step.kind}/${step.mechanism}`}
                    onChange={(event) => {
                      const option = KIND_OPTIONS.find((row) => `${row.kind}/${row.mechanism}` === event.target.value);
                      if (option) patchStep(index, { kind: option.kind, mechanism: option.mechanism });
                    }}
                  >
                    {KIND_OPTIONS.map((row) => (
                      <option key={`${row.kind}/${row.mechanism}`} value={`${row.kind}/${row.mechanism}`}>{row.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  时长 (min)
                  <input type="number" step="0.005" value={step.duration}
                         onChange={(event) => patchStep(index, { duration: Number(event.target.value) })} />
                </label>
                <label>
                  自定义模型覆写 (可选)
                  <input
                    type="text"
                    placeholder="如 cmp_dishing"
                    value={step.model ?? ''}
                    onChange={(event) => patchStep(index, { model: event.target.value.trim() || null })}
                  />
                </label>
              </div>
              <table className="cmp-rates">
                <thead><tr><th>材料</th><th>速率 (µm/min，负=去除)</th></tr></thead>
                <tbody>
                  {MATERIALS.map((material) => (
                    <tr key={material}>
                      <td>{material}</td>
                      <td>
                        <input
                          type="number" step="0.01" value={step.rates[material] ?? 0}
                          onChange={(event) => {
                            const rates = { ...step.rates };
                            const value = Number(event.target.value);
                            if (value === 0) delete rates[material]; else rates[material] = value;
                            patchStep(index, { rates });
                          }}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        })}

        <div className="cmp-row">
          <button type="button"
                  onClick={() => onChange({
                    ...draft,
                    steps: [...draft.steps, {
                      step_id: `s${draft.steps.length}_etch`, kind: 'etch', mechanism: 'isotropic',
                      duration: 0.02, rates: { SiO2: -1.0 }, comparability: 'exact',
                    }],
                  })}>
            增加步骤
          </button>
        </div>

        <h3>渲染视角（多引擎共用）</h3>
        <div className="cmp-row">
          <label className="cmp-check">
            <input type="radio" checked={draft.cameraMode === 'free'}
                   onChange={() => onChange({ ...draft, cameraMode: 'free' })} />
            自由相机
          </label>
          <label className="cmp-check">
            <input type="radio" checked={draft.cameraMode === 'preset'}
                   onChange={() => onChange({ ...draft, cameraMode: 'preset' })} />
            预设
          </label>
          {draft.cameraMode === 'preset'
            ? <select value={draft.view} onChange={(event) => onChange({ ...draft, view: event.target.value as Draft['view'] })}>
                {['iso', 'iso90', 'iso180', 'iso270', 'xz', 'yz', 'xy'].map((view) => (
                  <option key={view} value={view}>{view}</option>
                ))}
              </select>
            : null}
        </div>
        {draft.cameraMode === 'free' ? (
          <div className="cmp-grid">
            <label>方位 {draft.camera.azimuth}°
              <input type="range" min="0" max="360" value={draft.camera.azimuth}
                     onChange={(event) => onChange({ ...draft, camera: { ...draft.camera, azimuth: Number(event.target.value) } })} />
            </label>
            <label>仰角 {draft.camera.elevation}°
              <input type="range" min="-85" max="85" value={draft.camera.elevation}
                     onChange={(event) => onChange({ ...draft, camera: { ...draft.camera, elevation: Number(event.target.value) } })} />
            </label>
            <label>缩放 {draft.camera.zoom.toFixed(2)}
              <input type="range" min="0.4" max="3" step="0.05" value={draft.camera.zoom}
                     onChange={(event) => onChange({ ...draft, camera: { ...draft.camera, zoom: Number(event.target.value) } })} />
            </label>
          </div>
        ) : null}

        <h3>运行引擎（自动发现自引擎注册表）</h3>
        <div className="cmp-row">
          {availableEngines.map((item) => (
            <label key={item.engineId} className="cmp-check" title={item.rationale || item.label}>
              <input type="checkbox" checked={engines.includes(item.engineId)}
                     onChange={() => onToggleEngine(item.engineId)} />
              {item.engineId}
            </label>
          ))}
          <label className="cmp-check" title="串行执行可消除多引擎 CPU/缓存抢占噪声，用于速度基准测试">
            <input type="checkbox" checked={sequential}
                   onChange={(e) => onToggleSequential(e.target.checked)} />
            串行测速
          </label>
          <button type="button" disabled={busy || engines.length === 0} onClick={onRun}>
            {busy ? '运行中…' : '开始对比运行'}
          </button>
        </div>
        {error ? <p className="cmp-error">{error}</p> : null}
      </div>

      <div className="cmp-preview">
        <h3>编译产物 · StepIR</h3>
        <pre className="cmp-json">{JSON.stringify(job, null, 2)}</pre>
      </div>
    </section>
  );
}
