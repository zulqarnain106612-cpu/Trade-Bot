import { usePolling } from '../../hooks/useApi';

const DIR_COLOR = { 1: 'var(--c-green)', '-1': 'var(--c-red)', 0: 'var(--c-faint)' };
const DIR_LABEL = { 1: 'LONG', '-1': 'SHORT', 0: 'FLAT' };

// A horizon whose worker has stopped answering keeps its last value
// forever otherwise — age is the only thing that distinguishes "predicting
// flat" from "not predicting at all".
const STALE_AFTER_S = 120;

/**
 * crypto-intel-v6 horizon term structure (config/horizons.yaml).
 *
 * Shows every DECLARED horizon, not only those that reported, so a dead
 * worker appears as a gap rather than vanishing from the view.
 */
export function HorizonsPanel() {
  const data = usePolling('/horizons', 10000);

  if (!data) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>Loading horizons…</div>;
  }

  const rows = data.horizons || [];
  if (rows.length === 0) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>No horizons declared</div>;
  }

  const maxConf = Math.max(
    0.01,
    ...rows.map((h) => h.last_prediction?.confidence ?? 0)
  );

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--c-faint)' }}>
          {rows.length} horizons{data.symbol ? ` · ${data.symbol}` : ''}
        </span>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            color: data.enabled ? 'var(--c-green)' : 'var(--c-yellow)',
          }}
        >
          {data.enabled ? 'ENGINE ON' : 'ENGINE OFF'}
        </span>
      </div>

      {!data.enabled && (
        <div style={{ fontSize: 10, color: 'var(--c-faint)', marginBottom: 8 }}>
          Set INTEL_ENABLED=true to start the inference engine. Labels,
          models and schedules below come from config/horizons.yaml.
        </div>
      )}

      <div style={{ overflowX: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              {['Horizon', 'Dir', 'Conf', 'Move', 'Models', 'Age'].map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => {
              const p = h.last_prediction;
              const stale = h.age_seconds != null && h.age_seconds > STALE_AFTER_S;
              return (
                <tr key={h.key}>
                  <td style={{ fontWeight: 600 }}>{h.label}</td>
                  <td style={{ color: p ? DIR_COLOR[String(p.direction)] : 'var(--c-faint)' }}>
                    {p ? DIR_LABEL[String(p.direction)] : '—'}
                  </td>
                  <td>
                    {p ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                        <div
                          style={{
                            width: `${Math.round((p.confidence / maxConf) * 40)}px`,
                            height: 6,
                            background: DIR_COLOR[String(p.direction)],
                            borderRadius: 3,
                            flexShrink: 0,
                          }}
                        />
                        <span>{p.confidence.toFixed(2)}</span>
                      </div>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    {p ? `${p.magnitude_mu >= 0 ? '+' : ''}${p.magnitude_mu.toFixed(4)} ±${p.magnitude_sigma.toFixed(4)}` : '—'}
                  </td>
                  <td style={{ color: 'var(--c-faint)', fontSize: 10 }}>
                    {(h.models || []).join(', ') || '—'}
                  </td>
                  <td style={{ color: stale ? 'var(--c-red)' : 'var(--c-faint)' }}>
                    {h.age_seconds == null ? 'never' : `${Math.round(h.age_seconds)}s`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {rows.some((h) => h.last_prediction?.error) && (
        <div style={{ marginTop: 8 }}>
          {rows
            .filter((h) => h.last_prediction?.error)
            .map((h) => (
              <div key={h.key} style={{ fontSize: 10, color: 'var(--c-red)' }}>
                {h.label}: {h.last_prediction.error}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
