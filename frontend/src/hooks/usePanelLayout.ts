import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { fitPanelWidths, type PanelWidths, type ResizablePanel } from '../lib/panelLayout';

// Left/right panel sizing for the workspace: the fit pass keeps both panels
// inside the viewer's minimum, and the drag/keyboard handlers write the same
// CSS custom properties the layout reads.
export function usePanelLayout() {
  const [panelWidths, setPanelWidths] = useState<PanelWidths>({ structure: 300, settings: 320 });
  const [isResizingPanels, setIsResizingPanels] = useState(false);
  const workspaceRef = useRef<HTMLElement>(null);
  const panelResizeRef = useRef<{ panel: ResizablePanel; pointerX: number; width: number } | null>(null);

  useEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return undefined;
    const fitToWorkspace = () => {
      if (window.matchMedia('(max-width: 900px)').matches) return;
      setPanelWidths(previous => {
        const next = fitPanelWidths(previous, workspace.clientWidth);
        return Math.abs(next.structure - previous.structure) < 0.5 && Math.abs(next.settings - previous.settings) < 0.5 ? previous : next;
      });
    };
    const observer = new ResizeObserver(fitToWorkspace);
    observer.observe(workspace);
    window.addEventListener('resize', fitToWorkspace);
    fitToWorkspace();
    return () => { observer.disconnect(); window.removeEventListener('resize', fitToWorkspace); };
  }, []);

  useEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return;
    workspace.style.setProperty('--left-panel-width', `${panelWidths.structure}px`);
    workspace.style.setProperty('--right-panel-width', `${panelWidths.settings}px`);
  }, [panelWidths]);

  const beginPanelResize = (event: PointerEvent<HTMLDivElement>, panel: ResizablePanel) => {
    if (window.matchMedia('(max-width: 900px)').matches) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    panelResizeRef.current = { panel, pointerX: event.clientX, width: panelWidths[panel] };
    setIsResizingPanels(true);
  };
  const movePanelResize = (event: PointerEvent<HTMLDivElement>) => {
    const drag = panelResizeRef.current;
    const workspace = workspaceRef.current;
    if (!drag || !workspace) return;
    const delta = event.clientX - drag.pointerX;
    const nextWidth = drag.width + (drag.panel === 'structure' ? delta : -delta);
    setPanelWidths(previous => fitPanelWidths({ ...previous, [drag.panel]: nextWidth }, workspace.clientWidth));
  };
  const endPanelResize = () => {
    panelResizeRef.current = null;
    setIsResizingPanels(false);
  };
  const adjustPanelWidth = (event: KeyboardEvent<HTMLDivElement>, panel: ResizablePanel) => {
    const grows = panel === 'structure' ? event.key === 'ArrowRight' : event.key === 'ArrowLeft';
    const shrinks = panel === 'structure' ? event.key === 'ArrowLeft' : event.key === 'ArrowRight';
    if (!grows && !shrinks) return;
    event.preventDefault();
    const workspace = workspaceRef.current;
    if (!workspace) return;
    const delta = grows ? 12 : -12;
    setPanelWidths(previous => fitPanelWidths({ ...previous, [panel]: previous[panel] + delta }, workspace.clientWidth));
  };

  // The splitter double-click reset still lives in JSX, so the raw setter stays
  // exposed; everything else uses the handlers above.
  return { panelWidths, setPanelWidths, isResizingPanels, workspaceRef, beginPanelResize, movePanelResize, endPanelResize, adjustPanelWidth };
}
