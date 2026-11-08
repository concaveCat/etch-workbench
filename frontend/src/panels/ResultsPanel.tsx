import type { ReactNode } from 'react';
import { PanelSlot } from './PanelSlot';

export function ResultsPanel({ children }: { children: ReactNode }) { return <PanelSlot activity="results">{children}</PanelSlot>; }
