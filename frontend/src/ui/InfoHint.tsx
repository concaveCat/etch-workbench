import type { ReactNode } from 'react';

export function InfoHint({ children, label = '说明' }: { children: ReactNode; label?: string }) {
  return <details className="info-hint"><summary aria-label={label}>ⓘ</summary><div>{children}</div></details>;
}
