import { usePolling } from '../../hooks/useApi';
import { Toggle } from '../Controls';
import { fmt } from '../../utils/format';

export function SelfTuningPanel({ action }) {
  const data = usePolling('/self-tuning/status', 15000);

  if (!data) return <div style={{ color: 'var(--c-faint)', fontSize: 11 }}>Loading self-tuning status...</div>;

  const probationColor = (s) => {
    if (s === 'clear') return 'var(--c-green)';
    if (s === 'on_probation') return 'var(--c-yellow)';
    return 'var(--c-muted)';
  };

  return (
    <div style={{ fontSize: 11 }}>
      <div style={{ display: 'flex', gap: 12, marginBottom: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <StatusBadge label="Enabled" value={data.enabled} />
        <StatusBadge label="Shadow Mode" value={data.shadow_mode} />
        <StatusBadge label="Paused" value={data.paused} inverted />
        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
          <button
            className="btn btn-claude"
            onClick={() => action('/self-tuning/pause', 'POST', {})}
          >
            Pause
          </button>
          <button
            className="btn btn-green"
            onClick={() => action('/self-tuning/resume', 'POST', {})}
          >
            Resume
          </button>
        </div>
      </div>

      {data.parameters && data.parameters.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Parameter</th>
                <th>Floor</th>
                <th>Ceiling</th>
                <th>Current</th>
                <th>Version</th>
                <th>Probation</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.parameters.map((p) => (
                <tr key={p.name}>
                  <td style={{ color: 'var(--c-cyan)' }}>{p.name}</td>
                  <td>{fmt(p.floor, 4)}</td>
                  <td>{fmt(p.ceiling, 4)}</td>
                  <td style={{ fontWeight: 700 }}>
                    {p.current_version ? fmt(p.current_version.value, 6) : fmt(p.registered_current, 6)}
                  </td>
                  <td>{p.current_version?.version ?? '—'}</td>
                  <td style={{ color: probationColor(p.probation_status) }}>
                    {p.probation_status?.replace(/_/g, ' ')}
                  </td>
                  <td>
                    <button
                      className="btn btn-red"
                      style={{ padding: '2px 8px', fontSize: 9 }}
                      onClick={() => action(`/self-tuning/rollback/${encodeURIComponent(p.name)}`, 'POST', {})}
                    >
                      Rollback
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ label, value, inverted }) {
  const isGood = inverted ? !value : value;
  return (
    <span className="badge" style={{
      background: isGood ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.12)',
      color: isGood ? 'var(--c-green)' : 'var(--c-red)',
    }}>
      {label}: {value ? 'Yes' : 'No'}
    </span>
  );
}
