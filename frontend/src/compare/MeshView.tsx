/** Orthographic mesh renderer with a camera object shared with the raster pane.
 *
 * The comparison only works if both sides show the same view. SPX renders with a
 * fixed camera, so the mesh side is projected with a fixed isometric default and
 * the shared camera state (azimuth/elevation/zoom) is applied to both panes: the
 * raster pane cannot re-project, so it exposes the same values as metadata and
 * draws the frame it was given.
 */

import { useEffect, useRef } from 'react';
import type { MeshFrame } from './api';

export type Camera = { azimuth: number; elevation: number; zoom: number };

export const DEFAULT_CAMERA: Camera = { azimuth: 35, elevation: 22, zoom: 1 };

type Props = {
  frame: MeshFrame | null;
  camera: Camera;
  width: number;
  height: number;
  title: string;
  note?: string;
};

function boxCorners(bbox: number[]): number[][] {
  const [x0, y0, z0, x1, y1, z1] = bbox;
  return [
    [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
    [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
  ];
}

const BOX_EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];

function drawBox(ctx: CanvasRenderingContext2D, corners: { x: number; y: number }[], width: number, _height: number) {
  ctx.save();
  ctx.strokeStyle = 'rgba(90,150,190,0.35)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  for (const [a, b] of BOX_EDGES) {
    ctx.beginPath();
    ctx.moveTo(corners[a].x, corners[a].y);
    ctx.lineTo(corners[b].x, corners[b].y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(140,190,220,0.55)';
  ctx.font = '10px ui-monospace, monospace';
  ctx.fillText(`域 ${width | 0}px · 虚线为计算域 bbox（ViennaPS 周期边界无侧壁）`, 8, 14);
  ctx.restore();
}

function project(points: number[][], camera: Camera, bbox: number[], width: number, height: number) {
  const [x0, y0, z0, x1, y1, z1] = bbox;
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  const cz = (z0 + z1) / 2;
  const span = Math.max(x1 - x0, y1 - y0, z1 - z0) || 1;
  const az = (camera.azimuth * Math.PI) / 180;
  const el = (camera.elevation * Math.PI) / 180;
  const scale = (Math.min(width, height) / span) * 0.75 * camera.zoom;
  return points.map(([px, py, pz]) => {
    const dx = px - cx;
    const dy = py - cy;
    const dz = pz - cz;
    // rotate about z, then tilt; y is flipped for canvas coordinates
    const rx = dx * Math.cos(az) - dy * Math.sin(az);
    const ry = dx * Math.sin(az) + dy * Math.cos(az);
    const sx = rx;
    const sy = -dz * Math.cos(el) + ry * Math.sin(el);
    const depth = ry * Math.cos(el) + dz * Math.sin(el);
    return { x: width / 2 + sx * scale, y: height / 2 + sy * scale, depth };
  });
}

export function MeshView({ frame, camera, width, height, title, note }: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#0b1620';
    ctx.fillRect(0, 0, width, height);
    if (!frame) return;
    drawBox(ctx, project(boxCorners(frame.bbox), camera, frame.bbox, width, height), width, height);
    const colors = new Map(frame.materials.map((m) => [m.id, m.color]));
    const projected = project(frame.points, camera, frame.bbox, width, height);
    const faces = frame.triangles.map((tri, index) => {
      const [a, b, c] = tri.map((i) => projected[i]);
      return {
        depth: (a.depth + b.depth + c.depth) / 3,
        color: colors.get(frame.materialIds[index]) ?? '#888888',
        a, b, c,
      };
    });
    faces.sort((left, right) => left.depth - right.depth);
    for (const face of faces) {
      const shade = 0.65 + 0.35 * Math.min(1, Math.max(-1, face.depth));
      ctx.fillStyle = face.color;
      ctx.globalAlpha = 0.85 * shade + 0.15;
      ctx.beginPath();
      ctx.moveTo(face.a.x, face.a.y);
      ctx.lineTo(face.b.x, face.b.y);
      ctx.lineTo(face.c.x, face.c.y);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 0.25;
      ctx.strokeStyle = face.color;
      ctx.lineWidth = 0.3;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }, [frame, camera, width, height]);

  return (
    <figure className="cmp-pane">
      <figcaption>
        <strong>{title}</strong>
        {note ? <span className="cmp-note">{note}</span> : null}
      </figcaption>
      <canvas ref={ref} style={{ width, height, borderRadius: 8 }} />
    </figure>
  );
}
