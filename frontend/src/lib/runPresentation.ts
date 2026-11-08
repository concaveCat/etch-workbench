import type { ArtifactEstimate, ArtifactEstimateRisk } from '../app/types';
import type { RunInfo } from '../api/types';
import { ApiError } from '../api/client';

// Presentation helpers for run state, engine badges, artifact estimates and the
// failure dialog. Pure functions only: they never fetch or mutate state.

// Thicknesses are small (a spacer etch removes a couple of nm), so keep µm for
// absolute heights and switch to nm for sub-10 nm thickness readouts.
export function formatMicron(value: number | null | undefined): string { return value == null || !Number.isFinite(value) ? '—' : `${value.toFixed(4)} µm`; }
export function formatThickness(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return Math.abs(value) < 0.01 ? `${(value * 1000).toFixed(2)} nm` : `${value.toFixed(4)} µm`;
}

export const terminalStates = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']);

export function humanStatus(status?: string): string {
  const labels: Record<string, string> = { pending: '排队中', running: '运行中', cancelling: '正在取消', succeeded: '已完成', failed: '失败', timed_out: '超时', cancelled: '已取消', interrupted: '服务中断' };
  return labels[status ?? ''] ?? '未运行';
}

export const ARTIFACT_RISK_LABELS: Record<ArtifactEstimateRisk, string> = { ok: '可运行', warning: '接近上限', reject: '将拒绝运行' };
export const ARTIFACT_CONFIDENCE_LABELS: Record<ArtifactEstimate['confidence'], string> = { high: '高', medium: '中', low: '低 · 未校准输出' };
export const NOT_APPLICABLE_ENGINE = 'not-applicable';
export function formatEstimateMiB(mib: number): string {
  if (!Number.isFinite(mib)) return '—';
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB` : `${mib < 10 ? mib.toFixed(1) : Math.round(mib)} MiB`;
}
// The 422 from POST /api/runs carries the same structured estimate under
// detail.artifactEstimate; keep the card in sync even if validation lagged.
export function artifactEstimateFromError(error: unknown): ArtifactEstimate | null {
  if (!(error instanceof ApiError) || typeof error.detail !== 'object' || error.detail === null) return null;
  if (!('artifactEstimate' in error.detail)) return null;
  const estimate = (error.detail as { artifactEstimate?: unknown }).artifactEstimate;
  return typeof estimate === 'object' && estimate !== null && 'risk' in estimate ? estimate as ArtifactEstimate : null;
}
// Host GPU capability is about the machine; a displayed run has its own engine
// recorded in RunInfo.actualExecution (actualEngine per step).
export function runEnginesOf(info: RunInfo | null | undefined): string[] {
  return [...new Set((info?.actualExecution ?? [])
    .map(item => item.actualEngine)
    .filter((engine): engine is string => !!engine && engine !== NOT_APPLICABLE_ENGINE))].sort();
}
export function runEngineLabel(engines: string[]): string {
  if (engines.length === 0) return '未执行/无粒子步骤';
  return engines.length === 1 ? engines[0] : '混合';
}
export function engineTone(engines: string[]): 'gpu' | 'cpu' | 'mixed' | 'idle' {
  if (engines.length > 1) return 'mixed';
  if (engines[0] === 'GPU_DISK') return 'gpu';
  if (engines[0] === 'CPU_DISK') return 'cpu';
  return 'idle';
}
export type RunFailureDialogData = { runId: string; status: string; reason: string; details: string; stepLabel?: string };
export const errorRunStates = new Set(['failed', 'timed_out', 'interrupted']);
export function cleanRunOutput(value?: string | null): string {
  return (value ?? '').replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '').trim();
}
export function runFailureDialogData(info: RunInfo): RunFailureDialogData {
  const termination = info.termination;
  const returnCode = termination?.returnCode;
  const rawReason = cleanRunOutput(info.failureReason);
  let reason = '';
  if (termination?.reason === 'rss_limit') {
    const peakMiB = termination.peakObservedRssBytes == null
      ? null : termination.peakObservedRssBytes / (1024 * 1024);
    const limitMiB = termination.rssLimitMiB;
    reason = `进程组占用的系统内存超过 ${limitMiB ?? '未知'} MiB 上限，监督器终止了运行${peakMiB == null ? '' : `（峰值约 ${peakMiB.toFixed(1)} MiB）`}。`;
  } else if (returnCode === -11) {
    reason = '原生计算进程发生段错误（SIGSEGV，退出码 -11）并异常退出。';
  } else if (returnCode != null && returnCode < 0) {
    const signal = -returnCode;
    const signalName: Record<number, string> = { 6: 'SIGABRT', 9: 'SIGKILL', 11: 'SIGSEGV', 15: 'SIGTERM' };
    reason = `计算子进程被 ${signalName[signal] ?? `信号 ${signal}`} 终止（退出码 ${returnCode}）。`;
  } else if (info.status === 'timed_out') {
    reason = rawReason.length < 260 && !rawReason.includes('\n') ? rawReason : '运行超过配置的时间限制。';
  } else if (rawReason && rawReason.length < 260 && !rawReason.includes('\n')) {
    reason = rawReason;
  } else {
    const exceptionLine = rawReason.split(/\r?\n/).reverse().find(line => /(?:Error|Exception):/.test(line));
    reason = exceptionLine?.slice(0, 260) ?? (info.status === 'interrupted' ? '工作台服务中断，运行未能完成。' : '计算进程异常退出；请查看运行输出获取更多线索。');
  }

  const recentStep = info.actualExecution?.at(-1);
  const stepLabel = recentStep
    ? [recentStep.operation, recentStep.model, recentStep.stepId].filter(Boolean).join(' · ')
    : undefined;
  const logText = cleanRunOutput(info.log?.text);
  const details = [rawReason, logText && logText !== rawReason ? logText : '']
    .filter(Boolean).join('\n\n').slice(-16_000);
  return { runId: info.runId, status: info.status, reason, details, stepLabel };
}
