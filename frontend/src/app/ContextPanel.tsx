import type { ReactNode } from 'react';

export function ContextPanel({ children }: { children: ReactNode }) {
  return <div className="context-panel-content">{children}</div>;
}
