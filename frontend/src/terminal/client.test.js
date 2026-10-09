/**
 * TERM-004 / TERM-005 — the dashboard's terminal client.
 *
 * What these decide: a reconnect is a full resync (a session that vanished
 * while offline is forgotten, output resumes from the byte already shown and
 * is never rendered twice); a refused hello is final rather than retried in a
 * loop; the browser transport only ever talks to loopback and only sends a
 * token over the WebSocket, never over the Electron IPC relay.
 */
import { describe, expect, it, vi } from 'vitest';

import {
  PROTOCOL_VERSION,
  TerminalClient,
  createIpcTransport,
  createWebSocketTransport,
  decodeBase64,
  terminalWsUrl,
} from './client.js';

const b64 = (text) => btoa(text);
const flush = () => new Promise((resolve) => { queueMicrotask(resolve); });

/** A transport whose daemon side the test plays by hand. */
function fakeTransport(kind = 'ws') {
  const transport = {
    kind,
    sent: [],
    handlers: null,
    closed: false,
    open(handlers) { transport.handlers = handlers; },
    send(frame) { transport.sent.push(frame); },
    close() { transport.closed = true; },
    last(t) { return [...transport.sent].reverse().find((f) => f.t === t); },
  };
  return transport;
}

/** Timers the test fires explicitly; nothing waits on a clock. */
function manualTimers() {
  const timers = new Map();
  let next = 1;
  return {
    timers,
    setTimer: (fn, ms) => { const id = next; next += 1; timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => { timers.delete(id); },
    fire(id) { const t = timers.get(id); timers.delete(id); t.fn(); },
  };
}

function makeClient({ kind = 'ws', token = 'secret' } = {}) {
  const transports = [];
  const clock = manualTimers();
  const client = new TerminalClient({
    transportFactory: () => { const t = fakeTransport(kind); transports.push(t); return t; },
    token,
    random: () => 0.5,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  const current = () => transports[transports.length - 1];
  return { client, transports, clock, current };
}

/** Connect and complete the hello exchange with the given snapshot. */
async function connectOpen(ctx, snapshot = { sessions: [], processes: { active: [], history: [] } }) {
  ctx.client.connect();
  const transport = ctx.current();
  transport.handlers.onOpen();
  const hello = transport.last('hello');
  transport.handlers.onFrame({ t: 'snapshot', ...snapshot });
  transport.handlers.onFrame({ t: 'welcome', id: hello.id, protocol: PROTOCOL_VERSION });
  await flush();
  return transport;
}

describe('terminalWsUrl', () => {
  it.each(['ws://127.0.0.1:8766/', 'ws://localhost:9000', 'ws://[::1]:8766/'])('accepts loopback %s', (url) => {
    expect(terminalWsUrl(url)).toBe(url);
  });

  it.each(['ws://10.0.0.5:8766/', 'wss://127.0.0.1:8766/', 'ws://evil.example:8766/', 'ws://127.0.0.1.evil.example:1/'])(
    'refuses %s',
    (url) => {
      expect(() => terminalWsUrl(url)).toThrow(/loopback/);
    },
  );

  it('defaults to the daemon port on loopback', () => {
    expect(terminalWsUrl('')).toBe('ws://127.0.0.1:8766/');
  });
});

describe('decodeBase64', () => {
  it('decodes bytes, including ones that are not valid UTF-8 on their own', () => {
    expect([...decodeBase64(btoa('\x1b[31m'))]).toEqual([27, 91, 51, 49, 109]);
    expect([...decodeBase64(btoa('\xe2\x9c'))]).toEqual([0xe2, 0x9c]);
    expect(decodeBase64('').length).toBe(0);
    expect(decodeBase64(undefined).length).toBe(0);
  });
});

describe('hello', () => {
  it('sends the token only over the WebSocket transport', async () => {
    const ws = makeClient({ kind: 'ws', token: 'tok' });
    const wsTransport = await connectOpen(ws);
    expect(wsTransport.sent[0]).toMatchObject({ t: 'hello', protocol: PROTOCOL_VERSION, client: 'gui', token: 'tok' });
    expect(ws.client.state).toBe('open');

    const ipc = makeClient({ kind: 'ipc', token: 'tok' });
    const ipcTransport = await connectOpen(ipc);
    expect(ipcTransport.sent[0]).not.toHaveProperty('token');
  });

  it.each([
    ['unauthorized', 'unauthorized'],
    ['protocol_mismatch', 'incompatible'],
  ])('a hello refused as %s is final, not retried', async (code, state) => {
    const ctx = makeClient();
    ctx.client.connect();
    const transport = ctx.current();
    transport.handlers.onOpen();
    transport.handlers.onFrame({ t: 'error', id: transport.last('hello').id, code, message: 'no' });
    await flush();
    expect(ctx.client.state).toBe(state);
    transport.handlers.onClose('closed');
    expect(ctx.clock.timers.size).toBe(0);
    expect(ctx.transports).toHaveLength(1);
  });

  it('a new token reconnects', async () => {
    const ctx = makeClient({ token: null });
    await connectOpen(ctx);
    ctx.client.setToken('fresh');
    expect(ctx.transports[0].closed).toBe(true);
    ctx.current().handlers.onOpen();
    expect(ctx.current().last('hello').token).toBe('fresh');
  });
});

describe('reconnect', () => {
  it('backs off after a drop and resyncs from the new snapshot', async () => {
    const ctx = makeClient();
    const first = await connectOpen(ctx, { sessions: [{ id: 's-00000001' }, { id: 's-00000002' }] });
    ctx.client.attach('s-00000001');
    ctx.client.attach('s-00000002');
    first.handlers.onFrame({ t: 'output', sid: 's-00000001', offset: 0, data: b64('hello') });
    first.handlers.onFrame({ t: 'output', sid: 's-00000002', offset: 0, data: b64('xy') });

    const states = [];
    ctx.client.on('state', (s) => states.push(s));
    first.handlers.onClose('network');
    expect(ctx.client.state).toBe('offline');
    // The unanswered attaches were failed; only the retry timer is left.
    expect(ctx.clock.timers.size).toBe(1);
    const [[retryId, retry]] = [...ctx.clock.timers.entries()];
    expect(retry.ms).toBeGreaterThan(0);
    ctx.clock.fire(retryId);
    expect(states).toEqual(['offline', 'connecting']);

    // s-00000002 ended while offline: the new snapshot no longer lists it.
    const second = ctx.current();
    expect(second).not.toBe(first);
    second.handlers.onOpen();
    second.handlers.onFrame({ t: 'snapshot', sessions: [{ id: 's-00000001' }] });
    second.handlers.onFrame({ t: 'welcome', id: second.last('hello').id });
    await flush();
    expect(ctx.client.state).toBe('open');
    expect([...ctx.client.attached.keys()]).toEqual(['s-00000001']);
    // Re-attached from the byte already rendered, not from the start.
    expect(second.last('session.attach')).toMatchObject({ sid: 's-00000001', since: 5 });
  });

  it('a drop fails every pending request instead of leaving it hanging', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx);
    const pending = ctx.client.request('session.create', {});
    transport.handlers.onClose('gone');
    await expect(pending).rejects.toMatchObject({ code: 'disconnected' });
  });

  it('a request that is never answered times out', async () => {
    const ctx = makeClient();
    await connectOpen(ctx);
    const pending = ctx.client.request('session.create', {});
    const [timerId] = [...ctx.clock.timers.keys()];
    ctx.clock.fire(timerId);
    await expect(pending).rejects.toMatchObject({ code: 'timeout' });
  });

  it('frames from a replaced transport are ignored', async () => {
    const ctx = makeClient();
    const old = await connectOpen(ctx);
    ctx.client.connect();
    const seen = vi.fn();
    ctx.client.on('session', seen);
    old.handlers.onFrame({ t: 'session', session: { id: 's-00000001' } });
    old.handlers.onClose('late');
    expect(seen).not.toHaveBeenCalled();
    expect(ctx.client.state).toBe('connecting');
  });

  it('close stops retrying', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx);
    transport.handlers.onClose('network');
    expect(ctx.clock.timers.size).toBe(1);
    ctx.client.close();
    expect(ctx.clock.timers.size).toBe(0);
    expect(ctx.client.state).toBe('closed');
  });
});

describe('requests before the daemon accepted hello', () => {
  it('are refused locally rather than sent', async () => {
    const ctx = makeClient();
    ctx.client.connect();
    await expect(ctx.client.request('session.create')).rejects.toMatchObject({ code: 'disconnected' });
    ctx.client.send('session.input', { sid: 's-00000001', data: 'x' });
    expect(ctx.current().sent).toEqual([]);
  });

  it('are refused with no transport at all', async () => {
    const ctx = makeClient();
    await expect(ctx.client._request('ping', {})).rejects.toMatchObject({ code: 'disconnected' });
  });
});

describe('output', () => {
  it('drops bytes already rendered and keeps only the unseen tail', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx, { sessions: [{ id: 's-00000001' }] });
    ctx.client.attach('s-00000001');
    const chunks = [];
    ctx.client.on('output', (o) => chunks.push([o.offset, new TextDecoder().decode(o.bytes)]));
    const out = (offset, text) => transport.handlers.onFrame({ t: 'output', sid: 's-00000001', offset, data: b64(text) });
    out(0, 'abc');
    out(0, 'abc'); // a replay of what was shown
    out(2, 'cde'); // overlaps by one byte
    out(9, 'zz');
    // Output for a session this view is not attached to is not rendered.
    transport.handlers.onFrame({ t: 'output', sid: 's-00000009', offset: 0, data: b64('no') });
    expect(chunks).toEqual([[0, 'abc'], [3, 'de'], [9, 'zz']]);
  });

  it('reports a gap the daemon could not fill', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx, { sessions: [{ id: 's-00000001' }] });
    const gaps = [];
    ctx.client.on('gap', (g) => gaps.push(g));
    ctx.client.attach('s-00000001');
    const attach = transport.last('session.attach');
    transport.handlers.onFrame({ t: 'ok', id: attach.id, offset: 4096, truncated: true });
    await flush();
    await flush();
    expect(gaps).toEqual([{ sid: 's-00000001', from: null, offset: 4096 }]);
  });

  it('forgets a session the daemon no longer has', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx);
    const attaching = ctx.client.attach('s-0000dead');
    const attach = transport.last('session.attach');
    transport.handlers.onFrame({ t: 'error', id: attach.id, code: 'not_found', message: 'gone' });
    expect(await attaching).toBeNull();
    expect(ctx.client.attached.has('s-0000dead')).toBe(false);
  });

  it('detach stops output and tells the daemon', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx, { sessions: [{ id: 's-00000001' }] });
    ctx.client.attach('s-00000001');
    ctx.client.detach('s-00000001');
    expect(transport.last('session.detach')).toMatchObject({ sid: 's-00000001' });
    expect(ctx.client.attached.size).toBe(0);
  });
});

describe('events', () => {
  it('relays registry and session events and survives a throwing listener', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx, { sessions: [{ id: 's-00000001' }] });
    ctx.client.attach('s-00000001');
    const got = [];
    ctx.client.on('process', () => { throw new Error('listener bug'); });
    for (const type of ['session', 'session_removed', 'process', 'process_done', 'error']) {
      ctx.client.on(type, (payload) => got.push([type, payload]));
    }
    transport.handlers.onFrame({ t: 'session', session: { id: 's-00000001' } });
    transport.handlers.onFrame({ t: 'process', process: { id: 'p-00000001' } });
    transport.handlers.onFrame({ t: 'process_done', process: { id: 'p-00000001' } });
    transport.handlers.onFrame({ t: 'error', code: 'bad_frame' });
    transport.handlers.onFrame({ t: 'session_removed', id: 's-00000001' });
    transport.handlers.onFrame({ t: 'unknown_kind' });
    expect(got.map(([type]) => type)).toEqual(['session', 'process', 'process_done', 'error', 'session_removed']);
    expect(ctx.client.attached.has('s-00000001')).toBe(false);
    expect(ctx.client.lastError).toMatchObject({ code: 'bad_frame' });
  });

  it('unsubscribes', async () => {
    const ctx = makeClient();
    const transport = await connectOpen(ctx);
    const seen = vi.fn();
    const off = ctx.client.on('process', seen);
    off();
    transport.handlers.onFrame({ t: 'process', process: {} });
    expect(seen).not.toHaveBeenCalled();
  });
});

describe('createWebSocketTransport', () => {
  class FakeSocket {
    static last = null;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; this.closed = false; FakeSocket.last = this; }
    send(data) { this.sent.push(data); }
    close() { this.closed = true; }
  }

  it('parses frames, ignores garbage, and only sends on an open socket', () => {
    const transport = createWebSocketTransport('ws://127.0.0.1:1/', FakeSocket);
    const frames = [];
    const handlers = { onOpen: vi.fn(), onFrame: (f) => frames.push(f), onClose: vi.fn() };
    transport.open(handlers);
    const socket = FakeSocket.last;
    transport.send({ t: 'early' });
    expect(socket.sent).toEqual([]);
    socket.readyState = 1;
    socket.onopen();
    transport.send({ t: 'ping' });
    expect(socket.sent).toEqual(['{"t":"ping"}']);
    socket.onmessage({ data: '{"t":"pong"}' });
    socket.onmessage({ data: 'not json' });
    expect(frames).toEqual([{ t: 'pong' }]);
    socket.onerror();
    socket.onclose({ reason: '' });
    expect(handlers.onClose).toHaveBeenCalledWith('closed');
    transport.close();
    expect(socket.closed).toBe(true);
    expect(socket.onmessage).toBeNull();
    transport.close(); // idempotent
  });
});

describe('createIpcTransport', () => {
  function fakeBridge(result) {
    const listeners = { frame: null, status: null };
    return {
      listeners,
      sent: [],
      disconnected: 0,
      onFrame(cb) { listeners.frame = cb; return () => { listeners.frame = null; }; },
      onStatus(cb) { listeners.status = cb; return () => { throw new Error('already removed'); }; },
      connect: vi.fn(async () => {
        if (result instanceof Error) throw result;
        return result;
      }),
      send(frame) { this.sent.push(frame); },
      disconnect() { this.disconnected += 1; },
    };
  }

  it('opens through the bridge and relays frames and closes', async () => {
    const bridge = fakeBridge({ ok: true });
    const transport = createIpcTransport(bridge);
    const handlers = { onOpen: vi.fn(), onFrame: vi.fn(), onClose: vi.fn() };
    await transport.open(handlers);
    expect(handlers.onOpen).toHaveBeenCalledOnce();
    bridge.listeners.frame({ t: 'pong' });
    expect(handlers.onFrame).toHaveBeenCalledWith({ t: 'pong' });
    bridge.listeners.status({ state: 'open' });
    bridge.listeners.status({ state: 'closed', reason: 'daemon stopped' });
    expect(handlers.onClose).toHaveBeenCalledWith('daemon stopped');
    transport.send({ t: 'ping' });
    expect(bridge.sent).toEqual([{ t: 'ping' }]);
    transport.close();
    expect(bridge.listeners.frame).toBeNull();
    expect(bridge.disconnected).toBe(1);
  });

  it.each([
    [{ ok: false, error: 'service not running' }, 'service not running'],
    [new Error('ipc broke'), 'ipc broke'],
    [undefined, 'unavailable'],
  ])('reports a failed connect as a close (%#)', async (result, reason) => {
    const transport = createIpcTransport(fakeBridge(result));
    const handlers = { onOpen: vi.fn(), onFrame: vi.fn(), onClose: vi.fn() };
    await transport.open(handlers);
    expect(handlers.onOpen).not.toHaveBeenCalled();
    expect(handlers.onClose).toHaveBeenCalledWith(reason);
  });
});
