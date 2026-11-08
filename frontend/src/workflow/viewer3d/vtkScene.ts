/**
 * vtk.js scene manager for the process-structure viewer.
 *
 * Design constraints (see README):
 *  - exactly one actor / one mapper / one draw call for the whole structure,
 *    coloured by a per-cell scalar + lookup table,
 *  - the render window is created once per mount and never rebuilt when the
 *    step changes (only the poly data is swapped),
 *  - rendering is coalesced through `requestAnimationFrame`,
 *  - the camera survives step switches,
 *  - every vtk object created here is deleted in `destroy()`.
 *
 * vtk.js is loaded with dynamic `import()` so it never lands in the first
 * paint bundle.
 */
import { hexToRgb } from './colors';
import type { vtkActor } from '@kitware/vtk.js/Rendering/Core/Actor';
import type { vtkAxesActor } from '@kitware/vtk.js/Rendering/Core/AxesActor';
import type { vtkDataArray } from '@kitware/vtk.js/Common/Core/DataArray';
import type { vtkLookupTable } from '@kitware/vtk.js/Common/Core/LookupTable';
import type { vtkPlane } from '@kitware/vtk.js/Common/DataModel/Plane';
import type { vtkPolyData } from '@kitware/vtk.js/Common/DataModel/PolyData';
import type { vtkGenericRenderWindow } from '@kitware/vtk.js/Rendering/Misc/GenericRenderWindow';
import type { vtkMapper } from '@kitware/vtk.js/Rendering/Core/Mapper';
import type { vtkOrientationMarkerWidget } from '@kitware/vtk.js/Interaction/Widgets/OrientationMarkerWidget';
import type { ClipState, DisplayMode, ParsedMesh, Viewer3DMarker } from './types';

export type StandardView = 'iso' | 'xy' | 'xz' | 'yz';

/** [minX, minY, minZ, maxX, maxY, maxZ] */
type Bounds6 = [number, number, number, number, number, number];

export type VtkScene = {
  attach(mesh: ParsedMesh, hidden: ReadonlySet<number>): void;
  setHidden(hidden: ReadonlySet<number>): void;
  setDisplayMode(mode: DisplayMode): void;
  setProjection(mode: 'parallel' | 'perspective'): void;
  setClip(clip: ClipState): void;
  setMarker(marker: Viewer3DMarker | null): void;
  /** Fit the visible geometry while keeping the current camera direction. */
  resetView(): void;
  /** Fit only when the current camera no longer frames the geometry. */
  ensureVisible(): void;
  setStandardView(view: StandardView): void;
  rotate(azimuthDeg: number, elevationDeg: number): void;
  /** World units per CSS pixel of the current view (scale bar). */
  getUnitsPerPixel(): number;
  /** Called whenever the zoom level changes by more than a few percent. */
  setScaleListener(listener: ((unitsPerPixel: number) => void) | null): void;
  requestRender(): void;
  renderNow(): void;
  getFps(): number;
  getTriangleCount(): number;
  getPointCount(): number;
  /** Bounds of the geometry currently on screen (hidden/clipped removed). */
  getVisibleBounds(): Bounds6 | null;
  readPixels(): { width: number; height: number; pixels: Uint8Array } | null;
  destroy(): void;
};

type Geometry = {
  positions: Float32Array;
  polys: Uint32Array;
  pointColor: Float32Array;
  triangleCount: number;
};

/**
 * Drop the triangles of hidden materials. The vertex buffer and the per-vertex
 * colour scalars are shared by all materials, so hiding only rebuilds the index
 * buffer: still one actor and one draw call.
 */
function filterGeometry(geometry: Geometry, hidden: ReadonlySet<number>): Uint32Array {
  if (hidden.size === 0) return geometry.polys;
  const { polys, pointColor, triangleCount } = geometry;
  let kept = 0;
  for (let t = 0; t < triangleCount; t++) if (!hidden.has(pointColor[polys[t * 4 + 1]])) kept += 1;
  const outPolys = new Uint32Array(kept * 4);
  let w = 0;
  for (let t = 0; t < triangleCount; t++) {
    if (hidden.has(pointColor[polys[t * 4 + 1]])) continue;
    outPolys[w * 4] = 3;
    outPolys[w * 4 + 1] = polys[t * 4 + 1];
    outPolys[w * 4 + 2] = polys[t * 4 + 2];
    outPolys[w * 4 + 3] = polys[t * 4 + 3];
    w += 1;
  }
  return outPolys;
}

const AXIS_INDEX = { x: 0, y: 1, z: 2 } as const;

export async function createVtkScene(container: HTMLElement): Promise<VtkScene> {
  // The profile registers the OpenGL poly data mapper; it must load first.
  await import('@kitware/vtk.js/Rendering/OpenGL/Profiles/Geometry');
  const [genericModule, polyDataModule, mapperModule, actorModule, lutModule, dataArrayModule, planeModule, axesModule, widgetModule] =
    await Promise.all([
      import('@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'),
      import('@kitware/vtk.js/Common/DataModel/PolyData'),
      import('@kitware/vtk.js/Rendering/Core/Mapper'),
      import('@kitware/vtk.js/Rendering/Core/Actor'),
      import('@kitware/vtk.js/Common/Core/LookupTable'),
      import('@kitware/vtk.js/Common/Core/DataArray'),
      import('@kitware/vtk.js/Common/DataModel/Plane'),
      import('@kitware/vtk.js/Rendering/Core/AxesActor'),
      import('@kitware/vtk.js/Interaction/Widgets/OrientationMarkerWidget'),
    ]);

  const win: vtkGenericRenderWindow = genericModule.default.newInstance({
    background: [0.965, 0.968, 0.973],
    listenWindowResize: false,
  });
  win.setContainer(container);
  win.resize();
  // vtk.js defaults to `powerPreference: 'high-performance'`, but the default is
  // implicit and easy to lose across upgrades. Pin it before vtk's first
  // render creates the context, so laptops with iGPU + dGPU ask for the
  // discrete GPU (Windows may still hand Edge the iGPU unless the browser is
  // listed as "high performance" in the OS graphics settings).
  try {
    const apiWindow = win.getApiSpecificRenderWindow();
    // `get3DContext` exists on the OpenGL backend; WebGPU has no such knob.
    if ('get3DContext' in apiWindow) {
      (apiWindow as { get3DContext: (options: Record<string, unknown>) => unknown }).get3DContext({
        preserveDrawingBuffer: false,
        depth: true,
        alpha: true,
        powerPreference: 'high-performance',
      });
    }
  } catch {
    // The render pass creates the context with vtk's own defaults if this fails.
  }
  const renderer = win.getRenderer();
  const renderWindow = win.getRenderWindow();
  const interactor = win.getInteractor();

  const axes: vtkAxesActor = axesModule.default.newInstance();
  // The default palette (bright yellow Y axis) disappears on the light stage
  // background; deepen every shaft so the orientation marker stays readable.
  axes.setXAxisColor([178, 34, 34]);
  axes.setYAxisColor([161, 98, 7]);
  axes.setZAxisColor([21, 115, 71]);
  axes.update();
  const widget: vtkOrientationMarkerWidget = widgetModule.default.newInstance({
    actor: axes,
    interactor,
    parentRenderer: renderer,
  });
  widget.setEnabled(true);
  widget.setViewportSize(0.16);
  widget.setMinPixelSize(96);
  widget.setMaxPixelSize(160);

  const data: vtkPolyData = polyDataModule.default.newInstance();
  let lookupTable: vtkLookupTable = lutModule.default.newInstance();
  lookupTable.setTable([[0, 0, 0, 255], [255, 255, 255, 255]]);
  lookupTable.setRange(0, 1);
  const scalars: vtkDataArray = dataArrayModule.default.newInstance({
    name: 'material',
    numberOfComponents: 1,
    values: new Float32Array(0),
  });
  data.getPointData().setScalars(scalars);
  const normalsData: vtkDataArray = dataArrayModule.default.newInstance({
    name: 'normals',
    numberOfComponents: 3,
    values: new Float32Array(0),
  });
  data.getPointData().setNormals(normalsData);
  const mapper: vtkMapper = mapperModule.default.newInstance();
  mapper.setInputData(data);
  mapper.setLookupTable(lookupTable);
  mapper.setUseLookupTableScalarRange(true);
  mapper.setScalarModeToUsePointData();
  mapper.setColorModeToMapScalars();
  mapper.setInterpolateScalarsBeforeMapping(false);
  mapper.setScalarVisibility(true);
  const actor: vtkActor = actorModule.default.newInstance();
  actor.setMapper(mapper);
  // Real lighting: Gouraud shading over the worker-computed vertex normals,
  // so a curved surface is readable while the base colour still comes from
  // the material LUT.
  actor.getProperty().setInterpolationToGouraud();
  actor.getProperty().setAmbient(0.25);
  actor.getProperty().setDiffuse(0.75);
  actor.getProperty().setSpecular(0.15);
  actor.getProperty().setSpecularPower(24);
  renderer.addActor(actor);

  const plane: vtkPlane = planeModule.default.newInstance();
  const camera = renderer.getActiveCamera();
  camera.setParallelProjection(true);

  // ---- measurement marker: one thin red polyline, no lighting ------------
  const markerData: vtkPolyData = polyDataModule.default.newInstance();
  markerData.getPoints().setData(new Float32Array(6), 3);
  markerData.getLines().setData(new Uint32Array([2, 0, 1]));
  const markerMapper: vtkMapper = mapperModule.default.newInstance();
  markerMapper.setInputData(markerData);
  markerMapper.setScalarVisibility(false);
  const markerActor: vtkActor = actorModule.default.newInstance();
  markerActor.setMapper(markerMapper);
  markerActor.getProperty().setColor(0.706, 0.137, 0.094);
  markerActor.getProperty().setLineWidth(4);
  markerActor.getProperty().setLighting(false);
  markerActor.setVisibility(false);
  renderer.addActor(markerActor);

  let currentMesh: ParsedMesh | null = null;
  let hiddenMaterials: ReadonlySet<number> = new Set<number>();
  let currentClip: ClipState = { enabled: false, axis: 'x', position: 0, keep: 'positive' };
  let markerSpec: Viewer3DMarker | null = null;
  let markerBounds: Bounds6 | null = null;
  let cachedGeometryBounds: Bounds6 | null = null;
  let frameTimes: number[] = [];
  let rafId: number | null = null;
  let destroyed = false;
  let cameraInitialized = false;
  /** Viewport aspect used by the last fit; a resize past this re-frames. */
  let lastFitAspect = 0;
  let scaleListener: ((unitsPerPixel: number) => void) | null = null;
  let lastReportedScale = 0;

  const recordFrame = (): void => {
    const now = performance.now();
    frameTimes.push(now);
    if (frameTimes.length > 90) frameTimes.splice(0, frameTimes.length - 90);
  };

  const getUnitsPerPixel = (): number => {
    // CSS pixels: the scale bar is drawn in the DOM, not in the framebuffer.
    const heightPx = container.clientHeight || 1;
    if (!heightPx) return 0;
    if (camera.getParallelProjection()) return (camera.getParallelScale() * 2) / heightPx;
    const position = camera.getPosition();
    const focal = camera.getFocalPoint();
    const distance = Math.hypot(position[0] - focal[0], position[1] - focal[1], position[2] - focal[2]);
    const angle = (camera.getViewAngle() * Math.PI) / 180;
    return (2 * distance * Math.tan(angle / 2)) / heightPx;
  };

  const emitScale = (): void => {
    if (!scaleListener || destroyed) return;
    const value = getUnitsPerPixel();
    if (!Number.isFinite(value) || value <= 0) return;
    if (lastReportedScale > 0 && Math.abs(value - lastReportedScale) / lastReportedScale < 0.02) return;
    lastReportedScale = value;
    scaleListener(value);
  };

  const renderNow = (): void => {
    if (destroyed) return;
    renderWindow.render();
    recordFrame();
    emitScale();
  };

  const requestRender = (): void => {
    if (destroyed || rafId !== null) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      renderNow();
    });
  };

  const frameSubscription = interactor.onEndAnimation(() => {
    recordFrame();
    // Only the camera moved: reuse the cached visible bounds.
    updateMarkerGeometry(cachedGeometryBounds);
    // Zoom/pan also changes world-units-per-pixel; refresh the DOM scale bar.
    emitScale();
  });

  // Wheel zoom and trackpad pan modify the camera without an EndAnimation
  // event, so watch the camera itself. emitScale() already throttles to 2%.
  const cameraSubscription = camera.onModified(() => emitScale());
  // Some drivers/interaction styles mutate the camera without firing either
  // event; a cheap poll guarantees the DOM scale bar never goes stale.
  const scaleTimer = window.setInterval(() => {
    if (!destroyed) emitScale();
  }, 120);

  const resizeObserver = new ResizeObserver(() => {
    if (destroyed) return;
    // `win.resize()` reallocates the framebuffer, which clears it. Without a
    // render here the canvas stays blank after the toolbar/stage changes size
    // (for example when the clip controls appear).
    win.resize();
    const bounds = unionBounds(visibleBounds() ?? fullBounds(), markerBounds);
    if (bounds) applyClippingRange(bounds);
    // The fit depends on the viewport aspect: a resize that makes the stage
    // narrower/taller can push the top face or the hole opening out of frame.
    const aspect = (container.clientWidth || 1) / Math.max(1, container.clientHeight || 1);
    if (cameraInitialized && Math.abs(aspect - lastFitAspect) > 0.02 && bounds) {
      fitToBounds(bounds, camera.getDirectionOfProjection(), camera.getViewUp());
    }
    renderNow();
  });
  resizeObserver.observe(container);

  const fullBounds = (): Bounds6 | null =>
    currentMesh ? [...currentMesh.bounds] as Bounds6 : null;

  const dot = (a: readonly number[], b: readonly number[]): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

  const normalize = (v: readonly number[]): [number, number, number] => {
    const length = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / length, v[1] / length, v[2] / length];
  };

  const cross = (a: readonly number[], b: readonly number[]): [number, number, number] => [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];

  /**
   * Bounds of the geometry that is actually on screen: hidden materials are
   * dropped and the triangles the clip plane removes are skipped. Framing the
   * camera on this box is what makes XZ/YZ and a cross-section readable.
   */
  const visibleBounds = (): Bounds6 | null => {
    const mesh = currentMesh;
    if (!mesh) return null;
    const { positions, polys, pointColor, triangleCount, pointCount } = mesh;
    const clipIndex = AXIS_INDEX[currentClip.axis];
    const clipSign = currentClip.keep === 'positive' ? 1 : -1;
    const min: [number, number, number] = [Infinity, Infinity, Infinity];
    const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];
    let seen = false;
    const include = (index: number): void => {
      const base = index * 3;
      for (let k = 0; k < 3; k++) {
        const value = positions[base + k];
        if (value < min[k]) min[k] = value;
        if (value > max[k]) max[k] = value;
      }
    };
    if (triangleCount > 0) {
      for (let t = 0; t < triangleCount; t++) {
        const i0 = polys[t * 4 + 1];
        if (hiddenMaterials.has(pointColor[i0])) continue;
        const i1 = polys[t * 4 + 2];
        const i2 = polys[t * 4 + 3];
        if (currentClip.enabled) {
          // The centroid is close enough to the GPU clipping for framing.
          const centroid = (positions[i0 * 3 + clipIndex] + positions[i1 * 3 + clipIndex] + positions[i2 * 3 + clipIndex]) / 3;
          if ((centroid - currentClip.position) * clipSign < 0) continue;
        }
        include(i0); include(i1); include(i2);
        seen = true;
      }
    } else {
      for (let i = 0; i < pointCount; i++) {
        if (hiddenMaterials.has(pointColor[i])) continue;
        if (currentClip.enabled && (positions[i * 3 + clipIndex] - currentClip.position) * clipSign < 0) continue;
        include(i);
        seen = true;
      }
    }
    return seen ? [min[0], min[1], min[2], max[0], max[1], max[2]] : null;
  };

  /**
   * Explicit near/far range over `bounds`. vtk's own resetCameraClippingRange
   * does not always cover an actor that sits in front of the geometry (the
   * measurement marker), so the range is computed here.
   */
  const applyClippingRange = (bounds: Bounds6): void => {
    const position = camera.getPosition();
    const focal = camera.getFocalPoint();
    const axis = normalize([focal[0] - position[0], focal[1] - position[1], focal[2] - position[2]]);
    let near = Infinity;
    let far = -Infinity;
    for (let corner = 0; corner < 8; corner++) {
      const point = [
        corner & 1 ? bounds[3] : bounds[0],
        corner & 2 ? bounds[4] : bounds[1],
        corner & 4 ? bounds[5] : bounds[2],
      ];
      const distance = dot([point[0] - position[0], point[1] - position[1], point[2] - position[2]], axis);
      if (distance < near) near = distance;
      if (distance > far) far = distance;
    }
    if (!Number.isFinite(near) || !Number.isFinite(far)) return;
    const margin = Math.max((far - near) * 0.05, 1e-6);
    camera.setClippingRange(Math.max(near - margin, 1e-6), far + margin);
  };

  const unionBounds = (a: Bounds6 | null, b: Bounds6 | null): Bounds6 | null => {
    if (!a) return b;
    if (!b) return a;
    return [
      Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2]),
      Math.max(a[3], b[3]), Math.max(a[4], b[4]), Math.max(a[5], b[5]),
    ];
  };

  /**
   * Place the marker line in front of the whole structure along the view axis.
   * Under a parallel projection this does not move the projected position, it
   * only keeps the line out of the solid (a line at the mid-plane would be
   * hidden behind the front face in XZ/YZ).
   *
   * `knownBounds` skips the (O(triangles)) visible-bounds scan: the camera can
   * move without changing which geometry is on screen.
   */
  const updateMarkerGeometry = (knownBounds?: Bounds6 | null): void => {
    if (destroyed) return;
    if (!markerSpec || !currentMesh) {
      const hadMarker = markerBounds !== null;
      markerActor.setVisibility(false);
      markerBounds = null;
      cachedGeometryBounds = null;
      if (knownBounds) applyClippingRange(knownBounds);
      else if (hadMarker) {
        const bounds = visibleBounds() ?? fullBounds();
        if (bounds) applyClippingRange(bounds);
      }
      return;
    }
    const geometryBounds = knownBounds ?? visibleBounds() ?? fullBounds();
    if (!geometryBounds) {
      markerActor.setVisibility(false);
      markerBounds = null;
      cachedGeometryBounds = null;
      return;
    }
    cachedGeometryBounds = geometryBounds;
    const [x0, y0, z0, x1, y1, z1] = geometryBounds;
    const cy = (y0 + y1) / 2;
    const value = markerSpec.value;
    let from: [number, number, number];
    let to: [number, number, number];
    if (markerSpec.axis === 'x') {
      from = [value, cy, z0];
      to = [value, cy, z1];
    } else if (markerSpec.axis === 'z') {
      from = [x0, cy, value];
      to = [x1, cy, value];
    } else {
      from = [(x0 + x1) / 2, value, z0];
      to = [(x0 + x1) / 2, value, z1];
    }
    const direction = normalize(camera.getDirectionOfProjection());
    const diagonal = Math.hypot(x1 - x0, y1 - y0, z1 - z0) || 1;
    const offset = diagonal * 0.6;
    const points = new Float32Array([
      from[0] - direction[0] * offset, from[1] - direction[1] * offset, from[2] - direction[2] * offset,
      to[0] - direction[0] * offset, to[1] - direction[1] * offset, to[2] - direction[2] * offset,
    ]);
    markerData.getPoints().setData(points, 3);
    markerData.modified();
    markerActor.setVisibility(true);
    markerBounds = [
      Math.min(points[0], points[3]), Math.min(points[1], points[4]), Math.min(points[2], points[5]),
      Math.max(points[0], points[3]), Math.max(points[1], points[4]), Math.max(points[2], points[5]),
    ];
    // The marker floats in front of the structure, so the near/far range has
    // to cover it as well.
    applyClippingRange(unionBounds(geometryBounds, markerBounds) ?? geometryBounds);
    requestRender();
  };

  /** Half height (world units) needed so `bounds` fills the view. */
  const fitMetrics = (bounds: Bounds6, direction: readonly number[], up: readonly number[]): {
    center: [number, number, number];
    dir: [number, number, number];
    upAxis: [number, number, number];
    halfHeight: number;
    diagonal: number;
  } => {
    const center: [number, number, number] = [
      (bounds[0] + bounds[3]) / 2,
      (bounds[1] + bounds[4]) / 2,
      (bounds[2] + bounds[5]) / 2,
    ];
    const dir = normalize(direction);
    let upAxis = normalize(up);
    if (Math.abs(dot(upAxis, dir)) > 0.999) upAxis = normalize(cross(dir, [1, 0, 0]));
    // `dir` points into the scene (camera -> focal): right = dir x up,
    // up = right x dir. Getting these backwards mirrors the section.
    const right = normalize(cross(dir, upAxis));
    upAxis = normalize(cross(right, dir));
    let halfRight = 0;
    let halfUp = 0;
    for (let corner = 0; corner < 8; corner++) {
      const point = [
        corner & 1 ? bounds[3] : bounds[0],
        corner & 2 ? bounds[4] : bounds[1],
        corner & 4 ? bounds[5] : bounds[2],
      ];
      const rel = [point[0] - center[0], point[1] - center[1], point[2] - center[2]];
      halfRight = Math.max(halfRight, Math.abs(dot(rel, right)));
      halfUp = Math.max(halfUp, Math.abs(dot(rel, upAxis)));
    }
    const widthPx = container.clientWidth || 1;
    const heightPx = container.clientHeight || 1;
    const aspect = widthPx / Math.max(1, heightPx);
    const diagonal = Math.hypot(bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]) || 1;
    // 8 % of the viewport stays free on every side, so the top face and the
    // hole opening never touch (or cross) the canvas edge after XZ/YZ/fit.
    const fitPadding = 0.08;
    return {
      center,
      dir,
      upAxis,
      halfHeight: Math.max(halfUp, halfRight / aspect, diagonal * 1e-4) * (1 + 2 * fitPadding),
      diagonal,
    };
  };

  const fitToBounds = (bounds: Bounds6, direction: readonly number[], up: readonly number[]): void => {
    const { center, dir, upAxis, halfHeight, diagonal } = fitMetrics(bounds, direction, up);
    let distance = diagonal * 3;
    if (!camera.getParallelProjection()) {
      const angle = (camera.getViewAngle() * Math.PI) / 180;
      distance = (halfHeight / Math.tan(angle / 2)) * 1.05;
    }
    // `getDirectionOfProjection` is the viewing direction (camera -> focal), so
    // the eye sits opposite it.
    camera.setFocalPoint(center[0], center[1], center[2]);
    camera.setPosition(
      center[0] - dir[0] * distance,
      center[1] - dir[1] * distance,
      center[2] - dir[2] * distance,
    );
    camera.setViewUp(upAxis[0], upAxis[1], upAxis[2]);
    camera.setParallelScale(halfHeight);
    cameraInitialized = true;
    lastFitAspect = (container.clientWidth || 1) / Math.max(1, container.clientHeight || 1);
    // Also sets the near/far range (including the marker when present).
    updateMarkerGeometry(bounds);
    if (!markerSpec) applyClippingRange(bounds);
    requestRender();
  };

  /** Fit the visible geometry without changing the viewing direction. */
  const resetView = (): void => {
    const bounds = visibleBounds() ?? fullBounds();
    if (!bounds) return;
    fitToBounds(bounds, camera.getDirectionOfProjection(), camera.getViewUp());
  };

  /**
   * Keep a user-rotated camera across step switches, but recover when the new
   * geometry is far outside (or far inside) the current frame.
   */
  const ensureVisible = (): void => {
    const bounds = visibleBounds() ?? fullBounds();
    if (!bounds) return;
    if (!cameraInitialized) {
      fitToBounds(bounds, camera.getDirectionOfProjection(), camera.getViewUp());
      return;
    }
    const { halfHeight } = fitMetrics(bounds, camera.getDirectionOfProjection(), camera.getViewUp());
    const current = camera.getParallelScale();
    if (!Number.isFinite(current) || current <= 0
      || current > halfHeight * 2.5 || current < halfHeight * 0.4) {
      fitToBounds(bounds, camera.getDirectionOfProjection(), camera.getViewUp());
    } else {
      updateMarkerGeometry();
    }
  };

  const setStandardView = (view: StandardView): void => {
    const bounds = visibleBounds() ?? fullBounds();
    if (!bounds) return;
    // Directions are viewing directions (camera -> focal), so the eye sits at
    // `center - direction * distance`.
    if (view === 'xy') fitToBounds(bounds, [0, 0, -1], [0, 1, 0]);
    else if (view === 'xz') fitToBounds(bounds, [0, 1, 0], [0, 0, 1]);
    else if (view === 'yz') fitToBounds(bounds, [-1, 0, 0], [0, 0, 1]);
    else fitToBounds(bounds, [-1, 1, -0.8], [0, 0, 1]);
  };

  const applyMaterials = (mesh: ParsedMesh): void => {
    const count = mesh.materials.length;
    // vtk.js 37 dropped setTableValue/setNumberOfTableValues: build a fresh
    // lookup table with one RGBA entry per material and swap it in.
    const table: number[][] = [];
    for (let i = 0; i < Math.max(2, count); i++) {
      const material = mesh.materials[Math.min(i, count - 1)];
      const [r, g, b] = hexToRgb(material?.color ?? '#9aa3ad');
      table.push([Math.round(r * 255), Math.round(g * 255), Math.round(b * 255), 255]);
    }
    const next = lutModule.default.newInstance();
    next.setTable(table);
    next.setRange(0, Math.max(1, count - 1));
    next.modified();
    mapper.setLookupTable(next);
    lookupTable.delete();
    lookupTable = next;
  };

  const upload = (mesh: ParsedMesh): void => {
    data.getPoints().setData(mesh.positions, 3);
    data.getPolys().setData(filterGeometry(mesh, hiddenMaterials));
    scalars.setData(mesh.pointColor, 1);
    data.getPointData().setScalars(scalars);
    // Normals arrive precomputed from the parse worker. A mesh without them
    // (an old cache entry) gets a neutral normal once, never per frame.
    let normals = mesh.normals;
    if (normals.length !== mesh.positions.length) {
      normals = new Float32Array(mesh.positions.length);
      for (let i = 2; i < normals.length; i += 3) normals[i] = 1;
      mesh.normals = normals;
    }
    normalsData.setData(normals, 3);
    data.getPointData().setNormals(normalsData);
    applyMaterials(mesh);
    data.modified();
    mapper.modified();
  };

  const attach = (mesh: ParsedMesh, hidden: ReadonlySet<number>): void => {
    currentMesh = mesh;
    hiddenMaterials = hidden;
    upload(mesh);
    if (!cameraInitialized) setStandardView('iso');
    else {
      updateMarkerGeometry();
      requestRender();
    }
  };

  const setHidden = (hidden: ReadonlySet<number>): void => {
    if (!currentMesh) return;
    hiddenMaterials = hidden;
    upload(currentMesh);
    updateMarkerGeometry();
    requestRender();
  };

  const setDisplayMode = (mode: DisplayMode): void => {
    const property = actor.getProperty();
    if (mode === 'wireframe') property.setRepresentationToWireframe();
    else property.setRepresentationToSurface();
    requestRender();
  };

  const setProjection = (mode: 'parallel' | 'perspective'): void => {
    camera.setParallelProjection(mode === 'parallel');
    requestRender();
  };

  const setClip = (clip: ClipState): void => {
    currentClip = clip;
    mapper.removeAllClippingPlanes();
    if (!clip.enabled) {
      updateMarkerGeometry();
      requestRender();
      return;
    }
    const index = AXIS_INDEX[clip.axis];
    const normal: [number, number, number] = [0, 0, 0];
    normal[index] = clip.keep === 'positive' ? 1 : -1;
    const origin: [number, number, number] = [0, 0, 0];
    origin[index] = clip.position;
    plane.setNormal(normal[0], normal[1], normal[2]);
    plane.setOrigin(origin[0], origin[1], origin[2]);
    mapper.addClippingPlane(plane);
    updateMarkerGeometry();
    requestRender();
  };

  const setMarker = (marker: Viewer3DMarker | null): void => {
    markerSpec = marker;
    updateMarkerGeometry();
    requestRender();
  };

  const setScaleListener = (listener: ((unitsPerPixel: number) => void) | null): void => {
    scaleListener = listener;
    lastReportedScale = 0;
    if (listener) {
      const value = getUnitsPerPixel();
      if (Number.isFinite(value) && value > 0) {
        lastReportedScale = value;
        listener(value);
      }
    }
  };

  const rotate = (azimuthDeg: number, elevationDeg: number): void => {
    camera.azimuth(azimuthDeg);
    camera.elevation(elevationDeg);
    camera.orthogonalizeViewUp();
    if (markerSpec) updateMarkerGeometry(cachedGeometryBounds);
    renderNow();
  };

  const getFps = (): number => {
    if (frameTimes.length < 2) return 0;
    const now = performance.now();
    const last = frameTimes[frameTimes.length - 1];
    if (now - last > 1000) {
      frameTimes = [];
      return 0;
    }
    const span = last - frameTimes[0];
    return span > 0 ? ((frameTimes.length - 1) * 1000) / span : 0;
  };

  const readPixels = (): { width: number; height: number; pixels: Uint8Array } | null => {
    if (destroyed) return null;
    const canvas = container.querySelector('canvas');
    if (!canvas) return null;
    const gl = (canvas.getContext('webgl2') ?? canvas.getContext('webgl')) as WebGL2RenderingContext | null;
    if (!gl) return null;
    renderNow();
    const width = canvas.width;
    const height = canvas.height;
    const pixels = new Uint8Array(width * height * 4);
    gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return { width, height, pixels };
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    if (rafId !== null) cancelAnimationFrame(rafId);
    rafId = null;
    frameSubscription.unsubscribe();
    cameraSubscription.unsubscribe();
    window.clearInterval(scaleTimer);
    resizeObserver.disconnect();
    widget.setEnabled(false);
    widget.delete();
    axes.delete();
    mapper.removeAllClippingPlanes();
    plane.delete();
    renderer.removeActor(actor);
    renderer.removeActor(markerActor);
    markerActor.delete();
    markerMapper.delete();
    markerData.delete();
    actor.delete();
    mapper.delete();
    scalars.delete();
    normalsData.delete();
    data.delete();
    lookupTable.delete();
    const style = interactor.getInteractorStyle();
    win.delete();
    interactor.delete();
    if (style && typeof style.delete === 'function') style.delete();
    renderer.delete();
    renderWindow.delete();
    for (const canvas of Array.from(container.querySelectorAll('canvas'))) canvas.remove();
  };

  return {
    attach,
    setHidden,
    setDisplayMode,
    setProjection,
    setClip,
    setMarker,
    resetView,
    ensureVisible,
    setStandardView,
    getUnitsPerPixel,
    setScaleListener,
    rotate,
    requestRender,
    renderNow,
    getFps,
    getTriangleCount: () => currentMesh?.triangleCount ?? 0,
    getPointCount: () => currentMesh?.pointCount ?? 0,
    getVisibleBounds: () => visibleBounds() ?? fullBounds(),
    readPixels,
    destroy,
  };
}
