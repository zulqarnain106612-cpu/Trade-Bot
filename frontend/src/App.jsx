import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useWebSocket, useStream, usePolling, useOperatorAction, postJson } from './hooks/useApi';
import { ControlHubPanel } from './components/panels/ControlHubPanel';
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
import { RuntimePanel } from './components/panels/RuntimePanel';
import {
  HealthPanel, DriftPanel, AuditPanel, ReconcilePanel,
  ModelMetricsPanel, LedgerPanel, RecoveryPanel,
} from './components/panels/MonitoringPanel';
import {
  ModelTrainingPanel, BackfillPanel, CapitalFloorPanel,
} from './components/panels/OperationsPanel';
import { HorizonsPanel } from './components/panels/HorizonsPanel';
import { VenuesPanel, useVenues } from './components/panels/VenuesPanel';
import { Workspace } from './components/workspace/Workspace';
import { TerminalDock } from './components/terminal/TerminalDock';
import { ProcessCenter } from './components/terminal/ProcessCenter';
import { StatusBar } from './components/terminal/StatusBar';
import { TerminalProvider, useTerminal } from './terminal/TerminalContext';
import {
  compactLayout, defaultLayout, loadLayout, moveBy, resizeBy, saveLayout, setAllHidden, setHidden,
  setMaximized, toggleMinimized,
} from './workspace/layoutStore';
import { WORKSPACE_ITEMS } from './workspace/items';
import { fmt, pnlColor } from './utils/format';

const REGIME_COLOR = { 0: '#22c55e', 1: '#da7756', 2: '#ef4444' };
const REGIME_NAME = { 0: 'RANGING', 1: 'TRENDING', 2: 'VOLATILE' };

const SPEC_BY_ID = Object.fromEntries(WORKSPACE_ITEMS.map((spec) => [spec.id, spec]));

export default function App() {
  return (
    <TerminalProvider>
      <Dashboard />
    </TerminalProvider>
  );
}

function Dashboard() {
  const [tick, setTick] = useState(null);
  const [layout, setLayout] = useState(() => compactLayout(loadLayout(WORKSPACE_ITEMS), WORKSPACE_ITEMS));
  // Every change goes through compaction, so the stored layout is always the
  // one the grid renders (see layoutStore.compactLayout).
  const updateLayout = useCallback(
    (change) => setLayout((current) => compactLayout(change(current), WORKSPACE_ITEMS)),
    [],
  );
  const [showPanelManager, setShowPanelManager] = useState(false);
  const viewportRef = useRef(null);
  const { state: terminal } = useTerminal();
  // The dock is created the first time it is opened and then only hidden, so
  // collapsing it never throws away a terminal's screen or scrollback.
  const [dockMounted, setDockMounted] = useState(false);
  useEffect(() => { if (terminal.dockOpen) setDockMounted(true); }, [terminal.dockOpen]);

  const { operatorId, setOperatorId, operatorSecret, setOperatorSecret, action } = useOperatorAction();

  const onTick = useCallback((msg) => setTick(msg), []);
  // A control_changed frame carries no tick fields, so it advances its own
  // counter rather than being pushed through setTick; the hub re-reads on it.
  // venue_changed does the same for the venue state.
  const [controlVersion, setControlVersion] = useState(0);
  const [venueVersion, setVenueVersion] = useState(0);
  const onEvent = useCallback((msg) => {
    if (msg.type === 'control_changed') setControlVersion((v) => v + 1);
    if (msg.type === 'venue_changed') setVenueVersion((v) => v + 1);
  }, []);
  const { connected: wsConnected, lagMs } = useWebSocket(onTick, onEvent);
  const { venues, error: venuesError, reload: reloadVenues } = useVenues(venueVersion);

  // Every list endpoint returns its rows under a named key
  // (src/api/main.py), while the panels below take plain arrays — so each
  // poll unwraps its key via usePolling's `transform`. Without this the
  // panels get the envelope object, `.length` is undefined, and they render
  // their empty-state placeholder forever with no error shown.
  const status = usePolling('/status', 5000);
  const equityCurve = usePolling('/equity?limit=200', 30000, (b) => b?.curve ?? []);
  // A closed position is a new trade row, so the close event is the trigger
  // to re-read rather than a 15s timer. Re-fetching on the event keeps the
  // row exactly as the server renders it -- the event payload is not the
  // trade record and should not be reshaped into one here.
  const trades = useStream('position', '/trades?limit=50', {
    transform: (b) => b?.trades ?? [],
    refetch: true,
  });
  const missedTrades = usePolling('/missed-trades?limit=30', 30000, (b) => b?.missed_trades ?? []);
  // Streamed, not polled. An approval is the one event with a human waiting
  // on the other end, and it used to take up to a 10s poll before the
  // operator could even see that a decision was being asked for. The `apply`
  // folds each event into the list rather than replacing it, because an
  // approval event describes one request, not the queue.
  const approvals = useStream('approval', '/approvals', {
    transform: (b) => b?.approvals ?? [],
    apply: (prev, ev) => {
      const list = prev ?? [];
      if (ev.action === 'resolved') {
        return list.filter((a) => a.request_id !== ev.request_id);
      }
      if (list.some((a) => a.request_id === ev.request_id)) return list;
      return [...list, ev];
    },
  });
  const riskControls = usePolling('/risk-controls', 10000, (b) => b?.risk_controls ?? null);
  // Polled here only for the backfill timeframe options; ModelTrainingPanel
  // polls the same endpoint for its own display.
  const modelsStatus = usePolling('/models/status', 30000);

  useEffect(() => { saveLayout(layout); }, [layout]);

  const handleModeSwitch = (mode) => {
    action('/execution-mode', 'POST', { mode });
  };

  // RiskControlsPanel calls onUpdate({ field: value }) with a single patch
  // object, not (field, value). Taking two args here made `field` the whole
  // object and `value` undefined, producing a body of
  // {"[object Object]": undefined} that the endpoint rejected.
  const handleRiskUpdate = async (patch) => {
    await action('/risk-controls', 'POST', patch);
  };

  const handleApprovalResolve = async (id, approved) => {
    await action(`/approvals/${encodeURIComponent(id)}/resolve`, 'POST', { approved });
  };

  const handleRetrain = async (timeframe) =>
    action('/models/retrain', 'POST', { timeframe });

  // Not `action`: /backfill is gated by API-key role, not the operator
  // secret, so demanding a secret here would block the call on a
  // credential the server never verifies.
  const handleBackfill = async (timeframe, lookback_days) =>
    postJson('/backfill', { timeframe, lookback_days });

  const handleFloorReAuthorize = async (timeframe, reason) => {
    const res = await action('/capital-floor/re-authorize', 'POST', { timeframe, reason });
    return Boolean(res);
  };

  // Derived from the server's own active timeframes rather than a hardcoded
  // list — Settings.active_timeframes is configurable, so any constant here
  // would be wrong for some deployment.
  const activeTimeframes = Object.keys(modelsStatus?.timeframes ?? {});

  const equity = tick?.equity_usd ?? status?.equity_usd;
  const dailyPnl = tick?.daily_pnl_usd ?? status?.daily_pnl_usd;
  const positions = tick?.positions ?? status?.positions ?? [];
  const regime = tick?.regime ?? status?.regime;
  const prediction = tick?.prediction ?? status?.prediction;
  const executionMode = status?.execution_mode || 'restricted';
  const startingCapital = status?.starting_capital_usd;

  // id -> what the panel shows. Rendered only while visible, exactly as the
  // flex layout did, so a hidden panel still costs no polling.
  const panels = {
    overview: {
      accent: 'var(--c-claude)',
      render: () => (
        <StatsRow equity={equity} dailyPnl={dailyPnl} positions={positions} regime={regime}
          prediction={prediction} startingCapital={startingCapital} status={status} />
      ),
    },
    controlhub: { accent: 'var(--c-cyan)', render: () => <ControlHubPanel operatorAction={action} refreshToken={controlVersion} /> },
    equity: { accent: 'var(--c-cyan)', render: () => <EquityChart curve={equityCurve} startingCapital={startingCapital} /> },
    drawdown: { accent: 'var(--c-red)', render: () => <DrawdownChart curve={equityCurve} /> },
    venues: {
      accent: 'var(--c-green)',
      render: () => <VenuesPanel venues={venues} error={venuesError} reload={reloadVenues} operatorAction={action} />,
    },
    positions: { accent: 'var(--c-blue)', badge: positions.length, render: () => <PositionsTable positions={positions} /> },
    trades: { accent: 'var(--c-purple)', badge: trades?.length, render: () => <TradesTable trades={trades} /> },
    missed: { accent: 'var(--c-yellow)', badge: missedTrades?.length, render: () => <MissedTradesTable missedTrades={missedTrades} /> },
    approvals: {
      accent: 'var(--c-green)',
      badge: approvals?.length,
      render: () => <ApprovalsPanel approvals={approvals} onResolve={handleApprovalResolve} />,
    },
    risk: { accent: 'var(--c-claude)', render: () => <RiskControlsPanel riskControls={riskControls} onUpdate={handleRiskUpdate} /> },
    config: { accent: 'var(--c-silver)', render: () => <ConfigPanel status={status} /> },
    selftuning: { accent: 'var(--c-cyan)', render: () => <SelfTuningPanel action={action} /> },
    strategies: { accent: 'var(--c-purple)', render: () => <StrategiesPanel action={action} /> },
    runtime: { accent: 'var(--c-cyan)', render: () => <RuntimePanel operatorAction={action} /> },
    health: { accent: 'var(--c-green)', render: () => <HealthPanel /> },
    drift: { accent: 'var(--c-yellow)', render: () => <DriftPanel /> },
    audit: { accent: 'var(--c-silver)', render: () => <AuditPanel /> },
    reconcile: { accent: 'var(--c-blue)', render: () => <ReconcilePanel /> },
    model: { accent: 'var(--c-cyan)', render: () => <ModelMetricsPanel /> },
    ledger: { accent: 'var(--c-silver)', render: () => <LedgerPanel /> },
    recovery: { accent: 'var(--c-red)', render: () => <RecoveryPanel action={action} /> },
    training: { accent: 'var(--c-claude)', render: () => <ModelTrainingPanel onRetrain={handleRetrain} /> },
    backfill: { accent: 'var(--c-green)', render: () => <BackfillPanel onBackfill={handleBackfill} timeframes={activeTimeframes} /> },
    capitalfloor: { accent: 'var(--c-red)', render: () => <CapitalFloorPanel onReAuthorize={handleFloorReAuthorize} /> },
    horizons: { accent: 'var(--c-cyan)', render: () => <HorizonsPanel /> },
  };

  const renderPanel = (id) => {
    const spec = SPEC_BY_ID[id];
    const panel = panels[id];
    const item = layout.items[id];
    if (!spec || !panel || !item) return null;
    return (
      <Panel
        title={spec.label}
        icon={spec.icon}
        accentColor={panel.accent}
        badge={panel.badge}
        minimized={item.minimized}
        maximized={layout.maximized === id}
        onMinimize={() => updateLayout((l) => toggleMinimized(l, id, spec))}
        onMaximize={() => updateLayout((l) => setMaximized(l, l.maximized === id ? null : id))}
        onHide={() => updateLayout((l) => setHidden(l, id, true))}
        onMove={(dx, dy) => updateLayout((l) => moveBy(l, id, dx, dy))}
        onResize={(dw, dh) => updateLayout((l) => resizeBy(l, id, dw, dh, spec))}
      >
        {panel.render()}
      </Panel>
    );
  };

  const hiddenCount = useMemo(
    () => Object.values(layout.items).filter((item) => item.hidden).length,
    [layout],
  );

  return (
    <div className="app-shell">
      <Header
        wsConnected={wsConnected}
        lagMs={lagMs}
        executionMode={executionMode}
        onModeSwitch={handleModeSwitch}
        operatorId={operatorId}
        setOperatorId={setOperatorId}
        operatorSecret={operatorSecret}
        setOperatorSecret={setOperatorSecret}
        showPanelManager={showPanelManager}
        setShowPanelManager={setShowPanelManager}
      />

      <div className="app-main">
        <main ref={viewportRef} className="workspace-viewport" aria-label="Dashboard workspace">
          {showPanelManager && (
            <PanelManager
              layout={layout}
              togglePanel={(id) => updateLayout((l) => setHidden(l, id, !l.items[id].hidden))}
              showAll={() => updateLayout((l) => setAllHidden(l, false))}
              hideAll={() => updateLayout((l) => setAllHidden(l, true))}
              resetLayout={() => {
                if (window.confirm('Reset every panel to its default position, size and visibility?')) {
                  updateLayout(() => defaultLayout(WORKSPACE_ITEMS));
                }
              }}
              onClose={() => setShowPanelManager(false)}
            />
          )}
          {hiddenCount > 0 && !showPanelManager && (
            <button type="button" className="ws-hidden-chip" onClick={() => setShowPanelManager(true)}>
              {hiddenCount} hidden panel{hiddenCount === 1 ? '' : 's'} — show
            </button>
          )}
          <Workspace
            specs={WORKSPACE_ITEMS}
            layout={layout}
            onChange={setLayout}
            renderPanel={renderPanel}
            viewportRef={viewportRef}
          />
        </main>
        {terminal.centerOpen && <ProcessCenter />}
      </div>

      {dockMounted && (
        <div className="dock-slot" hidden={!terminal.dockOpen}>
          <TerminalDock />
        </div>
      )}
      <StatusBar
        venues={venues}
        onVenueClick={() => updateLayout((l) => setHidden(l, 'venues', false))}
      />
      <p id="ws-keyboard-help" className="sr-only">
        Arrow keys move the panel one grid step; Shift with an arrow key resizes it.
        Escape restores a maximized panel.
      </p>
    </div>
  );
}

function Header({
  wsConnected, lagMs, executionMode, onModeSwitch,
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
        {/* A green dot only says the socket is open. It stays green while a
            starved stream shows minute-old numbers, which is the failure this
            transport work exists to remove -- so the dot carries the measured
            producer-to-browser lag beside it. */}
        {wsConnected && lagMs !== null && (
          <span
            title="Producer-to-browser lag, measured from the timestamp the producer stamped"
            style={{
              fontSize: 10,
              color: lagMs > 5000 ? 'var(--c-red)' : lagMs > 1500 ? 'var(--c-yellow)' : 'var(--c-muted)',
            }}
          >
            {lagMs < 1000 ? `${lagMs}ms` : `${(lagMs / 1000).toFixed(1)}s`}
          </span>
        )}
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

function PanelManager({ layout, togglePanel, showAll, hideAll, resetLayout, onClose }) {
  return (
    <div className="panel-manager claude-fade-in" role="region" aria-label="Panel visibility and layout">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--c-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Panels
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          <button type="button" className="btn btn-green" style={{ fontSize: 9, padding: '2px 8px' }} onClick={showAll}>Show All</button>
          <button type="button" className="btn btn-red" style={{ fontSize: 9, padding: '2px 8px' }} onClick={hideAll}>Hide All</button>
          <button type="button" className="btn btn-blue" style={{ fontSize: 9, padding: '2px 8px' }} onClick={resetLayout}>Reset Layout</button>
          <button
            type="button"
            aria-label="Close panel manager"
            style={{ background: 'none', border: 'none', color: 'var(--c-faint)', cursor: 'pointer', fontSize: 14 }}
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {WORKSPACE_ITEMS.map((p) => {
          const visible = !layout.items[p.id]?.hidden;
          return (
            <button
              type="button"
              key={p.id}
              aria-pressed={visible}
              onClick={() => togglePanel(p.id)}
              className={`pm-toggle${visible ? ' on' : ''}`}
            >
              {p.icon} {p.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
