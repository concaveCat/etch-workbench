/** Comparison shell: three pages, one draft, one run. */

import { useCallback, useEffect, useState } from 'react';
import type { CompareRun } from './api';
import { fetchComparison, fetchRecentRuns, submitComparison } from './api';
import { ComparePage } from './ComparePage';
import { FlowPage } from './FlowPage';
import { GeometryPage } from './GeometryPage';
import { defaultDraft, toJob, withLayout, type Draft } from './draft';
import './compare.css';

type Page = 'geometry' | 'flow' | 'compare';

const PAGES: { id: Page; label: string }[] = [
  { id: 'geometry', label: '1 · 初始几何' },
  { id: 'flow', label: '2 · 流程图' },
  { id: 'compare', label: '3 · 对比' },
];

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
        <strong>引擎对比工作台</strong>
        <nav>
          {PAGES.map((row) => (
            <button key={row.id} type="button" className={page === row.id ? 'active' : ''}
                    onClick={() => setPage(row.id)}>{row.label}</button>
          ))}
        </nav>
        <span className="cmp-spacer" />
        <button type="button" onClick={onExit}>返回 Workbench</button>
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
