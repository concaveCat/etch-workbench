import type { ReactNode } from 'react';
import { PanelSlot } from './PanelSlot';

export function MaterialsPanel({ children }: { children: ReactNode }) { return <PanelSlot activity="materials">{children}</PanelSlot>; }
