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

// Topic subscribers, keyed by the server's event topic. One WebSocket feeds
// every panel, so the fan-out has to live outside any single component.
//
// A plain Map rather than a store library: the plan called for Redux Toolkit
// on the grounds that it was already a dependency, and it is not -- it
// appears nowhere in package.json or src/. Adding it to hold fifteen keys
// would be a new runtime dependency for a feature this module already has.
const _topicListeners = new Map();

function _emit(topic, payload) {
  const listeners = _topicListeners.get(topic);
  if (!listeners) return;
  // Copied before iterating: a listener that unsubscribes itself while the
  // set is being walked would otherwise skip the listener after it.
  for (const listener of [...listeners]) {
    try { listener(payload); } catch (_) {}
  }
}

// Exported for the reconnect path: a socket that comes back has missed
// whatever happened while it was away, so panels re-fetch their cold-start
// state rather than resuming blind.
const _resyncListeners = new Set();

export function useWebSocket(onTick, onEvent) {
  const [connected, setConnected] = useState(false);
  const [lagMs, setLagMs] = useState(null);
  const wsRef = useRef(null);

  // Handlers go through refs so the effect does not list them as
  // dependencies. It did, and a caller passing an inline function would
  // therefore tear down and rebuild the WebSocket on every single render --
  // a reconnect storm triggered by unrelated state. The current caller
  // happens to wrap both in useCallback, so this was latent, but it made the
  // socket's lifetime depend on a detail of its caller.
  const onTickRef = useRef(onTick);
  const onEventRef = useRef(onEvent);
  onTickRef.current = onTick;
  onEventRef.current = onEvent;

  useEffect(() => {
    let attempt = 0;
    let retryTimer = null;
    let closed = false;
    // The socket *this* effect run owns. Cleanup used to close wsRef.current,
    // which is shared across runs -- under StrictMode's deliberate
    // mount/unmount/remount that is not necessarily the socket being torn
    // down, so the first run's reconnect loop could outlive its own cleanup
    // and race a second one. Closing what this run actually opened removes
    // the ambiguity rather than relying on the order of two effect runs.
    let socket = null;

    function connect() {
      if (closed) return;
      const wsUrl = API_KEY ? `${WS_URL}?api_key=${encodeURIComponent(API_KEY)}` : WS_URL;
      const ws = new WebSocket(wsUrl);
      socket = ws;
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        // Only a successful open resets the backoff. Resetting on the
        // attempt instead would turn a server that accepts and immediately
        // drops into a 250ms hot loop.
        const wasReconnect = attempt > 0;
        attempt = 0;
        if (wasReconnect) {
          for (const listener of [..._resyncListeners]) {
            try { listener(); } catch (_) {}
          }
        }
      };

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);

          // The producer stamped ts_ms, so this is genuine end-to-end lag
          // rather than the age of the send call. "Connected" without a lag
          // number hides exactly the failure this transport work is about.
          if (typeof msg.ts_ms === 'number') {
            setLagMs(Math.max(0, Date.now() - msg.ts_ms));
          }

          // Ticks and events are kept apart deliberately. Panels read fields
          // off the tick (equity_usd, positions); handing them an out-of-band
          // frame that has none would blank the dashboard on every control
          // change.
          if (msg.type === 'tick') {
            onTickRef.current?.(msg);
          } else if (msg.type === 'event' && msg.topic) {
            _emit(msg.topic, msg.data);
          } else {
            onEventRef.current?.(msg);
          }
        } catch (_) {}
      };

      ws.onerror = () => setConnected(false);

      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Exponential backoff with jitter, 250ms -> 10s. The flat 3s retry
        // this replaces put every dashboard in the building on the same
        // 3-second cadence, so a server coming back up was met by all of
        // them at once, in lockstep, forever.
        const base = Math.min(250 * 2 ** attempt, 10_000);
        attempt += 1;
        retryTimer = setTimeout(connect, base * (0.5 + Math.random() * 0.5));
      };
    }

    connect();
    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      // Drop the handlers before closing: close() fires onclose, and an
      // onclose that still points at this run's `connect` would schedule a
      // reconnect for a hook instance that no longer exists.
      if (socket) {
        socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
        socket.close();
      }
    };
  }, []);

  return { connected, lagMs };
}

/**
 * Cold-start once over HTTP, then stay current from the event stream.
 *
 * This is what replaces a polling timer: the fetch happens on mount (and
 * again after a reconnect, which is the one moment the stream is known to
 * have a hole in it) instead of every N seconds forever.
 */
export function useStream(topic, hydratePath, options = {}) {
  const { transform, apply, refetch = false } = options;
  const [data, setData] = useState(null);

  const transformRef = useRef(transform);
  const applyRef = useRef(apply);
  const refetchRef = useRef(refetch);
  transformRef.current = transform;
  applyRef.current = apply;
  refetchRef.current = refetch;

  useEffect(() => {
    let cancelled = false;

    async function hydrate() {
      if (!hydratePath) return;
      try {
        const res = await apiFetch(hydratePath);
        if (!res.ok || cancelled) return;
        const body = await res.json();
        if (cancelled) return;
        setData(transformRef.current ? transformRef.current(body) : body);
      } catch (_) {}
    }

    function onEventPayload(payload) {
      // Three shapes, because an event means different things to different
      // panels:
      //
      //   refetch  the event is only a trigger. A position close means a new
      //            trade row exists, but the event payload is not that row --
      //            re-reading is what keeps the table exactly as the server
      //            renders it, and is still event-driven rather than timed.
      //   apply    fold the event into what is already held; an approval
      //            describes one request, not the whole queue.
      //   default  replace, for a topic whose event *is* the whole state.
      if (refetchRef.current) {
        hydrate();
        return;
      }
      setData((prev) => (applyRef.current ? applyRef.current(prev, payload) : payload));
    }

    if (!_topicListeners.has(topic)) _topicListeners.set(topic, new Set());
    _topicListeners.get(topic).add(onEventPayload);
    _resyncListeners.add(hydrate);

    hydrate();

    return () => {
      cancelled = true;
      _topicListeners.get(topic)?.delete(onEventPayload);
      _resyncListeners.delete(hydrate);
    };
  }, [topic, hydratePath]);

  return data;
}

export function usePolling(path, interval, transform) {
  const [data, setData] = useState(null);

  // REG-0017. The effect's dependency array is [path, interval] but the body
  // closed over `transform`, so a caller passing an inline arrow -- which
  // App.jsx does at five call sites -- pinned the first render's function
  // forever. Adding `transform` to the deps is the obvious fix and the wrong
  // one: an inline arrow is a new identity every render, so the effect would
  // tear down and restart the interval continuously, and a 10s poll would
  // fire on every keystroke instead.
  //
  // A ref is the fix that addresses the actual defect: the latest function is
  // always called, and the timer's lifetime stays tied to what genuinely
  // defines it.
  const transformRef = useRef(transform);
  transformRef.current = transform;

  useEffect(() => {
    let inFlight = false;
    async function poll() {
      if (inFlight) return;
      inFlight = true;
      try {
        const res = await apiFetch(path);
        if (res.ok) {
          const body = await res.json();
          const fn = transformRef.current;
          setData(fn ? fn(body) : body);
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
