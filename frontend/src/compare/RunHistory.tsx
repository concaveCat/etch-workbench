/** Run-history picker: a compact popover listing every stored comparison.
 *
 * The page is visual-only, so the trigger is an icon; the panel is the one place
 * that shows names, because choosing a run needs identifying information.
 */

import { useEffect, useRef, useState } from 'react';
import type { CompareRun } from './api';

type Props = {
  runs: CompareRun[];
  current: string | null;
  onSelect: (runId: string) => void;
};

function stateColor(state: string | undefined): string {
  switch (state) {
    case 'succeeded': return '#18b8a6';
    case 'running':
    case 'queued': return '#f0b429';
    case 'failed':
    case 'rejected': return '#e84a4a';
    default: return '#cfd6df';
  }
}

function stamp(run: CompareRun): string {
  const value = (run as unknown as { updatedAt?: number }).updatedAt;
  if (!value) return '';
  const date = new Date(value * 1000);
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} `
    + `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

export function RunHistory({ runs, current, onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  const active = runs.find((run) => run.runId === current);

  return (
    <div className="cmp-history" ref={ref}>
      <button type="button" className="cmp-history-trigger" onClick={() => setOpen((value) => !value)}
              title="run history">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M12 7v5l3 2" /><circle cx="12" cy="12" r="8.5" />
        </svg>
        <span className="cmp-history-current">{active?.caseId ?? current ?? '—'}</span>
        <span className="cmp-history-count">{runs.length}</span>
      </button>
      {open ? (
        <div className="cmp-history-panel" role="listbox">
          {runs.length === 0 ? <div className="cmp-history-empty" /> : null}
          {runs.map((run) => (
            <button
              key={run.runId}
              type="button"
              role="option"
              aria-selected={run.runId === current}
              className={`cmp-history-item ${run.runId === current ? 'active' : ''}`}
              onClick={() => { onSelect(run.runId); setOpen(false); }}
            >
              <span className="cmp-history-state" style={{ background: stateColor(run.state) }} />
              <span className="cmp-history-name">{run.caseId ?? run.runId}</span>
              <span className="cmp-history-meta">{stamp(run)}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
