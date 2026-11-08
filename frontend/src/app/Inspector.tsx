import type { ReactNode } from 'react';

export function Inspector({ children, className = '', id = 'settings-panel' }: { children: ReactNode; className?: string; id?: string }) {
  return <aside id={id} className={`side-panel settings-panel inspector-panel ${className}`}>{children}</aside>;
}
