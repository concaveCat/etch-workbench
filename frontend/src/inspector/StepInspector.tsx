import type { ReactNode } from 'react';

export function StepInspector({ children, activeTab }: { children: ReactNode; activeTab: 'basic' | 'physics' | 'native' }) {
  return <div id="step-parameter-panel" className="step-inspector" role="tabpanel" aria-labelledby={`step-tab-${activeTab}`} data-active-tab={activeTab}>{children}</div>;
}
