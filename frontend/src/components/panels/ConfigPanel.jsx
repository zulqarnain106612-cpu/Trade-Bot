import { useState } from 'react';
import { Toggle, Slider, NumberInput } from '../Controls';

export function ConfigPanel({ status }) {
  const [activeSection, setActiveSection] = useState('risk');

  const sections = [
    { id: 'risk', label: 'Risk' },
    { id: 'hmm', label: 'HMM / Regime' },
    { id: 'xgboost', label: 'XGBoost' },
    { id: 'features', label: 'Features' },
    { id: 'strategies', label: 'Strategies' },
    { id: 'execution', label: 'Execution' },
  ];

  return (
    <div>
      <div style={{ display: 'flex', gap: 2, marginBottom: 12, flexWrap: 'wrap', borderBottom: '1px solid var(--c-border)', paddingBottom: 6 }}>
        {sections.map((s) => (
          <button
            key={s.id}
            className={`tab-btn ${activeSection === s.id ? 'active' : ''}`}
            onClick={() => setActiveSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {activeSection === 'risk' && <RiskConfigSection />}
      {activeSection === 'hmm' && <HMMConfigSection />}
      {activeSection === 'xgboost' && <XGBoostConfigSection />}
      {activeSection === 'features' && <FeaturesConfigSection />}
      {activeSection === 'strategies' && <StrategyConfigSection />}
      {activeSection === 'execution' && <ExecutionConfigSection />}
    </div>
  );
}

function ConfigNote() {
  return (
    <div style={{ fontSize: 10, color: 'var(--c-faint)', padding: '8px 0', borderTop: '1px solid var(--c-border)', marginTop: 8 }}>
      Static config parameters (env-based). Changing these requires a server restart via .env.
    </div>
  );
}

function RiskConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>Position Limits</SectionLabel>
        <ReadonlySlider label="Daily DD Halt %" value={2.0} min={0.1} max={10} unit="%" />
        <ReadonlySlider label="Max Position Size %" value={5.0} min={0.1} max={25} unit="%" />
        <ReadonlySlider label="Capital Preservation DD" value={0.30} min={0.01} max={0.99} unit="" decimals={2} />
        <ReadonlyField label="Consecutive Loss Halt" value="3" />
      </div>
      <div>
        <SectionLabel>Kelly Sizing</SectionLabel>
        <ReadonlySlider label="Kelly Multiplier" value={0.5} min={0.01} max={1.0} unit="x" />
        <ReadonlySlider label="Kelly Ceiling" value={0.25} min={0.01} max={1.0} unit="" />
        <ReadonlySlider label="Ensemble Blend" value={0.15} min={0.0} max={1.0} unit="" />
        <ReadonlySlider label="GARCH Vol Threshold" value={0.02} min={0.001} max={0.5} unit="" decimals={3} />
      </div>
      <div>
        <SectionLabel>Slippage (Almgren-Chriss)</SectionLabel>
        <ReadonlySlider label="Default Spread (bps)" value={2.0} min={0} max={500} unit="bps" decimals={1} />
        <ReadonlySlider label="Impact Coeff (bps)" value={10.0} min={0} max={2000} unit="bps" decimals={1} />
        <ReadonlySlider label="Veto Margin (bps)" value={1.0} min={0} max={500} unit="bps" decimals={1} />
      </div>
      <div>
        <SectionLabel>Live Gate</SectionLabel>
        <ReadonlySlider label="OOS Sharpe Threshold" value={1.5} min={0} max={5} unit="" />
        <ReadonlySlider label="Max DD Threshold" value={15.0} min={1} max={100} unit="%" />
        <ReadonlyField label="Min Trades (Live)" value="500" />
        <ReadonlyToggle label="Whale Gate Advisory" value={false} />
        <ReadonlyToggle label="Macro Exposure" value={true} />
      </div>
      <div>
        <SectionLabel>Restricted Mode</SectionLabel>
        <ReadonlySlider label="Notional Limit USD" value={100} min={0} max={10000} unit="$" decimals={0} />
        <ReadonlySlider label="Approval Timeout" value={30} min={1} max={300} unit="s" decimals={0} />
      </div>
      <div>
        <SectionLabel>CVaR</SectionLabel>
        <ReadonlyField label="CVaR Limit" value="Not set" />
        <ReadonlySlider label="CVaR Confidence" value={0.95} min={0.5} max={0.99} unit="" />
        <ReadonlyField label="CVaR Lookback" value="250 bars" />
      </div>
      <ConfigNote />
    </div>
  );
}

function HMMConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>HMM Parameters</SectionLabel>
        <ReadonlyField label="N Components" value="3" />
        <ReadonlyField label="Covariance Type" value="full" />
        <ReadonlyField label="N Iterations" value="200" />
        <ReadonlyField label="Tolerance" value="1e-4" />
        <ReadonlyField label="Volatile State Index" value="2" />
      </div>
      <div>
        <SectionLabel>Entropy Gate</SectionLabel>
        <ReadonlySlider label="Entropy Threshold" value={0.5} min={0} max={1} unit="" />
        <ReadonlySlider label="Entropy Scalar Floor" value={0.5} min={0} max={1} unit="" />
      </div>
      <ConfigNote />
    </div>
  );
}

function XGBoostConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>Model Hyperparameters</SectionLabel>
        <ReadonlyField label="N Estimators" value="500" />
        <ReadonlySlider label="Max Depth" value={6} min={1} max={20} unit="" decimals={0} />
        <ReadonlySlider label="Learning Rate" value={0.05} min={0.00001} max={1} unit="" decimals={5} />
        <ReadonlySlider label="Subsample" value={0.8} min={0.1} max={1} unit="" />
        <ReadonlySlider label="Colsample Bytree" value={0.8} min={0.1} max={1} unit="" />
        <ReadonlyField label="Min Child Weight" value="5" />
      </div>
      <div>
        <SectionLabel>Regularization</SectionLabel>
        <ReadonlySlider label="Reg Alpha (L1)" value={0.1} min={0} max={100} unit="" />
        <ReadonlySlider label="Reg Lambda (L2)" value={1.0} min={0} max={100} unit="" />
        <ReadonlyField label="Eval Metric" value="logloss" />
        <ReadonlyField label="Tree Method" value="hist" />
        <ReadonlyField label="Early Stopping" value="50 rounds" />
      </div>
      <div>
        <SectionLabel>Shadow Mode</SectionLabel>
        <ReadonlyToggle label="Shadow Mode" value={true} />
        <ReadonlyField label="Shadow Min Evals" value="100" />
        <ReadonlyField label="Shadow Max Evals" value="400" />
      </div>
      <ConfigNote />
    </div>
  );
}

function FeaturesConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>Feature Windows</SectionLabel>
        <ReadonlySlider label="Frac Diff d" value={0.4} min={0} max={1} unit="" />
        <ReadonlyField label="VWAP Window" value="20 bars" />
        <ReadonlyField label="OFI Window" value="20 bars" />
        <ReadonlyField label="Realized Vol Short" value="10 bars" />
        <ReadonlyField label="Realized Vol Long" value="60 bars" />
        <ReadonlyField label="ATR Window" value="14 bars" />
        <ReadonlyField label="Sharpe Window" value="60 bars" />
        <ReadonlyField label="Volume Z-Score" value="20 bars" />
        <ReadonlyField label="GARCH Window" value="100 bars" />
      </div>
      <div>
        <SectionLabel>Triple Barrier</SectionLabel>
        <ReadonlySlider label="PT Multiplier" value={2.0} min={0.1} max={10} unit="x" />
        <ReadonlySlider label="SL Multiplier" value={1.0} min={0.1} max={10} unit="x" />
        <ReadonlyField label="Max Holding Bars" value="60" />
        <SectionLabel>CPCV</SectionLabel>
        <ReadonlyField label="N Splits" value="10" />
        <ReadonlyField label="N Test Splits" value="2" />
        <ReadonlyField label="Purge Gap" value="60 bars" />
        <ReadonlySlider label="Embargo %" value={0.01} min={0} max={0.5} unit="" />
        <ReadonlyToggle label="MTF Confirmation" value={false} />
      </div>
      <ConfigNote />
    </div>
  );
}

function StrategyConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>Mean Reversion</SectionLabel>
        <ReadonlyToggle label="Enabled" value={false} />
        <ReadonlySlider label="Fraction" value={0.15} min={0} max={1} unit="" />
        <ReadonlySlider label="Lookback" value={20} min={5} max={200} unit=" bars" decimals={0} />
        <ReadonlySlider label="Entry Z" value={2.0} min={0.1} max={5} unit="" />
        <ReadonlySlider label="Exit Z" value={0.5} min={0} max={5} unit="" />
        <ReadonlyToggle label="Require OU" value={true} />
      </div>
      <div>
        <SectionLabel>Breakout</SectionLabel>
        <ReadonlyToggle label="Enabled" value={false} />
        <ReadonlySlider label="Fraction" value={0.15} min={0} max={1} unit="" />
        <ReadonlyField label="Entry Period" value="20 bars" />
        <ReadonlyField label="Exit Period" value="10 bars" />
        <ReadonlySlider label="Min ATR %" value={0.1} min={0} max={10} unit="%" />
        <ReadonlySlider label="Max ATR %" value={10.0} min={0} max={50} unit="%" />
      </div>
      <div>
        <SectionLabel>Other Strategies</SectionLabel>
        <ReadonlyToggle label="Signal Engine" value={true} />
        <ReadonlyToggle label="Funding Carry" value={false} />
        <ReadonlyToggle label="XSec Momentum" value={false} />
        <ReadonlyToggle label="Basis Trade" value={false} />
        <ReadonlyToggle label="Cross-Exchange Arb" value={false} />
        <ReadonlyToggle label="Options Carry" value={false} />
      </div>
      <div>
        <SectionLabel>Regime Strategy Selector</SectionLabel>
        <ReadonlySlider label="Min Confidence" value={0.55} min={0} max={1} unit="" />
        <ReadonlySlider label="Max Entropy" value={0.75} min={0} max={1} unit="" />
        <ReadonlyToggle label="Transition Guard" value={true} />
        <SectionLabel>Allocation</SectionLabel>
        <ReadonlySlider label="Max Shift/Step" value={0.10} min={0} max={1} unit="" />
        <ReadonlyField label="Rebalance Interval" value="3600s" />
      </div>
      <ConfigNote />
    </div>
  );
}

function ExecutionConfigSection() {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div>
        <SectionLabel>Order Throttle</SectionLabel>
        <ReadonlyToggle label="Enabled" value={true} />
        <ReadonlySlider label="Rate" value={8.0} min={0.1} max={50} unit="/s" />
        <ReadonlyField label="Burst" value="16" />
        <ReadonlySlider label="Max Wait" value={2.0} min={0} max={30} unit="s" />
      </div>
      <div>
        <SectionLabel>General</SectionLabel>
        <ReadonlyField label="Primary Symbol" value="BTC/USDT" />
        <ReadonlyField label="Timeframes" value="1m, 15m, 4h" />
        <ReadonlyField label="Primary Timeframe" value="15m" />
        <ReadonlyField label="Starting Capital" value="$1,000" />
        <ReadonlyField label="WS Heartbeat" value="5.0s" />
        <ReadonlyField label="Position Monitor" value="5.0s" />
      </div>
      <ConfigNote />
    </div>
  );
}

function SectionLabel({ children }) {
  return (
    <div style={{ fontSize: 10, color: 'var(--c-cyan)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8, marginTop: 4 }}>
      {children}
    </div>
  );
}

function ReadonlySlider({ label, value, min, max, unit, decimals = 2 }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ fontSize: 11, color: 'var(--c-muted)' }}>{label}</span>
        <span style={{ fontSize: 10, color: 'var(--c-faint)' }}>{min} — {max}</span>
      </div>
      <div className="slider-container">
        <input
          type="range"
          min={min}
          max={max}
          value={value}
          readOnly
          style={{ opacity: 0.6 }}
        />
        <span className="slider-val" style={{ cursor: 'default' }}>
          {Number(value).toFixed(decimals)}{unit || ''}
        </span>
      </div>
    </div>
  );
}

function ReadonlyToggle({ label, value }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 6, opacity: 0.7 }}>
      <span style={{ color: 'var(--c-text)', fontSize: 12 }}>{label}</span>
      <div className={`toggle-track ${value ? 'on' : 'off'}`} style={{ cursor: 'default' }}>
        <span className="toggle-thumb" />
      </div>
    </div>
  );
}

function ReadonlyField({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 0', marginBottom: 4 }}>
      <span style={{ fontSize: 11, color: 'var(--c-muted)' }}>{label}</span>
      <span style={{ fontFamily: 'monospace', fontSize: 11, color: 'var(--c-silver)' }}>{value}</span>
    </div>
  );
}
