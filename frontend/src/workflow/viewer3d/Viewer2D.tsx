/**
 * 2D snapshot renderer: a plain canvas wireframe of the native interface lines.
 *
 * 2D snapshots are line sets (`points` + `lines` + per-line material ids), so
 * they are drawn directly instead of being faked through the 3D engine. The
 * horizontal axis is `axisMap.x` and the vertical axis is `axisMap.z`
 * (ViennaPS 2D uses x/z with z as height).
 */
import { useEffect, useRef } from 'react';
import type { ParsedMesh } from './types';

type Props = {
  mesh: ParsedMesh;
  /** Hidden material ids (not indices). */
  hidden: ReadonlySet<number>;
};

type View = { zoom: number; panX: number; panY: number };

const BACKGROUND = '#f7f9fb';
const GRID = '#e3e8ee';
const AXIS = '#94a3b0';
const TEXT = '#4b5b6b';

/** Round 1/2/5 × 10^k step that yields roughly `target` ticks. */
function niceStep(span: number, target: number): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const raw = span / Math.max(1, target);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalized = raw / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function formatTick(value: number, step: number): string {
  const digits = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step)));
  return value.toFixed(digits);
}

export default function Viewer2D({ mesh, hidden }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewRef = useRef<View>({ zoom: 1, panX: 0, panY: 0 });
  const dragRef = useRef<{ x: number; y: number } | null>(null);
  const frameRef = useRef<number | null>(null);
  const scheduleRef = useRef<() => void>(() => undefined);

  const horizontal = mesh.axisMap.x;
  const vertical = mesh.axisMap.z;
  const hiddenIndices = new Set<number>();
  mesh.materials.forEach((material, index) => {
    if (hidden.has(material.id)) hiddenIndices.add(index);
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = (): void => {
      const parent = canvas.parentElement;
      if (!parent) return;
      const dpr = window.devicePixelRatio || 1;
      const width = Math.max(1, parent.clientWidth);
      const height = Math.max(1, parent.clientHeight);
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
      }
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = BACKGROUND;
      ctx.fillRect(0, 0, width, height);

      const positions = mesh.positions;
      const pointCount = mesh.pointCount;
      let minU = Infinity;
      let maxU = -Infinity;
      let minV = Infinity;
      let maxV = -Infinity;
      for (let i = 0; i < pointCount; i++) {
        const u = positions[i * 3 + horizontal];
        const v = positions[i * 3 + vertical];
        if (u < minU) minU = u;
        if (u > maxU) maxU = u;
        if (v < minV) minV = v;
        if (v > maxV) maxV = v;
      }
      if (!Number.isFinite(minU)) {
        minU = -1;
        maxU = 1;
        minV = -1;
        maxV = 1;
      }
      const spanU = Math.max(maxU - minU, 1e-9);
      const spanV = Math.max(maxV - minV, 1e-9);
      const padding = { left: 56, right: 24, top: 20, bottom: 42 };
      const drawWidth = Math.max(1, width - padding.left - padding.right);
      const drawHeight = Math.max(1, height - padding.top - padding.bottom);
      const baseScale = Math.min(drawWidth / spanU, drawHeight / spanV);
      const view = viewRef.current;
      const scale = baseScale * view.zoom;
      const centerU = (minU + maxU) / 2;
      const centerV = (minV + maxV) / 2;
      const originX = padding.left + drawWidth / 2 + view.panX;
      const originY = padding.top + drawHeight / 2 + view.panY;
      const toX = (u: number): number => originX + (u - centerU) * scale;
      const toY = (v: number): number => originY - (v - centerV) * scale;

      // Engineering grid at the tick spacing of the visible range.
      const step = niceStep(Math.max(spanU, spanV) / view.zoom, 6);
      ctx.strokeStyle = GRID;
      ctx.lineWidth = 1;
      const startU = Math.ceil((centerU - (originX - padding.left) / scale) / step) * step;
      for (let u = startU; toX(u) < width - padding.right + 1; u += step) {
        const x = Math.round(toX(u)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x, padding.top);
        ctx.lineTo(x, height - padding.bottom);
        ctx.stroke();
      }
      const startV = Math.ceil((centerV - (height - padding.bottom - originY) / scale) / step) * step;
      for (let v = startV; toY(v) > padding.top - 1; v += step) {
        const y = Math.round(toY(v)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(width - padding.right, y);
        ctx.stroke();
      }

      // Interface lines, one colour per material.
      const lineWidth = Math.max(1, Math.min(2.4, 1.1 * Math.sqrt(view.zoom)));
      ctx.lineWidth = lineWidth;
      ctx.lineCap = 'round';
      const lines = mesh.lines;
      let drawn = 0;
      for (let i = 0; i < mesh.lineCount; i++) {
        const colorIndex = mesh.lineColor[i];
        if (hiddenIndices.has(colorIndex)) continue;
        const a = lines[i * 2];
        const b = lines[i * 2 + 1];
        if (a >= pointCount || b >= pointCount) continue;
        ctx.strokeStyle = mesh.materials[colorIndex]?.color ?? '#9aa3ad';
        ctx.beginPath();
        ctx.moveTo(toX(positions[a * 3 + horizontal]), toY(positions[a * 3 + vertical]));
        ctx.lineTo(toX(positions[b * 3 + horizontal]), toY(positions[b * 3 + vertical]));
        ctx.stroke();
        drawn += 1;
      }

      // Axes with tick labels.
      ctx.strokeStyle = AXIS;
      ctx.fillStyle = TEXT;
      ctx.lineWidth = 1;
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
      const axisY = height - padding.bottom;
      const axisX = padding.left;
      ctx.beginPath();
      ctx.moveTo(axisX, axisY + 0.5);
      ctx.lineTo(width - padding.right, axisY + 0.5);
      ctx.moveTo(axisX + 0.5, padding.top);
      ctx.lineTo(axisX + 0.5, axisY);
      ctx.stroke();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      for (let u = startU; toX(u) < width - padding.right + 1; u += step) {
        const x = toX(u);
        if (x < axisX) continue;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, axisY);
        ctx.lineTo(Math.round(x) + 0.5, axisY + 4);
        ctx.stroke();
        ctx.fillText(formatTick(u, step), x, axisY + 6);
      }
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (let v = startV; toY(v) > padding.top - 1; v += step) {
        const y = toY(v);
        if (y > axisY) continue;
        ctx.beginPath();
        ctx.moveTo(axisX - 4, Math.round(y) + 0.5);
        ctx.lineTo(axisX, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.fillText(formatTick(v, step), axisX - 7, y);
      }
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(`x / ${mesh.coordinateUnit}`, width - padding.right - 44, axisY + 26);
      ctx.save();
      ctx.translate(14, padding.top + 10);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = 'right';
      ctx.fillText(`${vertical === 2 ? 'z' : 'y'} / ${mesh.coordinateUnit}`, 0, 0);
      ctx.restore();

      // Scale bar: a round length that stays readable at any zoom.
      const barStep = niceStep(spanU / view.zoom, 5);
      const barPixels = barStep * scale;
      if (barPixels > 12 && barPixels < drawWidth) {
        const x0 = width - padding.right - barPixels;
        const y0 = padding.top + 14;
        ctx.strokeStyle = TEXT;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x0 + barPixels, y0);
        ctx.moveTo(x0, y0 - 4);
        ctx.lineTo(x0, y0 + 4);
        ctx.moveTo(x0 + barPixels, y0 - 4);
        ctx.lineTo(x0 + barPixels, y0 + 4);
        ctx.stroke();
        ctx.fillStyle = TEXT;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';
        ctx.fillText(`${formatTick(barStep, barStep)} ${mesh.coordinateUnit}`, x0 + barPixels / 2, y0 - 6);
      }

      if (drawn === 0) {
        ctx.fillStyle = '#6b7a8a';
        ctx.font = '13px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('当前材料均被隐藏', width / 2, height / 2);
      }
    };

    const schedule = (): void => {
      if (frameRef.current !== null) return;
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null;
        draw();
      });
    };
    scheduleRef.current = schedule;
    schedule();
    const observer = new ResizeObserver(schedule);
    if (canvas.parentElement) observer.observe(canvas.parentElement);
    return () => {
      observer.disconnect();
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
      scheduleRef.current = () => undefined;
    };
  }, [mesh, hidden, horizontal, vertical]);

  return (
    <canvas
      ref={canvasRef}
      className="wb-viewer3d__canvas2d"
      role="img"
      aria-label="二维工艺结构界面线"
      onWheel={(event) => {
        event.preventDefault();
        const view = viewRef.current;
        view.zoom = Math.min(60, Math.max(0.2, view.zoom * Math.exp(-event.deltaY * 0.0015)));
        scheduleRef.current();
      }}
      onPointerDown={(event) => {
        dragRef.current = { x: event.clientX, y: event.clientY };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const drag = dragRef.current;
        if (!drag) return;
        const view = viewRef.current;
        view.panX += event.clientX - drag.x;
        view.panY += event.clientY - drag.y;
        dragRef.current = { x: event.clientX, y: event.clientY };
        scheduleRef.current();
      }}
      onPointerUp={(event) => {
        dragRef.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
      onDoubleClick={() => {
        viewRef.current = { zoom: 1, panX: 0, panY: 0 };
        scheduleRef.current();
      }}
    />
  );
}
