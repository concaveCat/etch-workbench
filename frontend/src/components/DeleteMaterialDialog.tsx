import { useEffect, useRef } from 'react';
import type { MaterialCatalogItem } from '../api/types';

export function DeleteMaterialDialog({ material, busy, error, onClose, onConfirm }: {
  material: MaterialCatalogItem; busy: boolean; error: string; onClose: () => void; onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (!dialog.open) dialog.showModal();
    return () => { if (dialog.open) dialog.close(); };
  }, []);

  return <dialog ref={dialogRef} className="material-delete-dialog" aria-labelledby="material-delete-title" aria-describedby="material-delete-description"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}
    onClick={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section className="material-delete-card">
      <header className="material-delete-header">
        <span className="material-delete-mark" aria-hidden="true">!</span>
        <div><h2 id="material-delete-title">确认删除自定义材料？</h2><strong>{material.name}</strong></div>
        <button type="button" className="run-error-close" aria-label="关闭确认对话框" disabled={busy} onClick={onClose}>×</button>
      </header>
      <div className="material-delete-body">
        <p id="material-delete-description">删除后，该材料会从目录中移除。已分配的原生 ID {material.id} 会保留且不会复用。</p>
        <small>若项目或运行记录仍引用该材料，系统会阻止删除。</small>
        {error && <div className="material-delete-error" role="alert">{error}</div>}
      </div>
      <footer className="material-delete-footer">
        <button type="button" className="button" autoFocus disabled={busy} onClick={onClose}>取消</button>
        <button type="button" className="button danger material-delete-confirm" disabled={busy} onClick={onConfirm}>{busy ? '正在删除…' : '确认删除'}</button>
      </footer>
    </section>
  </dialog>;
}
