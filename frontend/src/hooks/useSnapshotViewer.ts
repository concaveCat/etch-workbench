import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { api, prettyError, saveBlob } from '../api/client';
import type { Project, RunInfo, Snapshot, ViewSettings } from '../api/types';
import { defaultProject } from '../project/defaults';
import { defaultView3D, snapshotDimension } from '../types3d';
import type { View3D } from '../types3d';

// Snapshot/viewer state: the snapshot currently on screen, the comparison
// run/snapshot beside it, and the view settings used when the visible result
// is a history run rather than the project under edit. `project` stays the
// source of truth for the editor view, so writes go through `updateProject`.
export type SnapshotViewerDeps = {
  project: Project;
  updateProject: (updater: (previous: Project) => Project) => void;
  displayRunId: string | null;
  latestRunId: string | null;
  fetchRunInfo: (runId: string, cursor?: number) => Promise<RunInfo>;
  setDisplayRunId: Dispatch<SetStateAction<string | null>>;
  setNotice: Dispatch<SetStateAction<string>>;
};

export function useSnapshotViewer({ project, updateProject, displayRunId, latestRunId, fetchRunInfo, setDisplayRunId, setNotice }: SnapshotViewerDeps) {
  const [selectedSnapshot, setSelectedSnapshot] = useState<Snapshot | null>(null);
  const [compareRunId, setCompareRunId] = useState('');
  const [compareInfo, setCompareInfo] = useState<RunInfo | null>(null);
  const [compareSnapshot, setCompareSnapshot] = useState<Snapshot | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [historyView2D, setHistoryView2D] = useState<ViewSettings>(structuredClone(defaultProject.view!));
  const [historyView3D, setHistoryView3D] = useState<View3D>(structuredClone(defaultView3D));
  const svgRef = useRef<SVGSVGElement | null>(null);
  const snapshotSequence = useRef(0);
  const history3DRun = useRef<string | null>(null);
  const comparisonSequence = useRef(0);

  const fetchSnapshot = useCallback(async (runId: string, stepId: string) => {
    try {
      const sequence = ++snapshotSequence.current;
      const snapshot = await api<Snapshot>(`/api/runs/${runId}/snapshots/${encodeURIComponent(stepId)}`);
      const dimension = snapshotDimension(snapshot);
      if (sequence !== snapshotSequence.current) return;
      if (dimension === 3 && history3DRun.current !== runId) {
        // A reopened history result owns its view. The current editor can be
        // 2D or have an unrelated material filter and camera.
        history3DRun.current = runId;
        setHistoryView3D({
          ...structuredClone(defaultView3D),
          visibleMaterialIds: [...new Set(snapshot.cellData.materialIds)],
        });
      }
      setSelectedSnapshot(snapshot); setSelectedStepId(stepId); setDisplayRunId(runId);
    } catch (error) { setNotice(prettyError(error)); }
  }, []);

  const exportSvg = () => {
    const svg = svgRef.current;
    if (!svg) { setNotice('请先加载一个网格结果。'); return; }
    const clone = svg.cloneNode(true) as SVGSVGElement;
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width', String(Math.round(svg.viewBox.baseVal.width)));
    clone.setAttribute('height', String(Math.round(svg.viewBox.baseVal.height)));
    saveBlob(`viennaps-view-${displayRunId ?? 'snapshot'}.svg`, new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml' }));
  };

  const loadComparison = async (runId: string, stepId?: string) => {
    setCompareRunId(runId);
    if (!runId) { comparisonSequence.current++; setCompareInfo(null); setCompareSnapshot(null); setSelectedStepId(selectedSnapshot?.stepId ?? null); return; }
    try {
      const info = await fetchRunInfo(runId, 0); setCompareInfo(info);
      const row = info.snapshots.find(item => item.stepId === stepId) ?? info.snapshots.at(-1);
      const sequence = ++comparisonSequence.current;
      if (row) {
        const snapshot = await api<Snapshot>(`/api/runs/${runId}/snapshots/${row.stepId}`);
        snapshotDimension(snapshot);
        if (sequence === comparisonSequence.current) setCompareSnapshot(snapshot);
      }
    } catch (error) { setNotice(prettyError(error)); setCompareSnapshot(null); }
  };

  const view2D = project.dimension === 2 ? project.view ?? defaultProject.view! : historyView2D;
  const showingHistory3D = selectedSnapshot && snapshotDimension(selectedSnapshot) === 3 && displayRunId !== latestRunId;
  const view3D = showingHistory3D ? historyView3D : project.dimension === 3 ? project.view3D ?? defaultView3D : historyView3D;
  const changeView = (partial: Partial<ViewSettings>) => {
    if (project.dimension === 2) updateProject(previous => ({ ...previous, view: { ...view2D, ...partial } }));
    else setHistoryView2D(previous => ({ ...previous, ...partial }));
  };
  const changeView3D = (next: View3D) => {
    if (project.dimension === 3 && !showingHistory3D) updateProject(previous => ({ ...previous, view3D: next }));
    else setHistoryView3D(next);
  };
  const displayDimension = selectedSnapshot ? snapshotDimension(selectedSnapshot) : project.dimension;
  const compatibleCompare = selectedSnapshot && compareSnapshot && snapshotDimension(selectedSnapshot) === snapshotDimension(compareSnapshot) ? compareSnapshot : null;

  return {
    selectedSnapshot, setSelectedSnapshot,
    compareRunId, setCompareRunId,
    compareInfo, setCompareInfo,
    compareSnapshot, setCompareSnapshot,
    selectedStepId, setSelectedStepId,
    historyView2D, setHistoryView2D,
    historyView3D, setHistoryView3D,
    snapshotSequence, comparisonSequence,
    svgRef,
    fetchSnapshot, loadComparison, changeView, changeView3D, exportSvg,
    view2D, showingHistory3D, view3D, displayDimension, compatibleCompare,
  };
}
