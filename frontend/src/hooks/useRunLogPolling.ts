import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { prettyError } from '../api/client';
import type { RunInfo } from '../api/types';
import { terminalStates } from '../lib/runPresentation';
import type { WorkbenchHealth } from './useWorkbenchBootstrap';

// Run-log polling: while the newest run is still active, poll its incremental
// log every second and append it to the visible log. `cursorRef` mirrors
// `logCursor` so the interval closure always reads the live cursor, and
// `polling` prevents a slow request from overlapping the next tick. App keeps
// runInfo/displayInfo because the run inspector and results view own them;
// startRun/startReplay/clearDisplayedRun still reset the log through the
// setters returned here.
export type RunLogPollingDeps = {
  latestRunId: string | null;
  runInfo: RunInfo | null;
  displayRunId: string | null;
  autoFollow: boolean;
  selectedStepId: string | null;
  fluxResultRunId: string | null;
  fetchRunInfo: (runId: string, cursor?: number) => Promise<RunInfo>;
  fetchSnapshot: (runId: string, stepId: string) => Promise<void>;
  fetchFluxResult: (runId: string, expectedMeshIdentity?: string) => Promise<void>;
  refreshLists: () => Promise<void>;
  setRunInfo: Dispatch<SetStateAction<RunInfo | null>>;
  setDisplayInfo: Dispatch<SetStateAction<RunInfo | null>>;
  setHealth: Dispatch<SetStateAction<WorkbenchHealth>>;
  setNotice: Dispatch<SetStateAction<string>>;
};

export function useRunLogPolling({
  latestRunId,
  runInfo,
  displayRunId,
  autoFollow,
  selectedStepId,
  fluxResultRunId,
  fetchRunInfo,
  fetchSnapshot,
  fetchFluxResult,
  refreshLists,
  setRunInfo,
  setDisplayInfo,
  setHealth,
  setNotice,
}: RunLogPollingDeps) {
  const [runLog, setRunLog] = useState('');
  const [logCursor, setLogCursor] = useState(0);
  const cursorRef = useRef(0);
  const polling = useRef(false);

  useEffect(() => {
    if (!latestRunId || !runInfo || terminalStates.has(runInfo.status)) return undefined;
    let mounted = true;
    const poll = async () => {
      if (polling.current) return;
      polling.current = true;
      try {
        const info = await fetchRunInfo(latestRunId, cursorRef.current);
        if (!mounted) return;
        setRunInfo(info);
        if (displayRunId === latestRunId) setDisplayInfo(info);
        if (info.log.nextCursor > cursorRef.current) {
          cursorRef.current = info.log.nextCursor; setLogCursor(info.log.nextCursor);
          if (info.log.text) setRunLog(previous => (previous + info.log.text).slice(-250_000));
        }
        if (displayRunId === latestRunId && autoFollow && info.snapshots.length) {
          const newest = info.snapshots[info.snapshots.length - 1];
          if (selectedStepId !== newest.stepId) await fetchSnapshot(latestRunId, newest.stepId);
        }
        if (displayRunId === latestRunId && info.kind === 'flux' && info.status === 'succeeded' && info.fluxResult && fluxResultRunId !== latestRunId) {
          await fetchFluxResult(latestRunId, info.fluxResult.meshIdentity);
        }
        if (terminalStates.has(info.status)) { setHealth('ready'); void refreshLists(); }
      } catch (error) { if (mounted) setNotice(prettyError(error)); }
      finally { polling.current = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, [latestRunId, runInfo?.status, fetchRunInfo, fetchSnapshot, fetchFluxResult, refreshLists, displayRunId, autoFollow, selectedStepId, fluxResultRunId]);

  return { runLog, setRunLog, logCursor, setLogCursor, cursorRef };
}
