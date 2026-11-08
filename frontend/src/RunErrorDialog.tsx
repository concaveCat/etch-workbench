import { useEffect, useRef } from 'react';

type Props = {
  status: string;
  runId: string;
  reason: string;
  details?: string;
  stepLabel?: string;
  onClose: () => void;
  onViewLog: () => void;
};

const titles: Record<string, string> = {
  failed: '运行失败',
  timed_out: '运行超时',
  interrupted: '运行中断',
};

export function RunErrorDialog({ status, runId, reason, details, stepLabel, onClose, onViewLog }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, []);

  return <dialog
    ref={dialogRef}
    className="run-error-dialog"
    aria-labelledby="run-error-title"
    aria-describedby="run-error-reason"
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}
  >
    <section className="run-error-card">
      <header className="run-error-header">
        <span className="run-error-mark" aria-hidden="true">!</span>
        <div>
          <span className="eyebrow">{status === 'interrupted' ? '服务状态' : '运行诊断'}</span>
          <h2 id="run-error-title">{titles[status] ?? '运行异常'}</h2>
        </div>
        <button type="button" className="run-error-close" aria-label="关闭错误详情" onClick={onClose}>×</button>
      </header>
      <div className="run-error-body">
        <strong>错误原因</strong>
        <p id="run-error-reason">{reason}</p>
        {stepLabel && <small>最近执行步骤：{stepLabel}</small>}
        <small className="run-error-run-id">运行 ID：{runId}</small>
        {details?.trim() && <details className="run-error-details">
          <summary>查看原始错误和运行输出</summary>
          <pre>{details}</pre>
        </details>}
      </div>
      <footer className="run-error-footer">
        <button type="button" className="button" onClick={onViewLog}>打开运行日志</button>
        <button type="button" className="button primary" onClick={onClose}>关闭</button>
      </footer>
    </section>
  </dialog>;
}
