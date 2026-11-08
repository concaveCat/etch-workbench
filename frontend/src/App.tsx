import { useState } from 'react';
import LegacyApp from './LegacyApp';
import WorkflowApp from './workflow/WorkflowApp';

export default function App() {
  const [legacy, setLegacy] = useState(() => window.location.hash.startsWith('#legacy'));
  if (legacy) {
    return (
      <>
        <LegacyApp />
        <button
          type="button"
          style={{ position: 'fixed', right: 16, bottom: 16, zIndex: 9999, padding: '8px 12px', borderRadius: 8, border: '1px solid #35d6c2', background: '#07111d', color: '#35d6c2', cursor: 'pointer' }}
          onClick={() => { window.location.hash = ''; setLegacy(false); }}
        >返回 Etch Workbench</button>
      </>
    );
  }
  return <WorkflowApp onOpenLegacy={() => { window.location.hash = 'legacy'; setLegacy(true); }} />;
}
