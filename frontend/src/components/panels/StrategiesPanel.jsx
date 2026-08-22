import { usePolling } from '../../hooks/useApi';
import { fmt } from '../../utils/format';

export function StrategiesPanel({ action }) {
  const attribution = usePolling('/strategies/attribution', 30000);
  const allocation = usePolling('/strategies/allocation', 30000);
  const gauntlet = usePolling('/strategies/gauntlet', 60000);

  return (
    <div style={{ fontSize: 11 }}>
      {allocation && (
        <Section title="CAPITAL ALLOCATION">
          {allocation.allocations ? (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 6 }}>
              {Object.entries(allocation.allocations).map(([name, frac]) => (
                <div key={name} style={{
                  background: 'var(--c-surface2)',
                  borderRadius: 4,
                  padding: '6px 10px',
                  border: '1px solid var(--c-border)',
                }}>
                  <div style={{ fontSize: 10, color: 'var(--c-muted)', marginBottom: 2 }}>{name}</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <div style={{
                      flex: 1, height: 4, background: 'var(--c-surface3)', borderRadius: 2, overflow: 'hidden',
                    }}>
                      <div style={{
                        width: `${Math.min(100, (frac || 0) * 100)}%`,
                        height: '100%',
                        background: 'var(--c-cyan)',
                        borderRadius: 2,
                      }} />
                    </div>
                    <span style={{ fontFamily: 'monospace', color: 'var(--c-cyan)', fontSize: 11 }}>
                      {fmt((frac || 0) * 100, 1)}%
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: 'var(--c-faint)' }}>No allocation data</div>
          )}
        </Section>
      )}

      {attribution && (
        <Section title="P&L ATTRIBUTION">
          {attribution.strategies ? (
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Total PnL</th>
                    <th>Trades</th>
                    <th>Win Rate</th>
                    <th>Sharpe</th>
                    <th>Max DD</th>
                  </tr>
                </thead>
                <tbody>
                  {(Array.isArray(attribution.strategies) ? attribution.strategies : Object.entries(attribution.strategies).map(([k, v]) => ({ name: k, ...v }))).map((s) => (
                    <tr key={s.name || s.strategy}>
                      <td style={{ color: 'var(--c-purple)' }}>{s.name || s.strategy}</td>
                      <td style={{ color: (s.total_pnl || s.pnl_usd || 0) >= 0 ? 'var(--c-green)' : 'var(--c-red)', fontWeight: 700 }}>
                        ${fmt(s.total_pnl || s.pnl_usd, 2)}
                      </td>
                      <td>{s.trade_count || s.n_trades || '—'}</td>
                      <td>{s.win_rate != null ? `${fmt(s.win_rate * 100, 1)}%` : '—'}</td>
                      <td>{fmt(s.sharpe, 2)}</td>
                      <td>{s.max_drawdown != null ? `${fmt(s.max_drawdown, 2)}%` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ color: 'var(--c-faint)' }}>No attribution data</div>
          )}
        </Section>
      )}

      {gauntlet && (
        <Section title="PROMOTION GAUNTLET">
          {gauntlet.candidates || gauntlet.strategies ? (
            <div style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th>Trades</th>
                    <th>Days</th>
                    <th>Sharpe</th>
                    <th>Max DD</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {(Array.isArray(gauntlet.candidates || gauntlet.strategies)
                    ? (gauntlet.candidates || gauntlet.strategies)
                    : Object.entries(gauntlet.candidates || gauntlet.strategies || {}).map(([k, v]) => ({ id: k, ...v }))
                  ).map((c) => {
                    const passed = c.passed || c.status === 'passed';
                    return (
                      <tr key={c.id || c.strategy_id || c.name}>
                        <td style={{ color: 'var(--c-purple)' }}>{c.id || c.strategy_id || c.name}</td>
                        <td style={{ color: passed ? 'var(--c-green)' : c.status === 'killed' ? 'var(--c-red)' : 'var(--c-yellow)' }}>
                          {c.status || (passed ? 'PASSED' : 'PENDING')}
                        </td>
                        <td>{c.n_trades || c.trades || '—'}</td>
                        <td>{fmt(c.days_running || c.days, 1)}</td>
                        <td>{fmt(c.sharpe, 2)}</td>
                        <td>{c.max_drawdown != null ? `${fmt(c.max_drawdown, 2)}%` : '—'}</td>
                        <td>
                          {(c.status === 'killed' || c.killed) && (
                            <button
                              className="btn btn-green"
                              style={{ padding: '2px 8px', fontSize: 9 }}
                              onClick={() => action(`/strategies/${encodeURIComponent(c.id || c.strategy_id || c.name)}/re-enable`, 'POST', {})}
                            >
                              Re-enable
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ color: 'var(--c-faint)' }}>No gauntlet data</div>
          )}
        </Section>
      )}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 10, color: 'var(--c-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6, borderBottom: '1px solid var(--c-border)', paddingBottom: 4 }}>
        {title}
      </div>
      {children}
    </div>
  );
}
