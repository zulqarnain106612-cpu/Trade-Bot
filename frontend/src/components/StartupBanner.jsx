// What the API is doing while the dashboard waits on it -- GET /readyz's
// phase and reasons -- or why the dashboard cannot ask at all: the API is
// unreachable, rejected this dashboard's key, or predates /readyz. Renders
// nothing until the first answer, and nothing once the API is ready.
const PHASE_TITLE = {
  starting: 'API starting',
  warming_up: 'API up, models warming up: the trading loops start when they are built',
  degraded: 'API running degraded',
  failed: 'Startup failed: no trading loop is running',
  stopping: 'API shutting down',
  ready: 'API ready with optional component warnings',
};

const box = {
  margin: '0 0 12px',
  padding: '10px 14px',
  borderRadius: 6,
  fontSize: 12,
};
const warning = {
  ...box,
  border: '1px solid rgba(234, 179, 8, 0.35)',
  background: 'rgba(234, 179, 8, 0.08)',
  color: 'var(--c-yellow)',
};
const error = {
  ...box,
  border: '1px solid rgba(239, 68, 68, 0.35)',
  background: 'rgba(239, 68, 68, 0.08)',
  color: 'var(--c-red)',
};

export function StartupBanner({ report, problem }) {
  if (problem) {
    return (
      <div role="alert" style={error}>
        <strong>The dashboard cannot use the API.</strong> {problem.message}
        {problem.detail ? ` (${problem.detail})` : null}
      </div>
    );
  }
  if (!report) return null;
  const reasons = report.reasons ?? [];
  const warnings = report.warnings ?? [];
  if (report.ready && warnings.length === 0) return null;
  return (
    <div role="status" aria-live="polite" style={warning}>
      <strong>{report.ready ? PHASE_TITLE.ready : (PHASE_TITLE[report.phase] ?? `API ${report.phase}`)}</strong>
      {report.trading_mode ? ` · ${report.trading_mode} trading` : null}
      {(reasons.length > 0 || warnings.length > 0) && (
        <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
          {reasons.map((reason) => <li key={reason}>{reason}</li>)}
          {warnings.map((item) => <li key={item}>Warning: {item}</li>)}
        </ul>
      )}
    </div>
  );
}
