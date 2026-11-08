import { useEffect, useMemo, useState } from 'react';
import { parseNativeVtp } from './parseNativeVtp';

type OutputRef = {
  stepId: string; fileName: string; url: string; format: string; pointCount: number | null;
  cellCounts: Record<string, number>; coordinateUnit: string | null; axisMap: Record<string, number> | null;
  fields: { name: string; association: 'pointData' | 'cellData'; components: number; tuples: number;
    nativeType?: string | null; nativeUnit: string | null; unitStatus?: string }[];
  sizeBytes: number; sha256: string; associationPolicy?: string;
};
type NativeField = { name: string; association: 'pointData' | 'cellData'; components: number; tuples: number; values: number[] };
type ParsedOutput = { fields: NativeField[]; pointCount: number };

function formatValue(values: number[], index: number, components: number): string {
  const start = index * components;
  return Array.from({ length: components }, (_, component) => Number(values[start + component]).toPrecision(8)).join(', ');
}

export default function NativeIntermediatePanel({ runId, outputs }: { runId: string; outputs: OutputRef[] }) {
  const vtpOutputs = useMemo(() => outputs.filter(output => output.format === 'VTP' && output.fields.length > 0), [outputs]);
  const [selectedOutput, setSelectedOutput] = useState('');
  const [selectedField, setSelectedField] = useState('');
  const [parsed, setParsed] = useState<ParsedOutput | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const key = vtpOutputs.map(output => `${output.fileName}:${output.sha256}`).join('|');
  const activeOutput = vtpOutputs.find(output => output.fileName === selectedOutput) ?? vtpOutputs[0];
  const activeField = parsed?.fields.find(field => `${field.association}:${field.name}` === selectedField) ?? parsed?.fields[0];

  useEffect(() => {
    if (!activeOutput) {
      setParsed(null); setSelectedOutput(''); setSelectedField(''); setError('');
      return;
    }
    if (selectedOutput !== activeOutput.fileName) setSelectedOutput(activeOutput.fileName);
    let live = true;
    setLoading(true); setError(''); setParsed(null);
    (async () => {
      const response = await fetch(activeOutput.url);
      if (!response.ok) throw new Error(`中间场文件读取失败（HTTP ${response.status}）`);
      const buffer = await response.arrayBuffer();
      const digest = await crypto.subtle.digest('SHA-256', buffer);
      const actualHash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
      if (actualHash !== activeOutput.sha256) throw new Error('VTP 文件 SHA-256 与运行清单不一致。');
      const parsedOutput = await parseNativeVtp(new TextDecoder().decode(buffer), activeOutput);
      const fields: NativeField[] = parsedOutput.fields;
      if (!live) return;
      setParsed(parsedOutput);
      setSelectedField(previous => fields.some(field => `${field.association}:${field.name}` === previous)
        ? previous : fields[0] ? `${fields[0].association}:${fields[0].name}` : '');
    })().catch(reason => { if (live) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  // `key` is the stable content identity; the complete output array is refreshed while polling.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, key, activeOutput?.fileName, activeOutput?.sha256]);

  const exportCsv = () => {
    if (!activeOutput || !activeField) return;
    const quote = (value: unknown) => `"${String(value).replaceAll('"', '""')}"`;
    const unit = activeOutput.fields.find(field => field.association === activeField.association && field.name === activeField.name)?.nativeUnit;
    const title = unit ? `${activeField.name} (${unit})` : `${activeField.name} (native unit not declared)`;
    const rows = [['association', activeField.association], ['field', title], ['sourceVtp', activeOutput.fileName], [],
      ['tupleIndex', title]];
    for (let index = 0; index < activeField.tuples; index++) {
      rows.push([String(index), formatValue(activeField.values, index, activeField.components)]);
    }
    const csv = rows.map(row => row.map(quote).join(',')).join('\r\n') + '\r\n';
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a'); anchor.href = url;
    anchor.download = `viennals-${activeOutput.stepId}-${activeField.name}.csv`;
    anchor.click(); URL.revokeObjectURL(url);
  };

  if (!vtpOutputs.length) return null;
  return <section className="flux-result-panel native-output-panel" aria-label="ViennaLS 原生中间场结果">
    <div className="flux-result-heading"><div><span className="eyebrow">原生求解器输出</span><h2>求解器中间场</h2></div>
      {activeOutput && <a className="button" href={activeOutput.url} download={activeOutput.fileName.split('/').at(-1)}>下载原生 VTP</a>}
      {activeField && <button className="button" onClick={exportCsv} disabled={!parsed}>导出所选字段 CSV</button>}
    </div>
    {vtpOutputs.length > 1 && <label className="flux-field-select"><span>原生输出文件</span><select aria-label="中间场文件" value={activeOutput?.fileName ?? ''} onChange={event => setSelectedOutput(event.target.value)}>
      {vtpOutputs.map(output => <option key={output.fileName} value={output.fileName}>{output.stepId} · {output.fileName.split('/').at(-1)}</option>)}
    </select></label>}
    {activeOutput && <div className="flux-result-meta"><span>{activeOutput.pointCount?.toLocaleString()} 个点 · {Object.entries(activeOutput.cellCounts).filter(([, count]) => count).map(([name, count]) => `${count} ${name}`).join(' · ')}</span>
      <span>坐标：{activeOutput.axisMap ? Object.keys(activeOutput.axisMap).join(' / ') : '原生 VTP' } · {activeOutput.coordinateUnit ?? '单位未声明'}</span>
      <span>SHA-256：{activeOutput.sha256}</span></div>}
    {loading ? <div className="inline-note">正在读取并校验 ViennaLS 原生网格和场数组…</div>
      : error ? <div className="inline-note warning">{error}</div>
      : !parsed ? <div className="inline-note">尚无可显示的原生场数据。</div>
      : <>
        <div className="inline-note">字段从 ViennaLS VTP 原样读取，数组关联保持为 pointData 或 cellData；这里只列出该 VTP 自带网格上的元组，不投影到工艺表面。原生数组未提供单位时不会推断单位。</div>
        <label className="flux-field-select"><span>原生字段</span><select aria-label="原生中间场字段" value={activeField ? `${activeField.association}:${activeField.name}` : ''}
          onChange={event => setSelectedField(event.target.value)}>{parsed.fields.map(field => <option key={`${field.association}:${field.name}`} value={`${field.association}:${field.name}`}>
            {field.association} · {field.name} ({field.components} 分量，{field.tuples} 元组)
          </option>)}</select></label>
        {activeField && <div className="flux-table-wrap"><table className="flux-table"><thead><tr><th>tupleIndex</th><th>{activeField.name}</th></tr></thead>
          <tbody>{Array.from({ length: Math.min(200, activeField.tuples) }, (_, index) => <tr key={index}><td>{index}</td><td>{formatValue(activeField.values, index, activeField.components)}</td></tr>)}</tbody></table>
          {activeField.tuples > 200 && <small>显示前 200 / {activeField.tuples} 个元组；CSV 和 VTP 文件包含完整数组。</small>}</div>}
      </>}
  </section>;
}
