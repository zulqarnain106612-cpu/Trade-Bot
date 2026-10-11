/**
 * REG-0019 / GOV-035 — the dashboard's reconnect must not leak sockets or
 * retry in lockstep.
 *
 * These are the first tests in `frontend/`, and they exist because the two
 * defects below are invisible to `npm run build`: both are lifecycle bugs
 * that only appear once a socket closes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import {
  RECONNECT_BASE_MS,
  RECONNECT_CAP_MS,
  apiProblem,
  reconnectDelay,
  useApiResource,
  usePolling,
  useReadiness,
  useStream,
  useWebSocket,
} from './useApi.js';

/** Minimal stand-in: records instances and lets a test drive the callbacks. */
class FakeWebSocket {
  static instances = [];
  // The real constant, because `_sendSubscribe` compares against
  // `WebSocket.OPEN` and a stub without it would let every frame through a
  // check the browser applies.
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.readyState = FakeWebSocket.OPEN;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    // A real socket fires onclose when closed from either end. Reproducing
    // that is the whole point: the unmount leak lived in this callback.
    this.onclose?.();
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('reconnectDelay', () => {
  it('draws from [0, backoff) so a fleet spreads instead of arriving together', () => {
    // Full jitter, not backoff +/- a wobble: a wobble around a common
    // centre still puts every client on the server at the same moment.
    expect(reconnectDelay(0, () => 0)).toBe(0);
    expect(reconnectDelay(0, () => 0.999)).toBeLessThan(RECONNECT_BASE_MS);
  });

  it('grows exponentially with the attempt', () => {
    const ceiling = (n) => reconnectDelay(n, () => 0.999) + 1;
    expect(ceiling(3)).toBeGreaterThan(ceiling(1));
  });

  it('caps, so a long outage does not push the retry past the cap', () => {
    expect(reconnectDelay(50, () => 0.999)).toBeLessThanOrEqual(RECONNECT_CAP_MS);
  });

  it('never returns a negative or non-finite delay', () => {
    for (const r of [0, 0.5, 0.999]) {
      for (const attempt of [0, 1, 10, 100]) {
        const d = reconnectDelay(attempt, () => r);
        expect(Number.isFinite(d)).toBe(true);
        expect(d).toBeGreaterThanOrEqual(0);
      }
    }
  });
});

describe('useWebSocket', () => {
  const noop = () => {};

  it('opens one socket on mount', () => {
    renderHook(() => useWebSocket(noop, noop));
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('reconnects after the connection drops', () => {
    renderHook(() => useWebSocket(noop, noop));
    act(() => { FakeWebSocket.instances[0].onclose(); });
    act(() => { vi.advanceTimersByTime(RECONNECT_CAP_MS); });
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it('does not reconnect after unmount', () => {
    // The defect: cleanup closes the socket, the close handler schedules a
    // reconnect, and the loop outlives the component. In React 18
    // StrictMode it starts on the very first mount in dev.
    const { unmount } = renderHook(() => useWebSocket(noop, noop));
    unmount();
    act(() => { vi.advanceTimersByTime(RECONNECT_CAP_MS * 4); });
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('cancels a retry already scheduled when it unmounts', () => {
    const { unmount } = renderHook(() => useWebSocket(noop, noop));
    act(() => { FakeWebSocket.instances[0].onclose(); });  // retry pending
    unmount();
    act(() => { vi.advanceTimersByTime(RECONNECT_CAP_MS * 4); });
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('routes ticks and other frames to their own handler', () => {
    // Panels read fields off the tick; an out-of-band frame reaching
    // onTick would blank the dashboard on every control change.
    const onTick = vi.fn();
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onTick, onEvent));
    const ws = FakeWebSocket.instances[0];

    act(() => {
      ws.onmessage({ data: JSON.stringify({ type: 'tick', equity_usd: 1 }) });
      ws.onmessage({ data: JSON.stringify({ type: 'control_changed' }) });
    });

    expect(onTick).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent.mock.calls[0][0].type).toBe('control_changed');
  });

  it('survives a malformed frame without tearing down the socket', () => {
    const onTick = vi.fn();
    renderHook(() => useWebSocket(onTick, noop));
    act(() => { FakeWebSocket.instances[0].onmessage({ data: 'not json' }); });
    expect(onTick).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
/**
 * REG-0022 — a hook that takes a callback must call the one its caller passed
 * on the current render, and the socket's lifetime must not depend on the
 * identity of the handlers passed to it.
 *
 * These two pull against each other, which is why the defect survived: listing
 * the callbacks in the dependency array fixes staleness and introduces a
 * teardown storm, because `App.jsx` passes inline arrows at five call sites and
 * an inline arrow is a new identity on every render. Refs are what satisfy both,
 * so both halves are asserted here -- a fix that drops either one fails.
 */
describe('REG-0022: callbacks are read from the current render', () => {
  const noop = () => {};

  it('does not rebuild the socket when the handler identities change', () => {
    const { rerender } = renderHook(({ t, e }) => useWebSocket(t, e), {
      initialProps: { t: () => {}, e: () => {} },
    });
    expect(FakeWebSocket.instances).toHaveLength(1);

    // What App.jsx does on every render: fresh arrows, same intent.
    rerender({ t: () => {}, e: () => {} });
    rerender({ t: () => {}, e: () => {} });

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].closed).toBe(false);
  });

  it('delivers a tick to the handler from the current render', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ t }) => useWebSocket(t, noop), {
      initialProps: { t: first },
    });
    rerender({ t: second });

    act(() => {
      FakeWebSocket.instances[0].onmessage({ data: JSON.stringify({ type: 'tick' }) });
    });

    expect(first).toHaveBeenCalledTimes(0);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('folds a stream event with the apply callback from the current render', () => {
    renderHook(() => useWebSocket(noop, noop));
    const { result, rerender } = renderHook(
      ({ f }) => useStream('trades', null, { apply: f }),
      { initialProps: { f: (_prev, payload) => `first:${payload.v}` } },
    );
    rerender({ f: (_prev, payload) => `second:${payload.v}` });

    act(() => {
      FakeWebSocket.instances[0].onmessage({
        data: JSON.stringify({ type: 'event', topic: 'trades', data: { v: 7 } }),
      });
    });

    expect(result.current).toBe('second:7');
  });

  it('polls with the transform from the current render', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ n: 1 }) })),
    );

    const { result, rerender } = renderHook(({ t }) => usePolling('/equity', 10_000, t), {
      initialProps: { t: (body) => `first:${body.n}` },
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(result.current).toBe('first:1');

    rerender({ t: (body) => `second:${body.n}` });
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });

    // The interval is still the one the first render started -- the point of
    // the ref is that the timer survives while the function does not.
    expect(result.current).toBe('second:1');
  });
});

describe('the subscribe frame', () => {
  const noop = () => {};

  /**
   * The server runs every inbound frame through its guard (API-005) before
   * any handler sees it, and a frame missing `type`, `nonce` or `ts` is a
   * policy violation the guard answers by closing the socket. A subscribe
   * carrying only `op` and `topics` therefore turns each connect into a
   * disconnect, and the reconnect sends the same frame again -- a loop in
   * which no panel ever receives anything. Mirrored by
   * tests/test_ws_subscriptions.py::TestTheFrameTheClientSends, which drives
   * the server's real reader with this exact shape.
   */
  it('carries the envelope the server guard requires', () => {
    // No hydrate path: the subscription is what is under test, not the
    // cold-start fetch.
    renderHook(() => useStream('equity', null));
    renderHook(() => useWebSocket(noop, noop));

    act(() => { FakeWebSocket.instances[0].onopen(); });

    const frame = JSON.parse(FakeWebSocket.instances[0].sent[0]);
    expect(frame.type).toBe('subscribe');
    expect(frame.op).toBe('subscribe');
    expect(frame.topics).toContain('equity');
    expect(frame.nonce.length).toBeGreaterThanOrEqual(8);
    expect(frame.nonce.length).toBeLessThanOrEqual(64);
    // Seconds, not milliseconds: the guard compares against its own wall
    // clock with a 120s skew allowance, so ms would read as the year 57000.
    expect(Math.abs(frame.ts - Date.now() / 1000)).toBeLessThan(120);
  });

  it('is not sent on a socket that is not open', () => {
    renderHook(() => useWebSocket(noop, noop));
    const ws = FakeWebSocket.instances[0];
    ws.readyState = 0; // CONNECTING

    act(() => { ws.onopen(); });

    expect(ws.sent).toHaveLength(0);
  });
});

/**
 * RES-025 — the dashboard says why it has no data. An unreachable API, a
 * rejected key and a runtime platform that has not started each read
 * differently, and none of them reads as an empty list.
 */
describe('useApiResource and useReadiness', () => {
  const answer = (status, body) => vi.fn(() => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => (body === undefined ? Promise.reject(new Error('no body')) : Promise.resolve(body)),
  }));

  async function settle() {
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  }

  it('returns the data and no problem on a good answer', async () => {
    vi.stubGlobal('fetch', answer(200, []));
    const { result } = renderHook(() => useApiResource('/runtime/components', 5000));
    await settle();
    expect(result.current).toEqual({ data: [], problem: null });
  });

  it('applies the transform from the current render', async () => {
    vi.stubGlobal('fetch', answer(200, { rows: [1, 2] }));
    const { result } = renderHook(() => useApiResource('/x', 5000, (b) => b.rows));
    await settle();
    expect(result.current.data).toEqual([1, 2]);
  });

  it('names a rejected key with what to set, and keeps the server detail', async () => {
    vi.stubGlobal('fetch', answer(401, { detail: 'Invalid API key' }));
    const { result } = renderHook(() => useApiResource('/runtime', 5000));
    await settle();
    const { data, problem } = result.current;
    expect(data).toBeNull();
    expect(problem.kind).toBe('unauthorized');
    expect(problem.message).toContain('VITE_API_KEY');
    expect(problem.detail).toBe('Invalid API key');
  });

  it('says the API is unreachable when the request never gets an answer', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    const { result } = renderHook(() => useApiResource('/runtime', 5000));
    await settle();
    expect(result.current.problem.kind).toBe('unreachable');
    expect(result.current.problem.message).toContain('http://localhost:8000');
  });

  it('recovers on the next poll once the API answers', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => Promise.reject(new TypeError('Failed to fetch')))
      .mockImplementation(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) }));
    vi.stubGlobal('fetch', fetch);
    const { result } = renderHook(() => useApiResource('/runtime/components', 5000));
    await settle();
    expect(result.current.problem.kind).toBe('unreachable');
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(result.current).toEqual({ data: [], problem: null });
  });

  it('a 503 /readyz report is the API saying what it is doing, not a failure', async () => {
    const report = { ready: false, phase: 'warming_up', reasons: ['model warm-up in progress: 1m training'] };
    vi.stubGlobal('fetch', answer(503, report));
    const { result } = renderHook(() => useReadiness());
    await settle();
    expect(result.current).toEqual({ report, problem: null });
  });

  it('a /readyz the API does not have is a version mismatch', async () => {
    vi.stubGlobal('fetch', answer(404, { detail: 'Not Found' }));
    const { result } = renderHook(() => useReadiness());
    await settle();
    expect(result.current.report).toBeNull();
    expect(result.current.problem.kind).toBe('missing');
  });

  it('a 503 without a report, or an unreadable body, stays a problem', async () => {
    vi.stubGlobal('fetch', answer(503, undefined));
    const { result } = renderHook(() => useReadiness());
    await settle();
    expect(result.current.problem.kind).toBe('unavailable');
    expect(result.current.problem.detail).toBeNull();
  });

  it('stops polling and ignores a late answer after unmount', async () => {
    let resolve;
    const fetch = vi.fn(() => new Promise((r) => { resolve = r; }));
    vi.stubGlobal('fetch', fetch);
    const { result, unmount } = renderHook(() => useApiResource('/runtime', 5000));
    unmount();
    await act(async () => {
      resolve({ ok: true, status: 200, json: () => Promise.resolve({ late: true }) });
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBeNull();
  });

  it('does not start a second request while one is in flight', async () => {
    const fetch = vi.fn(() => new Promise(() => {}));
    vi.stubGlobal('fetch', fetch);
    renderHook(() => useApiResource('/runtime', 1000));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('describes any other status by its code', () => {
    expect(apiProblem(500)).toMatchObject({ kind: 'error', status: 500 });
    expect(apiProblem(500).message).toContain('HTTP 500');
    expect(apiProblem(403).kind).toBe('unauthorized');
  });
});
