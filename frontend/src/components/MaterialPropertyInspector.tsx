import type { FormEvent } from 'react';
import type { MaterialCatalogItem } from '../api/types';
import { MATERIAL_CATEGORY_OPTIONS, materialCategoryLabel } from '../lib/projectRules';

export function MaterialPropertyInspector({ material, busy, onSave }: { material: MaterialCatalogItem; busy: boolean; onSave: (name: string, event: FormEvent<HTMLFormElement>) => Promise<void> }) {
  return <div className="material-property-inspector" key={material.name}>
    <div className="material-inspector-head">
      <i className="material-swatch large" style={{ background: material.color }} />
      <div><strong>{material.name}</strong><span>{material.isCustom ? '自定义材料' : '预设材料'} · 原生 ID {material.id}{material.customId != null ? ` · customId ${material.customId}` : ''}</span></div>
    </div>
    {material.isCustom
      ? <form className="field-grid one-column" onSubmit={event => void onSave(material.name, event)}>
          <label className="field"><span className="field-label">材料类型</span><select aria-label={`${material.name} 类型`} name="category" defaultValue={material.category ?? 'Generic'}>{MATERIAL_CATEGORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="field"><span className="field-label">显示颜色</span><input aria-label={`${material.name} 颜色`} name="color" type="color" defaultValue={material.color} /></label>
          <label className="field"><span className="field-label">密度</span><input aria-label={`${material.name} 密度`} name="densityGcm3" type="number" min="0" max="100" step="0.01" placeholder="g/cm³，可留空" defaultValue={material.densityGcm3 ?? ''} /></label>
          <label className="field"><span className="field-label">是否导电</span><span className="field-checkbox"><input aria-label={`${material.name} 是否导电`} name="conductive" type="checkbox" defaultChecked={material.conductive ?? false} /></span></label>
          <label className="field"><span className="field-label">备注</span><textarea aria-label={`${material.name} 备注`} name="description" maxLength={500} defaultValue={material.description ?? ''} /></label>
          <div className="inline-note">名称与原生 ID 固定；类型、颜色、密度和导电标记只影响 ViennaPS 元数据与显示，不会自动建立刻蚀、沉积或黏附物理模型。</div>
          <button className="button" type="submit" disabled={busy}>保存材料属性</button>
        </form>
      : <div className="field-grid one-column">
          <div className="material-property-row"><span>材料类型</span><strong>{materialCategoryLabel(material.category)}</strong></div>
          <div className="material-property-row"><span>显示颜色</span><strong><i className="material-swatch" style={{ background: material.color }} />{material.color}</strong></div>
          <div className="material-property-row"><span>密度</span><strong>{material.densityGcm3 != null ? `${material.densityGcm3} g/cm³` : '未设置'}</strong></div>
          <div className="material-property-row"><span>是否导电</span><strong>{material.conductive ? '是' : '否'}</strong></div>
          <div className="inline-note">预设材料的 MaterialInfo 由 ViennaPS 4.6.2 固定，不能修改。</div>
        </div>}
  </div>;
}
