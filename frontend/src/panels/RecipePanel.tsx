import type { ReactNode } from 'react';
import { PanelSlot } from './PanelSlot';

export function RecipePanel({ children }: { children: ReactNode }) { return <PanelSlot activity="recipe">{children}</PanelSlot>; }
