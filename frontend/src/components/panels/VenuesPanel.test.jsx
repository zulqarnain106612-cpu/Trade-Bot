/**
 * VEN-001 — the Binance / OKX connection controls.
 *
 * What these decide: market data and account access are shown as two
 * separate states; credentials appear only as configured / incomplete /
 * missing; every button goes through the operator action (the second factor)
 * to exactly one venue's endpoint; Disconnect asks first; and the status is
 * re-read after every action rather than assumed. No test here needs an
 * exchange or a credential.
 */
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { VENUE_POLL_MS, VenuesPanel, useVenues } from './VenuesPanel.jsx';

const api = vi.hoisted(() => ({ fetch: null }));
vi.mock('../../hooks/useApi', async (importOriginal) => ({
  ...(await importOriginal()),
  apiFetch: (...args) => api.fetch(...args),
}));

const VENUES = {
  binance: {
    state: 'connected',
    available: true,
    testnet: true,
    credentials: 'configured',
    error: null,
    account: { state: 'authenticated', error: null, checked_at: 1 },
  },
  okx: {
    state: 'failed',
    available: false,
    testnet: false,
    credentials: 'incomplete',
    error: 'NetworkError: timed out',
    account: { state: 'unavailable', error: null, checked_at: null },
  },
};

function renderPanel(props = {}) {
  const operatorAction = vi.fn(async () => ({ ok: true }));
  const reload = vi.fn();
  const view = render(
    <VenuesPanel venues={VENUES} error={null} reload={reload} operatorAction={operatorAction} {...props} />,
  );
  return { operatorAction, reload, ...view };
}

const card = (container, name) => container.querySelector(`[data-venue="${name}"]`);

describe('VenuesPanel', () => {
  it('shows market data and account access separately, and never a key', () => {
    const { container } = renderPanel();
    const binance = card(container, 'binance');
    expect(binance.dataset.state).toBe('connected');
    expect(binance.textContent).toContain('Connected');
    expect(binance.textContent).toContain('Authenticated');
    expect(binance.textContent).toContain('testnet');
    expect(binance.textContent).toContain('configured');

    const okx = card(container, 'okx');
    expect(okx.textContent).toContain('Failed');
    expect(okx.textContent).toContain('Unavailable');
    expect(okx.textContent).toContain('incomplete');
    expect(okx.textContent).toContain('NetworkError: timed out');
    expect(okx.textContent).not.toContain('testnet');
  });

  it('offers the actions that fit each state', () => {
    const { container } = renderPanel({
      venues: { ...VENUES, okx: { ...VENUES.okx, state: 'disconnected', error: null } },
    });
    const buttons = (name) => [...card(container, name).querySelectorAll('button')].map((b) => b.textContent);
    expect(buttons('binance')).toEqual(['Reconnect', 'Verify account', 'Disconnect']);
    expect(buttons('okx')).toEqual(['Connect']);
  });

  it('offers Retry after a failure and no Verify without complete credentials', () => {
    const { container } = renderPanel({
      venues: { ...VENUES, binance: { ...VENUES.binance, credentials: 'missing' } },
    });
    const buttons = (name) => [...card(container, name).querySelectorAll('button')].map((b) => b.textContent);
    expect(buttons('okx')).toEqual(['Retry']);
    expect(buttons('binance')).toEqual(['Reconnect', 'Disconnect']);
  });

  it.each([
    ['binance', 'Reconnect', '/venues/binance/reconnect'],
    ['binance', 'Verify account', '/venues/binance/verify'],
    ['okx', 'Retry', '/venues/okx/connect'],
  ])('%s %s goes through the operator action to one venue', async (name, label, path) => {
    const { container, operatorAction, reload } = renderPanel();
    const button = [...card(container, name).querySelectorAll('button')].find((b) => b.textContent === label);
    await act(async () => { fireEvent.click(button); });
    expect(operatorAction).toHaveBeenCalledExactlyOnceWith(path, 'POST', {});
    expect(reload).toHaveBeenCalledOnce();
  });

  it('Disconnect asks first and does nothing when declined', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { operatorAction, reload } = renderPanel();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Disconnect' })); });
    expect(confirm.mock.calls[0][0]).toContain('Disconnect binance?');
    expect(operatorAction).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Disconnect' })); });
    expect(operatorAction).toHaveBeenCalledExactlyOnceWith('/venues/binance/disconnect', 'POST', {});
    expect(reload).toHaveBeenCalledOnce();
  });

  it('disables only the venue that is busy', async () => {
    let finish;
    const operatorAction = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const { container } = renderPanel({ operatorAction });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reconnect' })); });
    expect([...card(container, 'binance').querySelectorAll('button')].every((b) => b.disabled)).toBe(true);
    expect(screen.getByRole('button', { name: 'Retry' }).disabled).toBe(false);
    await act(async () => { finish(null); });
    expect(screen.getByRole('button', { name: 'Reconnect' }).disabled).toBe(false);
  });

  it('says why the status is missing, or stale', () => {
    const { rerender } = render(<VenuesPanel venues={null} error={null} reload={() => {}} operatorAction={vi.fn()} />);
    expect(screen.getByText('Loading venue status…')).not.toBeNull();
    rerender(<VenuesPanel venues={null} error="GET /venues answered 503" reload={() => {}} operatorAction={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toContain('answered 503');
    rerender(<VenuesPanel venues={VENUES} error="network error" reload={() => {}} operatorAction={vi.fn()} />);
    expect(screen.getByText('Last refresh failed: network error')).not.toBeNull();
    expect(card(document, 'okx')).not.toBeNull();
  });
});

describe('useVenues', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const respond = (status, body) => ({ ok: status < 400, status, json: async () => body });

  it('loads, polls, and keeps the last good state when a refresh fails', async () => {
    const calls = [];
    const replies = [respond(200, { venues: VENUES }), respond(503, {}), new Error('offline')];
    api.fetch = vi.fn(async (path) => {
      calls.push(path);
      const next = replies.shift();
      if (next instanceof Error) throw next;
      return next;
    });
    const { result, unmount } = renderHook(() => useVenues(0));
    await act(async () => {});
    expect(calls).toEqual(['/venues']);
    expect(result.current.venues).toEqual(VENUES);
    expect(result.current.error).toBeNull();

    await act(async () => { vi.advanceTimersByTime(VENUE_POLL_MS); });
    expect(result.current.error).toBe('GET /venues answered 503');
    expect(result.current.venues).toEqual(VENUES);

    await act(async () => { vi.advanceTimersByTime(VENUE_POLL_MS); });
    expect(result.current.error).toBe('offline');
    unmount();
    vi.advanceTimersByTime(VENUE_POLL_MS * 3);
    expect(calls).toHaveLength(3);
  });

  it('treats a body without venues as none', async () => {
    api.fetch = vi.fn(async () => respond(200, {}));
    const { result } = renderHook(() => useVenues(0));
    await act(async () => {});
    expect(result.current.venues).toEqual({});
  });
});
