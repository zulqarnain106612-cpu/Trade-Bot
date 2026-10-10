// Binance / OKX connection controls.
//
// Two lights per venue, because they answer different questions: market
// data (did the venue's public API load) and account access (did an
// authenticated, read-only balance request succeed). Every button goes
// through the operator second factor; none of them places an order, changes
// the execution mode or touches a risk limit. Credentials are shown only as
// configured / incomplete / missing -- the API never returns their values.
import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '../../hooks/useApi';

export const VENUE_POLL_MS = 5000;

const STATE_STYLE = {
  connected: { color: 'var(--c-green)', label: 'Connected' },
  connecting: { color: 'var(--c-yellow)', label: 'Connecting' },
  reconnecting: { color: 'var(--c-yellow)', label: 'Reconnecting' },
  disconnected: { color: 'var(--c-muted)', label: 'Disconnected' },
  failed: { color: 'var(--c-red)', label: 'Failed' },
  unavailable: { color: 'var(--c-faint)', label: 'Not started' },
};

const ACCOUNT_STYLE = {
  authenticated: { color: 'var(--c-green)', label: 'Authenticated' },
  unverified: { color: 'var(--c-yellow)', label: 'Not verified' },
  verifying: { color: 'var(--c-yellow)', label: 'Verifying' },
  rejected: { color: 'var(--c-red)', label: 'Credentials rejected' },
  failed: { color: 'var(--c-red)', label: 'Check failed' },
  unconfigured: { color: 'var(--c-muted)', label: 'No credentials' },
  unavailable: { color: 'var(--c-faint)', label: 'Unavailable' },
};

const CREDENTIALS_LABEL = {
  configured: 'configured',
  incomplete: 'incomplete',
  missing: 'missing',
};

export function useVenues(refreshToken) {
  const [venues, setVenues] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/venues');
      if (!res.ok) {
        setError(`GET /venues answered ${res.status}`);
        return;
      }
      const body = await res.json();
      setVenues(body.venues || {});
      setError(null);
    } catch (e) {
      setError(e.message || 'network error');
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, VENUE_POLL_MS);
    return () => clearInterval(id);
  }, [load, refreshToken]);

  return { venues, error, reload: load };
}

function Light({ style, title }) {
  return (
    <span className="venue-light" title={title}>
      <span className="sb-dot" style={{ background: style.color }} aria-hidden="true" />
      <span style={{ color: style.color }}>{style.label}</span>
    </span>
  );
}

export function VenueCard({ name, venue, busy, onAction }) {
  const state = STATE_STYLE[venue.state] || { color: 'var(--c-faint)', label: venue.state };
  const account = ACCOUNT_STYLE[venue.account?.state] || { color: 'var(--c-faint)', label: venue.account?.state };
  const connected = venue.state === 'connected';
  return (
    <div className="venue-card" data-venue={name} data-state={venue.state}>
      <div className="venue-head">
        <strong className="venue-name">{name}</strong>
        {venue.testnet && <span className="badge" style={{ color: 'var(--c-yellow)' }}>testnet</span>}
      </div>
      <dl className="venue-grid">
        <dt>Market data</dt>
        <dd><Light style={state} title={`market data: ${venue.state}`} /></dd>
        <dt>Account</dt>
        <dd><Light style={account} title={`account: ${venue.account?.state}`} /></dd>
        <dt>Credentials</dt>
        <dd>{CREDENTIALS_LABEL[venue.credentials] || venue.credentials}</dd>
      </dl>
      {venue.error && <p className="venue-error" role="status">{venue.error}</p>}
      {venue.account?.error && <p className="venue-error" role="status">{venue.account.error}</p>}
      <div className="venue-actions">
        {!connected && (
          <button type="button" className="btn btn-green" disabled={busy}
            onClick={() => onAction(name, 'connect')}>
            {venue.state === 'failed' ? 'Retry' : 'Connect'}
          </button>
        )}
        {connected && (
          <>
            <button type="button" className="btn btn-blue" disabled={busy}
              onClick={() => onAction(name, 'reconnect')}>Reconnect</button>
            {venue.credentials === 'configured' && (
              <button type="button" className="btn btn-blue" disabled={busy}
                onClick={() => onAction(name, 'verify')}>Verify account</button>
            )}
            <button type="button" className="btn btn-red" disabled={busy}
              onClick={() => onAction(name, 'disconnect')}>Disconnect</button>
          </>
        )}
      </div>
    </div>
  );
}

// The state comes from App's single useVenues() so the status bar and this
// panel can never disagree about a venue.
export function VenuesPanel({ venues, error, reload, operatorAction }) {
  const [busy, setBusy] = useState(null);

  const onAction = async (name, verb) => {
    if (verb === 'disconnect'
      && !window.confirm(`Disconnect ${name}? Market data and orders on ${name} stop until it is connected again.`)) {
      return;
    }
    setBusy(name);
    try {
      await operatorAction(`/venues/${encodeURIComponent(name)}/${verb}`, 'POST', {});
    } finally {
      setBusy(null);
      reload();
    }
  };

  if (error && !venues) return <p className="proc-empty" role="alert">Venue status unavailable: {error}</p>;
  if (!venues) return <p className="proc-empty">Loading venue status…</p>;
  return (
    <div className="venues">
      {error && <p className="venue-error" role="status">Last refresh failed: {error}</p>}
      {Object.entries(venues).map(([name, venue]) => (
        <VenueCard key={name} name={name} venue={venue} busy={busy === name} onAction={onAction} />
      ))}
    </div>
  );
}
