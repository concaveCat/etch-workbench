// Left/right editor panel sizing. The viewer keeps a minimum width so the
// layout stays usable on narrow windows.
export type PanelWidths = { structure: number; settings: number };
export type ResizablePanel = keyof PanelWidths;
export const panelWidthLimits = { structure: { min: 260, max: 420 }, settings: { min: 280, max: 420 }, viewerMin: 320, splitter: 10, workspacePadding: 20, rail: 64 } as const;
export function fitPanelWidths(widths: PanelWidths, workspaceWidth: number): PanelWidths {
  const structure = Math.max(panelWidthLimits.structure.min, Math.min(panelWidthLimits.structure.max, widths.structure));
  const settings = Math.max(panelWidthLimits.settings.min, Math.min(panelWidthLimits.settings.max, widths.settings));
  const available = Math.max(panelWidthLimits.structure.min + panelWidthLimits.settings.min,
    workspaceWidth - panelWidthLimits.workspacePadding - panelWidthLimits.splitter * 2 - panelWidthLimits.viewerMin - panelWidthLimits.rail);
  const structureExtra = structure - panelWidthLimits.structure.min;
  const settingsExtra = settings - panelWidthLimits.settings.min;
  const totalExtra = structureExtra + settingsExtra;
  const remainingExtra = Math.max(0, available - panelWidthLimits.structure.min - panelWidthLimits.settings.min);
  if (totalExtra <= remainingExtra || totalExtra === 0) return { structure, settings };
  const factor = remainingExtra / totalExtra;
  return {
    structure: panelWidthLimits.structure.min + structureExtra * factor,
    settings: panelWidthLimits.settings.min + settingsExtra * factor,
  };
}
export function maxPanelWidth(panel: ResizablePanel, workspaceWidth: number): number {
  const otherMinimum = panel === 'structure' ? panelWidthLimits.settings.min : panelWidthLimits.structure.min;
  const available = workspaceWidth - panelWidthLimits.workspacePadding - panelWidthLimits.splitter * 2 - panelWidthLimits.viewerMin - otherMinimum;
  return Math.max(panelWidthLimits[panel].min, Math.min(panelWidthLimits[panel].max, available));
}
