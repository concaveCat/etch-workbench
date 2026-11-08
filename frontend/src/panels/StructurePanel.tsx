import type { ReactNode } from 'react';
import { PanelSlot } from './PanelSlot';

export function StructurePanel({ children }: { children: ReactNode }) { return <PanelSlot activity="structure">{children}</PanelSlot>; }
