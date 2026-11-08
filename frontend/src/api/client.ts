import type { Capabilities } from './types';

// Thin fetch wrapper for the workbench HTTP API. Every non-JSON response is
// treated as a backend-address/service problem instead of being cast blindly.
export class ApiError extends Error {
  detail: unknown;
  constructor(message: string, detail: unknown) { super(message); this.detail = detail; }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers } });
  const contentType = response.headers.get('content-type') ?? '';
  const data = contentType.includes('json') ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === 'object' && data !== null && 'detail' in data ? (data as { detail: unknown }).detail : data;
    let message = response.statusText || `HTTP ${response.status}`;
    if (typeof detail === 'string') message = detail;
    else if (Array.isArray(detail) && detail[0]) message = `${detail[0].path ? `${detail[0].path}：` : ''}${detail[0].message ?? ''}`;
    else if (typeof detail === 'object' && detail !== null && 'message' in detail) message = String((detail as { message: unknown }).message);
    throw new ApiError(message, detail);
  }
  if (!contentType.toLowerCase().includes('json')) {
    throw new ApiError('本机工作台接口返回了非 JSON 内容，请检查后端地址或服务状态。', data);
  }
  return data as T;
}

export function saveBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function prettyError(error: unknown): string { return error instanceof Error ? error.message : String(error); }

export const ARTIFACT_CAP_FALLBACK_BYTES = 512 * 1024 * 1024;
// The backend publishes the effective run-artifact cap in /api/capabilities
// (deployments can raise it with WORKBENCH_MAX_ARTIFACT_MIB, default 4096 MiB).
// Prefer the published byte/MiB fields; the 512 MiB historical guard is only a
// fallback for backends that predate the configurable cap.
export function effectiveArtifactCapBytes(capabilities: Capabilities | null | undefined): number {
  const run = capabilities?.resourcePolicy?.run;
  const preflight = run?.artifactPreflight;
  const resolved = [
    run?.maximumArtifactBytes,
    run?.artifactLimitBytes,
    run?.maximumArtifactMiB != null ? run.maximumArtifactMiB * 1024 * 1024 : null,
    run?.artifactLimitMiB != null ? run.artifactLimitMiB * 1024 * 1024 : null,
    preflight?.capBytes,
    preflight?.capMiB != null ? preflight.capMiB * 1024 * 1024 : null,
    run?.maximumResultArchiveBytes,
  ].find(value => typeof value === 'number' && Number.isFinite(value) && value > 0);
  return resolved ?? ARTIFACT_CAP_FALLBACK_BYTES;
}
export function formatArtifactCapMiB(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024)).toLocaleString('zh-CN')} MiB`;
}
