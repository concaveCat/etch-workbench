/** Mesh pane: the production vtk.js viewer, fed by an engine comparison frame.
 *
 * The comparison frame is converted to the viewer's `SnapshotDoc` shape instead of
 * duplicating a renderer: the workbench viewer already handles materials, clipping,
 * projection, scale and resource teardown. The shared camera drives it through
 * `Viewer3DHandle.rotate`, which sets absolute azimuth/elevation.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import WorkflowViewer3D from '../workflow/viewer3d/WorkflowViewer3D';
import type { SnapshotDoc, Viewer3DHandle } from '../workflow/viewer3d/types';
import type { MeshFrame } from './api';

export type Camera = { azimuth: number; elevation: number; zoom: number };

export const DEFAULT_CAMERA: Camera = { azimuth: 45, elevation: 30, zoom: 1 };

/** Map SPX's view presets onto the vtk.js standard views. */
export function cameraForView(view: string | undefined): Camera {
  switch (view) {
    case 'iso90': return { azimuth: 135, elevation: 30, zoom: 1 };
    case 'iso180': return { azimuth: 225, elevation: 30, zoom: 1 };
    case 'iso270': return { azimuth: 315, elevation: 30, zoom: 1 };
    case 'xz': return { azimuth: 0, elevation: 0, zoom: 1 };
    case 'yz': return { azimuth: 90, elevation: 0, zoom: 1 };
    case 'xy': return { azimuth: 0, elevation: 89, zoom: 1 };
    default: return DEFAULT_CAMERA;
  }
}

export function frameToSnapshot(frame: MeshFrame): SnapshotDoc {
  const materials: Record<string, { name: string; color: string }> = {};
  for (const material of frame.materials) {
    materials[String(material.id)] = { name: material.name, color: material.color };
  }
  return {
    dimension: frame.dimension,
    points: frame.points,
    triangles: frame.triangles,
    cellData: { materialIds: frame.materialIds },
    materials,
    coordinateUnit: 'um',
    stepName: frame.stepId,
  };
}

type Props = {
  frame: MeshFrame | null;
  camera: Camera;
  height: number;
  title: string;
  note?: string;
};

export function MeshPane({ frame, camera, height, title, note }: Props) {
  const handleRef = useRef<Viewer3DHandle | null>(null);
  const [ready, setReady] = useState(false);
  const snapshot = useMemo(() => (frame ? frameToSnapshot(frame) : null), [frame]);

  useEffect(() => {
    if (!ready || !handleRef.current) return;
    handleRef.current.rotate(camera.azimuth, camera.elevation);
  }, [camera.azimuth, camera.elevation, ready, snapshot]);

  // The viewer keys its cache by runId/stepId; comparison frames are always
  // supplied inline, so a stable pseudo-run keeps the cache from refetching.
  const stepId = frame?.stepId ?? 'none';

  return (
    <figure className="cmp-pane">
      <figcaption>
        <strong>{title}</strong>
        {note ? <span className="cmp-note">{note}</span> : null}
      </figcaption>
      <div style={{ width: 520, height }}>
        {snapshot ? (
          <WorkflowViewer3D
            runId="engine-compare"
            stepId={stepId}
            dimension={3}
            snapshot={snapshot}
            height={height}
            onSceneReady={(handle) => {
              handleRef.current = handle;
              setReady(true);
              handle.resetView();
              handle.rotate(camera.azimuth, camera.elevation);
            }}
          />
        ) : (
          <div className="cmp-missing">无帧</div>
        )}
      </div>
    </figure>
  );
}
