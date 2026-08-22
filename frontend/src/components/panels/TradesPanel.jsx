import { fmt, tsToTime } from '../../utils/format';

const REGIME_COLOR = { 0: '#22c55e', 1: '#da7756', 2: '#ef4444' };
const REGIME_NAME = { 0: 'RANGING', 1: 'TRENDING', 2: 'VOLATILE' };

export function TradesTable({ trades }) {
  if (!trades || trades.length === 0)
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>No trades yet</div>;

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {['Time', 'Symbol', 'TF', 'Dir', 'Entry', 'Exit', 'PnL', 'PnL%', 'Reason', 'Kelly', 'Regime'].map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => {
            const pnlColor = t.pnl_usd == null ? '' : t.pnl_usd >= 0 ? 'var(--c-green)' : 'var(--c-red)';
            return (
              <tr key={t.id}>
                <td>{tsToTime(t.entry_ts)}</td>
                <td>{t.symbol}</td>
                <td>{t.timeframe}</td>
                <td style={{ color: t.direction === 'long' ? 'var(--c-green)' : 'var(--c-red)', fontWeight: 700 }}>
                  {t.direction?.toUpperCase()}
                </td>
                <td>${fmt(t.entry_price)}</td>
                <td>{t.exit_price ? `$${fmt(t.exit_price)}` : 'open'}</td>
                <td style={{ color: pnlColor, fontWeight: 700 }}>
                  {t.pnl_usd != null ? `$${fmt(t.pnl_usd, 4)}` : '—'}
                </td>
                <td style={{ color: pnlColor }}>{t.pnl_pct != null ? `${fmt(t.pnl_pct, 3)}%` : '—'}</td>
                <td style={{ color: 'var(--c-muted)' }}>{t.exit_reason || '—'}</td>
                <td>{fmt(t.kelly_fraction, 4)}</td>
                <td style={{ color: REGIME_COLOR[t.regime_at_entry] }}>
                  {REGIME_NAME[t.regime_at_entry]}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const MISSED_REASON_LABEL = {
  rejected: 'Rejected',
  skipped: 'Approval Timeout',
  queued: 'Queued',
  auto_timeout: 'Auto Timeout',
};

export function MissedTradesTable({ missedTrades }) {
  if (!missedTrades || missedTrades.length === 0)
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>No missed trades</div>;

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {['Time', 'Symbol', 'TF', 'Dir', 'Reason', 'Notional', 'Kelly', 'Meta', 'Signal', 'Regime'].map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {missedTrades.map((m) => (
            <tr key={m.id}>
              <td>{tsToTime(m.ts)}</td>
              <td>{m.symbol}</td>
              <td>{m.timeframe}</td>
              <td style={{ color: m.direction === 'long' ? 'var(--c-green)' : 'var(--c-red)', fontWeight: 700 }}>
                {m.direction?.toUpperCase()}
              </td>
              <td>
                <span className="badge" style={{ background: 'rgba(218,119,86,0.15)', color: 'var(--c-claude-light)' }}>
                  {MISSED_REASON_LABEL[m.reason] || m.reason}
                </span>
              </td>
              <td>${fmt(m.notional_usd)}</td>
              <td>{fmt(m.kelly_fraction, 4)}</td>
              <td>{fmt(m.meta_label_prob, 3)}</td>
              <td>{fmt(m.raw_signal, 3)}</td>
              <td style={{ color: REGIME_COLOR[m.regime_at_entry] }}>
                {REGIME_NAME[m.regime_at_entry]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
