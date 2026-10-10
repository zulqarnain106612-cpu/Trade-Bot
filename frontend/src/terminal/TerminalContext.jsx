// Shared terminal + process-center state for the whole dashboard.
//
// The daemon is the authority; this holds a mirror of it. A snapshot
// replaces the mirror wholesale (initial connect and every reconnect), and
// events patch it in between. Nothing here guesses at state the daemon did
// not report: a process is "running" only while the daemon says so.
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef } from 'react';
import {
  TOKEN_STORAGE_KEY,
  TerminalClient,
  createIpcTransport,
  createWebSocketTransport,
  terminalWsUrl,
} from './client';

const TerminalContext = createContext(null);
export const HISTORY_LIMIT = 100;

export const FAILURES_SEEN_KEY = 'tradebot.processes.failuresSeenAt';

export function readFailuresSeenAt() {
  try {
    const value = Number(localStorage.getItem(FAILURES_SEEN_KEY));
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

export function readStoredToken() {
  try { return sessionStorage.getItem(TOKEN_STORAGE_KEY); } catch { return null; }
}

export function storeToken(token) {
  try { sessionStorage.setItem(TOKEN_STORAGE_KEY, token); } catch { /* private mode */ }
}

export function defaultClientFactory() {
  const bridge = typeof window !== 'undefined' ? window.tradeBotDesktop?.terminal : null;
  if (bridge) {
    return new TerminalClient({ transportFactory: () => createIpcTransport(bridge) });
  }
  const url = terminalWsUrl();
  return new TerminalClient({
    transportFactory: () => createWebSocketTransport(url),
    token: readStoredToken(),
  });
}

export const initialState = {
  connection: 'idle',
  welcome: null,
  sessions: {},
  order: [],
  active: {},
  history: [],
  selected: null,
  dockOpen: false,
  centerOpen: false,
  consoleProcess: null,
  failuresSeenAt: 0,
};

function upsertSession(state, session) {
  const exists = Boolean(state.sessions[session.id]);
  return {
    ...state,
    sessions: { ...state.sessions, [session.id]: session },
    order: exists ? state.order : [...state.order, session.id],
    selected: state.selected ?? session.id,
  };
}

export function reducer(state, action) {
  switch (action.type) {
    case 'connection':
      return { ...state, connection: action.state };
    case 'welcome':
      return { ...state, welcome: action.welcome };
    case 'snapshot': {
      const sessions = {};
      const order = [];
      for (const session of action.snapshot.sessions || []) {
        sessions[session.id] = session;
        order.push(session.id);
      }
      const active = {};
      for (const entry of action.snapshot.processes?.active || []) active[entry.id] = entry;
      const selected = state.selected && sessions[state.selected] ? state.selected : order[0] ?? null;
      return {
        ...state,
        sessions,
        order,
        active,
        history: (action.snapshot.processes?.history || []).slice(0, HISTORY_LIMIT),
        selected,
      };
    }
    case 'session':
      return upsertSession(state, action.session);
    case 'session_removed': {
      if (!state.sessions[action.id]) return state;
      const sessions = { ...state.sessions };
      delete sessions[action.id];
      const order = state.order.filter((id) => id !== action.id);
      const selected = state.selected === action.id ? order[order.length - 1] ?? null : state.selected;
      return { ...state, sessions, order, selected };
    }
    case 'process':
      return { ...state, active: { ...state.active, [action.process.id]: action.process } };
    case 'process_done': {
      const active = { ...state.active };
      delete active[action.process.id];
      const history = [action.process, ...state.history.filter((p) => p.id !== action.process.id)];
      return { ...state, active, history: history.slice(0, HISTORY_LIMIT) };
    }
    case 'select':
      return { ...state, selected: action.sid };
    case 'dock':
      return { ...state, dockOpen: action.open ?? !state.dockOpen };
    case 'center': {
      const centerOpen = action.open ?? !state.centerOpen;
      // Opening or closing the center both count as having seen the
      // failures it showed; failures while it is open are seen live.
      return {
        ...state,
        centerOpen,
        consoleProcess: centerOpen ? state.consoleProcess : null,
        failuresSeenAt: Date.now() / 1000,
      };
    }
    case 'console':
      return { ...state, consoleProcess: action.id, centerOpen: action.id ? true : state.centerOpen };
    default:
      return state;
  }
}

export function TerminalProvider({ children, clientFactory = defaultClientFactory }) {
  // The "failed" badge counts failures the operator has not looked at; when
  // they last looked survives a reload, so the badge does not re-announce
  // the same failures every time the dashboard restarts.
  const [state, dispatch] = useReducer(reducer, initialState, (base) => ({
    ...base,
    failuresSeenAt: readFailuresSeenAt(),
  }));
  useEffect(() => {
    try { localStorage.setItem(FAILURES_SEEN_KEY, String(state.failuresSeenAt)); } catch { /* storage unavailable */ }
  }, [state.failuresSeenAt]);
  const clientRef = useRef(null);
  if (clientRef.current === null) {
    try {
      clientRef.current = clientFactory();
    } catch (error) {
      clientRef.current = { error };
    }
  }
  const client = clientRef.current;

  useEffect(() => {
    if (client.error) {
      dispatch({ type: 'connection', state: 'misconfigured' });
      return undefined;
    }
    const offs = [
      client.on('state', (next) => dispatch({ type: 'connection', state: next })),
      client.on('welcome', (welcome) => dispatch({ type: 'welcome', welcome })),
      client.on('snapshot', (snapshot) => dispatch({ type: 'snapshot', snapshot })),
      client.on('session', (session) => dispatch({ type: 'session', session })),
      client.on('session_removed', (id) => dispatch({ type: 'session_removed', id })),
      client.on('process', (process) => dispatch({ type: 'process', process })),
      client.on('process_done', (process) => dispatch({ type: 'process_done', process })),
    ];
    client.connect();
    return () => {
      for (const off of offs) off();
      client.close();
    };
  }, [client]);

  const createSession = useCallback(async (fields = {}) => {
    const reply = await client.request('session.create', { rows: 24, cols: 80, ...fields });
    dispatch({ type: 'session', session: reply.session });
    dispatch({ type: 'select', sid: reply.session.id });
    dispatch({ type: 'dock', open: true });
    return reply.session;
  }, [client]);

  const actions = useMemo(() => ({
    client,
    createSession,
    closeSession: (sid) => client.request('session.close', { sid }),
    renameSession: (sid, name) => client.request('session.rename', { sid, name }),
    signalSession: (sid, signal) => client.request('session.signal', { sid, signal }),
    killProcess: (processId, signal) => client.request('process.kill', { process_id: processId, signal }),
    fetchProcessOutput: (processId) => client.request('process.output', { process_id: processId }),
    selectSession: (sid) => dispatch({ type: 'select', sid }),
    setDockOpen: (open) => dispatch({ type: 'dock', open }),
    toggleDock: () => dispatch({ type: 'dock' }),
    setCenterOpen: (open) => dispatch({ type: 'center', open }),
    toggleCenter: () => dispatch({ type: 'center' }),
    showConsole: (id) => dispatch({ type: 'console', id }),
    setToken: (token) => { storeToken(token); client.setToken(token); },
    retry: () => client.connect(),
  }), [client, createSession]);

  // Clicking a process: a live command or job opens its own terminal; an
  // application job, or anything that has finished, opens its console with
  // the output the daemon retained and the status it recorded.
  const openProcess = useCallback((entry) => {
    if (entry.state === 'running' && entry.session_id && state.sessions[entry.session_id]) {
      dispatch({ type: 'select', sid: entry.session_id });
      dispatch({ type: 'dock', open: true });
      return;
    }
    dispatch({ type: 'console', id: entry.id });
  }, [state.sessions]);

  const value = useMemo(() => ({ state, ...actions, openProcess }), [state, actions, openProcess]);
  return <TerminalContext.Provider value={value}>{children}</TerminalContext.Provider>;
}

export function useTerminal() {
  const value = useContext(TerminalContext);
  if (!value) throw new Error('useTerminal must be used inside <TerminalProvider>');
  return value;
}
