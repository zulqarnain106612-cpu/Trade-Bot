import { useState, useCallback, useEffect } from 'react';
import { useWebSocket, usePolling, useOperatorAction, apiFetch } from './hooks/useApi';
import { Panel } from './components/Panel';
import { ModeSwitcher, StatCard } from './components/Controls';
import { EquityChart, DrawdownChart } from './components/panels/EquityChart';
import { PositionsTable } from './components/panels/PositionsPanel';
import { TradesTable, MissedTradesTable } from './components/panels/TradesPanel';
import { ApprovalsPanel } from './components/panels/ApprovalsPanel';
import { RiskControlsPanel } from './components/panels/RiskControlsPanel';
import { ConfigPanel } from './components/panels/ConfigPanel';
import { SelfTuningPanel } from './components/panels/SelfTuningPanel';
import { StrategiesPanel } from './components/panels/StrategiesPanel';
import {
  HealthPanel, DriftPanel, AuditPanel, ReconcilePanel,
  ModelMetricsPanel, LedgerPanel, RecoveryPanel,
} from './components/panels/MonitoringPanel';
import { fmt, pnlColor } from './utils/format';

const REGIME_COLOR = { 0: '#22c55e', 1: '#da7756', 2: '#ef4444' };
const REGIME_NAME = { 0: 'RANGING', 1: 'TRENDING', 2: 'VOLATILE' };

const ALL_PANELS = [
  { id: 'equity', label: 'Equity Curve', icon: '📈' },
  { id: 'drawdown', label: 'Drawdown', icon: '📉' },
  { id: 'positions', label: 'Positions', icon: '💼' },
  { id: 'trades', label: 'Trade History', icon: '🔄' },
  { id: 'missed', label: 'Missed Trades', icon: '⏭' },
  { id: 'approvals', label: 'Approvals', icon: '✅' },
  { id: 'risk', label: 'Risk Controls', icon: '🛡' },
  { id: 'config', label: 'Configuration', icon: '⚙' },
  { id: 'selftuning', label: 'Self-Tuning', icon: '🎛' },
  { id: 'strategies', label: 'Strategies', icon: '🧠' },
  { id: 'health', label: 'Health', icon: '❤' },
  { id: 'drift', label: 'Drift Monitor', icon: '🔬' },
  { id: 'audit', label: 'Audit Trail', icon: '📋' },
  { id: 'reconcile', label: 'Reconciliation', icon: '⚖' },
  { id: 'model', label: 'Model Metrics', icon: '🤖' },
  { id: 'ledger', label: 'Ledger', icon: '📒' },
  { id: 'recovery', label: 'Recovery', icon: '🔧' },
];

function getInitialVisibility() {
  const defaults = {};
  ALL_PANELS.forEach((p) => { defaults[p.id] = true; });
  try {
    const saved = localStorage.getItem('panel-visibility');
    if (saved) return { ...defaults, ...JSON.parse(saved) };
  } catch {}
  return defaults;
}

export default function App() {
  const [tick, setTick] = useState(null);
  const [visibility, setVisibility] = useState(getInitialVisibility);
  const [showPanelManager, setShowPanelManager] = useState(false);

  const { operatorId, setOperatorId, operatorSecret, setOperatorSecret, action } = useOperatorAction();

  const onTick = useCallback((msg) => setTick(msg), []);
  const wsConnected = useWebSocket(onTick);

  const status = usePolling('/status', 5000);
  const equityCurve = usePolling('/equity-curve?limit=200', 30000);
  const trades = usePolling('/trades?limit=50', 15000);
  const missedTrades = usePolling('/missed-trades?limit=30', 30000);
  const approvals = usePolling('/approvals/pending', 10000);
  const riskControls = usePolling('/risk-controls', 10000);

  useEffect(() => {
    try { localStorage.setItem('panel-visibility', JSON.stringify(visibility)); } catch {}
  }, [visibility]);

  const togglePanel = (id) => {
    setVisibility((v) => ({ ...v, [id]: !v[id] }));
  };

  const showAll = () => {
    const next = {};
    ALL_PANELS.forEach((p) => { next[p.id] = true; });
    setVisibility(next);
  };

  const hideAll = () => {
    const next = {};
    ALL_PANELS.forEach((p) => { next[p.id] = false; });
    setVisibility(next);
  };

  const handleModeSwitch = (mode) => {
    action('/execution-mode', 'POST', { mode });
  };

  const handleRiskUpdate = async (field, value) => {
    await action('/risk-controls', 'POST', { [field]: value });
  };

  const handleApprovalResolve = async (id, approved) => {
    await action(`/approvals/${encodeURIComponent(id)}/resolve`, 'POST', { approved });
  };

  const equity = tick?.equity_usd ?? status?.equity_usd;
  const dailyPnl = tick?.daily_pnl_usd ?? status?.daily_pnl_usd;
  const positions = tick?.positions ?? status?.positions ?? [];
  const regime = tick?.regime ?? status?.regime;
  const prediction = tick?.prediction ?? status?.prediction;
  const executionMode = status?.execution_mode || 'restricted';
  const startingCapital = status?.starting_capital_usd;

  return (
    <div style={{ minHeight: '100vh', background: 'var(--c-bg)', color: 'var(--c-text)', fontFamily: 'Inter, system-ui, -apple-system, sans-serif' }}>
      <Header
        wsConnected={wsConnected}
        executionMode={executionMode}
        onModeSwitch={handleModeSwitch}
        operatorId={operatorId}
        setOperatorId={setOperatorId}
        operatorSecret={operatorSecret}
        setOperatorSecret={setOperatorSecret}
        showPanelManager={showPanelManager}
        setShowPanelManager={setShowPanelManager}
      />

      <div style={{ padding: '0 16px 16px' }}>
        <StatsRow
          equity={equity}
          dailyPnl={dailyPnl}
          positions={positions}
          regime={regime}
          prediction={prediction}
          startingCapital={startingCapital}
          status={status}
        />

        {showPanelManager && (
          <PanelManager
            visibility={visibility}
            togglePanel={togglePanel}
            showAll={showAll}
            hideAll={hideAll}
            onClose={() => setShowPanelManager(false)}
          />
        )}

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'flex-start' }}>
          {visibility.equity && (
            <Panel title="Equity Curve" icon="📈" defaultWidth={580} defaultHeight={260}
              accentColor="var(--c-cyan)" onToggleVisible={() => togglePanel('equity')}>
              <EquityChart curve={equityCurve} startingCapital={startingCapital} />
            </Panel>
          )}

          {visibility.drawdown && (
            <Panel title="Drawdown" icon="📉" defaultWidth={580} defaultHeight={200}
              accentColor="var(--c-red)" onToggleVisible={() => togglePanel('drawdown')}>
              <DrawdownChart curve={equityCurve} />
            </Panel>
          )}

          {visibility.positions && (
            <Panel title="Positions" icon="💼" defaultWidth={560} defaultHeight={250}
              accentColor="var(--c-blue)" badge={positions.length}
              onToggleVisible={() => togglePanel('positions')}>
              <PositionsTable positions={positions} />
            </Panel>
          )}

          {visibility.trades && (
            <Panel title="Trade History" icon="🔄" defaultWidth={640} defaultHeight={300}
              accentColor="var(--c-purple)" badge={trades?.length}
              onToggleVisible={() => togglePanel('trades')}>
              <TradesTable trades={trades} />
            </Panel>
          )}

          {visibility.missed && (
            <Panel title="Missed Trades" icon="⏭" defaultWidth={500} defaultHeight={250}
              accentColor="var(--c-yellow)" badge={missedTrades?.length}
              onToggleVisible={() => togglePanel('missed')}>
              <MissedTradesTable missedTrades={missedTrades} />
            </Panel>
          )}

          {visibility.approvals && (
            <Panel title="Approvals" icon="✅" defaultWidth={440} defaultHeight={280}
              accentColor="var(--c-green)" badge={approvals?.length}
              onToggleVisible={() => togglePanel('approvals')}>
              <ApprovalsPanel approvals={approvals} onResolve={handleApprovalResolve} />
            </Panel>
          )}

          {visibility.risk && (
            <Panel title="Risk Controls" icon="🛡" defaultWidth={420} defaultHeight={320}
              accentColor="var(--c-claude)" onToggleVisible={() => togglePanel('risk')}>
              <RiskControlsPanel riskControls={riskControls} onUpdate={handleRiskUpdate} />
            </Panel>
          )}

          {visibility.config && (
            <Panel title="Configuration" icon="⚙" defaultWidth={640} defaultHeight={400}
              accentColor="var(--c-silver)" onToggleVisible={() => togglePanel('config')}>
              <ConfigPanel status={status} />
            </Panel>
          )}

          {visibility.selftuning && (
            <Panel title="Self-Tuning" icon="🎛" defaultWidth={620} defaultHeight={300}
              accentColor="var(--c-cyan)" onToggleVisible={() => togglePanel('selftuning')}>
              <SelfTuningPanel action={action} />
            </Panel>
          )}

          {visibility.strategies && (
            <Panel title="Strategies" icon="🧠" defaultWidth={620} defaultHeight={340}
              accentColor="var(--c-purple)" onToggleVisible={() => togglePanel('strategies')}>
              <StrategiesPanel action={action} />
            </Panel>
          )}

          {visibility.health && (
            <Panel title="Health" icon="❤" defaultWidth={420} defaultHeight={260}
              accentColor="var(--c-green)" onToggleVisible={() => togglePanel('health')}>
              <HealthPanel />
            </Panel>
          )}

          {visibility.drift && (
            <Panel title="Drift Monitor" icon="🔬" defaultWidth={520} defaultHeight={300}
              accentColor="var(--c-yellow)" onToggleVisible={() => togglePanel('drift')}>
              <DriftPanel />
            </Panel>
          )}

          {visibility.audit && (
            <Panel title="Audit Trail" icon="📋" defaultWidth={460} defaultHeight={280}
              accentColor="var(--c-silver)" onToggleVisible={() => togglePanel('audit')}>
              <AuditPanel />
            </Panel>
          )}

          {visibility.reconcile && (
            <Panel title="Reconciliation" icon="⚖" defaultWidth={480} defaultHeight={260}
              accentColor="var(--c-blue)" onToggleVisible={() => togglePanel('reconcile')}>
              <ReconcilePanel />
            </Panel>
          )}

          {visibility.model && (
            <Panel title="Model Metrics" icon="🤖" defaultWidth={440} defaultHeight={280}
              accentColor="var(--c-cyan)" onToggleVisible={() => togglePanel('model')}>
              <ModelMetricsPanel />
            </Panel>
          )}

          {visibility.ledger && (
            <Panel title="Ledger" icon="📒" defaultWidth={520} defaultHeight={280}
              accentColor="var(--c-silver)" onToggleVisible={() => togglePanel('ledger')}>
              <LedgerPanel />
            </Panel>
          )}

          {visibility.recovery && (
            <Panel title="Recovery" icon="🔧" defaultWidth={440} defaultHeight={250}
              accentColor="var(--c-red)" onToggleVisible={() => togglePanel('recovery')}>
              <RecoveryPanel action={action} />
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}

function Header({
  wsConnected, executionMode, onModeSwitch,
  operatorId, setOperatorId, operatorSecret, setOperatorSecret,
  showPanelManager, setShowPanelManager,
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '10px 16px', borderBottom: '1px solid var(--c-border)',
      background: 'var(--c-surface)', flexWrap: 'wrap', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 16, fontWeight: 700, color: 'var(--c-claude)', letterSpacing: '0.02em' }}>
          Trade-Bot
        </span>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 4,
          fontSize: 10, color: wsConnected ? 'var(--c-green)' : 'var(--c-red)',
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: wsConnected ? 'var(--c-green)' : 'var(--c-red)',
            animation: wsConnected ? 'pulse 2s infinite' : 'none',
          }} />
          {wsConnected ? 'LIVE' : 'DISCONNECTED'}
        </span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <ModeSwitcher current={executionMode} onSwitch={onModeSwitch} />

        <button
          className="btn btn-blue"
          style={{ fontSize: 10, padding: '4px 10px' }}
          onClick={() => setShowPanelManager((v) => !v)}
        >
          {showPanelManager ? 'Hide Manager' : 'Panels'}
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <input
            className="input-sm"
            style={{ width: 80 }}
            value={operatorId}
            onChange={(e) => setOperatorId(e.target.value)}
            placeholder="Operator"
          />
          <input
            className="input-sm"
            style={{ width: 100 }}
            type="password"
            value={operatorSecret}
            onChange={(e) => setOperatorSecret(e.target.value)}
            placeholder="Secret"
          />
        </div>
      </div>
    </div>
  );
}

function StatsRow({ equity, dailyPnl, positions, regime, prediction, startingCapital, status }) {
  const regimeState = regime?.state;
  const regimeColor = REGIME_COLOR[regimeState] || 'var(--c-muted)';
  const regimeName = REGIME_NAME[regimeState] || 'N/A';

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))',
      gap: 8, padding: '12px 0',
    }}>
      <StatCard
        label="Equity"
        value={equity != null ? `$${fmt(equity, 2)}` : '—'}
        color="var(--c-cyan)"
        sub={startingCapital ? `Start: $${fmt(startingCapital, 0)}` : undefined}
      />
      <StatCard
        label="Daily P&L"
        value={dailyPnl != null ? `$${fmt(dailyPnl, 2)}` : '—'}
        color={pnlColor(dailyPnl)}
        sub={equity && startingCapital ? `${fmt(((equity - startingCapital) / startingCapital) * 100, 2)}% total` : undefined}
      />
      <StatCard
        label="Positions"
        value={positions?.length ?? 0}
        color="var(--c-blue)"
      />
      <StatCard
        label="Regime"
        value={regimeName}
        color={regimeColor}
        sub={regime ? `P: ${fmt(regime.prob_ranging || regime.probabilities?.[0], 2)} / ${fmt(regime.prob_trending || regime.probabilities?.[1], 2)} / ${fmt(regime.prob_volatile || regime.probabilities?.[2], 2)}` : undefined}
      />
      <StatCard
        label="Prediction"
        value={prediction?.direction != null ? (prediction.direction > 0 ? 'LONG' : prediction.direction < 0 ? 'SHORT' : 'FLAT') : '—'}
        color={prediction?.direction > 0 ? 'var(--c-green)' : prediction?.direction < 0 ? 'var(--c-red)' : 'var(--c-muted)'}
        sub={prediction?.confidence != null ? `Conf: ${fmt(prediction.confidence * 100, 1)}%` : undefined}
      />
      <StatCard
        label="Mode"
        value={status?.execution_mode?.toUpperCase() || '—'}
        color={status?.execution_mode === 'automatic' ? 'var(--c-green)' : status?.execution_mode === 'restricted' ? 'var(--c-yellow)' : 'var(--c-red)'}
        sub={status?.trading_mode ? `Trading: ${status.trading_mode}` : undefined}
      />
    </div>
  );
}

function PanelManager({ visibility, togglePanel, showAll, hideAll, onClose }) {
  return (
    <div style={{
      background: 'var(--c-surface)',
      border: '1px solid var(--c-border)',
      borderRadius: 8,
      padding: 12,
      marginBottom: 12,
    }}
      className="claude-fade-in"
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--c-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Panel Visibility
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          <button className="btn btn-green" style={{ fontSize: 9, padding: '2px 8px' }} onClick={showAll}>Show All</button>
          <button className="btn btn-red" style={{ fontSize: 9, padding: '2px 8px' }} onClick={hideAll}>Hide All</button>
          <button
            style={{ background: 'none', border: 'none', color: 'var(--c-faint)', cursor: 'pointer', fontSize: 14 }}
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {ALL_PANELS.map((p) => (
          <button
            key={p.id}
            onClick={() => togglePanel(p.id)}
            style={{
              padding: '4px 10px',
              borderRadius: 4,
              fontSize: 10,
              border: '1px solid var(--c-border)',
              background: visibility[p.id] ? 'rgba(0,212,255,0.12)' : 'var(--c-surface2)',
              color: visibility[p.id] ? 'var(--c-cyan)' : 'var(--c-faint)',
              cursor: 'pointer',
              transition: 'all 0.15s',
            }}
          >
            {p.icon} {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
