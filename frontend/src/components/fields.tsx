import { useEffect, useState } from 'react';

// Shared form primitives used by the settings and materials panels. They keep
// the numeric draft local so typing a partial value does not commit on every
// keystroke.

export function NumericField({ label, unit, value, onChange, step = 'any', min, max, disabled = false, hint }: {
  label: string; unit?: string; value: number; onChange: (value: number) => void; step?: string; min?: number; max?: number; disabled?: boolean; hint?: string;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return <label className={`field ${disabled ? 'field-disabled' : ''}`}>
    <span className="field-label">{label}{hint && <small>{hint}</small>}</span>
    <span className="number-wrap"><input type="number" aria-label={label} value={draft} step={step} min={min} max={max} disabled={disabled}
      onChange={event => setDraft(event.target.value)}
      onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }}
      onBlur={() => { if (draft.trim() && Number.isFinite(Number(draft))) onChange(Number(draft)); else setDraft(String(value)); }} />
      {unit && <span>{unit}</span>}</span>
  </label>;
}

export function SelectField({ label, value, options, onChange, hint }: { label: string; value: string; options: { value: string; label: string; disabled?: boolean }[]; onChange: (value: string) => void; hint?: string }) {
  return <label className="field"><span className="field-label">{label}{hint && <small>{hint}</small>}</span>
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value)}>{options.map(item => <option key={item.value} value={item.value} disabled={item.disabled}>{item.label}</option>)}</select>
  </label>;
}
