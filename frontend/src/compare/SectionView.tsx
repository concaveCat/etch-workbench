/** Cross-section overlay: the two engines' interface polylines on one axis.
 *
 * Both sides are asked for the same columns (x positions) and the same materials,
 * so the polylines are directly comparable. The dashed line is SPX, the solid one
 * ViennaPS; the wall/void boundary is intentionally not drawn because a periodic
 * ViennaPS cell has no side wall.
 */

import type { SectionMetrics } from './api';

type Props = {
  section: SectionMetrics | undefined;
  width: number;
  height: number;
  zRange: number[] | null | undefined;
  engines: string[];
  domainWidth: number;
};

const COLORS: Record<string, string> = {
  Si: '#90a4ae', SiO2: '#00e5ff', Si3N4: '#dce775', PolySi: '#ff7043',
  SiGe: '#26a69a', Al2O3: '#80cbc4', HfO2: '#ef5350', TiN: '#bdbdbd',
  W: '#78909c', Cu: '#ffb74d', PMMA: '#fff176', Mask: '#fff176', void: '#37474f',
};

export function SectionView({ section, width, height, zRange, engines, domainWidth }: Props) {
  const materials = Object.keys(section?.materials ?? {});
  const heights = section?.heights ?? {};
  // Fit the vertical axis to the data: the domain is 2.5 µm tall while every
  // interface sits inside 0.4 µm, which flattened the curves onto the axis floor.
  const values: number[] = [];
  for (const perEngine of Object.values(heights)) {
    for (const rows of Object.values(perEngine)) {
      for (const value of rows) if (typeof value === 'number') values.push(value);
    }
  }
  const dataMin = values.length ? Math.min(...values) : -0.1;
  const dataMax = values.length ? Math.max(...values) : 0.4;
  const pad = Math.max((dataMax - dataMin) * 0.15, 0.01);
  const [zMin, zMax] = values.length
    ? [dataMin - pad, dataMax + pad]
    : (zRange ?? [-0.3, 0.4]);
  const plotW = width - 96;
  const plotH = height - 56;
  const xFor = (x: number) => 56 + (x / (domainWidth || 1)) * plotW;
  const yFor = (z: number) => 20 + ((zMax - z) / (zMax - zMin || 1)) * plotH;

  if (!section || section.status !== 'measured') {
    return (
      <div className="cmp-section-empty">
        截面：{section?.status ?? '无数据'}
        {section?.reason ? ` · ${section.reason}` : ''}
      </div>
    );
  }

  return (
    <div className="cmp-section">
      <svg width={width} height={height} role="img" aria-label="截面轮廓对比">
        <rect x={56} y={20} width={plotW} height={plotH} fill="#0a1a29" stroke="#16324a" />
        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const z = zMax - fraction * (zMax - zMin);
          return (
            <g key={fraction}>
              <line x1={56} x2={56 + plotW} y1={yFor(z)} y2={yFor(z)} stroke="#12283a" />
              <text x={50} y={yFor(z) + 3} fontSize={9} fill="#5f7d93" textAnchor="end">
                {z.toFixed(2)}
              </text>
            </g>
          );
        })}
        <text x={8} y={14} fontSize={9} fill="#5f7d93">z (µm)</text>
        {materials.map((material) => {
          const rows = engines.map((engineId, index) => {
            const values = heights[engineId]?.[material] ?? [];
            const points = values
              .map((z, i) => (z === null ? null : `${xFor((i + 0.5) / values.length * domainWidth)},${yFor(z)}`))
              .filter((value): value is string => value !== null);
            if (points.length < 2) return null;
            return (
              <polyline
                key={`${material}-${engineId}`}
                points={points.join(' ')}
                fill="none"
                stroke={COLORS[material] ?? '#90a4ae'}
                strokeWidth={index === 0 ? 2 : 1.4}
                strokeDasharray={index === 0 ? undefined : '5 3'}
                opacity={index === 0 ? 1 : 0.85}
              />
            );
          });
          return <g key={material}>{rows}</g>;
        })}
      </svg>
      <div className="cmp-section-legend">
        {materials.map((material) => {
          const row = section.materials?.[material];
          return (
            <span key={material} className="cmp-section-row">
              <i style={{ background: COLORS[material] ?? '#90a4ae' }} />
              {material}
              {row?.status === 'measured'
                ? ` 平均 |Δz| ${((row.meanAbsUm ?? 0) * 1000).toFixed(2)} nm · 最大 ${((row.maxAbsUm ?? 0) * 1000).toFixed(2)} nm`
                : ` ${row?.status ?? '—'}`}
            </span>
          );
        })}
        <span className="cmp-note">实线 ViennaPS · 虚线 SPX · 两侧同列同材料</span>
      </div>
    </div>
  );
}
