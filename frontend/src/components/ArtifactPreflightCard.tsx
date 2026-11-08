import type { ArtifactEstimate } from '../app/types';
import { ARTIFACT_CONFIDENCE_LABELS, ARTIFACT_RISK_LABELS, formatEstimateMiB } from '../lib/runPresentation';

export function ArtifactPreflightCard({ estimate }: { estimate: ArtifactEstimate | null }) {
  const risk = estimate?.risk ?? null;
  const emptyEstimate = estimate !== null && estimate.estimatedMiB <= 0 && estimate.risk === 'ok';
  return <section className="panel-section artifact-preflight-panel" aria-label="运行产物预估">
    <div className="section-heading">
      <div><span className="eyebrow">当前工程 · 工艺运行</span><h2>运行产物预估</h2></div>
      {risk && <span className={`artifact-risk ${risk}`}>{ARTIFACT_RISK_LABELS[risk]}</span>}
    </div>
    {emptyEstimate ? <div className="inline-note">当前工程没有可估算的工艺步骤（历史运行请在“结果”页查看该 run 的快照和下载信息）。导入或新建工程并添加步骤后，这里会显示本次运行的目录预估。</div> : !estimate ? <div className="inline-note">等待工作台校验后显示本次工艺运行目录的预估大小。该数值来自运行产物预检，是保守估算而不是实际测量。</div> : <>
      <div className={`artifact-meter ${estimate.risk}`} role="img"
        aria-label={`预估占 ${estimate.capMiB} MiB 上限的 ${Math.round(estimate.utilization * 100)}%`}>
        <i style={{ width: `${Math.min(100, Math.max(2, Math.round(estimate.utilization * 100)))}%` }} />
      </div>
      <div className="artifact-metrics">
        <div><span>预估运行目录</span><strong>{formatEstimateMiB(estimate.estimatedMiB)} / {estimate.capMiB} MiB</strong><small>占上限 {Math.round(estimate.utilization * 100)}% · 安全系数 ×{estimate.safetyFactor ?? 1.2}</small></div>
        <div><span>导出 ZIP 预估</span><strong>{formatEstimateMiB(estimate.archiveEstimateMiB)}</strong><small>{estimate.archiveRisk === 'ok' ? '低于上限；下载全部快照预计可用。' : '物化全部步骤快照后会超过上限；一次下载全部快照可能返回 HTTP 413。'}</small></div>
        <div><span>估算置信度</span><strong>{ARTIFACT_CONFIDENCE_LABELS[estimate.confidence]}</strong><small>{estimate.lazySnapshots === false ? '每个步骤快照都立即写入磁盘。' : '延迟快照仅在首次读取（查看器或下载）时物化。'}</small></div>
      </div>
      {(estimate.contributors?.length ?? 0) > 0 && <div className="artifact-contributors">
        <span className="field-label">主要占用</span>
        {[...(estimate.contributors ?? [])]
          .filter(item => item.bytes > 0 && item.kind !== 'archiveAllSnapshots')
          .sort((a, b) => b.bytes - a.bytes).slice(0, 3).map(item =>
          <div key={item.kind}><span>{item.detail}</span><strong>{formatEstimateMiB(item.bytes / (1024 * 1024))}</strong></div>)}
      </div>}
      {(estimate.suggestions?.length ?? 0) > 0 && <div className="artifact-suggestions">
        <span className="field-label">建议</span>
        <ul>{(estimate.suggestions ?? []).map((suggestion, index) => <li key={index}>{suggestion}</li>)}</ul>
      </div>}
      {estimate.risk === 'reject'
        ? <div className="artifact-blocked" role="alert">
            <strong>运行会在工作进程启动前被拒绝</strong>
            <span>预估值超过 {estimate.capMiB} MiB 上限，POST /api/runs 返回 422（artifact-budget-preflight），“运行工艺”按钮已置灰。结构预览仍可提交。</span>
          </div>
        : estimate.risk === 'warning'
          ? <div className="inline-note warning">预估值已进入警告区间（≥ {Math.round((estimate.warningFraction ?? 0.75) * 100)}% 上限）；仍可运行，但实际占用接近上限时下载或快照读取可能失败。{estimate.rejectionDowngraded ? '该配置使用未校准的输出模式，超限只警告、不拒绝。' : ''}</div>
          : <div className="inline-note">预估值低于警告阈值；实际占用仍以运行时目录为准。</div>}
      <div className="inline-note artifact-source">按当前工程的工艺运行规模估算；提交运行时后端会重新计算并强制执行上限。</div>
    </>}
  </section>;
}
