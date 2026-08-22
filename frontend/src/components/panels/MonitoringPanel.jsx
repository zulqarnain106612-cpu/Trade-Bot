import { usePolling } from '../../hooks/useApi';
import { fmt } from '../../utils/format';

export function HealthPanel() {
  const data = usePolling('/health', 10000);
  const debug = usePolling('/debug/health', 15000);

  return (
    <div style={{ fontSize: 11 }}>
      {data && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 12 }}>
          <InfoRow label="Status" value={data.status} color={data.status === 'ok' ? 'var(--c-green)' : 'var(--c-red)'} />
          <InfoRow label="Trading Mode" value={data.trading_mode?.toUpperCase()} color="var(--c-cyan)" />
          <InfoRow label="Execution Mode" value={data.execution_mode?.toUpperCase()} color="var(--c-claude)" />
          <InfoRow label="Timestamp" value={new Date(data.timestamp).toLocaleTimeString()} />
          {data.storage && Object.entries(data.storage).map(([k, v]) => (
            <InfoRow key={k} label={k.replace(/_/g, ' ')} value={v} />
          ))}
        </div>
      )}
      {debug && (
        <div>
          <div style={{ fontSize: 10, color: 'var(--c-muted)', textTransform: 'uppercase', marginBottom: 6 }}>Runtime Monitor</div>
          <InfoRow label="Overall" value={debug.overall} color={debug.overall === 'healthy' ? 'var(--c-green)' : 'var(--c-yellow)'} />
          {debug.alerts && debug.alerts.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 10, color: 'var(--c-red)', marginBottom: 4 }}>Alerts</div>
              {debug.alerts.map((a, i) => (
                <div key={i} style={{ color: 'var(--c-red)', fontSize: 10, padding: '2px 0' }}>{typeof a === 'string' ? a : JSON.stringify(a)}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function DriftPanel() {
  const data = usePolling('/debug/drift', 30000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading drift data...</div>;

  return (
    <div style={{ fontSize: 11 }}>
      {data.drifted_features && data.drifted_features.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ color: 'var(--c-yellow)', fontSize: 10, marginBottom: 4 }}>DRIFTED FEATURES</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {data.drifted_features.map((f) => (
              <span key={f} className="badge" style={{ background: 'rgba(234,179,8,0.15)', color: 'var(--c-yellow)' }}>{f}</span>
            ))}
          </div>
        </div>
      )}
      {data.model_degradation && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 10, color: 'var(--c-muted)', marginBottom: 4 }}>MODEL DEGRADATION</div>
          {Object.entries(data.model_degradation).map(([k, v]) => (
            <InfoRow key={k} label={k} value={typeof v === 'object' ? JSON.stringify(v) : String(v)} />
          ))}
        </div>
      )}
      {data.feature_drift && data.feature_drift.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Feature</th>
                <th>KS Stat</th>
                <th>Drifted</th>
                <th>Train Mean</th>
                <th>Live Mean</th>
              </tr>
            </thead>
            <tbody>
              {data.feature_drift.slice(0, 20).map((r) => (
                <tr key={r.feature}>
                  <td>{r.feature}</td>
                  <td>{fmt(r.ks_statistic, 4)}</td>
                  <td style={{ color: r.drifted ? 'var(--c-yellow)' : 'var(--c-green)' }}>
                    {r.drifted ? 'YES' : 'no'}
                  </td>
                  <td>{fmt(r.train_mean, 4)}</td>
                  <td>{fmt(r.live_mean, 4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function AuditPanel() {
  const data = usePolling('/audit/integrity', 30000);
  const tradeAudit = usePolling('/debug/audit?limit=20', 30000);

  return (
    <div style={{ fontSize: 11 }}>
      {data && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, color: 'var(--c-muted)', marginBottom: 6 }}>HASH CHAIN INTEGRITY</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
            <InfoRow label="Intact" value={data.intact ? 'YES' : 'BROKEN'} color={data.intact ? 'var(--c-green)' : 'var(--c-red)'} />
            <InfoRow label="Entry Count" value={data.entry_count} />
            <InfoRow label="Total Recorded" value={data.total_recorded} />
            <InfoRow label="Evicted" value={data.evicted_count} />
          </div>
        </div>
      )}
      {tradeAudit?.summary && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, color: 'var(--c-muted)', marginBottom: 6 }}>TRADE AUDIT SUMMARY</div>
          {Object.entries(tradeAudit.summary).map(([k, v]) => (
            <InfoRow key={k} label={k.replace(/_/g, ' ')} value={typeof v === 'number' ? fmt(v, 4) : String(v)} />
          ))}
        </div>
      )}
      {tradeAudit?.anomalies && Object.keys(tradeAudit.anomalies).length > 0 && (
        <div>
          <div style={{ fontSize: 10, color: 'var(--c-yellow)', marginBottom: 4 }}>ANOMALIES DETECTED</div>
          {Object.entries(tradeAudit.anomalies).map(([k, v]) => (
            <InfoRow key={k} label={k} value={JSON.stringify(v)} color="var(--c-yellow)" />
          ))}
        </div>
      )}
    </div>
  );
}

export function ReconcilePanel() {
  const data = usePolling('/debug/reconcile', 30000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading reconciliation...</div>;

  return (
    <div style={{ fontSize: 11 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 10 }}>
        <InfoRow label="Consistent" value={data.consistent ? 'YES' : 'NO'} color={data.consistent ? 'var(--c-green)' : 'var(--c-red)'} />
        <InfoRow label="Truncated" value={data.truncated ? 'YES' : 'no'} />
        <InfoRow label="Local Positions" value={data.local_position_count} />
        <InfoRow label="Reference Positions" value={data.reference_position_count} />
      </div>
      {data.discrepancies && data.discrepancies.length > 0 && (
        <div>
          <div style={{ color: 'var(--c-red)', fontSize: 10, marginBottom: 4 }}>DISCREPANCIES</div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Type</th>
                <th>Local Qty</th>
                <th>Reference Qty</th>
              </tr>
            </thead>
            <tbody>
              {data.discrepancies.map((d, i) => (
                <tr key={i}>
                  <td>{d.symbol}</td>
                  <td style={{ color: 'var(--c-red)' }}>{d.discrepancy_type}</td>
                  <td>{d.local_quantity}</td>
                  <td>{d.reference_quantity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ModelMetricsPanel() {
  const data = usePolling('/model-metrics', 30000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading model metrics...</div>;

  const renderModel = (label, m) => {
    if (!m) return <div style={{ color: 'var(--c-faint)', fontSize: 10 }}>{label}: No data</div>;
    return (
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 10, color: 'var(--c-cyan)', marginBottom: 4 }}>{label.toUpperCase()}</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4 }}>
          <InfoRow label="Version" value={m.version} />
          <InfoRow label="OOS Sharpe" value={fmt(m.oos_sharpe, 4)} color={m.oos_sharpe >= 1.5 ? 'var(--c-green)' : 'var(--c-yellow)'} />
          <InfoRow label="Max DD" value={`${fmt(m.max_drawdown, 2)}%`} />
          <InfoRow label="Accuracy" value={`${fmt(m.accuracy * 100, 1)}%`} />
          <InfoRow label="F1 Score" value={fmt(m.f1_score, 4)} />
          <InfoRow label="N Trades" value={m.n_trades} />
          <InfoRow label="Live Gate" value={m.live_gate_pass ? 'PASS' : 'FAIL'} color={m.live_gate_pass ? 'var(--c-green)' : 'var(--c-red)'} />
        </div>
      </div>
    );
  };

  return (
    <div style={{ fontSize: 11 }}>
      <InfoRow label="Timeframe" value={data.timeframe} color="var(--c-cyan)" />
      <div style={{ marginTop: 8 }}>
        {renderModel('Direction Model', data.direction)}
        {renderModel('Meta-Label Model', data.meta_label)}
      </div>
    </div>
  );
}

export function LedgerPanel() {
  const data = usePolling('/ledger', 15000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading ledger...</div>;

  return (
    <div style={{ fontSize: 11 }}>
      <InfoRow label="Total Margin Used" value={`$${fmt(data.total_margin_used_usd)}`} color="var(--c-cyan)" />
      {data.venues && data.venues.length > 0 && (
        <div style={{ marginTop: 8, overflowX: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Venue</th>
                <th>Symbol</th>
                <th>Quantity</th>
                <th>Entry Price</th>
                <th>Margin USD</th>
              </tr>
            </thead>
            <tbody>
              {data.venues.map((v, i) => (
                <tr key={i}>
                  <td style={{ color: 'var(--c-purple)' }}>{v.venue}</td>
                  <td>{v.symbol}</td>
                  <td>{v.quantity}</td>
                  <td>${fmt(v.entry_price, 4)}</td>
                  <td>${fmt(v.margin_used_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.by_symbol && Object.keys(data.by_symbol).length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 10, color: 'var(--c-muted)', marginBottom: 4 }}>BY SYMBOL</div>
          {Object.entries(data.by_symbol).map(([sym, info]) => (
            <div key={sym} style={{ display: 'flex', gap: 12, padding: '3px 0', borderBottom: '1px solid var(--c-border)' }}>
              <span style={{ fontWeight: 600 }}>{sym}</span>
              <span>Net: {fmt(info.net_exposure, 6)}</span>
              <span>Gross: {fmt(info.gross_exposure, 6)}</span>
              <span style={{ color: 'var(--c-faint)' }}>Venues: {info.venues?.join(', ')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RecoveryPanel({ action }) {
  const data = usePolling('/recovery/status', 15000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading recovery status...</div>;

  return (
    <div style={{ fontSize: 11 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 10 }}>
        <InfoRow label="Applicable" value={data.applicable ? 'YES' : 'NO'} />
        <InfoRow label="Blocked" value={data.blocked ? 'BLOCKED' : 'Clear'} color={data.blocked ? 'var(--c-red)' : 'var(--c-green)'} />
      </div>
      {data.blocked && (
        <div style={{ marginBottom: 8 }}>
          <button
            className="btn btn-claude"
            onClick={() => action('/recovery/acknowledge', 'POST', {})}
          >
            Acknowledge Recovery
          </button>
        </div>
      )}
      {data.discrepancies && data.discrepancies.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Type</th>
              <th>Local Qty</th>
              <th>Exchange Qty</th>
            </tr>
          </thead>
          <tbody>
            {data.discrepancies.map((d, i) => (
              <tr key={i}>
                <td>{d.symbol}</td>
                <td style={{ color: 'var(--c-red)' }}>{d.type}</td>
                <td>{d.local_quantity}</td>
                <td>{d.exchange_quantity}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function InfoRow({ label, value, color }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 0' }}>
      <span style={{ color: 'var(--c-muted)', fontSize: 10 }}>{label}</span>
      <span style={{ fontFamily: 'monospace', fontSize: 11, color: color || 'var(--c-text)' }}>{value ?? '—'}</span>
    </div>
  );
}
