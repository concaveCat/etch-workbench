import type { SelectHTMLAttributes } from 'react';

export function ToolbarSelect(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={`toolbar-select ${props.className ?? ''}`} {...props}/>;
}
