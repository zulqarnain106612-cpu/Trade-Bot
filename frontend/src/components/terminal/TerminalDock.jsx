// The terminal dock: session tabs above live terminals.
//
// Collapsing the dock hides it and nothing else -- every session keeps
// running, keeps producing output, and is still listed by
// `tradebot-term ls`. Only "close" (with a confirmation when something is
// still running in the foreground) terminates a session.
import { useEffect, useRef, useState } from 'react';
import { useTerminal } from '../../terminal/TerminalContext';
import { XtermView } from './XtermView';

const HEIGHT_KEY = 'tradebot.terminal.dockHeight';
const MIN_HEIGHT = 160;
const DEFAULT_HEIGHT = 320;

function readHeight() {
  try {
    const value = Number(localStorage.getItem(HEIGHT_KEY));
    if (Number.isFinite(value) && value >= MIN_HEIGHT) return value;
  } catch { /* storage unavailable */ }
  return DEFAULT_HEIGHT;
}

function maxHeight() {
  return Math.max(MIN_HEIGHT, Math.round((typeof window !== 'undefined' ? window.innerHeight : 800) * 0.8));
}

export function ConnectionNotice({ connection, onRetry, onToken, transportKind }) {
  const [token, setToken] = useState('');
  if (connection === 'unauthorized') {
    return (
      <form
        className="dock-notice"
        onSubmit={(e) => { e.preventDefault(); if (token.trim()) onToken(token.trim()); }}
      >
        <p>
          The terminal service needs its access token. Run{' '}
          <code>tradebot-term token</code> on this machine and paste it here.
          The desktop app connects without one.
        </p>
        <label className="sr-only" htmlFor="terminal-token">Terminal access token</label>
        <input
          id="terminal-token"
          className="input-sm"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="access token"
        />
        <button type="submit" className="btn btn-blue">Connect</button>
      </form>
    );
  }
  const messages = {
    idle: 'Starting…',
    connecting: 'Connecting to the terminal service…',
    offline: transportKind === 'ws'
      ? 'The terminal service is not reachable. Start it with `tradebot-term start`; retrying automatically.'
      : 'The terminal service is not reachable; retrying automatically.',
    incompatible: 'The running terminal service speaks a different protocol. Restart it with `tradebot-term restart`.',
    misconfigured: 'The terminal service URL is not a loopback address; the terminal is disabled.',
    closed: 'Disconnected.',
  };
  return (
    <div className="dock-notice" role="status">
      <p>{messages[connection] || connection}</p>
      {['offline', 'closed', 'incompatible'].includes(connection) && (
        <button type="button" className="btn btn-blue" onClick={onRetry}>Retry now</button>
      )}
    </div>
  );
}

function SessionTab({ session, selected, onSelect, onClose, onRename }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(session.name);
  const exited = session.state !== 'running';
  const failed = exited && (session.signal || (session.exit_code ?? 0) !== 0);

  const commit = () => {
    setEditing(false);
    const trimmed = name.trim();
    if (trimmed && trimmed !== session.name) onRename(trimmed);
    else setName(session.name);
  };

  return (
    <div
      role="tab"
      aria-selected={selected}
      tabIndex={selected ? 0 : -1}
      className={`dock-tab${selected ? ' active' : ''}${failed ? ' failed' : ''}`}
      onClick={onSelect}
      onDoubleClick={() => setEditing(true)}
      onKeyDown={(e) => {
        if (e.key === 'F2') setEditing(true);
        if (e.key === 'Enter' || e.key === ' ') onSelect();
      }}
      title={`${session.name} — pid ${session.pid}${session.cwd ? ` — ${session.cwd}` : ''}`}
    >
      <span className={`dock-dot ${exited ? (failed ? 'dot-red' : 'dot-muted') : 'dot-green'}`} aria-hidden="true" />
      {editing ? (
        <input
          className="dock-rename"
          aria-label="Session name"
          autoFocus
          value={name}
          maxLength={64}
          onChange={(e) => setName(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onBlur={commit}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Enter') commit();
            if (e.key === 'Escape') { setName(session.name); setEditing(false); }
          }}
        />
      ) : (
        <span className="dock-tab-name">{session.name}</span>
      )}
      {session.kind === 'job' && <span className="dock-kind">job</span>}
      {exited && (
        <span className="dock-kind">{session.signal || `exit ${session.exit_code ?? '?'}`}</span>
      )}
      <button
        type="button"
        className="dock-close"
        aria-label={`Close session ${session.name}`}
        onClick={(e) => { e.stopPropagation(); onClose(); }}
      >
        ×
      </button>
    </div>
  );
}

export function TerminalDock() {
  const {
    state, client, createSession, closeSession, renameSession, selectSession,
    signalSession, setDockOpen, retry, setToken,
  } = useTerminal();
  const [height, setHeight] = useState(readHeight);
  const [error, setError] = useState(null);
  const dragRef = useRef(null);

  useEffect(() => {
    try { localStorage.setItem(HEIGHT_KEY, String(height)); } catch { /* storage unavailable */ }
  }, [height]);

  const sessions = state.order.map((id) => state.sessions[id]).filter(Boolean);
  const selected = state.selected ? state.sessions[state.selected] : null;
  const open = state.connection === 'open';

  const run = async (fn) => {
    setError(null);
    try { await fn(); } catch (e) { setError(e.message || String(e)); }
  };

  const onClose = (session) => {
    if (session.state === 'running' && session.foreground) {
      const ok = window.confirm(
        `"${session.foreground.title || 'A process'}" is still running in ${session.name}. `
        + 'Close the session and terminate everything in it?',
      );
      if (!ok) return;
    }
    run(() => closeSession(session.id));
  };

  const startDrag = (e) => {
    e.preventDefault();
    dragRef.current = { startY: e.clientY, startH: height };
    const move = (ev) => {
      const d = dragRef.current;
      if (!d) return;
      setHeight(Math.min(maxHeight(), Math.max(MIN_HEIGHT, d.startH + (d.startY - ev.clientY))));
    };
    const up = () => {
      dragRef.current = null;
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  };

  return (
    <section id="terminal-dock" className="terminal-dock" style={{ height }} aria-label="Terminal">
      <div
        className="dock-resizer"
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize terminal"
        aria-valuemin={MIN_HEIGHT}
        aria-valuemax={maxHeight()}
        aria-valuenow={height}
        tabIndex={0}
        onMouseDown={startDrag}
        onKeyDown={(e) => {
          if (e.key === 'ArrowUp') setHeight((h) => Math.min(maxHeight(), h + 24));
          if (e.key === 'ArrowDown') setHeight((h) => Math.max(MIN_HEIGHT, h - 24));
        }}
      />
      <div className="dock-bar">
        <div className="dock-tabs" role="tablist" aria-label="Terminal sessions">
          {sessions.map((session) => (
            <SessionTab
              key={session.id}
              session={session}
              selected={session.id === state.selected}
              onSelect={() => selectSession(session.id)}
              onClose={() => onClose(session)}
              onRename={(name) => run(() => renameSession(session.id, name))}
            />
          ))}
          <button
            type="button"
            className="dock-new"
            aria-label="New terminal session"
            disabled={!open}
            onClick={() => run(() => createSession())}
          >
            +
          </button>
        </div>
        <div className="dock-actions">
          {error && <span className="dock-error" role="alert">{error}</span>}
          {selected && selected.state === 'running' && (
            <button
              type="button"
              className="btn btn-red"
              title="Send SIGINT to the foreground process group"
              onClick={() => run(() => signalSession(selected.id, 'INT'))}
            >
              Ctrl+C
            </button>
          )}
          <button
            type="button"
            className="dock-collapse"
            aria-label="Collapse terminal"
            onClick={() => setDockOpen(false)}
          >
            ▾
          </button>
        </div>
      </div>
      <div className="dock-body">
        {!open && (
          <ConnectionNotice
            connection={state.connection}
            onRetry={retry}
            onToken={setToken}
            transportKind={window.tradeBotDesktop?.terminal ? 'ipc' : 'ws'}
          />
        )}
        {open && sessions.length === 0 && (
          <div className="dock-notice">
            <p>No sessions yet.</p>
            <button type="button" className="btn btn-blue" onClick={() => run(() => createSession())}>
              New session
            </button>
          </div>
        )}
        {/* Views stay mounted across a dropped connection: the client
            re-attaches each one from the byte it had reached. */}
        {sessions.map((session) => (
          <XtermView
            key={session.id}
            sid={session.id}
            active={session.id === state.selected}
            client={client}
          />
        ))}
      </div>
    </section>
  );
}
