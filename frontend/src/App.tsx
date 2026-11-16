import { useState } from 'react';
import LegacyApp from './LegacyApp';
import WorkflowApp from './workflow/WorkflowApp';
import { CompareApp } from './compare/CompareApp';

export default function App() {
  const [mode, setMode] = useState(() => {
    if (window.location.hash.startsWith('#legacy')) return 'legacy';
    if (window.location.hash.startsWith('#compare')) return 'compare';
    return 'workflow';
  });
  const go = (next: 'legacy' | 'workflow' | 'compare') => {
    window.location.hash = next === 'workflow' ? '' : next;
    setMode(next);
  };
  if (mode === 'legacy') {
    return (
      <>
        <LegacyApp />
        <button
          type="button"
          style={{ position: 'fixed', right: 16, bottom: 16, zIndex: 9999, padding: '8px 12px', borderRadius: 8, border: '1px solid #35d6c2', background: '#07111d', color: '#35d6c2', cursor: 'pointer' }}
          onClick={() => go('workflow')}
        >返回 ViennaPS Workbench</button>
      </>
    );
  }
  if (mode === 'compare') {
    return <CompareApp onExit={() => go('workflow')} />;
  }
  return (
    <WorkflowApp
      onOpenLegacy={() => go('legacy')}
      onOpenCompare={() => go('compare')}
    />
  );
}
