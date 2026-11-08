/**
 * Colour resolution for the 3D viewer.
 *
 * Snapshot `materials` entries only carry a colour in newer schema versions, so
 * fall back to the shipped material catalogue (docs/materials-catalog.json) by
 * name, then to a stable palette, and finally to engineering grey for ids the
 * snapshot never describes.
 */
import type { MaterialInfo } from './types';

/** Colours from docs/materials-catalog.json (engine 4.6.2). */
const CATALOG_COLORS: Record<string, string> = {
  Mask: '#333333',
  Si: '#c79a08',
  SiO2: '#66ccff',
  Si3N4: '#00a8a8',
  SiN: '#00bfa5',
  PolySi: '#ed8713',
  SiGe: '#d66604',
  Al2O3: '#90caf9',
  HfO2: '#3f51b5',
  TiN: '#adb5bd',
  W: '#607d8b',
  Cu: '#b87333',
  Air: '#f2f4f7',
  Vacuum: '#f2f4f7',
};

const PALETTE = [
  '#4f8fdb',
  '#e0a13a',
  '#4fbf8b',
  '#c86ad4',
  '#e0655c',
  '#3fb5c8',
  '#a8b23c',
  '#8d7ce0',
];

export const UNKNOWN_MATERIAL_COLOR = '#9aa3ad';

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

export function normalizeHex(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!HEX.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`.toLowerCase();
  }
  return trimmed.toLowerCase();
}

/** Resolve one material to a `#rrggbb` colour. `ordinal` keeps unknowns stable. */
export function materialColor(info: MaterialInfo | undefined, ordinal: number): string {
  const explicit = normalizeHex(info?.color);
  if (explicit) return explicit;
  const name = info?.name;
  if (typeof name === 'string' && CATALOG_COLORS[name]) return CATALOG_COLORS[name];
  if (!info) return UNKNOWN_MATERIAL_COLOR;
  return PALETTE[Math.abs(ordinal) % PALETTE.length];
}

export function materialName(id: number, info: MaterialInfo | undefined): string {
  const name = info?.name;
  return typeof name === 'string' && name.trim() ? name.trim() : `材料 ${id}`;
}

export function hexToRgb(color: string): [number, number, number] {
  const hex = normalizeHex(color) ?? UNKNOWN_MATERIAL_COLOR;
  const value = Number.parseInt(hex.slice(1), 16);
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255];
}

/** Relative luminance, used to pick readable legend text. */
export function luminance(color: string): number {
  const [r, g, b] = hexToRgb(color);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
