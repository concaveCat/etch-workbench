import type { ReactNode } from 'react';

export function PanelSlot({ activity, children }: { activity: string; children: ReactNode }) {
  return <div className={`activity-slot activity-${activity}`} data-activity={activity}>{children}</div>;
}
