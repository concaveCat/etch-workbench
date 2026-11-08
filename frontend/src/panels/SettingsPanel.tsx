import type { ReactNode } from 'react';
import { PanelSlot } from './PanelSlot';

export function SettingsPanel({ children }: { children: ReactNode }) { return <PanelSlot activity="settings">{children}</PanelSlot>; }
