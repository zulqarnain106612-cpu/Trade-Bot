import { fmt } from '../../utils/format';

const REGIME_NAME = { 0: 'RANGING', 1: 'TRENDING', 2: 'VOLATILE' };

export function ApprovalsPanel({ approvals, onResolve }) {
  if (!approvals || approvals.length === 0)
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>No pending approvals</div>;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {approvals.map((req) => (
        <div
          key={req.request_id}
          style={{
            background: 'var(--c-surface2)',
            border: '1px solid var(--c-border)',
            borderRadius: 6,
            padding: 12,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 4 }}>
              <span style={{
                fontWeight: 700,
                color: req.direction === 'long' ? 'var(--c-green)' : 'var(--c-red)',
                fontSize: 12,
              }}>
                {req.direction?.toUpperCase()} {req.symbol}
              </span>
              <span style={{ color: 'var(--c-muted)', fontSize: 11 }}>{req.timeframe}</span>
              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>${fmt(req.notional_usd)}</span>
            </div>
            <div style={{ fontSize: 10, color: 'var(--c-faint)' }}>
              Kelly: {fmt(req.kelly_fraction, 4)} | Meta: {fmt(req.meta_label_prob, 3)} | Signal: {fmt(req.raw_signal, 3)}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn btn-green" onClick={() => onResolve(req.request_id, true)}>
              APPROVE
            </button>
            <button className="btn btn-red" onClick={() => onResolve(req.request_id, false)}>
              REJECT
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
