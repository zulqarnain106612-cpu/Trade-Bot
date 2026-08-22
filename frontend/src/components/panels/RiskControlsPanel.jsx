import { useState, useEffect } from 'react';
import { Toggle, Slider, NumberInput } from '../Controls';

export function RiskControlsPanel({ riskControls, onUpdate }) {
  if (!riskControls) {
    return <div style={{ color: 'var(--c-faint)', fontSize: 12, textAlign: 'center', padding: 20 }}>Loading risk controls...</div>;
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <Toggle
          checked={riskControls.stop_loss_enabled}
          onChange={(v) => onUpdate({ stop_loss_enabled: v })}
          label="Stop-Loss"
        />
        <div style={{ marginTop: 8 }}>
          <Slider
            label="Stop-Loss %"
            value={riskControls.stop_loss_pct}
            onChange={(v) => onUpdate({ stop_loss_pct: v })}
            min={0.1}
            max={50}
            step={0.1}
            unit="%"
          />
        </div>
      </div>

      <div>
        <Toggle
          checked={riskControls.take_profit_enabled}
          onChange={(v) => onUpdate({ take_profit_enabled: v })}
          label="Take-Profit"
        />
        <div style={{ marginTop: 8 }}>
          <Slider
            label="Take-Profit %"
            value={riskControls.take_profit_pct}
            onChange={(v) => onUpdate({ take_profit_pct: v })}
            min={0.1}
            max={200}
            step={0.1}
            unit="%"
          />
        </div>
      </div>

      <div>
        <Toggle
          checked={riskControls.trailing_stop_enabled}
          onChange={(v) => onUpdate({ trailing_stop_enabled: v })}
          label="Trailing Stop"
        />
        <div style={{ marginTop: 8 }}>
          <Slider
            label="Trailing Stop %"
            value={riskControls.trailing_stop_pct}
            onChange={(v) => onUpdate({ trailing_stop_pct: v })}
            min={0.1}
            max={50}
            step={0.1}
            unit="%"
          />
        </div>
      </div>

      <div>
        <NumberInput
          label="Max Hold (sec)"
          value={riskControls.max_holding_period_s}
          onChange={(v) => onUpdate({ max_holding_period_s: v })}
          min={60}
          step={60}
          unit="s"
        />
        <div style={{ fontSize: 10, color: 'var(--c-faint)', marginTop: 4 }}>
          Always enforced — closes position after this duration regardless of PnL
        </div>
      </div>
    </div>
  );
}
