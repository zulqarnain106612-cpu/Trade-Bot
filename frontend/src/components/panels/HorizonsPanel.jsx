import { usePolling } from '../../hooks/useApi';

const DIR_COLOR = { 1: 'var(--c-green)', '-1': 'var(--c-red)', 0: 'var(--c-faint)' };
const DIR_LABEL = { 1: 'LONG', '-1': 'SHORT', 0: 'FLAT' };

// A horizon whose worker has stopped answering keeps its last value
// forever otherwise — age is the only thing that distinguishes "predicting
// flat" from "not predicting at all".

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

      {data.enabled && (
        <div style={{ fontSize: 10, color: 'var(--c-faint)', marginBottom: 8 }}>
          Decision: <strong>{data.decision?.action || 'PENDING'}</strong>
          {data.decision?.abstain_reason ? ` · ${data.decision.abstain_reason}` : ''}
          {' · '}workers {data.scheduler?.alive_model_workers ?? 0}/{data.scheduler?.configured_workers ?? 0}
          {' · '}queue {data.scheduler?.input_queue_depth ?? 'n/a'}
          {' · '}dropped {data.scheduler?.dropped_tasks ?? 0}
          {' · '}observed cadence {rows[0]?.source_cadence_seconds == null
            ? 'unknown'
            : `${rows[0].source_cadence_seconds.toFixed(1)}s`}
        </div>
      )}

      <div style={{ overflowX: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              {['Horizon', 'State / artifact', 'Dir', 'Move ± RMSE', 'Samples', 'OOS', 'Latency / queue', 'Age'].map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((h) => {
              const p = h.last_prediction;
              const stale = h.status === 'STALE';
              const sampleTitle = [
                `last matured at ${h.last_matured_label_at ?? 'unknown'}`,
                `return ${h.last_matured_label ?? 'unknown'}`,
                `last update ${h.last_update ?? 'unknown'}`,
                `update ${h.last_update_duration_ms == null ? 'unknown' : `${h.last_update_duration_ms.toFixed(1)}ms`}`,
              ].join(' · ');
              return (
                <tr key={h.key}>
                  <td style={{ fontWeight: 600 }}>{h.label}</td>
                  <td
                    title={`schema ${p?.feature_schema_hash || 'unavailable'}`}
                    style={{ color: h.error ? 'var(--c-red)' : 'var(--c-faint)' }}
                  >
                    {h.status || h.model_status || '—'}
                    {` · ${h.active_lifecycle_state || h.lifecycle_state || 'UNKNOWN'}`}
                    {` · ${h.artifact_available ? 'artifact present' : 'no artifact'}`}
                    {h.candidate_artifact_available ? ' · trainer checkpoint' : ''}
                    {h.shadow_artifact_available
                      ? ` · candidate ${h.candidate_evaluation?.outcome || 'awaiting evaluation'}`
                      : ''}
                    {` · ${h.calibration_state || 'UNKNOWN'}`}
                    {p?.model_version ? ` · ${p.model_version}` : ''}
                  </td>
                  <td style={{ color: p ? DIR_COLOR[String(p.direction)] : 'var(--c-faint)' }}>
                    {p ? DIR_LABEL[String(p.direction)] : '—'}
                  </td>
                  <td>
                    {p ? `${p.magnitude_mu >= 0 ? '+' : ''}${p.magnitude_mu.toFixed(4)} ±${p.magnitude_sigma.toFixed(4)}` : '—'}
                  </td>
                  <td title={sampleTitle}>
                    {h.training_samples ?? p?.training_samples ?? 0}
                    {' / '}{h.matured_samples ?? 0} matured
                    {' · '}{h.training_pending_samples ?? 0} queued
                  </td>
                  <td title={h.candidate_evaluation?.reason || ''}>
                    {p?.oos_score == null ? '—' : `${p.oos_score.toFixed(3)} (${p.oos_sample_count ?? 0})`}
                    {h.candidate_oos_samples ? ` · candidate ${h.candidate_oos_directional_accuracy?.toFixed(3) ?? '—'} (${h.candidate_oos_samples})` : ''}
                  </td>
                  <td>
                    {p?.inference_latency_ms == null ? '—' : `${p.inference_latency_ms.toFixed(1)}ms`}
                    {p?.queue_age_ms == null ? '' : ` / ${p.queue_age_ms.toFixed(1)}ms`}
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

      {rows.some((h) => h.error || h.last_prediction?.error) && (
        <div style={{ marginTop: 8 }}>
          {rows
            .filter((h) => h.error || h.last_prediction?.error)
            .map((h) => (
              <div key={h.key} style={{ fontSize: 10, color: 'var(--c-red)' }}>
                {h.label}: {h.error || h.last_prediction.error}
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
