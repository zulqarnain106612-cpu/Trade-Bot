/**
 * TERM-003, SEC-0010 — the Electron main-process bridge to the terminal service.
 *
 * The renderer keeps contextIsolation and the sandbox and never holds a
 * credential; this bridge is the only path from page content to a shell, so
 * what is decided here is what it refuses: any frame that is not a plain
 * object of an allowed request type, anything over the daemon's frame limit,
 * and job registration (job.*), which is for local programs only. It also
 * decides the lifecycle: one service start shared by concurrent connects, a
 * stale socket path retried, and a replaced connection closing silently.
 */
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { describe, expect, it, vi } from 'vitest';

const require = createRequire(import.meta.url);
const {
  ALLOWED_TYPES,
  MAX_FRAME_BYTES,
  TerminalBridge,
  ensureService,
  resolvePython,
  runTerminalCli,
  validateFrame,
} = require('../../electron/terminalBridge.cjs');

const SERVICE = { socket: '/run/user/1000/tradebot-terminal/terminal.sock', version: '1.0.0', client_version: '1.0.0' };

class FakeSocket extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.written = [];
    this.destroyed = false;
  }
  setEncoding(encoding) { this.encoding = encoding; }
  write(data) { this.written.push(data); }
  destroy() {
    this.destroyed = true;
    this.emit('close');
  }
}

function fakeNet() {
  return {
    sockets: [],
    createConnection(options) {
      const socket = new FakeSocket(options);
      this.sockets.push(socket);
      return socket;
    },
  };
}

function fakeExec(replies) {
  const calls = [];
  const execFile = (file, args, options, callback) => {
    calls.push({ file, args, options });
    const reply = replies.length > 1 ? replies.shift() : replies[0];
    callback(reply.error ?? null, reply.stdout ?? '', reply.stderr ?? '');
  };
  return { calls, execFile };
}

function fakeSender(id = 7) {
  return {
    id,
    sent: [],
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    send(channel, payload) { this.sent.push([channel, payload]); },
    once(event, callback) { this.onDestroyed = callback; },
  };
}

const started = { stdout: `starting\n${JSON.stringify(SERVICE)}\n` };

function makeBridge(replies = [started]) {
  const net = fakeNet();
  const exec = fakeExec(replies);
  const log = { warn: vi.fn() };
  const bridge = new TerminalBridge({
    ipcMain: null,
    repoRoot: '/repo',
    env: { TB_TERMINAL_PYTHON: '/opt/python' },
    netImpl: net,
    execFileImpl: exec.execFile,
    log,
  });
  return { bridge, net, exec, log };
}

const ticks = async (n = 25) => {
  for (let i = 0; i < n; i += 1) await null;
};

async function connected(ctx, sender) {
  const pending = ctx.bridge.connect(sender);
  await ticks();
  const socket = ctx.net.sockets[ctx.net.sockets.length - 1];
  socket.emit('connect');
  return { reply: await pending, socket };
}

describe('validateFrame', () => {
  it.each([...ALLOWED_TYPES])('relays a %s request', (t) => {
    expect(validateFrame({ t, id: 1 })).toBe(JSON.stringify({ t, id: 1 }));
  });

  it.each([
    ['job registration', { t: 'job.begin', name: 'x' }],
    ['job output', { t: 'job.output', process_id: 'p-00000001', data: '' }],
    ['an unknown type', { t: 'shell.exec' }],
    ['a missing type', { id: 1 }],
    ['a non-string type', { t: ['hello'] }],
    ['null', null],
    ['a string', 'hello'],
    ['an array', [{ t: 'hello' }]],
    ['a class instance', new (class Frame { constructor() { this.t = 'hello'; } })()],
    ['an oversized frame', { t: 'session.input', data: 'x'.repeat(MAX_FRAME_BYTES) }],
  ])('refuses %s', (_label, frame) => {
    expect(validateFrame(frame)).toBeNull();
  });

  it('refuses a frame that cannot be serialized', () => {
    const frame = { t: 'ping' };
    frame.self = frame;
    expect(validateFrame(frame)).toBeNull();
  });

  it('accepts a null-prototype object', () => {
    const frame = Object.assign(Object.create(null), { t: 'ping' });
    expect(validateFrame(frame)).toBe('{"t":"ping"}');
  });
});

describe('starting the service', () => {
  it('prefers TB_TERMINAL_PYTHON, then the project venv, then python3', () => {
    expect(resolvePython('/repo', { TB_TERMINAL_PYTHON: '/x/python' }, () => true)).toBe('/x/python');
    expect(resolvePython('/repo', {}, (p) => p === '/repo/.venv/bin/python')).toBe('/repo/.venv/bin/python');
    expect(resolvePython('/repo', {}, () => false)).toBe('python3');
  });

  it('runs the CLI from the repository with the repository importable', async () => {
    const exec = fakeExec([{ stdout: 'ok' }]);
    const result = await runTerminalCli(['status'], {
      repoRoot: '/repo', env: { TB_TERMINAL_PYTHON: '/opt/python', PYTHONPATH: '/lib' }, execFileImpl: exec.execFile,
    });
    expect(result).toEqual({ code: 0, stdout: 'ok', stderr: '' });
    const [call] = exec.calls;
    expect(call.file).toBe('/opt/python');
    expect(call.args).toEqual(['-m', 'src.terminal', 'status']);
    expect(call.options.cwd).toBe('/repo');
    expect(call.options.env.PYTHONPATH.split(':')).toEqual(['/repo', '/lib']);
  });

  it.each([
    [{ error: Object.assign(new Error('exit'), { code: 3 }), stderr: 'refused: running as root' }, 3, 'refused: running as root'],
    [{ error: Object.assign(new Error('spawn python ENOENT'), { code: 'ENOENT' }) }, 1, 'spawn python ENOENT'],
  ])('reports a failed run (%#)', async (reply, code, stderr) => {
    const exec = fakeExec([reply]);
    const result = await runTerminalCli(['start'], { repoRoot: '/repo', env: {}, execFileImpl: exec.execFile });
    expect(result).toMatchObject({ code, stderr });
  });

  it('reads the socket path from the last line of `start --json`', async () => {
    const exec = fakeExec([started]);
    const service = await ensureService({ repoRoot: '/repo', env: {}, execFileImpl: exec.execFile });
    expect(service).toEqual({ ok: true, socket: SERVICE.socket, version: '1.0.0', clientVersion: '1.0.0' });
    expect(exec.calls[0].args).toEqual(['-m', 'src.terminal', 'start', '--json']);
  });

  it.each([
    [{ error: Object.assign(new Error('x'), { code: 2 }), stderr: 'daemon did not become ready\n' }, 'daemon did not become ready'],
    [{ error: Object.assign(new Error('x'), { code: 2 }) }, 'x'],
    [{ stdout: 'not json' }, /unexpected reply/],
    [{ stdout: '{"version":"1.0.0"}' }, /no socket path/],
  ])('reports a service that did not start (%#)', async (reply, error) => {
    const exec = fakeExec([reply]);
    const service = await ensureService({ repoRoot: '/repo', env: {}, execFileImpl: exec.execFile });
    expect(service.ok).toBe(false);
    if (error instanceof RegExp) expect(service.error).toMatch(error);
    else expect(service.error).toBe(error);
  });
});

describe('TerminalBridge', () => {
  it('registers exactly the three IPC channels', () => {
    const ipcMain = { handled: {}, listened: {}, handle(c, f) { this.handled[c] = f; }, on(c, f) { this.listened[c] = f; } };
    const ctx = makeBridge();
    ctx.bridge.ipcMain = ipcMain;
    ctx.bridge.register();
    expect(Object.keys(ipcMain.handled)).toEqual(['terminal:connect']);
    expect(Object.keys(ipcMain.listened).sort()).toEqual(['terminal:disconnect', 'terminal:send']);

    const sender = fakeSender();
    ipcMain.listened['terminal:send']({ sender }, { t: 'job.begin', id: 4 });
    expect(sender.sent[0][1]).toMatchObject({ t: 'error', id: 4, code: 'refused_by_bridge' });
  });

  it('connects to the service socket and relays complete lines only', async () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    const { reply, socket } = await connected(ctx, sender);
    expect(reply).toEqual({ ok: true, version: '1.0.0' });
    expect(socket.options).toEqual({ path: SERVICE.socket });
    expect(socket.encoding).toBe('utf8');

    socket.emit('data', '{"t":"pong"}\n{"t":"ses');
    expect(sender.sent).toEqual([['terminal:frame', { t: 'pong' }]]);
    socket.emit('data', 'sion","session":{}}\n\nnot json\n');
    expect(sender.sent.map(([, frame]) => frame.t)).toEqual(['pong', 'session']);

    expect(ctx.bridge.send(sender, { t: 'session.input', sid: 's-00000001', data: 'ls\r' })).toBe(true);
    expect(socket.written).toEqual(['{"t":"session.input","sid":"s-00000001","data":"ls\\r"}\n']);
  });

  it('refuses frames it does not relay and says so to the page', async () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    const { socket } = await connected(ctx, sender);
    expect(ctx.bridge.send(sender, { t: 'job.begin', id: 9, name: 'x' })).toBe(false);
    expect(ctx.bridge.send(sender, 'hello')).toBe(false);
    expect(socket.written).toEqual([]);
    expect(sender.sent.map(([, f]) => [f.code, f.id])).toEqual([['refused_by_bridge', 9], ['refused_by_bridge', null]]);
  });

  it('a send before connecting is answered, not dropped', () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    expect(ctx.bridge.send(sender, { t: 'ping', id: 3 })).toBe(false);
    expect(sender.sent[0][1]).toMatchObject({ t: 'error', id: 3, code: 'not_connected' });
    sender.destroyed = true;
    ctx.bridge.send(sender, { t: 'ping', id: 4 });
    expect(sender.sent).toHaveLength(1);
  });

  it('starts the service once for concurrent connects and reuses it', async () => {
    const ctx = makeBridge();
    const one = fakeSender(1);
    const two = fakeSender(2);
    const first = ctx.bridge.connect(one);
    const second = ctx.bridge.connect(two);
    await ticks();
    for (const socket of ctx.net.sockets) socket.emit('connect');
    expect((await first).ok && (await second).ok).toBe(true);
    await connected(ctx, fakeSender(3));
    expect(ctx.exec.calls).toHaveLength(1);
  });

  it('a service that cannot start is reported, and the next connect tries again', async () => {
    const ctx = makeBridge([{ error: Object.assign(new Error('x'), { code: 1 }), stderr: 'refused' }, started]);
    const reply = await ctx.bridge.connect(fakeSender());
    expect(reply).toEqual({ ok: false, error: 'refused' });
    expect(ctx.log.warn).toHaveBeenCalledOnce();
    const { reply: again } = await connected(ctx, fakeSender());
    expect(again.ok).toBe(true);
    expect(ctx.exec.calls).toHaveLength(2);
  });

  it('a stale socket path is forgotten so the next connect asks the CLI again', async () => {
    const ctx = makeBridge();
    const pending = ctx.bridge.connect(fakeSender());
    await ticks();
    ctx.net.sockets[0].emit('error', new Error('connect ENOENT'));
    expect(await pending).toEqual({ ok: false, error: 'connect ENOENT' });
    expect(ctx.bridge.service).toBeNull();
    await connected(ctx, fakeSender());
    expect(ctx.exec.calls).toHaveLength(2);
  });

  it('a replaced connection closes silently; the current one reports its close', async () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    const { socket: first } = await connected(ctx, sender);
    const { socket: second } = await connected(ctx, sender);
    expect(first.destroyed).toBe(true);
    expect(sender.sent).toEqual([]);
    second.emit('close');
    expect(sender.sent).toEqual([['terminal:status', { state: 'closed', reason: 'service connection closed' }]]);
    expect(ctx.bridge.connections.size).toBe(0);
  });

  it('a closed window or an explicit disconnect drops its connection', async () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    const { socket } = await connected(ctx, sender);
    sender.onDestroyed();
    expect(socket.destroyed).toBe(true);
    expect(ctx.bridge.connections.size).toBe(0);

    const other = fakeSender(8);
    const { socket: kept } = await connected(ctx, other);
    ctx.bridge.disconnect(999);
    expect(kept.destroyed).toBe(false);
    ctx.bridge.closeAll();
    expect(kept.destroyed).toBe(true);
    expect(other.sent).toEqual([]);
  });

  it('frames for a window that is gone are dropped', async () => {
    const ctx = makeBridge();
    const sender = fakeSender();
    const { socket } = await connected(ctx, sender);
    sender.destroyed = true;
    socket.emit('data', '{"t":"pong"}\n');
    expect(sender.sent).toEqual([]);
  });
});
