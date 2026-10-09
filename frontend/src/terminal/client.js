// Client for the terminal service (src/terminal on the host).
//
// One connection carries everything: session list, process registry events,
// and the output of every session this dashboard has attached to. Two
// transports reach the same daemon:
//
//   * Electron: the preload bridge relays frames through the main process to
//     the daemon's Unix socket. The renderer never holds a credential and
//     still has no Node access.
//   * Browser (Vite dev server): a loopback WebSocket that requires the
//     token printed by `tradebot-term token`, sent in the first frame.
//
// Reconnects are full resyncs, never patches: the daemon answers every hello
// with a complete snapshot, which replaces whatever was held -- so a process
// that finished while the socket was down cannot linger as "running". Output
// resumes from the byte offset already rendered, and a gap the daemon could
// not fill is reported rather than hidden.
import { reconnectDelay } from '../hooks/useApi';

export const PROTOCOL_VERSION = 1;
export const TOKEN_STORAGE_KEY = 'tradebot.terminal.token';
const DEFAULT_WS_URL = 'ws://127.0.0.1:8766/';
const _LOOPBACK_WS_RE = /^ws:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+\/?$/;

export function decodeBase64(data) {
  const binary = atob(data || '');
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function terminalWsUrl(raw = import.meta.env.VITE_TERMINAL_WS_URL) {
  const url = raw || DEFAULT_WS_URL;
  // The terminal runs commands as the logged-in user; it is only ever
  // reached on loopback, so a build pointed anywhere else is refused here
  // rather than sending a token across the network.
  if (!_LOOPBACK_WS_RE.test(url)) {
    throw new Error(`VITE_TERMINAL_WS_URL "${url}" must be a loopback ws:// URL.`);
  }
  return url;
}

export function createWebSocketTransport(url, WebSocketImpl = globalThis.WebSocket) {
  let socket = null;
  return {
    kind: 'ws',
    open(handlers) {
      socket = new WebSocketImpl(url);
      socket.onopen = () => handlers.onOpen();
      socket.onmessage = (event) => {
        let frame;
        try { frame = JSON.parse(event.data); } catch { return; }
        handlers.onFrame(frame);
      };
      socket.onerror = () => {};
      socket.onclose = (event) => handlers.onClose(event?.reason || 'closed');
    },
    send(frame) {
      if (socket && socket.readyState === 1) socket.send(JSON.stringify(frame));
    },
    close() {
      if (!socket) return;
      socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
      socket.close();
      socket = null;
    },
  };
}

export function createIpcTransport(bridge) {
  let unsubscribe = [];
  return {
    kind: 'ipc',
    async open(handlers) {
      unsubscribe = [
        bridge.onFrame((frame) => handlers.onFrame(frame)),
        bridge.onStatus((status) => {
          if (status?.state === 'closed') handlers.onClose(status.reason || 'closed');
        }),
      ];
      let result;
      try {
        result = await bridge.connect();
      } catch (error) {
        result = { ok: false, error: error?.message || 'unavailable' };
      }
      if (result?.ok) handlers.onOpen();
      else handlers.onClose(result?.error || 'unavailable');
    },
    send(frame) { bridge.send(frame); },
    close() {
      for (const off of unsubscribe) { try { off?.(); } catch { /* already gone */ } }
      unsubscribe = [];
      bridge.disconnect();
    },
  };
}

export class TerminalClient {
  constructor({
    transportFactory,
    token = null,
    random = Math.random,
    setTimer = (fn, ms) => setTimeout(fn, ms),
    clearTimer = (id) => clearTimeout(id),
    requestTimeoutMs = 15000,
  }) {
    this._transportFactory = transportFactory;
    this.token = token;
    this._random = random;
    this._setTimer = setTimer;
    this._clearTimer = clearTimer;
    this._requestTimeoutMs = requestTimeoutMs;
    this._listeners = new Map();
    this._pending = new Map();
    this._nextId = 1;
    this._attempt = 0;
    this._retryTimer = null;
    this._transport = null;
    this._closed = false;
    // sid -> next expected output offset (null = accept from the start).
    this.attached = new Map();
    this.state = 'idle';
    this.welcome = null;
    this.lastError = null;
  }

  on(type, callback) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(callback);
    return () => this._listeners.get(type)?.delete(callback);
  }

  _emit(type, payload) {
    for (const callback of [...(this._listeners.get(type) || [])]) {
      try { callback(payload); } catch { /* a broken listener must not stop the rest */ }
    }
  }

  _setState(state) {
    if (this.state === state) return;
    this.state = state;
    this._emit('state', state);
  }

  connect() {
    this._closed = false;
    if (this._retryTimer) { this._clearTimer(this._retryTimer); this._retryTimer = null; }
    if (this._transport) this._transport.close();
    this._setState('connecting');
    const transport = this._transportFactory();
    this._transport = transport;
    transport.open({
      onOpen: () => { if (this._transport === transport) this._hello(); },
      onFrame: (frame) => { if (this._transport === transport) this._onFrame(frame); },
      onClose: (reason) => { if (this._transport === transport) this._onClose(reason); },
    });
  }

  close() {
    this._closed = true;
    if (this._retryTimer) { this._clearTimer(this._retryTimer); this._retryTimer = null; }
    this._failPending('closed');
    if (this._transport) { this._transport.close(); this._transport = null; }
    this._setState('closed');
  }

  setToken(token) {
    this.token = token;
    this.connect();
  }

  async _hello() {
    try {
      const welcome = await this._request('hello', {
        protocol: PROTOCOL_VERSION,
        client: 'gui',
        ...(this._transport?.kind === 'ws' ? { token: this.token || '' } : {}),
      });
      this.welcome = welcome;
      this._attempt = 0;
      this._setState('open');
      this._emit('welcome', welcome);
      // Re-attach every session this dashboard was showing, from the byte it
      // had reached. A daemon that restarted has no such session; the
      // snapshot (already applied) has removed it, so it is skipped.
      for (const [sid, offset] of [...this.attached.entries()]) {
        this._attach(sid, offset);
      }
    } catch (error) {
      this.lastError = error;
      if (error.code === 'unauthorized') this._setState('unauthorized');
      else if (error.code === 'protocol_mismatch') this._setState('incompatible');
      // The daemon closes after a refused hello; _onClose does not retry
      // these two states, because retrying cannot change the answer.
    }
  }

  _onFrame(frame) {
    if (frame.id != null && this._pending.has(frame.id) && ['ok', 'welcome', 'error'].includes(frame.t)) {
      const { resolve, reject, timer } = this._pending.get(frame.id);
      this._pending.delete(frame.id);
      this._clearTimer(timer);
      if (frame.t === 'error') {
        const error = new Error(frame.message || frame.code);
        error.code = frame.code;
        reject(error);
      } else {
        resolve(frame);
      }
      return;
    }
    switch (frame.t) {
      case 'snapshot': {
        const live = new Set((frame.sessions || []).map((s) => s.id));
        for (const sid of [...this.attached.keys()]) if (!live.has(sid)) this.attached.delete(sid);
        this._emit('snapshot', frame);
        break;
      }
      case 'output': this._onOutput(frame); break;
      case 'session': this._emit('session', frame.session); break;
      case 'session_removed':
        this.attached.delete(frame.id);
        this._emit('session_removed', frame.id);
        break;
      case 'process': this._emit('process', frame.process); break;
      case 'process_done': this._emit('process_done', frame.process); break;
      case 'error': this.lastError = frame; this._emit('error', frame); break;
      default: break;
    }
  }

  _onOutput(frame) {
    if (!this.attached.has(frame.sid)) return;
    let bytes = decodeBase64(frame.data);
    let offset = frame.offset;
    const expected = this.attached.get(frame.sid);
    if (expected != null) {
      // Drop what was already rendered; keep only the unseen tail.
      if (offset + bytes.length <= expected) return;
      if (offset < expected) { bytes = bytes.subarray(expected - offset); offset = expected; }
    }
    this.attached.set(frame.sid, offset + bytes.length);
    this._emit('output', { sid: frame.sid, offset, bytes, replay: Boolean(frame.replay) });
  }

  _onClose(reason) {
    this._failPending(reason);
    this._transport = null;
    if (this._closed) return;
    if (this.state === 'unauthorized' || this.state === 'incompatible') return;
    this._setState('offline');
    const delay = reconnectDelay(this._attempt, this._random);
    this._attempt += 1;
    this._retryTimer = this._setTimer(() => { this._retryTimer = null; this.connect(); }, delay);
  }

  _failPending(reason) {
    for (const [, { reject, timer }] of this._pending) {
      this._clearTimer(timer);
      const error = new Error(`terminal connection ${reason}`);
      error.code = 'disconnected';
      reject(error);
    }
    this._pending.clear();
  }

  request(t, fields = {}) {
    // Anything but hello before the daemon has accepted hello would be
    // refused as unauthorized -- and an unauthorized frame closes the socket.
    if (this.state !== 'open') {
      const error = new Error('terminal service is not connected');
      error.code = 'disconnected';
      return Promise.reject(error);
    }
    return this._request(t, fields);
  }

  _request(t, fields) {
    if (!this._transport) {
      const error = new Error('terminal service is not connected');
      error.code = 'disconnected';
      return Promise.reject(error);
    }
    const id = this._nextId;
    this._nextId += 1;
    return new Promise((resolve, reject) => {
      const timer = this._setTimer(() => {
        this._pending.delete(id);
        const error = new Error(`${t} timed out`);
        error.code = 'timeout';
        reject(error);
      }, this._requestTimeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      this._transport.send({ t, id, ...fields });
    });
  }

  // Fire-and-forget: keystrokes and resizes would double the traffic if each
  // were acknowledged. Failures still arrive as id-less error frames.
  send(t, fields = {}) {
    if (this._transport && this.state === 'open') this._transport.send({ t, ...fields });
  }

  attach(sid) {
    // A fresh view wants everything still retained.
    this.attached.set(sid, null);
    return this._attach(sid, null);
  }

  async _attach(sid, since) {
    try {
      const reply = await this.request('session.attach', { sid, ...(since != null ? { since } : {}) });
      if (reply.truncated) this._emit('gap', { sid, from: since, offset: reply.offset });
      return reply;
    } catch (error) {
      if (error.code === 'not_found') this.attached.delete(sid);
      return null;
    }
  }

  detach(sid) {
    this.attached.delete(sid);
    if (this.state === 'open') this.request('session.detach', { sid }).catch(() => {});
  }
}
