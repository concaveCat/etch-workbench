import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './style.css';

type BoundaryState = { error: Error | null };

class ErrorBoundary extends React.Component<{ children: React.ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Etch Workbench UI error', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ padding: 24, fontFamily: 'ui-monospace, monospace', whiteSpace: 'pre-wrap' }}>
        <h2 style={{ fontFamily: 'system-ui, sans-serif' }}>界面渲染失败</h2>
        <p style={{ fontFamily: 'system-ui, sans-serif' }}>
          页面没有崩溃，错误信息如下。请刷新后重试，并把这段文本发给维护者。
        </p>
        <pre style={{ overflow: 'auto', padding: 12, background: '#f4f6f8', borderRadius: 6 }}>
          {this.state.error.name}: {this.state.error.message}
          {'\n\n'}
          {this.state.error.stack ?? ''}
        </pre>
      </div>
    );
  }
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
