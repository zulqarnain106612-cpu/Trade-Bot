import { useState } from 'react';
import { usePolling } from '../../hooks/useApi';

const box = {
  border: '1px solid var(--c-border)',
  borderRadius: 6,
  padding: 10,
  marginBottom: 10,
};

const btn = (disabled, color) => ({
  background: 'transparent',
  border: `1px solid ${disabled ? 'var(--c-border)' : color}`,
  color: disabled ? 'var(--c-faint)' : color,
  borderRadius: 4,
  padding: '3px 10px',
  fontSize: 11,
  cursor: disabled ? 'not-allowed' : 'pointer',
});

const input = {
  background: 'var(--c-bg)',
  border: '1px solid var(--c-border)',
  borderRadius: 4,
  color: 'var(--c-text)',
  padding: '3px 6px',
  fontSize: 11,
  width: '100%',
};

/**
 * Model training: per-timeframe artifact counts, in-flight state and a
 * retrain trigger.
 *
 * `busy` is keyed by timeframe, not a single boolean: retraining one
 * timeframe must not grey out the buttons for the others, since the
 * backend guards them independently.
 */
export function ModelTrainingPanel({ onRetrain }) {
  const data = usePolling('/models/status', 10000);
  const [busy, setBusy] = useState({});

  if (!data) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>Loading model status…</div>;
  }

  const entries = Object.entries(data.timeframes || {});
  if (entries.length === 0) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>No active timeframes</div>;
  }

  const run = async (tf) => {
    setBusy((b) => ({ ...b, [tf]: true }));
    try {
      await onRetrain(tf);
    } finally {
      setBusy((b) => ({ ...b, [tf]: false }));
    }
  };

  return (
    <div>
      <div style={{ fontSize: 10, color: 'var(--c-faint)', marginBottom: 8, wordBreak: 'break-all' }}>
        {data.model_dir}
      </div>
      {entries.map(([tf, s]) => (
        <div key={tf} style={box}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>
              {tf}
              {s.is_primary && (
                <span style={{ color: 'var(--c-claude)', fontSize: 10, marginLeft: 6 }}>PRIMARY</span>
              )}
            </span>
            <button
              style={btn(s.running || busy[tf], 'var(--c-claude)')}
              disabled={s.running || busy[tf]}
              onClick={() => run(tf)}
            >
              {s.running ? 'Training…' : busy[tf] ? 'Starting…' : 'Retrain'}
            </button>
          </div>
          <div style={{ fontSize: 11, color: 'var(--c-faint)', marginTop: 4 }}>
            {s.artifact_count} artifact file{s.artifact_count === 1 ? '' : 's'}
            {s.artifact_count === 0 && (
              <span style={{ color: 'var(--c-yellow)' }}> — none on disk</span>
            )}
          </div>
          {s.last_error && (
            <div style={{ fontSize: 11, color: 'var(--c-red)', marginTop: 4, wordBreak: 'break-word' }}>
              last error: {s.last_error}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * Historical bar backfill. The request is awaited server-side and returns a
 * real bar count, so the result line reports what was actually written
 * rather than just "started".
 */
export function BackfillPanel({ onBackfill, timeframes }) {
  const [tf, setTf] = useState(timeframes?.[0] || '');
  const [days, setDays] = useState(180);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await onBackfill(tf, Number(days));
      setResult(
        res ? `wrote ${res.bars_written} bars for ${res.timeframe}` : 'failed — see alert'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'flex-end', marginBottom: 8 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10, color: 'var(--c-faint)', marginBottom: 2 }}>Timeframe</div>
          <select style={input} value={tf} onChange={(e) => setTf(e.target.value)}>
            {(timeframes || []).map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>
        <div style={{ width: 80 }}>
          <div style={{ fontSize: 10, color: 'var(--c-faint)', marginBottom: 2 }}>Days</div>
          <input
            style={input}
            type="number"
            min={1}
            max={1825}
            value={days}
            onChange={(e) => setDays(e.target.value)}
          />
        </div>
        <button style={btn(busy || !tf, 'var(--c-green)')} disabled={busy || !tf} onClick={run}>
          {busy ? 'Fetching…' : 'Backfill'}
        </button>
      </div>
      <div style={{ fontSize: 10, color: 'var(--c-faint)' }}>
        Appends bars to storage. Cannot open, close or resize a position.
      </div>
      {result && (
        <div style={{ fontSize: 11, color: 'var(--c-green)', marginTop: 6 }}>{result}</div>
      )}
    </div>
  );
}

/**
 * Capital-preservation floor: halt state per timeframe and the
 * re-authorization control.
 *
 * The reason field is mandatory (the endpoint enforces >= 8 chars) because
 * it is what lands in the audit trail as the record of why trading resumed.
 */
export function CapitalFloorPanel({ onReAuthorize }) {
  const data = usePolling('/capital-floor', 10000);
  const [reasons, setReasons] = useState({});
  const [busy, setBusy] = useState({});

  if (!data) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>Loading floor status…</div>;
  }

  const entries = Object.entries(data.floors || {});
  if (entries.length === 0) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 20, textAlign: 'center' }}>No engines running</div>;
  }

  const run = async (tf) => {
    setBusy((b) => ({ ...b, [tf]: true }));
    try {
      const ok = await onReAuthorize(tf, reasons[tf] || '');
      if (ok) setReasons((r) => ({ ...r, [tf]: '' }));
    } finally {
      setBusy((b) => ({ ...b, [tf]: false }));
    }
  };

  return (
    <div>
      {entries.map(([tf, f]) => (
        <div key={tf} style={box}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>{tf}</span>
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: f.halted ? 'var(--c-red)' : 'var(--c-green)',
              }}
            >
              {f.halted ? 'HALTED' : 'ACTIVE'}
            </span>
          </div>

          {f.halted && (
            <>
              <div style={{ fontSize: 11, color: 'var(--c-red)', marginTop: 4, wordBreak: 'break-word' }}>
                {f.halt_reason}
              </div>
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <input
                  style={input}
                  placeholder="Reason for resuming (min 8 chars)"
                  value={reasons[tf] || ''}
                  onChange={(e) => setReasons((r) => ({ ...r, [tf]: e.target.value }))}
                />
                <button
                  style={btn(busy[tf] || (reasons[tf] || '').trim().length < 8, 'var(--c-red)')}
                  disabled={busy[tf] || (reasons[tf] || '').trim().length < 8}
                  onClick={() => run(tf)}
                >
                  {busy[tf] ? '…' : 'Re-authorize'}
                </button>
              </div>
            </>
          )}

          {f.last_reauthorization && (
            <div style={{ fontSize: 10, color: 'var(--c-faint)', marginTop: 4 }}>
              last cleared by {f.last_reauthorization.authorized_by}
              {f.last_reauthorization.reason ? ` — ${f.last_reauthorization.reason}` : ''}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
