import { fmt } from '../../utils/format';

const REGIME_COLOR = { 0: '#22c55e', 1: '#da7756', 2: '#ef4444' };
const REGIME_NAME = { 0: 'RANGING', 1: 'TRENDING', 2: 'VOLATILE' };

export function PositionsTable({ positions }) {
  if (!positions || positions.length === 0)
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>No open positions</div>;

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {['Symbol', 'TF', 'Dir', 'Entry', 'Current', 'Qty', 'Notional', 'Unreal PnL', 'Regime'].map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const pnlColor = p.unrealized_pnl >= 0 ? 'var(--c-green)' : 'var(--c-red)';
            return (
              <tr key={p.trade_id}>
                <td>{p.symbol}</td>
                <td>{p.timeframe}</td>
                <td style={{ color: p.direction === 'long' ? 'var(--c-green)' : 'var(--c-red)', fontWeight: 700 }}>
                  {p.direction?.toUpperCase()}
                </td>
                <td>${fmt(p.entry_price)}</td>
                <td>${fmt(p.current_price)}</td>
                <td>{p.quantity}</td>
                <td>${fmt(p.notional_usd)}</td>
                <td style={{ color: pnlColor, fontWeight: 700 }}>
                  ${fmt(p.unrealized_pnl, 4)} ({fmt(p.unrealized_pnl_pct)}%)
                </td>
                <td style={{ color: REGIME_COLOR[p.regime_at_entry] }}>
                  {REGIME_NAME[p.regime_at_entry]}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
