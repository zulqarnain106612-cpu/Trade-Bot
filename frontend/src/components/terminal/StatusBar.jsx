// The persistent bottom bar. Terminal and Processes sit side by side and
// toggle their panes; neither button ever stops anything.
import { useTerminal } from '../../terminal/TerminalContext';

const CONNECTION_LABEL = {
  open: 'terminal service connected',
  connecting: 'connecting to terminal service',
  idle: 'connecting to terminal service',
  offline: 'terminal service offline',
  unauthorized: 'terminal token required',
  incompatible: 'terminal service version mismatch',
  misconfigured: 'terminal disabled (bad URL)',
  closed: 'terminal disconnected',
};

const VENUE_COLOR = {
  connected: 'var(--c-green)',
  connecting: 'var(--c-yellow)',
  reconnecting: 'var(--c-yellow)',
  disconnected: 'var(--c-muted)',
  failed: 'var(--c-red)',
  unavailable: 'var(--c-faint)',
};

export function StatusBar({ venues, onVenueClick }) {
  const { state, toggleDock, toggleCenter } = useTerminal();
  const running = Object.keys(state.active).length;
  const unseenFailures = state.centerOpen ? 0 : state.history.filter(
    (p) => p.state === 'failed' && (p.ended_at ?? 0) > state.failuresSeenAt,
  ).length;
  const sessions = state.order.length;

  return (
    <footer className="status-bar" role="toolbar" aria-label="Workspace status">
      <div className="sb-group">
        <button
          type="button"
          className={`sb-btn${state.dockOpen ? ' on' : ''}`}
          aria-pressed={state.dockOpen}
          aria-controls="terminal-dock"
          onClick={toggleDock}
          title="Show or hide the terminal (sessions keep running)"
        >
          <span aria-hidden="true">⌨</span> Terminal
          {sessions > 0 && <span className="sb-count">{sessions}</span>}
        </button>
        <button
          type="button"
          className={`sb-btn${state.centerOpen ? ' on' : ''}`}
          aria-pressed={state.centerOpen}
          onClick={toggleCenter}
          title="Running processes and recent results"
        >
          <span aria-hidden="true">⚙</span> Processes
          <span className="sb-count">{running}</span>
          {unseenFailures > 0 && (
            <span className="sb-failed" aria-label={`${unseenFailures} failed`}>
              {unseenFailures} failed
            </span>
          )}
        </button>
      </div>
      <span className={`sb-conn sb-conn-${state.connection}`} role="status">
        <span className="sb-dot" aria-hidden="true" />
        {CONNECTION_LABEL[state.connection] || state.connection}
      </span>
      <div className="sb-venues">
        {Object.entries(venues || {}).map(([name, venue]) => (
          <button
            type="button"
            key={name}
            className="sb-venue"
            onClick={() => onVenueClick?.(name)}
            title={`${name}: market data ${venue.state}, account ${venue.account?.state ?? 'unknown'}`}
          >
            <span className="sb-dot" style={{ background: VENUE_COLOR[venue.state] || 'var(--c-faint)' }} aria-hidden="true" />
            {name}
            <span className="sr-only">{` market data ${venue.state}, account ${venue.account?.state ?? 'unknown'}`}</span>
          </button>
        ))}
      </div>
    </footer>
  );
}
