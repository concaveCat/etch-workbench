import { Icon } from '../ui/Icon';
import type { Activity } from './types';

const items: { id: Activity; label: string; icon: Parameters<typeof Icon>[0]['name'] }[] = [
  { id: 'structure', label: '结构', icon: 'structure' },
  { id: 'recipe', label: '配方', icon: 'recipe' },
  { id: 'materials', label: '材料', icon: 'materials' },
  { id: 'results', label: '结果', icon: 'results' },
  { id: 'settings', label: '设置', icon: 'settings' },
];

export function Rail({ activity, onChange, gpuStatus }: { activity: Activity; onChange: (activity: Activity) => void; gpuStatus: string }) {
  return <nav className="activity-rail" aria-label="工作区活动">
    <div className="rail-items">{items.map(item => <button key={item.id} type="button" className={`rail-item ${activity === item.id ? 'selected' : ''}`} aria-current={activity === item.id ? 'page' : undefined} onClick={() => onChange(item.id)}><Icon name={item.icon}/><span>{item.label}</span></button>)}</div>
    <div className="rail-foot"><span>ViennaPS</span><span className={`rail-gpu ${gpuStatus.includes('已验证') ? 'verified' : ''}`} title={`主机 GPU 能力：${gpuStatus} · 表示本机能否运行 ViennaPS CUDA 通量，不代表所选运行使用的引擎（见状态栏“本次运行引擎”）`}>主机 GPU</span></div>
  </nav>;
}
