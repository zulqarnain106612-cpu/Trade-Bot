import { useState, useCallback } from 'react';

export function Toggle({ checked, onChange, label, disabled }) {
  return (
    <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1 }}>
      <span style={{ color: 'var(--c-text)', fontSize: 12 }}>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={`toggle-track ${checked ? 'on' : 'off'}`}
      >
        <span className="toggle-thumb" />
      </button>
    </label>
  );
}

export function Slider({ value, onChange, min, max, step, label, unit, decimals = 2 }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');

  const handleSubmit = useCallback(() => {
    const n = parseFloat(draft);
    if (!Number.isNaN(n) && n >= min && n <= max) {
      onChange(n);
    }
    setEditing(false);
  }, [draft, min, max, onChange]);

  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 11, color: 'var(--c-muted)' }}>{label}</span>
        <span style={{ fontSize: 10, color: 'var(--c-faint)' }}>{min} — {max}{unit ? ` ${unit}` : ''}</span>
      </div>
      <div className="slider-container">
        <input
          type="range"
          min={min}
          max={max}
          step={step || (max - min) / 100}
          value={value ?? min}
          onChange={(e) => onChange(parseFloat(e.target.value))}
        />
        {editing ? (
          <input
            className="slider-val"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={handleSubmit}
            onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
            autoFocus
            style={{ width: 60 }}
          />
        ) : (
          <button
            className="slider-val"
            onClick={() => { setDraft(String(value != null ? Number(value).toFixed(decimals) : '')); setEditing(true); }}
            title="Click to type value"
          >
            {value != null ? Number(value).toFixed(decimals) : '—'}{unit || ''}
          </button>
        )}
      </div>
    </div>
  );
}

export function NumberInput({ value, onChange, label, min, max, step, unit }) {
  const [draft, setDraft] = useState(String(value ?? ''));
  const [focused, setFocused] = useState(false);

  const commit = () => {
    const n = parseFloat(draft);
    if (!Number.isNaN(n)) {
      if (min != null && n < min) return;
      if (max != null && n > max) return;
      onChange(n);
    }
    setFocused(false);
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
      <span style={{ fontSize: 11, color: 'var(--c-muted)', minWidth: 100 }}>{label}</span>
      <input
        className="input-sm"
        type="number"
        step={step || 1}
        min={min}
        max={max}
        value={focused ? draft : (value ?? '')}
        onChange={(e) => { setDraft(e.target.value); if (!focused) setFocused(true); }}
        onFocus={() => { setDraft(String(value ?? '')); setFocused(true); }}
        onBlur={commit}
        onKeyDown={(e) => e.key === 'Enter' && commit()}
        style={{ width: 80, textAlign: 'right' }}
      />
      {unit && <span style={{ fontSize: 10, color: 'var(--c-faint)' }}>{unit}</span>}
    </div>
  );
}

export function ModeSwitcher({ current, onSwitch }) {
  const modes = ['automatic', 'restricted', 'manual'];
  const activeClass = { automatic: 'active-auto', restricted: 'active-restricted', manual: 'active-manual' };
  return (
    <div style={{ display: 'flex', gap: 3 }}>
      {modes.map((m) => (
        <button
          key={m}
          onClick={() => onSwitch(m)}
          className={`mode-btn ${current === m ? activeClass[m] : ''}`}
        >
          {m}
        </button>
      ))}
    </div>
  );
}

export function StatCard({ label, value, sub, color, icon }) {
  return (
    <div className="stat-card">
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
        {icon && <span style={{ fontSize: 12, color: 'var(--c-cyan)' }}>{icon}</span>}
        <span style={{ fontSize: 10, color: 'var(--c-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</span>
      </div>
      <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'var(--font-mono, monospace)', color: color || 'var(--c-text)' }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: 'var(--c-faint)', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}
