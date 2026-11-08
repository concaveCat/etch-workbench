import type { DockTab } from './types';

const tabs: { id: DockTab; label: string }[] = [
  { id: 'log', label: '日志' }, { id: 'flux', label: '通量' }, { id: 'intermediate', label: '中间场' },
  { id: 'native', label: '原生参数' }, { id: 'artifacts', label: '产物' },
];

export function Dock({ tab, onChange, counts = {} }: { tab: DockTab; onChange: (tab: DockTab) => void; counts?: Partial<Record<DockTab, number>> }) {
  return <div className="dock-tabs" role="tablist" aria-label="运行输出">
    {tabs.map(item => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'selected' : ''} onClick={() => {
      onChange(item.id);
      document.getElementById(`dock-${item.id}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }}>{item.label}{(counts[item.id] ?? 0) > 0 && <span className="dock-badge">{counts[item.id]}</span>}</button>)}
  </div>;
}
