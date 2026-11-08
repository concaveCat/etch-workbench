import { useEffect, useMemo, useRef, useState, type PointerEvent, type RefObject } from 'react';
import type { MaterialCatalogItem, Snapshot, ViewSettings } from '../api/types';
import { linePathsByMaterial } from './linePaths';

// 2D snapshot viewer. Pan/zoom state lives in the caller (`view`), so this
// component only reports changes through `onViewChange`.

export function SvgViewer({ current, compare, view, onViewChange, svgRef, catalog }: {
  current: Snapshot | null; compare: Snapshot | null; view: ViewSettings; onViewChange: (next: Partial<ViewSettings>) => void; svgRef: RefObject<SVGSVGElement | null>; catalog: MaterialCatalogItem[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 900, height: 520 });
  const dragRef = useRef<{ x: number; y: number; panX: number; panH: number } | null>(null);
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver(items => {
      const rect = items[0]?.contentRect;
      if (rect && rect.width > 0 && rect.height > 0) setSize({ width: Math.max(1, Math.round(rect.width)), height: Math.max(1, Math.round(rect.height)) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const allPoints = useMemo(() => [...(current?.points ?? []), ...(compare?.points ?? [])], [current, compare]);
  const bounds = useMemo(() => {
    if (!allPoints.length) return { minX: -2.5, maxX: 2.5, minH: -2.5, maxH: 0.5 };
    let minX = Infinity; let maxX = -Infinity; let minH = Infinity; let maxH = -Infinity;
    for (const point of allPoints) { minX = Math.min(minX, point[0]); maxX = Math.max(maxX, point[0]); minH = Math.min(minH, point[1]); maxH = Math.max(maxH, point[1]); }
    const dx = Math.max(0.4, maxX - minX) * 0.08; const dh = Math.max(0.4, maxH - minH) * 0.12;
    return { minX: minX - dx, maxX: maxX + dx, minH: minH - dh, maxH: maxH + dh };
  }, [allPoints]);
  const currentPaths = useMemo(() => linePathsByMaterial(current), [current]);
  const comparePaths = useMemo(() => linePathsByMaterial(compare), [compare]);
  const centerX = (bounds.minX + bounds.maxX) / 2 + view.panX;
  const centerH = (bounds.minH + bounds.maxH) / 2 + view.panH;
  const scale = Math.max(1e-3, Math.min((size.width - 112) / Math.max(0.01, bounds.maxX - bounds.minX), (size.height - 104) / Math.max(0.01, bounds.maxH - bounds.minH)) * view.zoom);
  const worldX = (x: number) => size.width / 2 + (x - centerX) * scale;
  const worldY = (h: number) => size.height / 2 - (h - centerH) * scale;
  const delta = current?.gridDelta ?? compare?.gridDelta ?? 0.1;
  const coordinateUnit = current?.coordinateUnit === 'nm' || compare?.coordinateUnit === 'nm' ? 'nm' : 'µm';
  const ids = useMemo(() => [...new Set([...(current?.cellData.materialIds ?? []), ...(compare?.cellData.materialIds ?? [])])].sort((a, b) => a - b), [current, compare]);
  const rawGridCount = Math.ceil((bounds.maxX - bounds.minX) / delta) + Math.ceil((bounds.maxH - bounds.minH) / delta);
  const gridEvery = Math.max(1, Math.ceil(rawGridCount / 260));
  const gridSpacing = delta * gridEvery;
  const gridX: number[] = []; const gridH: number[] = [];
  if (view.showGrid) {
    for (let value = Math.ceil(bounds.minX / gridSpacing) * gridSpacing; value <= bounds.maxX + delta * 0.01; value += gridSpacing) gridX.push(value);
    for (let value = Math.ceil(bounds.minH / gridSpacing) * gridSpacing; value <= bounds.maxH + delta * 0.01; value += gridSpacing) gridH.push(value);
  }
  const colorFor = (id: number) => current?.materials[String(id)]?.color ?? compare?.materials[String(id)]?.color ?? catalog.find(item => item.id === id)?.color ?? '#8064a2';
  const panStart = (event: PointerEvent<SVGSVGElement>) => {
    dragRef.current = { x: event.clientX, y: event.clientY, panX: view.panX, panH: view.panH };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const panMove = (event: PointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current; if (!drag) return;
    onViewChange({ panX: drag.panX - (event.clientX - drag.x) / scale, panH: drag.panH + (event.clientY - drag.y) / scale });
  };
  const getScaleBar = () => {
    const micrometers = 100 / scale;
    const power = 10 ** Math.floor(Math.log10(Math.max(micrometers, 0.001)));
    const normalized = micrometers / power;
    const chosen = (normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1) * power;
    return { length: chosen * scale, label: `${Number(chosen.toPrecision(3))} ${coordinateUnit}` };
  };
  const bar = getScaleBar();
  const left = 70; const right = size.width - 24; const top = 26; const bottom = size.height - 62;
  const axisNames = current?.materials ?? compare?.materials ?? {};
  const meshTransform = `translate(${size.width / 2 - centerX * scale} ${size.height / 2 + centerH * scale}) scale(${scale} ${-scale})`;
  return <div className="viewer-surface" ref={containerRef}>
    {current ? <svg ref={svgRef} className="mesh-svg" viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="none" role="img" aria-label="ViennaPS 二维材料界面查看器"
      onPointerDown={panStart} onPointerMove={panMove} onPointerUp={() => { dragRef.current = null; }} onPointerCancel={() => { dragRef.current = null; }}
      onWheel={event => { event.preventDefault(); onViewChange({ zoom: Math.min(20, Math.max(0.1, view.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12))) }); }}>
      <rect width={size.width} height={size.height} fill="#fbfcfd" />
      {view.showGrid && <g className="grid-lines">
        {gridX.map(x => <line key={`gx${x}`} x1={worldX(x)} y1={top} x2={worldX(x)} y2={bottom} />)}
        {gridH.map(h => <line key={`gh${h}`} x1={left} y1={worldY(h)} x2={right} y2={worldY(h)} />)}
      </g>}
      <line className="axis-line" x1={left} y1={worldY(0)} x2={right} y2={worldY(0)} />
      <line className="axis-line" x1={worldX(0)} y1={top} x2={worldX(0)} y2={bottom} />
      <g transform={meshTransform} fill="none">
        {compare && ids.map(id => <path key={`compare-${id}`} d={comparePaths.get(id) ?? ''} stroke={colorFor(id)} strokeWidth="2.2" strokeDasharray="7 4" opacity="0.48" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />)}
        {ids.map(id => <path key={`current-${id}`} d={currentPaths.get(id) ?? ''} stroke={colorFor(id)} strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />)}
      </g>
      <text className="axis-label" x={right - 3} y={Math.min(bottom - 4, worldY(0) - 8)} textAnchor="end">h ({coordinateUnit})</text>
      <text className="axis-label" x={right - 4} y={bottom + 34} textAnchor="end">x ({coordinateUnit})</text>
      <line className="scale-bar" x1={left + 12} y1={bottom + 14} x2={left + 12 + bar.length} y2={bottom + 14} />
      <line className="scale-bar" x1={left + 12} y1={bottom + 9} x2={left + 12} y2={bottom + 19} />
      <line className="scale-bar" x1={left + 12 + bar.length} y1={bottom + 9} x2={left + 12 + bar.length} y2={bottom + 19} />
      <text className="scale-label" x={left + 12} y={bottom + 32}>{bar.label}</text>
    </svg> : <div className="empty-view"><div className="empty-icon">⌁</div><strong>还没有可显示的网格</strong><span>先生成初始结构预览，所有界面都来自本机 ViennaPS。</span></div>}
    {current && <div className="viewer-legend">
      {ids.map(id => <span key={id}><i style={{ background: colorFor(id) }} />{axisNames[String(id)]?.name ?? catalog.find(item => item.id === id)?.name ?? `Material ${id}`}</span>)}
      {compare && <span><i className="legend-dashed" />对比结果</span>}
    </div>}
    {current && <div className="viewer-grid-note">{view.showGrid ? `网格辅助线：${gridEvery} 格一条（Δ=${delta} ${coordinateUnit}）` : `计算网格间距 Δ=${delta} ${coordinateUnit}`}</div>}
  </div>;
}
