/**
 * WebGL renderer detection for the 3D viewer.
 *
 * Headless CI and remote sandboxes fall back to SwiftShader/llvmpipe. The
 * viewer still works there, but it is slow, so the panel says so instead of
 * leaving the user with "3D is laggy" and no explanation. Real desktops report
 * the actual GPU (for example `ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop
 * GPU ...)`), and no warning is shown.
 */

export type WebglRendererInfo = {
  /** `UNMASKED_RENDERER_WEBGL`, or the generic `RENDERER` when unavailable. */
  renderer: string;
  vendor: string;
  /** True for SwiftShader, llvmpipe, Microsoft Basic Render and friends. */
  software: boolean;
};

const SOFTWARE_RENDERER_PATTERN = /swiftshader|llvmpipe|softpipe|software|microsoft basic render|basic render driver|mesa offscreen/i;

/** Probe a throwaway canvas; returns null when WebGL is unavailable. */
export function readWebglRenderer(): WebglRendererInfo | null {
  if (typeof document === 'undefined') return null;
  let gl: WebGLRenderingContext | null = null;
  try {
    const canvas = document.createElement('canvas');
    gl = (canvas.getContext('webgl2') ?? canvas.getContext('webgl')) as WebGLRenderingContext | null;
    if (!gl) return null;
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = String(
      (debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) ?? '',
    ).trim();
    const vendor = String(
      (debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR)) ?? '',
    ).trim();
    return {
      renderer: renderer || '未知 WebGL 渲染器',
      vendor,
      software: SOFTWARE_RENDERER_PATTERN.test(`${vendor} ${renderer}`),
    };
  } catch {
    return null;
  } finally {
    // Release the probe context; browsers cap the number of live WebGL contexts.
    try {
      gl?.getExtension('WEBGL_lose_context')?.loseContext();
    } catch {
      /* already gone */
    }
  }
}

/** Short form for the panel: drop the ANGLE wrapper when a GPU name follows. */
export function shortRendererName(renderer: string): string {
  const angle = /^ANGLE \(([^,]+),\s*(.+)\)$/.exec(renderer);
  if (angle) return `${angle[1]} · ${angle[2]}`;
  return renderer;
}
