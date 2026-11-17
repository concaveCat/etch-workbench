/** Comparison shell: three icon-only pages, one draft, one run. */

import { useCallback, useEffect, useState } from 'react';
import type { CompareRun } from './api';
import { fetchComparison, fetchRecentRuns, submitComparison } from './api';
import { ComparePage } from './ComparePage';
import { FlowPage } from './FlowPage';
import { GeometryPage } from './GeometryPage';
import { defaultDraft, toJob, withLayout, type Draft } from './draft';
import './compare.css';

type Page = 'geometry' | 'flow' | 'compare';

/** Icon-only navigation: the interface carries no labels. */
const ICONS: Record<Page | 'exit', React.ReactNode> = {
  geometry: (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M12 3 21 8v8l-9 5-9-5V8z" /><path d="M3 8l9 5 9-5M12 13v8" />
    </svg>
  ),
  flow: (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="4" y="3" width="16" height="4" rx="1.5" />
      <rect x="4" y="10" width="16" height="4" rx="1.5" />
      <rect x="4" y="17" width="16" height="4" rx="1.5" />
    </svg>
  ),
  compare: (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="4" width="7.5" height="16" rx="1.5" />
      <rect x="13.5" y="4" width="7.5" height="16" rx="1.5" />
    </svg>
  ),
  exit: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" /><path d="M10 8l-4 4 4 4M6 12h9" />
    </svg>
  ),
};

export function CompareApp({ onExit }: { onExit: () => void }) {
  const [page, setPage] = useState<Page>('geometry');
  const [draft, setDraft] = useState<Draft>(defaultDraft);
  const [engines, setEngines] = useState<string[]>(['viennaps', 'spx']);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<CompareRun | null>(null);
  const [history, setHistory] = useState<CompareRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await fetchRecentRuns());
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => { void refreshHistory(); }, [refreshHistory]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const payload = await fetchComparison(runId);
        if (cancelled) return;
        setRun(payload);
        if (payload.state === 'queued' || payload.state === 'running') {
          window.setTimeout(tick, 800);
        } else {
          setBusy(false);
          void refreshHistory();
        }
      } catch (fetchError) {
        if (!cancelled) {
          setError(String(fetchError));
          setBusy(false);
        }
      }
    };
    void tick();
    return () => { cancelled = true; };
  }, [runId, refreshHistory]);

  const onRun = async () => {
    setBusy(true);
    setError(null);
    try {
      const { runId: created } = await submitComparison(withLayout(toJob(draft)), engines);
      setRunId(created);
      setRun(null);
      setPage('compare');
    } catch (submitError) {
      setError(String(submitError));
      setBusy(false);
    }
  };

  const selectRun = (id: string) => {
    setRunId(id);
    setBusy(false);
  };

  return (
    <div className="cmp-app">
      <header className="cmp-top">
        <nav>
          {(['geometry', 'flow', 'compare'] as Page[]).map((id) => (
            <button
              key={id}
              type="button"
              title={id}
              aria-label={id}
              className={page === id ? 'active' : ''}
              onClick={() => setPage(id)}
            >{ICONS[id]}</button>
          ))}
        </nav>
        <span className="cmp-spacer" />
        {busy ? <span className="cmp-dot cmp-dot-busy" aria-hidden /> : null}
        <button type="button" title="exit" aria-label="exit" onClick={onExit}>{ICONS.exit}</button>
      </header>
      {page === 'geometry' ? <GeometryPage draft={draft} onChange={setDraft} /> : null}
      {page === 'flow' ? (
        <FlowPage
          draft={draft}
          onChange={setDraft}
          engines={engines}
          onToggleEngine={(engineId) => setEngines((current) =>
            current.includes(engineId) ? current.filter((id) => id !== engineId) : [...current, engineId])}
          onRun={onRun}
          busy={busy}
          error={error}
        />
      ) : null}
      {page === 'compare' ? (
        <ComparePage run={run} runId={runId} history={history} onSelectRun={selectRun} />
      ) : null}
    </div>
  );
}
