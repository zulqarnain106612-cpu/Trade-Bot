import { useState, useEffect, useRef, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine
} from "recharts";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const WS  = API.replace(/^http/, "ws") + "/ws";

const REGIME_COLOR = { 0: "#22c55e", 1: "#3b82f6", 2: "#ef4444" };
const REGIME_NAME  = { 0: "RANGING",  1: "TRENDING", 2: "VOLATILE" };
const MODE_COLORS  = {
  automatic:  "bg-green-600",
  restricted: "bg-yellow-600",
  manual:     "bg-red-600",
};

function fmt(n, d = 2) {
  if (n == null) return "—";
  return Number(n).toFixed(d);
}

function tsToTime(ts) {
  if (!ts) return "—";
  return new Date(ts).toLocaleTimeString();
}

// ─── Regime Badge ────────────────────────────────────────────────────────────
function RegimeBadge({ regime }) {
  if (!regime) return <span className="text-gray-400 text-xs">No regime data</span>;
  const col = REGIME_COLOR[regime.state] || "#94a3b8";
  const name = REGIME_NAME[regime.state] || "UNKNOWN";
  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-block w-3 h-3 rounded-full"
        style={{ background: col }}
      />
      <span className="font-mono font-bold text-sm" style={{ color: col }}>
        {name}
      </span>
      <span className="text-xs text-gray-400">
        R:{fmt(regime.prob_ranging, 3)} T:{fmt(regime.prob_trending, 3)} V:{fmt(regime.prob_volatile, 3)}
      </span>
    </div>
  );
}

// ─── Execution Mode Switcher ─────────────────────────────────────────────────
function ModeSwitcher({ current, onSwitch }) {
  const modes = ["automatic", "restricted", "manual"];
  return (
    <div className="flex gap-1">
      {modes.map(m => (
        <button
          key={m}
          onClick={() => onSwitch(m)}
          className={`px-3 py-1 rounded text-xs font-bold uppercase transition-all
            ${current === m ? MODE_COLORS[m] + " text-white" : "bg-gray-700 text-gray-300 hover:bg-gray-600"}`}
        >
          {m}
        </button>
      ))}
    </div>
  );
}

// ─── Equity Chart ────────────────────────────────────────────────────────────
function EquityChart({ curve, startingCapital }) {
  if (!curve || curve.length === 0)
    return <div className="flex items-center justify-center h-48 text-gray-500 text-sm">No equity data yet</div>;

  const data = curve.map(p => ({
    t: new Date(p.ts).toLocaleTimeString(),
    equity: p.equity_usd,
    dd: p.drawdown_pct,
  }));

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
        <XAxis dataKey="t" tick={{ fontSize: 10, fill: "#9ca3af" }} interval="preserveStartEnd" />
        <YAxis
          tick={{ fontSize: 10, fill: "#9ca3af" }}
          domain={["auto", "auto"]}
          tickFormatter={v => `$${fmt(v, 0)}`}
        />
        <Tooltip
          contentStyle={{ background: "#1f2937", border: "1px solid #374151", fontSize: 11 }}
          formatter={(v, n) => [n === "equity" ? `$${fmt(v)}` : `${fmt(v, 3)}%`, n]}
        />
        <ReferenceLine y={startingCapital} stroke="#6b7280" strokeDasharray="4 2" />
        <Line type="monotone" dataKey="equity" stroke="#3b82f6" dot={false} strokeWidth={2} name="equity" />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ─── Positions Table ─────────────────────────────────────────────────────────
function PositionsTable({ positions }) {
  if (!positions || positions.length === 0)
    return <p className="text-gray-500 text-sm py-4 text-center">No open positions</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-gray-400 border-b border-gray-700">
          {["Symbol","TF","Dir","Entry","Current","Qty","Notional","Unreal PnL","Regime"].map(h => (
            <th key={h} className="py-1 px-2 text-left font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {positions.map(p => {
          const pnlColor = p.unrealized_pnl >= 0 ? "text-green-400" : "text-red-400";
          return (
            <tr key={p.trade_id} className="border-b border-gray-800 hover:bg-gray-800/50">
              <td className="py-1 px-2 font-mono">{p.symbol}</td>
              <td className="py-1 px-2">{p.timeframe}</td>
              <td className={`py-1 px-2 font-bold ${p.direction === "long" ? "text-green-400" : "text-red-400"}`}>
                {p.direction.toUpperCase()}
              </td>
              <td className="py-1 px-2 font-mono">${fmt(p.entry_price, 2)}</td>
              <td className="py-1 px-2 font-mono">${fmt(p.current_price, 2)}</td>
              <td className="py-1 px-2 font-mono">{p.quantity}</td>
              <td className="py-1 px-2 font-mono">${fmt(p.notional_usd, 2)}</td>
              <td className={`py-1 px-2 font-mono font-bold ${pnlColor}`}>
                ${fmt(p.unrealized_pnl, 4)} ({fmt(p.unrealized_pnl_pct, 2)}%)
              </td>
              <td className="py-1 px-2">
                <span style={{ color: REGIME_COLOR[p.regime_at_entry] }}>
                  {REGIME_NAME[p.regime_at_entry]}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ─── Approval Queue ───────────────────────────────────────────────────────────
function ApprovalQueue({ approvals, onResolve }) {
  const [operator, setOperator] = useState("operator");
  if (!approvals || approvals.length === 0)
    return <p className="text-gray-500 text-sm py-2 text-center">No pending approvals</p>;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs text-gray-400">Operator ID:</span>
        <input
          value={operator}
          onChange={e => setOperator(e.target.value)}
          className="bg-gray-700 text-white text-xs px-2 py-1 rounded w-32"
        />
      </div>
      {approvals.map(req => (
        <div key={req.request_id}
          className="bg-gray-800 border border-gray-700 rounded p-3 flex items-center justify-between gap-4">
          <div className="flex-1 text-xs space-y-0.5">
            <div className="flex gap-4">
              <span className={`font-bold ${req.direction === "long" ? "text-green-400" : "text-red-400"}`}>
                {req.direction?.toUpperCase()} {req.symbol}
              </span>
              <span className="text-gray-400">{req.timeframe}</span>
              <span className="font-mono">${fmt(req.notional_usd)}</span>
            </div>
            <div className="text-gray-400">
              Kelly: {fmt(req.kelly_fraction, 4)} | Meta: {fmt(req.meta_label_prob, 3)} | Signal: {fmt(req.raw_signal, 3)}
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => onResolve(req.request_id, true, operator)}
              className="bg-green-700 hover:bg-green-600 text-white text-xs px-3 py-1.5 rounded font-bold"
            >
              APPROVE
            </button>
            <button
              onClick={() => onResolve(req.request_id, false, operator)}
              className="bg-red-800 hover:bg-red-700 text-white text-xs px-3 py-1.5 rounded font-bold"
            >
              REJECT
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Trade History ────────────────────────────────────────────────────────────
function TradeHistory({ trades }) {
  if (!trades || trades.length === 0)
    return <p className="text-gray-500 text-sm py-4 text-center">No trades yet</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-gray-400 border-b border-gray-700">
          {["Time","Symbol","TF","Dir","Entry","Exit","PnL","PnL%","Reason","Kelly","Regime"].map(h => (
            <th key={h} className="py-1 px-2 text-left font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {trades.map(t => {
          const pnlColor = t.pnl_usd == null ? "" : t.pnl_usd >= 0 ? "text-green-400" : "text-red-400";
          return (
            <tr key={t.id} className="border-b border-gray-800 hover:bg-gray-800/50">
              <td className="py-1 px-2">{tsToTime(t.entry_ts)}</td>
              <td className="py-1 px-2 font-mono">{t.symbol}</td>
              <td className="py-1 px-2">{t.timeframe}</td>
              <td className={`py-1 px-2 font-bold ${t.direction === "long" ? "text-green-400" : "text-red-400"}`}>
                {t.direction?.toUpperCase()}
              </td>
              <td className="py-1 px-2 font-mono">${fmt(t.entry_price, 2)}</td>
              <td className="py-1 px-2 font-mono">{t.exit_price ? `$${fmt(t.exit_price, 2)}` : "open"}</td>
              <td className={`py-1 px-2 font-mono font-bold ${pnlColor}`}>
                {t.pnl_usd != null ? `$${fmt(t.pnl_usd, 4)}` : "—"}
              </td>
              <td className={`py-1 px-2 font-mono ${pnlColor}`}>
                {t.pnl_pct != null ? `${fmt(t.pnl_pct, 3)}%` : "—"}
              </td>
              <td className="py-1 px-2 text-gray-400">{t.exit_reason || "—"}</td>
              <td className="py-1 px-2 font-mono">{fmt(t.kelly_fraction, 4)}</td>
              <td className="py-1 px-2" style={{ color: REGIME_COLOR[t.regime_at_entry] }}>
                {REGIME_NAME[t.regime_at_entry]}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ─── Stats Cards ──────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="text-xs text-gray-400 mb-1">{label}</div>
      <div className={`text-2xl font-bold font-mono ${color || "text-white"}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────
export default function App() {
  const [tick, setTick]           = useState(null);
  const [curve, setCurve]         = useState([]);
  const [trades, setTrades]       = useState([]);
  const [connected, setConnected] = useState(false);
  const [tab, setTab]             = useState("positions");
  const wsRef = useRef(null);

  const startingCapital = 1000;

  // WebSocket
  useEffect(() => {
    function connect() {
      const ws = new WebSocket(WS);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
      };

      ws.onmessage = e => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "tick") setTick(msg);
        } catch (_) {}
      };

      ws.onerror = () => setConnected(false);

      ws.onclose = () => {
        setConnected(false);
        setTimeout(connect, 3000);
      };
    }
    connect();
    return () => wsRef.current?.close();
  }, []);

  // REST: equity curve + trades
  useEffect(() => {
    async function fetchData() {
      try {
        const [eqRes, trRes] = await Promise.all([
          fetch(`${API}/equity?limit=288`),
          fetch(`${API}/trades?limit=50`),
        ]);
        if (eqRes.ok) {
          const eq = await eqRes.json();
          setCurve(eq.curve || []);
        }
        if (trRes.ok) {
          const tr = await trRes.json();
          setTrades(tr.trades || []);
        }
      } catch (_) {}
    }
    fetchData();
    const id = setInterval(fetchData, 30000);
    return () => clearInterval(id);
  }, []);

  const switchMode = useCallback(async mode => {
    try {
      await fetch(`${API}/execution-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
    } catch (_) {}
  }, []);

  const resolveApproval = useCallback(async (id, approved, operator) => {
    try {
      await fetch(`${API}/approvals/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, operator }),
      });
    } catch (_) {}
  }, []);

  const equity      = tick?.equity_usd ?? 0;
  const cash        = tick?.cash_usd ?? 0;
  const pnl         = equity - startingCapital;
  const pnlPct      = startingCapital > 0 ? (pnl / startingCapital) * 100 : 0;
  const positions   = tick?.positions ?? [];
  const approvals   = tick?.pending_approvals ?? [];
  const regime      = tick?.regime ?? null;
  const mode        = tick?.execution_mode ?? "manual";

  return (
    <div className="min-h-screen bg-gray-900 text-white font-sans text-sm">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-bold tracking-tight">⚡ Trade Bot</h1>
          <span className={`text-xs px-2 py-0.5 rounded font-bold ${connected ? "bg-green-900 text-green-400" : "bg-red-900 text-red-400"}`}>
            {connected ? "LIVE" : "DISCONNECTED"}
          </span>
          <span className="text-xs text-gray-400">
            {tick?.trading_mode?.toUpperCase() || "PAPER"}
          </span>
        </div>
        <ModeSwitcher current={mode} onSwitch={switchMode} />
      </header>

      <main className="px-6 py-4 space-y-4">
        {/* Stat cards */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label="Equity" value={`$${fmt(equity)}`}
            sub={`Cash: $${fmt(cash)}`} />
          <StatCard label="Total PnL"
            value={`${pnl >= 0 ? "+" : ""}$${fmt(pnl)}`}
            sub={`${pnl >= 0 ? "+" : ""}${fmt(pnlPct, 2)}%`}
            color={pnl >= 0 ? "text-green-400" : "text-red-400"} />
          <StatCard label="Open Positions" value={positions.length}
            sub={`${approvals.length} pending approval${approvals.length !== 1 ? "s" : ""}`}
            color={approvals.length > 0 ? "text-yellow-400" : "text-white"} />
          <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
            <div className="text-xs text-gray-400 mb-2">Regime</div>
            <RegimeBadge regime={regime} />
          </div>
        </div>

        {/* Equity Chart */}
        <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
          <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
            Equity Curve
          </h2>
          <EquityChart curve={curve} startingCapital={startingCapital} />
        </div>

        {/* Tabs */}
        <div className="bg-gray-800 rounded-lg border border-gray-700">
          <div className="flex border-b border-gray-700">
            {[
              { id: "positions", label: "Positions", badge: positions.length },
              { id: "approvals", label: "Approvals", badge: approvals.length },
              { id: "trades",    label: "Trades",    badge: null },
            ].map(({ id, label, badge }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`px-4 py-2.5 text-xs font-medium border-b-2 transition-colors
                  ${tab === id
                    ? "border-blue-500 text-blue-400"
                    : "border-transparent text-gray-400 hover:text-white"}`}
              >
                {label}
                {badge > 0 && (
                  <span className="ml-1.5 bg-blue-600 text-white text-xs px-1.5 py-0.5 rounded-full">
                    {badge}
                  </span>
                )}
              </button>
            ))}
          </div>
          <div className="p-4 overflow-x-auto">
            {tab === "positions" && <PositionsTable positions={positions} />}
            {tab === "approvals" && (
              <ApprovalQueue approvals={approvals} onResolve={resolveApproval} />
            )}
            {tab === "trades" && <TradeHistory trades={trades} />}
          </div>
        </div>
      </main>
    </div>
  );
}