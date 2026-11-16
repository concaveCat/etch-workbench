/** P2 - the compiled StepIR flow. The JSON is shown on purpose: it is the only
 * contract both engines agree on, so the page doubles as a debugger. */

import { useMemo } from 'react';
import type { Draft } from './draft';
import { MATERIALS, gradeOf, toJob } from './draft';

type Props = {
  draft: Draft;
  onChange: (next: Draft) => void;
  engines: string[];
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
  { kind: 'planarize', mechanism: 'cmp', label: '平坦化 / CMP' },
];

const BADGE: Record<string, string> = { exact: '可比', approximate: '近似可比', no_counterpart: '无可比实现' };

export function FlowPage({ draft, onChange, engines, onToggleEngine, onRun, busy, error }: Props) {
  const job = useMemo(() => toJob(draft), [draft]);
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
        <h2>2 · 工艺流程图</h2>
        <p className="cmp-hint">只开放两端都支持的机制。缺少可比实现的步骤会立即标红，不会等到运行。</p>

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

        <h3>渲染视角（左右共用）</h3>
        <p className="cmp-hint">
          SPX 的 <code>meas_screenshot(View=…)</code> 接受 iso/iso90/iso180/iso270 与 xy/xz/yz 等；
          左侧网格按同一方位投影。每次改视角都需要重新运行。
        </p>
        <div className="cmp-row">
          <select value={draft.view} onChange={(event) => onChange({ ...draft, view: event.target.value as Draft['view'] })}>
            {['iso', 'iso90', 'iso180', 'iso270', 'xz', 'yz', 'xy'].map((view) => (
              <option key={view} value={view}>{view}</option>
            ))}
          </select>
        </div>

        <h3>运行</h3>
        <div className="cmp-row">
          {['viennaps', 'spx'].map((engineId) => (
            <label key={engineId} className="cmp-check">
              <input type="checkbox" checked={engines.includes(engineId)}
                     onChange={() => onToggleEngine(engineId)} />
              {engineId}
            </label>
          ))}
          <button type="button" disabled={busy || engines.length === 0} onClick={onRun}>
            {busy ? '运行中…' : '开始对比运行'}
          </button>
        </div>
        {error ? <p className="cmp-error">{error}</p> : null}
      </div>

      <div className="cmp-preview">
        <h3>编译产物 · StepIR</h3>
        <p className="cmp-hint">这就是两端共用的唯一契约。<code>comparability</code> 由引擎对推导，不由流程填写。</p>
        <pre className="cmp-json">{JSON.stringify(job, null, 2)}</pre>
      </div>
    </section>
  );
}
