import { useState, useEffect, useRef, useCallback } from 'react';

const _RAW_API = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const API_KEY = import.meta.env.VITE_API_KEY || '';

const _ALLOWED_API_RE = /^https?:\/\/[a-zA-Z0-9._-]+(:\d+)?$/;
if (!_ALLOWED_API_RE.test(_RAW_API)) {
  throw new Error(`VITE_API_URL "${_RAW_API}" is not an allowed origin.`);
}
const API = _RAW_API.replace(/\/$/, '');
const WS_URL = API.replace(/^http/, 'ws') + '/ws';

export function apiFetch(path, opts = {}) {
  if (typeof path !== 'string' || !path.startsWith('/')) {
    throw new Error(`apiFetch: path must start with "/", got: ${path}`);
  }
  return fetch(`${API}${path}`, {
    ...opts,
    headers: {
      ...(opts.headers || {}),
      'x-api-key': API_KEY,
    },
  });
}

// Reconnect delay: exponential with full jitter, capped.
//
// The flat 3s this replaces made every open dashboard retry in lockstep,
// so an API that had just come back up met the whole fleet at once, every
// three seconds, for as long as it stayed unhealthy. Full jitter --
// a uniform draw from [0, backoff] rather than backoff +/- a wobble -- is
// what actually spreads a fleet; a small wobble around a common centre
// still arrives together.
export const RECONNECT_BASE_MS = 500;
export const RECONNECT_CAP_MS = 30000;

export function reconnectDelay(attempt, random = Math.random) {
  const backoff = Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return Math.floor(random() * backoff);
}

export function useWebSocket(onTick, onEvent) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    // The effect's own cleanup closes the socket, which fires `onclose`,
    // which used to schedule another connect -- so every unmount left a
    // reconnect loop running against a component that no longer existed,
    // and React 18 StrictMode starts one on the first mount in dev. The
    // flag is what tells an intentional close from a dropped connection.
    let disposed = false;
    let retryTimer = null;
    let attempt = 0;

    function connect() {
      if (disposed) return;
      const wsUrl = API_KEY ? `${WS_URL}?api_key=${encodeURIComponent(API_KEY)}` : WS_URL;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => {
        attempt = 0;  // a connection that opened is not a failed one
        setConnected(true);
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          // Ticks and events are kept apart deliberately. Panels read fields
          // off the tick (equity_usd, positions); handing them an out-of-band
          // frame that has none would blank the dashboard on every control
          // change.
          if (msg.type === 'tick') onTick(msg);
          else if (onEvent) onEvent(msg);
        } catch (_) {}
      };
      ws.onerror = () => setConnected(false);
      ws.onclose = () => {
        setConnected(false);
        if (disposed) return;
        retryTimer = setTimeout(connect, reconnectDelay(attempt));
        attempt += 1;
      };
    }
    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, [onTick, onEvent]);

  return connected;
}

export function usePolling(path, interval, transform) {
  const [data, setData] = useState(null);

  useEffect(() => {
    let inFlight = false;
    async function poll() {
      if (inFlight) return;
      inFlight = true;
      try {
        const res = await apiFetch(path);
        if (res.ok) {
          const body = await res.json();
          setData(transform ? transform(body) : body);
        }
      } catch (_) {}
      finally { inFlight = false; }
    }
    poll();
    const id = setInterval(poll, interval);
    return () => clearInterval(id);
  }, [path, interval]);

  return data;
}

export function useOperatorAction() {
  const [operatorId, setOperatorId] = useState('operator');
  const [operatorSecret, setOperatorSecret] = useState('');

  const action = useCallback(async (path, method, body) => {
    if (!operatorSecret) {
      alert('Enter the operator secret first.');
      return null;
    }
    try {
      const res = await apiFetch(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, operator: operatorId, operator_secret: operatorSecret }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`Action failed: ${err.detail || res.status}`);
        return null;
      }
      return await res.json();
    } catch (e) {
      alert(`Action failed: ${e.message || 'network error'}`);
      return null;
    }
  }, [operatorId, operatorSecret]);

  return { operatorId, setOperatorId, operatorSecret, setOperatorSecret, action };
}
