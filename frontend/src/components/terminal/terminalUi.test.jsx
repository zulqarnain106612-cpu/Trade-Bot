/**
 * TERM-009 — the dashboard's terminal dock, status bar and
 * process center, driven by a fake terminal client.
 *
 * What these decide: the status bar shows live counts and only failures the
 * operator has not seen; collapsing the dock never closes a session; a click
 * on a running process opens its own terminal and a click on a finished one
 * opens its retained output; KILL needs a confirmation; a reconnect snapshot
 * replaces the mirror so nothing stale stays "running".
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FAILURES_SEEN_KEY,
  HISTORY_LIMIT,
  TerminalProvider,
  initialState,
  reducer,
  useTerminal,
} from '../../terminal/TerminalContext.jsx';
import { TOKEN_STORAGE_KEY } from '../../terminal/client.js';
import { ProcessCenter, formatBytes, formatDuration, outcomeLabel, stripAnsi } from './ProcessCenter.jsx';
import { StatusBar } from './StatusBar.jsx';
import { TerminalDock } from './TerminalDock.jsx';

const xterm = vi.hoisted(() => {
  const instances = [];
  class FakeTerminal {
    constructor(options) {
      this.options = options;
      this.written = [];
      this.disposed = false;
      this.focused = 0;
      this.dataHandlers = [];
      instances.push(this);
    }
    loadAddon() {}
    open(element) { this.element = element; }
    write(data) { this.written.push(data); }
    onData(callback) {
      this.dataHandlers.push(callback);
      return { dispose: () => { this.dataHandlers = []; } };
    }
    onResize() { return { dispose() {} }; }
    focus() { this.focused += 1; }
    dispose() { this.disposed = true; }
  }
  class FakeFitAddon { fit() {} }
  return { instances, FakeTerminal, FakeFitAddon };
});
vi.mock('@xterm/xterm', () => ({ Terminal: xterm.FakeTerminal }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: xterm.FakeFitAddon }));

const NOW_S = 2_000_000;

/** Plays the daemon: the test emits its events and scripts its replies. */
class FakeClient {
  constructor() {
    this.listeners = new Map();
    this.requests = [];
    this.sent = [];
    this.attached = [];
    this.detached = [];
    this.replies = {};
    this.connects = 0;
    this.closes = 0;
    this.token = null;
  }
  on(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
    return () => this.listeners.get(type).delete(callback);
  }
  emit(type, payload) {
    for (const callback of [...(this.listeners.get(type) || [])]) callback(payload);
  }
  connect() { this.connects += 1; }
  close() { this.closes += 1; }
  setToken(token) { this.token = token; }
  request(t, fields = {}) {
    this.requests.push({ t, ...fields });
    const reply = this.replies[t];
    if (reply instanceof Error) return Promise.reject(reply);
    return Promise.resolve(typeof reply === 'function' ? reply(fields) : reply ?? {});
  }
  send(t, fields = {}) { this.sent.push({ t, ...fields }); }
  attach(sid) { this.attached.push(sid); }
  detach(sid) { this.detached.push(sid); }
  requested(t) { return this.requests.filter((r) => r.t === t); }
}

let probe = null;
function Probe() {
  probe = useTerminal();
  return null;
}

function renderWith(client, children) {
  return render(
    <TerminalProvider clientFactory={() => client}>
      <Probe />
      {children}
    </TerminalProvider>,
  );
}

async function emit(client, type, payload) {
  await act(async () => { client.emit(type, payload); });
}

/** Let effects started by the last update fetch and render their result. */
const settle = () => act(async () => {});

async function openWith(client, snapshot) {
  await emit(client, 'state', 'open');
  await emit(client, 'snapshot', { sessions: [], processes: { active: [], history: [] }, ...snapshot });
}

const session = (id, extra = {}) => ({
  id, name: `shell ${id.slice(-1)}`, state: 'running', pid: 4000, cwd: '/repo', kind: 'shell',
  foreground: null, exit_code: null, signal: null, ...extra,
});

const running = (id, extra = {}) => ({
  id, kind: 'command', title: 'sleep 30', command: 'sleep 30', state: 'running', pid: 4242,
  pgid: 4242, session_id: 's-00000001', cwd: '/repo', started_at: NOW_S - 65,
  ended_at: null, cpu_percent: 1.5, rss_bytes: 3 * 1024 * 1024, exit_code: null, signal: null, ...extra,
});

const failed = (id, extra = {}) => ({
  ...running(id),
  title: 'make build', command: 'make build', state: 'failed', exit_code: 2,
  failure_reason: 'exited with status 2', ended_at: NOW_S - 5, ...extra,
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  xterm.instances.length = 0;
  probe = null;
  vi.spyOn(Date, 'now').mockReturnValue(NOW_S * 1000);
});

describe('reducer', () => {
  it('a snapshot replaces the mirror, so a process that ended offline is not "running"', () => {
    const stale = {
      ...initialState,
      sessions: { 's-00000001': session('s-00000001') },
      order: ['s-00000001'],
      active: { 'p-1': running('p-1') },
      selected: 's-00000001',
    };
    const next = reducer(stale, {
      type: 'snapshot',
      snapshot: { sessions: [session('s-00000002')], processes: { active: [], history: [failed('p-1')] } },
    });
    expect(next.active).toEqual({});
    expect(next.history.map((p) => p.id)).toEqual(['p-1']);
    expect(next.order).toEqual(['s-00000002']);
    expect(next.selected).toBe('s-00000002');
    expect(reducer(next, { type: 'snapshot', snapshot: {} }).selected).toBeNull();
  });

  it('a finished process leaves "running" for history, once, newest first, bounded', () => {
    let state = reducer(initialState, { type: 'process', process: running('p-1') });
    state = reducer(state, { type: 'process_done', process: failed('p-1') });
    state = reducer(state, { type: 'process_done', process: failed('p-1', { exit_code: 3 }) });
    expect(state.active).toEqual({});
    expect(state.history).toHaveLength(1);
    expect(state.history[0].exit_code).toBe(3);
    for (let i = 0; i < HISTORY_LIMIT + 5; i += 1) {
      state = reducer(state, { type: 'process_done', process: failed(`p-x${i}`) });
    }
    expect(state.history).toHaveLength(HISTORY_LIMIT);
    expect(state.history[0].id).toBe(`p-x${HISTORY_LIMIT + 4}`);
  });

  it('sessions keep their order and selection follows removal', () => {
    let state = reducer(initialState, { type: 'session', session: session('s-00000001') });
    state = reducer(state, { type: 'session', session: session('s-00000002') });
    state = reducer(state, { type: 'session', session: session('s-00000001', { name: 'renamed' }) });
    expect(state.order).toEqual(['s-00000001', 's-00000002']);
    expect(state.selected).toBe('s-00000001');
    expect(state.sessions['s-00000001'].name).toBe('renamed');
    state = reducer(state, { type: 'session_removed', id: 's-00000001' });
    expect(state.selected).toBe('s-00000002');
    expect(reducer(state, { type: 'session_removed', id: 's-0000dead' })).toBe(state);
    state = reducer(state, { type: 'session_removed', id: 's-00000002' });
    expect(state.selected).toBeNull();
  });

  it('opening a console opens the center; closing the center closes the console', () => {
    let state = reducer(initialState, { type: 'console', id: 'p-1' });
    expect(state).toMatchObject({ centerOpen: true, consoleProcess: 'p-1' });
    state = reducer(state, { type: 'center', open: false });
    expect(state).toMatchObject({ centerOpen: false, consoleProcess: null, failuresSeenAt: NOW_S });
    expect(reducer(state, { type: 'dock' }).dockOpen).toBe(true);
    expect(reducer(state, { type: 'unknown' })).toBe(state);
  });
});

describe('useTerminal', () => {
  it('refuses to work outside the provider', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Probe />)).toThrow(/TerminalProvider/);
  });

  it('a client that cannot be built disables the terminal instead of crashing', () => {
    render(
      <TerminalProvider clientFactory={() => { throw new Error('bad url'); }}>
        <StatusBar />
      </TerminalProvider>,
    );
    expect(screen.getByRole('status').textContent).toContain('terminal disabled');
  });

  it('connects on mount and closes on unmount', () => {
    const client = new FakeClient();
    const view = renderWith(client, null);
    expect(client.connects).toBe(1);
    view.unmount();
    expect(client.closes).toBe(1);
    expect([...client.listeners.values()].every((set) => set.size === 0)).toBe(true);
  });
});

describe('StatusBar', () => {
  it('counts sessions and running processes and shows the connection', async () => {
    const client = new FakeClient();
    renderWith(client, <StatusBar />);
    await openWith(client, {
      sessions: [session('s-00000001'), session('s-00000002')],
      processes: { active: [running('p-1'), running('p-2')], history: [] },
    });
    expect(screen.getByRole('button', { name: /^Terminal/ }).textContent).toContain('2');
    expect(screen.getByRole('button', { name: /^Processes/ }).textContent).toContain('2');
    expect(screen.getByRole('status').textContent).toContain('terminal service connected');
    await emit(client, 'state', 'offline');
    expect(screen.getByRole('status').textContent).toContain('terminal service offline');
  });

  it('shows only failures the operator has not seen, and remembers when they looked', async () => {
    const client = new FakeClient();
    const { container } = renderWith(client, <StatusBar />);
    await openWith(client, { processes: { active: [], history: [failed('p-1', { ended_at: 100 })] } });
    expect(container.querySelector('.sb-failed').textContent).toBe('1 failed');

    const processes = screen.getByRole('button', { name: /^Processes/ });
    fireEvent.click(processes);
    expect(container.querySelector('.sb-failed')).toBeNull();
    fireEvent.click(processes);
    expect(container.querySelector('.sb-failed')).toBeNull();
    expect(localStorage.getItem(FAILURES_SEEN_KEY)).toBe(String(NOW_S));

    await emit(client, 'process_done', failed('p-2', { ended_at: NOW_S + 5 }));
    expect(container.querySelector('.sb-failed').textContent).toBe('1 failed');
  });

  it('a reload does not re-announce failures already seen', async () => {
    localStorage.setItem(FAILURES_SEEN_KEY, '500');
    const client = new FakeClient();
    const { container } = renderWith(client, <StatusBar />);
    await openWith(client, {
      processes: { active: [], history: [failed('p-1', { ended_at: 400 }), failed('p-2', { ended_at: 600 })] },
    });
    expect(container.querySelector('.sb-failed').textContent).toBe('1 failed');
  });

  it('toggles the dock and reports venue clicks', async () => {
    const client = new FakeClient();
    const onVenueClick = vi.fn();
    const venues = { binance: { state: 'connected', account: { state: 'authenticated' } }, okx: { state: 'failed' } };
    renderWith(client, <StatusBar venues={venues} onVenueClick={onVenueClick} />);
    const terminal = screen.getByRole('button', { name: /^Terminal/ });
    fireEvent.click(terminal);
    expect(terminal.getAttribute('aria-pressed')).toBe('true');
    expect(probe.state.dockOpen).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /okx/ }));
    expect(onVenueClick).toHaveBeenCalledWith('okx');
    expect(screen.getByRole('button', { name: /binance/ }).textContent).toContain('account authenticated');
  });
});

describe('ProcessCenter', () => {
  async function renderCenter(snapshot, client = new FakeClient()) {
    const view = renderWith(client, <ProcessCenter />);
    await openWith(client, snapshot);
    return { client, ...view };
  }

  it('lists what runs and what finished, failures in red with their reason', async () => {
    const { container } = await renderCenter({
      sessions: [session('s-00000001')],
      processes: { active: [running('p-1')], history: [failed('p-2')] },
    });
    const rows = container.querySelectorAll('tr.proc-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].dataset.state).toBe('running');
    expect(rows[0].textContent).toContain('4242');
    expect(rows[0].textContent).toContain('shell 1');
    expect(rows[0].textContent).toContain('1:05');
    expect(rows[0].textContent).toContain('1.5%');
    expect(rows[0].textContent).toContain('3.0 M');
    expect(rows[1].className).toContain('proc-failed');
    expect(rows[1].textContent).toContain('exit 2');
    expect(rows[1].textContent).toContain('exited with status 2');
    expect(screen.getByText('1 running · 1 recent')).not.toBeNull();
  });

  it('says so when nothing runs and when the service is not connected', async () => {
    const { client } = await renderCenter({});
    expect(screen.getByText('Nothing is running.')).not.toBeNull();
    expect(screen.getByText('Nothing has finished yet.')).not.toBeNull();
    await emit(client, 'state', 'offline');
    expect(screen.getByRole('status').textContent).toContain('Terminal service offline');
  });

  it('signals only the process group, and KILL needs a confirmation', async () => {
    const { client } = await renderCenter({ processes: { active: [running('p-1')], history: [] } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Interrupt sleep 30' })); });
    expect(client.requested('process.kill')).toEqual([{ t: 'process.kill', process_id: 'p-1', signal: 'INT' }]);

    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Kill sleep 30' })); });
    expect(confirm.mock.calls[0][0]).toContain('process group 4242');
    expect(client.requested('process.kill')).toHaveLength(1);
    confirm.mockReturnValue(true);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Kill sleep 30' })); });
    expect(client.requested('process.kill')[1].signal).toBe('KILL');
    // The row's own click (open the session) is not triggered by the buttons.
    expect(probe.state.dockOpen).toBe(false);
  });

  it('shows a refused signal', async () => {
    const client = new FakeClient();
    client.replies['process.kill'] = new Error('process group 4242 belongs to another session');
    await renderCenter({ processes: { active: [running('p-1')], history: [] } }, client);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Terminate sleep 30' })); });
    expect(screen.getByRole('alert').textContent).toContain('belongs to another session');
  });

  it('a running command opens its own terminal session', async () => {
    const { container } = await renderCenter({
      sessions: [session('s-00000001'), session('s-00000002')],
      processes: { active: [running('p-1', { session_id: 's-00000002' })], history: [] },
    });
    fireEvent.click(container.querySelector('tr.proc-row'));
    expect(probe.state.selected).toBe('s-00000002');
    expect(probe.state.dockOpen).toBe(true);
    expect(probe.state.consoleProcess).toBeNull();
  });

  it('a failed process opens its retained output, without terminal escapes', async () => {
    const client = new FakeClient();
    const output = btoa('\x1b[31merror: missing target\x1b[0m\r\nmake: *** [all] Error 2\r\n');
    client.replies['process.output'] = { process: { ...failed('p-2'), output, output_truncated: true } };
    const { container } = await renderCenter({ processes: { active: [], history: [failed('p-2')] } }, client);
    await act(async () => { fireEvent.keyDown(container.querySelector('tr.proc-row'), { key: 'Enter' }); });
    await settle();
    const pre = screen.getByLabelText('Process output');
    expect(pre.textContent).toBe('error: missing target\nmake: *** [all] Error 2\n');
    expect(screen.getByText('Older output was not retained.')).not.toBeNull();
    expect(client.requested('process.output')).toEqual([{ t: 'process.output', process_id: 'p-2' }]);
    fireEvent.click(screen.getByRole('button', { name: '← Back' }));
    expect(probe.state.consoleProcess).toBeNull();
  });

  it('shows why output could not be read', async () => {
    const client = new FakeClient();
    client.replies['process.output'] = new Error('not_found');
    const { container } = await renderCenter({ processes: { active: [], history: [failed('p-2')] } }, client);
    await act(async () => { fireEvent.click(container.querySelector('tr.proc-row')); });
    await settle();
    expect(screen.getByRole('alert').textContent).toBe('not_found');
  });
});

describe('TerminalDock', () => {
  it('explains an unreachable service and retries on request', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    await emit(client, 'state', 'offline');
    expect(screen.getByRole('status').textContent).toContain('tradebot-term start');
    fireEvent.click(screen.getByRole('button', { name: 'Retry now' }));
    expect(client.connects).toBe(2);
    expect(screen.getByRole('button', { name: 'New terminal session' }).disabled).toBe(true);
  });

  it('asks for the token, keeps it for this browser session only, and reconnects', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    await emit(client, 'state', 'unauthorized');
    const input = screen.getByLabelText('Terminal access token');
    expect(input.getAttribute('type')).toBe('password');
    fireEvent.change(input, { target: { value: '  abc123  ' } });
    fireEvent.submit(input.closest('form'));
    expect(client.token).toBe('abc123');
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe('abc123');
    expect(localStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it('creates a session and wires a terminal view to it', async () => {
    const client = new FakeClient();
    client.replies['session.create'] = { session: session('s-00000001') };
    renderWith(client, <TerminalDock />);
    await openWith(client, {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'New session' })); });
    expect(client.requested('session.create')).toEqual([{ t: 'session.create', rows: 24, cols: 80 }]);
    expect(screen.getByRole('tab').textContent).toContain('shell 1');
    expect(client.attached).toEqual(['s-00000001']);

    const [term] = xterm.instances;
    term.dataHandlers[0]('ls\r');
    expect(client.sent).toEqual([{ t: 'session.input', sid: 's-00000001', data: 'ls\r' }]);
    client.emit('output', { sid: 's-00000001', bytes: 'one' });
    client.emit('output', { sid: 's-00000009', bytes: 'other' });
    expect(term.written).toEqual(['one']);
  });

  it('collapsing hides the dock without closing anything', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    await openWith(client, { sessions: [session('s-00000001')] });
    act(() => probe.setDockOpen(true));
    fireEvent.click(screen.getByRole('button', { name: 'Collapse terminal' }));
    expect(probe.state.dockOpen).toBe(false);
    expect(client.requested('session.close')).toEqual([]);
    expect(xterm.instances[0].disposed).toBe(false);
  });

  it('closing a session with a running command asks first', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    const busy = session('s-00000001', { foreground: { title: 'npm run build', pgid: 77 } });
    await openWith(client, { sessions: [busy] });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const close = screen.getByRole('button', { name: 'Close session shell 1' });
    await act(async () => { fireEvent.click(close); });
    expect(confirm.mock.calls[0][0]).toContain('npm run build');
    expect(client.requested('session.close')).toEqual([]);
    confirm.mockReturnValue(true);
    await act(async () => { fireEvent.click(close); });
    expect(client.requested('session.close')).toEqual([{ t: 'session.close', sid: 's-00000001' }]);
  });

  it('renames, interrupts and reports errors', async () => {
    const client = new FakeClient();
    client.replies['session.signal'] = new Error('nothing in the foreground');
    renderWith(client, <TerminalDock />);
    await openWith(client, { sessions: [session('s-00000001')] });
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' });
    const name = screen.getByLabelText('Session name');
    fireEvent.change(name, { target: { value: 'builds' } });
    await act(async () => { fireEvent.keyDown(name, { key: 'Enter' }); });
    expect(client.requested('session.rename')).toEqual([{ t: 'session.rename', sid: 's-00000001', name: 'builds' }]);

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Ctrl+C' })); });
    expect(client.requested('session.signal')).toEqual([{ t: 'session.signal', sid: 's-00000001', signal: 'INT' }]);
    expect(screen.getByRole('alert').textContent).toBe('nothing in the foreground');
  });

  it('marks an exited session with its status', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    await openWith(client, { sessions: [session('s-00000001', { state: 'exited', exit_code: 3 })] });
    const tab = screen.getByRole('tab');
    expect(tab.className).toContain('failed');
    expect(tab.textContent).toContain('exit 3');
    expect(screen.queryByRole('button', { name: 'Ctrl+C' })).toBeNull();
  });

  it('resizes from the keyboard within bounds', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    const resizer = screen.getByRole('separator');
    const before = Number(resizer.getAttribute('aria-valuenow'));
    fireEvent.keyDown(resizer, { key: 'ArrowUp' });
    expect(Number(resizer.getAttribute('aria-valuenow'))).toBe(before + 24);
    for (let i = 0; i < 40; i += 1) fireEvent.keyDown(resizer, { key: 'ArrowDown' });
    expect(Number(resizer.getAttribute('aria-valuenow'))).toBe(160);
  });

  it('a view that goes away detaches and frees its terminal', async () => {
    const client = new FakeClient();
    renderWith(client, <TerminalDock />);
    await openWith(client, { sessions: [session('s-00000001')] });
    await emit(client, 'session_removed', 's-00000001');
    expect(client.detached).toEqual(['s-00000001']);
    expect(xterm.instances[0].disposed).toBe(true);
  });
});

describe('formatting', () => {
  it.each([
    [null, '—'], [Number.NaN, '—'], [-3, '0:00'], [65, '1:05'], [3725, '1:02:05'],
  ])('formatDuration(%s) is %s', (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });

  it.each([
    [null, '—'], [512, '512 B'], [2048, '2.0 K'], [3 * 1024 ** 2, '3.0 M'], [5 * 1024 ** 3, '5.00 G'],
  ])('formatBytes(%s) is %s', (bytes, text) => {
    expect(formatBytes(bytes)).toBe(text);
  });

  it('labels outcomes from what was reported, never guessed', () => {
    expect(outcomeLabel({ state: 'running' })).toBe('running');
    expect(outcomeLabel({ state: 'failed', signal: 'SIGTERM', exit_code: 143 })).toBe('SIGTERM');
    expect(outcomeLabel({ state: 'succeeded', exit_code: 0 })).toBe('exit 0');
    expect(outcomeLabel({ state: 'unknown', exit_code: null })).toBe('unknown');
  });

  it('strips colour, cursor and title sequences', () => {
    expect(stripAnsi('\x1b[1;32mok\x1b[0m\x1b]0;title\x07 done\r\n')).toBe('ok done\n');
  });
});
