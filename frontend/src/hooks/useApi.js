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

/**
 * POST that surfaces failures without requiring the operator secret.
 *
 * useOperatorAction refuses to send anything until a secret is entered,
 * which is correct for the endpoints that verify one — but wrong for
 * endpoints gated only by API-key role, where it would demand a
 * credential the server never checks. Returns the parsed body, or null
 * after alerting.
 */
export async function postJson(path, body) {
  try {
    const res = await apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(`Request failed: ${err.detail || res.status}`);
      return null;
    }
    return await res.json();
  } catch (e) {
    alert(`Request failed: ${e.message || 'network error'}`);
    return null;
  }
}

export function useWebSocket(onTick, onEvent) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    function connect() {
      const wsUrl = API_KEY ? `${WS_URL}?api_key=${encodeURIComponent(API_KEY)}` : WS_URL;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
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
        setTimeout(connect, 3000);
      };
    }
    connect();
    return () => wsRef.current?.close();
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
