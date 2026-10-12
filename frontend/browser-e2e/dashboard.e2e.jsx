import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import '../src/index.css';
import App from '../src/App.jsx';
import { WORKSPACE_STORAGE_KEY } from '../src/workspace/layoutStore.js';

vi.mock('../src/hooks/useApi.js', () => {
  const status = {
    execution_mode: 'paper',
    starting_capital_usd: 10_000,
    equity_usd: 10_125,
    daily_pnl_usd: 125,
    regime: 0,
    prediction: { direction: 'long', confidence: 0.74 },
    positions: [{
      trade_id: 'position-1',
      symbol: 'BTC/USDT',
      timeframe: '1m',
      direction: 'long',
      entry_price: 100,
      current_price: 101,
      quantity: 1,
      notional_usd: 101,
      unrealized_pnl: 1,
      unrealized_pnl_pct: 1,
      regime_at_entry: 0,
    }],
  };
  const readiness = {
    ready: false,
    phase: 'warming_up',
    trading_mode: 'PAPER',
    reasons: ['Waiting for the first 1m market tick'],
    warnings: [],
  };
  const venueBody = {
    venues: {
      OKX: {
        state: 'connected',
        account: { state: 'unconfigured' },
        credentials: 'missing',
      },
    },
  };
  const apiFetch = async (path) => ({
    ok: true,
    status: 200,
    json: async () => {
      if (path === '/venues') return venueBody;
      if (path === '/controls') return { controls: [], counts: { live: 0, protected: 0 } };
      return {};
    },
  });
  const emptyAction = async () => ({ ok: true });
  const usePolling = (path) => {
    if (path === '/status') return status;
    if (path.startsWith('/equity')) return [];
    if (path.startsWith('/trades')) return [];
    if (path.startsWith('/missed-trades')) return [];
    if (path === '/risk-controls') return null;
    if (path === '/models/status') {
      return { timeframes: { '1m': { state: 'ready', status: 'ready' } } };
    }
    return null;
  };
  return {
    API_ORIGIN: 'http://127.0.0.1:8000',
    apiFetch,
    apiProblem: (code) => ({ status: code, message: `API error ${code}` }),
    postJson: emptyAction,
    reconnectDelay: () => 500,
    useWebSocket: () => ({ connected: true, lagMs: 35 }),
    useStream: () => [],
    usePolling,
    useApiResource: (path) => {
      if (path === '/runtime/components') return { data: [], problem: null };
      if (path === '/runtime') {
        return {
          data: { components: 0, by_state: {}, by_type: {}, open_changes: [], dependency_issues: [] },
          problem: null,
        };
      }
      return { data: {}, problem: null };
    },
    useReadiness: () => ({ report: readiness, problem: null }),
    useOperatorAction: () => ({
      operatorId: 'operator',
      setOperatorId: () => {},
      operatorSecret: '',
      setOperatorSecret: () => {},
      action: emptyAction,
    }),
  };
});

vi.mock('../src/terminal/TerminalContext.jsx', async () => {
  const ReactModule = await import('react');
  const Context = ReactModule.createContext(null);
  const now = Date.now() / 1000;
  const activeProcess = {
    id: 'process-running-1',
    title: 'horizon-worker',
    command: 'python -m src.workers.orchestrator',
    state: 'running',
    kind: 'command',
    pid: 1234,
    pgid: 1234,
    started_at: now - 30,
    cpu_percent: 3.2,
    rss_bytes: 1024 * 1024,
    cwd: '/tmp/trade-bot',
    session_id: null,
    output_bytes: 0,
  };
  const failedProcess = {
    id: 'process-failed-1',
    title: 'completed-backfill',
    command: 'python -m src.backfill',
    state: 'failed',
    kind: 'job',
    pid: 1235,
    started_at: now - 60,
    ended_at: now - 10,
    exit_code: 1,
    failure_reason: 'fixture failure',
    cwd: '/tmp/trade-bot',
    output_bytes: 12,
  };
  function TerminalProvider({ children }) {
    const [state, setState] = ReactModule.useState({
      connection: 'open',
      welcome: { protocol: 1 },
      sessions: {},
      order: [],
      active: { [activeProcess.id]: activeProcess },
      history: [failedProcess],
      selected: null,
      dockOpen: false,
      centerOpen: false,
      consoleProcess: null,
      failuresSeenAt: 0,
    });
    const value = {
      state,
      client: { request: async () => ({}) },
      createSession: async () => ({ id: 'session-1', name: 'Test terminal', state: 'running' }),
      closeSession: async () => {},
      renameSession: async () => {},
      selectSession: () => {},
      signalSession: async () => {},
      setDockOpen: (open) => setState((current) => ({ ...current, dockOpen: open })),
      retry: () => {},
      setToken: () => {},
      toggleDock: () => setState((current) => ({ ...current, dockOpen: !current.dockOpen })),
      toggleCenter: () => setState((current) => ({
        ...current,
        centerOpen: !current.centerOpen,
        failuresSeenAt: Date.now() / 1000,
      })),
      setCenterOpen: (open) => setState((current) => ({ ...current, centerOpen: open })),
      openProcess: (entry) => setState((current) => ({
        ...current,
        consoleProcess: entry.id,
        centerOpen: true,
      })),
      killProcess: async () => {},
      showConsole: (id) => setState((current) => ({ ...current, consoleProcess: id })),
      fetchProcessOutput: async () => ({
        process: { ...failedProcess, output: btoa('completed with fixture failure\\n') },
      }),
    };
    return ReactModule.createElement(Context.Provider, { value }, children);
  }
  function useTerminal() {
    const value = ReactModule.useContext(Context);
    if (!value) throw new Error('useTerminal must be used inside <TerminalProvider>');
    return value;
  }
  return { TerminalProvider, useTerminal };
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe('dashboard in a real Chromium browser', () => {
  it('shows readiness state and populated operational panels', async () => {
    render(<App />);

    expect(screen.getByText('Trade-Bot')).toBeTruthy();
    expect(screen.getByText('API up, models warming up: the trading loops start when they are built')).toBeTruthy();
    expect(screen.getByText('Waiting for the first 1m market tick')).toBeTruthy();
    expect(screen.getByText('BTC/USDT')).toBeTruthy();

    const viewport = screen.getByRole('main', { name: 'Dashboard workspace' });
    expect(viewport).toBeTruthy();
    await waitFor(() => expect(document.documentElement.scrollHeight).toBeGreaterThan(window.innerHeight));
    window.scrollTo(0, document.documentElement.scrollHeight);
    fireEvent.scroll(window);
    expect(window.scrollY).toBeGreaterThan(0);
  });

  it('opens the process center and terminal dock with populated process state', () => {
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: /Processes/ }));
    expect(screen.getByRole('complementary', { name: 'Processes' })).toBeTruthy();
    expect(screen.getByRole('row', { name: /horizon-worker, running/ })).toBeTruthy();
    expect(screen.getByText('completed-backfill')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /^Terminal/ }));
    expect(screen.getByRole('region', { name: 'Terminal' })).toBeTruthy();
    expect(screen.getByText('No sessions yet.')).toBeTruthy();
  });

  it('drags a real dashboard panel and persists the new grid placement', async () => {
    render(<App />);

    const item = document.querySelector('[data-ws-id="positions"]');
    const handle = item?.querySelector('.ws-drag-handle');
    expect(handle).toBeTruthy();
    expect(item).toBeTruthy();
    await waitFor(() => expect(localStorage.getItem(WORKSPACE_STORAGE_KEY)).toBeTruthy());

    const before = JSON.parse(localStorage.getItem(WORKSPACE_STORAGE_KEY)).items.positions;
    const rect = handle.getBoundingClientRect();
    const startX = rect.left + Math.min(24, rect.width / 2);
    const startY = rect.top + Math.min(12, rect.height / 2);
    fireEvent.mouseDown(handle, { button: 0, clientX: startX, clientY: startY });
    for (let step = 1; step <= 4; step += 1) {
      fireEvent.mouseMove(document, {
        clientX: startX + step * 35,
        clientY: startY + step * 8,
      });
    }
    fireEvent.mouseUp(document, { clientX: startX + 140, clientY: startY + 32 });

    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem(WORKSPACE_STORAGE_KEY));
      expect(saved.items.positions).not.toEqual(before);
    });
  });
});
