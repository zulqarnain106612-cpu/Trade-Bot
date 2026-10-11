/**
 * RES-025 — while the API starts, the dashboard shows what it is doing and,
 * when it cannot even ask, why: never a generic "not ready" that reads the
 * same for a cold start, a wrong port and a rejected key.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { apiProblem } from '../hooks/useApi.js';
import { StartupBanner } from './StartupBanner.jsx';

const warming = {
  ready: false,
  phase: 'warming_up',
  trading_mode: 'paper',
  reasons: ['model warm-up in progress: 1m training, 15m pending'],
};

describe('StartupBanner', () => {
  it('shows nothing before the first answer and nothing once ready', () => {
    const { container, rerender } = render(<StartupBanner report={null} problem={null} />);
    expect(container.innerHTML).toBe('');
    rerender(<StartupBanner report={{ ...warming, ready: true, phase: 'ready' }} problem={null} />);
    expect(container.innerHTML).toBe('');
  });

  it('shows optional component degradation even after core readiness succeeds', () => {
    render(
      <StartupBanner
        report={{ ...warming, ready: true, phase: 'ready', reasons: [], warnings: ['15m: ensemble members not fitted: lstm'] }}
        problem={null}
      />,
    );
    expect(screen.getByRole('status').textContent).toContain('API ready with optional component warnings');
    expect(screen.getByRole('status').textContent).toContain('15m: ensemble members not fitted: lstm');
  });

  it('names the phase, the trading mode and every reason while not ready', () => {
    render(<StartupBanner report={warming} problem={null} />);
    const banner = screen.getByRole('status');
    expect(banner.textContent).toContain('models warming up');
    expect(banner.textContent).toContain('paper trading');
    expect(screen.getByText('model warm-up in progress: 1m training, 15m pending')).not.toBeNull();
  });

  it('says a failed start plainly, and falls back to the raw phase name', () => {
    const { rerender } = render(
      <StartupBanner report={{ ...warming, phase: 'failed', reasons: [] }} problem={null} />,
    );
    expect(screen.getByRole('status').textContent).toContain('Startup failed');
    expect(screen.queryByRole('listitem')).toBeNull();
    rerender(<StartupBanner report={{ ready: false, phase: 'rebooting' }} problem={null} />);
    expect(screen.getByRole('status').textContent).toContain('API rebooting');
  });

  it('an unreachable API or a rejected key is an alert that says what to fix', () => {
    const { rerender } = render(<StartupBanner report={null} problem={apiProblem(0)} />);
    expect(screen.getByRole('alert').textContent).toContain('uv run python -m src.api');
    rerender(
      <StartupBanner report={null} problem={{ ...apiProblem(401), detail: 'Invalid API key' }} />,
    );
    const alert = screen.getByRole('alert').textContent;
    expect(alert).toContain('VITE_API_KEY');
    expect(alert).toContain('(Invalid API key)');
  });
});
