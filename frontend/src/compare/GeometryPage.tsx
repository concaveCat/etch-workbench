/** P1 - initial structure: the geometry both engines must start from. */

import type { Draft } from './draft';
import { MATERIALS, MATERIAL_COLORS } from './draft';

type Props = { draft: Draft; onChange: (next: Draft) => void };

export function GeometryPage({ draft, onChange }: Props) {
  const { initial, domain } = draft;
  const totalHeight = initial.substrate_thickness + initial.films.reduce((sum, f) => sum + f.thickness, 0);

  const patchInitial = (patch: Partial<Draft['initial']>) => onChange({ ...draft, initial: { ...initial, ...patch } });
  const patchDomain = (patch: Partial<Draft['domain']>) => onChange({ ...draft, domain: { ...domain, ...patch } });

  const updateFilm = (index: number, patch: Partial<{ material: string; thickness: number }>) => {
    const films = initial.films.map((film, i) => (i === index ? { ...film, ...patch } : film));
    patchInitial({ films });
  };

  return (
    <section className="cmp-page">
      <div className="cmp-form">
        <h2>1 · 初始几何</h2>
        <p className="cmp-hint">
          两端共用的入口结构。单位 µm / min；z 向上，原点在衬底上表面中心。
          工艺仿真期间不再转换几何表示。
        </p>

        <label>
          案例名
          <input value={draft.caseId} onChange={(event) => onChange({ ...draft, caseId: event.target.value })} />
        </label>

        <label>
          结构类型
          <select value={initial.kind} onChange={(event) => patchInitial({ kind: event.target.value as 'plane' | 'trench' })}>
            <option value="plane">平面（phase A）</option>
            <option value="trench" disabled>沟槽（需要 GDS 掩膜，phase B）</option>
          </select>
        </label>

        <label>
          衬底材料
          <select value={initial.substrate} onChange={(event) => patchInitial({ substrate: event.target.value })}>
            {MATERIALS.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>

        <label>
          衬底厚度 (µm)
          <input type="number" step="0.01" value={initial.substrate_thickness}
                 onChange={(event) => patchInitial({ substrate_thickness: Number(event.target.value) })} />
        </label>

        <h3>薄膜栈（自下而上）</h3>
        {initial.films.map((film, index) => (
          <div className="cmp-row" key={index}>
            <select value={film.material} onChange={(event) => updateFilm(index, { material: event.target.value })}>
              {MATERIALS.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            <input type="number" step="0.005" value={film.thickness}
                   onChange={(event) => updateFilm(index, { thickness: Number(event.target.value) })} />
            <button type="button" onClick={() => patchInitial({ films: initial.films.filter((_, i) => i !== index) })}>删除</button>
          </div>
        ))}
        <button type="button" onClick={() => patchInitial({ films: [...initial.films, { material: 'SiO2', thickness: 0.05 }] })}>
          增加薄膜
        </button>

        <h3>计算域</h3>
        <div className="cmp-grid">
          <label>宽 X (µm)<input type="number" step="0.01" value={domain.width}
               onChange={(event) => patchDomain({ width: Number(event.target.value) })} /></label>
          <label>深 Y (µm)<input type="number" step="0.01" value={domain.depth}
               onChange={(event) => patchDomain({ depth: Number(event.target.value) })} /></label>
          <label>高 Z (µm)<input type="number" step="0.01" value={domain.height}
               onChange={(event) => patchDomain({ height: Number(event.target.value) })} /></label>
          <label>Δx (µm)<input type="number" step="0.001" value={domain.grid_delta}
               onChange={(event) => patchDomain({ grid_delta: Number(event.target.value) })} /></label>
        </div>
        <p className="cmp-hint">Δx 只对 ViennaPS 有意义；SPX 是几何/经验模型，不栅格化。</p>
      </div>

      <div className="cmp-preview">
        <h3>材料栈示意</h3>
        <svg viewBox="0 0 220 260" className="cmp-stack">
          {initial.films.map((film, index) => {
            const height = (film.thickness / totalHeight) * 200;
            const below = initial.films.slice(0, index).reduce((sum, f) => sum + f.thickness, 0);
            const y = 220 - ((below + film.thickness) / totalHeight) * 200;
            return (
              <g key={index}>
                <rect x="20" y={y} width="180" height={height} fill={MATERIAL_COLORS[film.material] ?? '#888'} />
                <text x="205" y={y + height / 2 + 4} fontSize="9" fill="#cfe">
                  {film.material} {film.thickness}µm
                </text>
              </g>
            );
          })}
          <rect x="20" y={220 - (initial.substrate_thickness / totalHeight) * 200}
                width="180" height={(initial.substrate_thickness / totalHeight) * 200}
                fill={MATERIAL_COLORS[initial.substrate] ?? '#888'} />
          <text x="6" y={250} fontSize="9" fill="#7cf">{initial.substrate}</text>
        </svg>
        <p className="cmp-hint">总高 {totalHeight.toFixed(3)} µm · 域 {domain.width}×{domain.depth}×{domain.height} µm</p>
      </div>
    </section>
  );
}
