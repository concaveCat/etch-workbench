import type { ReactNode } from 'react';

export function Menu({ label, children }: { label: string; children: ReactNode }) {
  return <details className="ui-menu"><summary>{label}</summary><div className="ui-menu-items">{children}</div></details>;
}
