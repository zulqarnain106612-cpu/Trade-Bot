import { useCallback, useEffect, useState } from 'react';
import { NumberInput, Slider, Toggle } from '../Controls';
import { apiFetch } from '../../hooks/useApi';

/**
 * Every control the backend says it has, rendered by the tier it reports.
 *
 * The panel does not decide what is adjustable. GET /controls does, and each
 * row carries `live` and `protected`, so a widget appears only where a write
 * will actually be honoured. A control this panel invented, or one it offered
 * because the field looked editable, would be a control that silently does
 * nothing -- which for a position-size cap is worse than no control at all.
 */

const GROUP_TITLES = {
  execution: 'Execution',
  risk_controls: 'Exit controls',
  tuning: 'Self-tuning parameters',
  protected: 'Hard limits — not adjustable at runtime',
};

const GROUP_ORDER = ['execution', 'risk_controls', 'tuning', 'protected'];

function ProtectedRow({ control }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'baseline',
        gap: 12,
        padding: '4px 0',
        borderBottom: '1px solid var(--c-line, #222)',
      }}
    >
      <span style={{ fontSize: 11, color: 'var(--c-faint)' }}>{control.name}</span>
      <span style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
        <span style={{ fontSize: 11, fontVariantNumeric: 'tabular-nums' }}>
          {control.value === null ? '—' : String(control.value)}
        </span>
        <span title={control.reason} style={{ fontSize: 9, color: 'var(--c-faint)' }}>
          locked
        </span>
      </span>
    </div>
  );
}

function LiveControl({ control, onWrite, pending }) {
  const disabled = pending === control.name;

  if (control.kind === 'toggle') {
    return (
      <Toggle
        checked={Boolean(control.value)}
        disabled={disabled}
        label={control.name}
        onChange={(v) => onWrite(control.name, v)}
      />
    );
  }

  if (control.kind === 'select') {
    return (
      <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{ fontSize: 10, color: 'var(--c-faint)' }}>{control.name}</span>
        <select
          value={control.value}
          disabled={disabled}
          onChange={(e) => onWrite(control.name, e.target.value)}
          style={{ fontSize: 11, padding: '2px 4px' }}
        >
          {control.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
    );
  }

  // A slider needs both ends, and inventing the missing one would be exactly
  // the bounds-drift this surface exists to avoid. But rendering a live
  // control as a locked row is the same lie pointing the other way -- it was
  // doing that to max_holding_period_s, which the validator bounds below and
  // not above. An open-ended numeric gets a number input: editable, and
  // honest about having no ceiling.
  if (control.min === null || control.max === null) {
    return (
      <NumberInput
        value={control.value}
        min={control.min ?? undefined}
        max={control.max ?? undefined}
        label={control.name}
        onChange={(v) => onWrite(control.name, v)}
      />
    );
  }

  return (
    <Slider
      value={Number(control.value)}
      min={control.min}
      max={control.max}
      step={(control.max - control.min) / 100}
      label={control.name}
      onChange={(v) => onWrite(control.name, v)}
    />
  );
}

export function ControlHubPanel({ operatorAction, refreshToken }) {
  const [surface, setSurface] = useState(null);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/controls');
      if (!res.ok) {
        setError(`/controls returned ${res.status}`);
        return;
      }
      setSurface(await res.json());
      setError(null);
    } catch (e) {
      setError(e.message || 'network error');
    }
  }, []);

  // refreshToken advances on every websocket message, so the hub re-reads the
  // surface the bot actually has rather than trusting what it last wrote. A
  // control_changed frame is pushed out of band the moment any client writes,
  // so a value moved in another tab -- or by the autotuner -- lands here
  // without waiting for the next heartbeat.
  useEffect(() => {
    load();
  }, [load, refreshToken]);

  const write = useCallback(
    async (name, value) => {
      setPending(name);
      try {
        const result = await operatorAction(`/controls/${name}`, 'POST', { value });
        // Re-read either way: on success to confirm what landed, on failure
        // because the widget is showing a value the backend never accepted.
        await load();
        return result;
      } finally {
        setPending(null);
      }
    },
    [operatorAction, load],
  );

  if (error) {
    return <div style={{ color: 'var(--c-red, #f66)', fontSize: 12, padding: 20 }}>{error}</div>;
  }
  if (!surface) {
    return (
      <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>
        Loading controls…
      </div>
    );
  }

  const groups = GROUP_ORDER.map((group) => [
    group,
    surface.controls.filter((c) => c.group === group),
  ]).filter(([, controls]) => controls.length > 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ fontSize: 10, color: 'var(--c-faint)' }}>
        {surface.counts.live} adjustable · {surface.counts.protected} locked
      </div>

      {groups.map(([group, controls]) => (
        <section key={group}>
          <h3 style={{ fontSize: 11, margin: '0 0 6px', color: 'var(--c-faint)' }}>
            {GROUP_TITLES[group] || group}
          </h3>
          <div
            style={{
              display: group === 'protected' ? 'block' : 'grid',
              gridTemplateColumns: group === 'protected' ? undefined : '1fr 1fr',
              gap: 12,
            }}
          >
            {controls.map((control) =>
              control.live ? (
                <LiveControl
                  key={control.name}
                  control={control}
                  onWrite={write}
                  pending={pending}
                />
              ) : (
                <ProtectedRow key={control.name} control={control} />
              ),
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
