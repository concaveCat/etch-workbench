// Two separate concepts share this bar on purpose: the host GPU capability
// (can this machine run CUDA flux at all) and the selected run's actual engine
// (RunInfo.actualExecution). Label them differently so a GPU-capable host does
// not make a CPU_DISK run read as a GPU run.
export function StatusBar({ health, validationMessage, hash, gpu, engine, stepEngine, stepCount, stepLimit, elapsed, runId, warnings }: {
  health: 'checking' | 'ready' | 'offline'; validationMessage: string; hash: string; gpu: string;
  engine: string; stepEngine?: string | null;
  stepCount: number; stepLimit: number | null; elapsed: string; runId: string; warnings: string[];
}) {
  return <footer className="status-bar">
    <span className={`status-service ${health}`}><i/> {health === 'ready' ? '本机服务已连接' : health === 'checking' ? '正在连接' : '本机服务离线'}</span>
    <details className="status-validation"><summary>校验 {hash ? '✓' : '…'}</summary><div><strong>{validationMessage || '等待工作台校验…'}</strong>{warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div></details>
    <span title="主机 GPU 能力来自 /api/gpu-devices，表示本机能否运行 CUDA 通量；不代表所选运行使用的引擎。">主机 GPU 能力 {gpu}</span>
    <span className="status-engine" title="来自所选运行的 RunInfo.actualExecution.actualEngine；未执行或只有几何操作时显示“未执行/无粒子步骤”。">本次运行引擎 {engine}{stepEngine ? ` · 本步 ${stepEngine}` : ''}</span>
    <span>步骤 {stepCount}/{stepLimit ?? '不限'}</span><span>{elapsed}</span>{runId && <code>RUN {runId.slice(0, 12)}</code>}
  </footer>;
}
