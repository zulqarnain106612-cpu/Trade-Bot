// The process center: what is running now, and what recently finished.
//
// Every row is a registry entry the daemon reported -- the real argv from
// /proc, the real pid, the exit status the shell or waitpid produced. A
// finished entry leaves "Running" the moment the daemon says it finished and
// appears in "Recent" with its outcome; failures are red and keep their
// output and reason. Nothing here invents a progress figure or a state.
import { useEffect, useState } from 'react';
import { useTerminal } from '../../terminal/TerminalContext';
import { decodeBase64 } from '../../terminal/client';

const _ANSI_RE = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;

export function stripAnsi(text) {
  return text.replace(_ANSI_RE, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

export function formatBytes(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} K`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} M`;
  return `${(bytes / 1024 ** 3).toFixed(2)} G`;
}

export function outcomeLabel(entry) {
  if (entry.state === 'running') return 'running';
  if (entry.signal) return entry.signal;
  if (entry.exit_code != null) return `exit ${entry.exit_code}`;
  return entry.state;
}

function useNow(enabled) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [enabled]);
  return now;
}

function ProcessRow({ entry, sessionName, now, onOpen, onSignal }) {
  const running = entry.state === 'running';
  const failed = entry.state === 'failed';
  const elapsed = running ? now - entry.started_at : (entry.ended_at ?? now) - entry.started_at;
  return (
    <tr
      className={`proc-row ${failed ? 'proc-failed' : ''} ${running ? 'proc-running' : ''}`}
      data-state={entry.state}
      tabIndex={0}
      onClick={() => onOpen(entry)}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(entry); }}
      aria-label={`${entry.title}, ${outcomeLabel(entry)}`}
    >
      <td><span className={`proc-state proc-state-${entry.state}`}>{outcomeLabel(entry)}</span></td>
      <td className="proc-title" title={entry.command || entry.title}>
        {entry.title}
        {entry.kind !== 'command' && <span className="proc-kind">{entry.kind}</span>}
        {failed && entry.failure_reason && <div className="proc-reason">{entry.failure_reason}</div>}
      </td>
      <td>{entry.pid ?? '—'}</td>
      <td>{sessionName ?? (entry.kind === 'app' ? 'trading API' : '—')}</td>
      <td className="proc-cwd" title={entry.cwd || ''}>{entry.cwd || '—'}</td>
      <td title={new Date(entry.started_at * 1000).toLocaleString()}>
        {new Date(entry.started_at * 1000).toLocaleTimeString()}
      </td>
      <td>{formatDuration(elapsed)}</td>
      <td>{running && entry.cpu_percent != null ? `${entry.cpu_percent.toFixed(1)}%` : '—'}</td>
      <td>{running ? formatBytes(entry.rss_bytes) : '—'}</td>
      <td className="proc-actions">
        {running && entry.pgid != null && (
          <>
            <button type="button" className="btn btn-blue" aria-label={`Interrupt ${entry.title}`}
              onClick={(e) => { e.stopPropagation(); onSignal(entry, 'INT'); }}>INT</button>
            <button type="button" className="btn btn-red" aria-label={`Terminate ${entry.title}`}
              onClick={(e) => { e.stopPropagation(); onSignal(entry, 'TERM'); }}>TERM</button>
            <button type="button" className="btn btn-red" aria-label={`Kill ${entry.title}`}
              onClick={(e) => {
                e.stopPropagation();
                if (window.confirm(`Send SIGKILL to process group ${entry.pgid} (${entry.title})?`)) {
                  onSignal(entry, 'KILL');
                }
              }}>KILL</button>
          </>
        )}
      </td>
    </tr>
  );
}

function ProcessTable({ entries, sessions, now, onOpen, onSignal, empty }) {
  if (entries.length === 0) return <p className="proc-empty">{empty}</p>;
  return (
    <table className="data-table proc-table">
      <thead>
        <tr>
          <th>State</th><th>Process</th><th>PID</th><th>Session</th><th>Directory</th>
          <th>Started</th><th>Time</th><th>CPU</th><th>Memory</th><th><span className="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <ProcessRow
            key={entry.id}
            entry={entry}
            sessionName={entry.session_id ? sessions[entry.session_id]?.name ?? entry.session_id : null}
            now={now}
            onOpen={onOpen}
            onSignal={onSignal}
          />
        ))}
      </tbody>
    </table>
  );
}

export function ProcessConsole({ processId, onBack }) {
  const { state, fetchProcessOutput } = useTerminal();
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const entry = state.active[processId] || state.history.find((p) => p.id === processId);
  // Re-read whenever the entry changes (new output for an app job, or the
  // moment it finishes).
  const revision = entry ? `${entry.state}:${entry.output_bytes}:${entry.ended_at}` : '';

  useEffect(() => {
    let cancelled = false;
    fetchProcessOutput(processId)
      .then((reply) => { if (!cancelled) { setDetail(reply.process); setError(null); } })
      .catch((e) => { if (!cancelled) setError(e.message || String(e)); });
    return () => { cancelled = true; };
  }, [processId, revision, fetchProcessOutput]);

  const shown = detail || entry;
  const output = detail?.output ? stripAnsi(new TextDecoder().decode(decodeBase64(detail.output))) : '';
  return (
    <div className="proc-console">
      <div className="proc-console-head">
        <button type="button" className="btn btn-blue" onClick={onBack}>← Back</button>
        {shown && (
          <>
            <span className={`proc-state proc-state-${shown.state}`}>{outcomeLabel(shown)}</span>
            <code className="proc-title">{shown.title}</code>
            {shown.failure_reason && <span className="proc-reason">{shown.failure_reason}</span>}
          </>
        )}
      </div>
      {error && <p className="dock-error" role="alert">{error}</p>}
      {detail?.output_truncated && <p className="proc-empty">Older output was not retained.</p>}
      <pre className="proc-output" aria-label="Process output">{output || (detail ? '(no output)' : 'Loading…')}</pre>
    </div>
  );
}

export function ProcessCenter() {
  const { state, setCenterOpen, openProcess, killProcess, showConsole } = useTerminal();
  const [error, setError] = useState(null);
  const active = Object.values(state.active).sort((a, b) => b.started_at - a.started_at);
  const now = useNow(active.length > 0);

  const onSignal = async (entry, signal) => {
    setError(null);
    try { await killProcess(entry.id, signal); } catch (e) { setError(e.message || String(e)); }
  };

  return (
    <aside className="process-center" aria-label="Processes">
      <div className="pc-head">
        <span className="panel-title">Processes</span>
        <span className="pc-counts">{active.length} running · {state.history.length} recent</span>
        <button type="button" className="dock-collapse" aria-label="Close processes" onClick={() => setCenterOpen(false)}>×</button>
      </div>
      {error && <p className="dock-error" role="alert">{error}</p>}
      {state.connection !== 'open' && (
        <p className="proc-empty" role="status">
          Terminal service {state.connection}; the list below is the last state it reported.
        </p>
      )}
      {state.consoleProcess ? (
        <ProcessConsole processId={state.consoleProcess} onBack={() => showConsole(null)} />
      ) : (
        <div className="pc-body">
          <h3 className="pc-section">Running</h3>
          <ProcessTable entries={active} sessions={state.sessions} now={now}
            onOpen={openProcess} onSignal={onSignal} empty="Nothing is running." />
          <h3 className="pc-section">Recent</h3>
          <ProcessTable entries={state.history} sessions={state.sessions} now={now}
            onOpen={openProcess} onSignal={onSignal} empty="Nothing has finished yet." />
        </div>
      )}
    </aside>
  );
}
