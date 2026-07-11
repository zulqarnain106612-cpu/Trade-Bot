import { useState, useEffect, useRef, useCallback } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine
} from "recharts";

const _RAW_API  = import.meta.env.VITE_API_URL || "http://localhost:8000";
const API_KEY   = import.meta.env.VITE_API_KEY || "";

// Build-time config sanity check: rejects malformed/non-http(s) VITE_API_URL values (operator misconfiguration guard — does NOT block internal IPs).
const _ALLOWED_API_RE = /^https?:\/\/[a-zA-Z0-9._-]+(:\d+)?$/;
if (!_ALLOWED_API_RE.test(_RAW_API)) {
  throw new Error(`VITE_API_URL "${_RAW_API}" is not an allowed origin.`);
}
const API    = _RAW_API.replace(/\/$/, ""); // strip trailing slash
const WS_URL = API.replace(/^http/, "ws") + "/ws";

// Attach API key to every fetch — server requires X-Api-Key on all endpoints
function apiFetch(path, opts = {}) {
  if (typeof path !== "string" || !path.startsWith("/")) {
    throw new Error(`apiFetch: path must be a string starting with "/", got: ${path}`);
  }
  return fetch(`${API}${path}`, {
    ...opts,
    headers: {
      ...(opts.headers || {}),
      "x-api-key": API_KEY,
    },
  });
}

const REGIME_COLOR = { 0: "#22c55e", 1: "#da7756", 2: "#ef4444" };
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
  if (!regime) return <span className="text-claude-muted text-xs">No regime data</span>;
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
      <span className="text-xs text-claude-muted">
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
            ${current === m ? MODE_COLORS[m] + " text-white" : "bg-claude-surface3 text-claude-text/80 hover:bg-claude-surface2"}`}
        >
          {m}
        </button>
      ))}
    </div>
  );
}

// ─── Risk Controls (GAP-013) ─────────────────────────────────────────────────
// Toggle stop-loss / take-profit on or off and edit their thresholds at
// runtime, without a redeploy. Mirrors ModeSwitcher's pattern: the operator
// secret already entered in the header is reused for every control action.
function ToggleSwitch({ checked, onChange, label }) {
  return (
    <label className="flex items-center justify-between gap-3 cursor-pointer">
      <span className="text-claude-text/80">{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors
          ${checked ? "bg-green-600" : "bg-claude-surface3"}`}
      >
        <span
          className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform
            ${checked ? "translate-x-5" : "translate-x-1"}`}
        />
      </button>
    </label>
  );
}

function RiskControlsPanel({ riskControls, onUpdate }) {
  // Local draft state for the numeric inputs so typing doesn't fire a
  // request on every keystroke — only on blur / explicit "Save" click.
  // Toggles, by contrast, apply immediately on click (matches ModeSwitcher's
  // immediate-apply behavior for mode changes).
  const [slDraft, setSlDraft] = useState("");
  const [tpDraft, setTpDraft] = useState("");
  const [holdDraft, setHoldDraft] = useState("");

  useEffect(() => {
    if (riskControls) {
      setSlDraft(String(riskControls.stop_loss_pct ?? ""));
      setTpDraft(String(riskControls.take_profit_pct ?? ""));
      setHoldDraft(String(riskControls.max_holding_period_s ?? ""));
    }
  }, [riskControls]);

  if (!riskControls) {
    return <div className="text-claude-faint text-sm">Loading risk controls…</div>;
  }

  // Mirrors the backend's Pydantic bounds (SetRiskControlsRequest in
  // src/api/main.py) — the server is the real enforcement point, this is
  // just fast feedback so an out-of-range value doesn't silently round-trip
  // to a 422 with no visible explanation in this panel.
  const THRESHOLD_BOUNDS = {
    stop_loss_pct: [0.1, 50.0],
    take_profit_pct: [0.1, 200.0],
    max_holding_period_s: [60.0, Infinity],
  };

  const saveThreshold = (field, draftValue) => {
    const n = parseFloat(draftValue);
    if (Number.isNaN(n)) return;
    const [min, max] = THRESHOLD_BOUNDS[field] || [-Infinity, Infinity];
    if (n < min || n > max) {
      alert(`${field.replace(/_/g, " ")} must be between ${min} and ${max === Infinity ? "∞" : max}.`);
      return;
    }
    onUpdate({ [field]: n });
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-2xl">
      <div className="space-y-3">
        <ToggleSwitch
          checked={riskControls.stop_loss_enabled}
          onChange={v => onUpdate({ stop_loss_enabled: v })}
          label="Stop-loss enabled"
        />
        <div className="flex items-center gap-2">
          <span className="text-claude-muted w-32">Stop-loss %</span>
          <input
            type="number"
            step="0.1"
            min="0.1"
            max="50"
            value={slDraft}
            onChange={e => setSlDraft(e.target.value)}
            onBlur={() => saveThreshold("stop_loss_pct", slDraft)}
            className="bg-claude-surface3 text-white px-2 py-1 rounded w-24 text-right"
          />
        </div>
      </div>

      <div className="space-y-3">
        <ToggleSwitch
          checked={riskControls.take_profit_enabled}
          onChange={v => onUpdate({ take_profit_enabled: v })}
          label="Take-profit enabled"
        />
        <div className="flex items-center gap-2">
          <span className="text-claude-muted w-32">Take-profit %</span>
          <input
            type="number"
            step="0.1"
            min="0.1"
            max="200"
            value={tpDraft}
            onChange={e => setTpDraft(e.target.value)}
            onBlur={() => saveThreshold("take_profit_pct", tpDraft)}
            className="bg-claude-surface3 text-white px-2 py-1 rounded w-24 text-right"
          />
        </div>
      </div>

      <div className="space-y-3 md:col-span-2">
        <div className="flex items-center gap-2">
          <span className="text-claude-muted w-32">Max hold (sec)</span>
          <input
            type="number"
            step="60"
            min="60"
            value={holdDraft}
            onChange={e => setHoldDraft(e.target.value)}
            onBlur={() => saveThreshold("max_holding_period_s", holdDraft)}
            className="bg-claude-surface3 text-white px-2 py-1 rounded w-32 text-right"
          />
          <span className="text-claude-faint text-xs">
            (always enforced — no toggle; closes a position after this long regardless of PnL)
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── Equity Chart ────────────────────────────────────────────────────────────
function EquityChart({ curve, startingCapital }) {
  if (!curve || curve.length === 0)
    return <div className="flex items-center justify-center h-48 text-claude-faint text-sm">No equity data yet</div>;

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
        <Line type="monotone" dataKey="equity" stroke="#da7756" dot={false} strokeWidth={2} name="equity" />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ─── Positions Table ─────────────────────────────────────────────────────────
function PositionsTable({ positions }) {
  if (!positions || positions.length === 0)
    return <p className="text-claude-faint text-sm py-4 text-center">No open positions</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-claude-muted border-b border-claude-border">
          {["Symbol","TF","Dir","Entry","Current","Qty","Notional","Unreal PnL","Regime"].map(h => (
            <th key={h} className="py-1 px-2 text-left font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {positions.map(p => {
          const pnlColor = p.unrealized_pnl >= 0 ? "text-green-400" : "text-red-400";
          return (
            <tr key={p.trade_id} className="border-b border-claude-border hover:bg-claude-surface2/60">
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
    return <p className="text-claude-faint text-sm py-2 text-center">No pending approvals</p>;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs text-claude-muted">Operator ID:</span>
        <input
          value={operator}
          onChange={e => setOperator(e.target.value)}
          className="bg-claude-surface3 text-white text-xs px-2 py-1 rounded w-32"
        />
      </div>
      {approvals.map(req => (
        <div key={req.request_id}
          className="bg-claude-surface border border-claude-border rounded p-3 flex items-center justify-between gap-4">
          <div className="flex-1 text-xs space-y-0.5">
            <div className="flex gap-4">
              <span className={`font-bold ${req.direction === "long" ? "text-green-400" : "text-red-400"}`}>
                {req.direction?.toUpperCase()} {req.symbol}
              </span>
              <span className="text-claude-muted">{req.timeframe}</span>
              <span className="font-mono">${fmt(req.notional_usd)}</span>
            </div>
            <div className="text-claude-muted">
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
    return <p className="text-claude-faint text-sm py-4 text-center">No trades yet</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-claude-muted border-b border-claude-border">
          {["Time","Symbol","TF","Dir","Entry","Exit","PnL","PnL%","Reason","Kelly","Regime"].map(h => (
            <th key={h} className="py-1 px-2 text-left font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {trades.map(t => {
          const pnlColor = t.pnl_usd == null ? "" : t.pnl_usd >= 0 ? "text-green-400" : "text-red-400";
          return (
            <tr key={t.id} className="border-b border-claude-border hover:bg-claude-surface2/60">
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
              <td className="py-1 px-2 text-claude-muted">{t.exit_reason || "—"}</td>
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

// ─── Missed Trades (UI-001) ──────────────────────────────────────────────────
const MISSED_REASON_LABEL = {
  rejected: "Rejected",
  skipped: "Approval Timeout",
  queued: "Queued",
  auto_timeout: "Auto Timeout",
};

function MissedTradesTable({ missedTrades }) {
  if (!missedTrades || missedTrades.length === 0)
    return <p className="text-claude-faint text-sm py-4 text-center">No missed trades</p>;

  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-claude-muted border-b border-claude-border">
          {["Time","Symbol","TF","Dir","Reason","Notional","Kelly","Meta","Signal","Regime"].map(h => (
            <th key={h} className="py-1 px-2 text-left font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {missedTrades.map(m => (
          <tr key={m.id} className="border-b border-claude-border hover:bg-claude-surface2/60">
            <td className="py-1 px-2">{tsToTime(m.ts)}</td>
            <td className="py-1 px-2 font-mono">{m.symbol}</td>
            <td className="py-1 px-2">{m.timeframe}</td>
            <td className={`py-1 px-2 font-bold ${m.direction === "long" ? "text-green-400" : "text-red-400"}`}>
              {m.direction?.toUpperCase()}
            </td>
            <td className="py-1 px-2">
              <span className="bg-claude-surface3 text-claude-orangeLight px-1.5 py-0.5 rounded text-xs">
                {MISSED_REASON_LABEL[m.reason] || m.reason}
              </span>
            </td>
            <td className="py-1 px-2 font-mono">${fmt(m.notional_usd, 2)}</td>
            <td className="py-1 px-2 font-mono">{fmt(m.kelly_fraction, 4)}</td>
            <td className="py-1 px-2 font-mono">{fmt(m.meta_label_prob, 3)}</td>
            <td className="py-1 px-2 font-mono">{fmt(m.raw_signal, 3)}</td>
            <td className="py-1 px-2" style={{ color: REGIME_COLOR[m.regime_at_entry] }}>
              {REGIME_NAME[m.regime_at_entry]}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ─── Stats Cards ──────────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }) {
  return (
    <div className="bg-claude-surface rounded-lg p-4 border border-claude-border shadow-claude hover:border-claude-orange/40 transition-colors">
      <div className="text-xs text-claude-muted mb-1 uppercase tracking-wide">{label}</div>
      <div className={`text-2xl font-bold font-mono ${color || "text-claude-cream"}`}>{value}</div>
      {sub && <div className="text-xs text-claude-faint mt-0.5">{sub}</div>}
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────
export default function App() {
  const [tick, setTick]                     = useState(null);
  const [curve, setCurve]                   = useState([]);
  const [trades, setTrades]                 = useState([]);
  const [missedTrades, setMissedTrades]     = useState([]);
  const [connected, setConnected]           = useState(false);
  const [tab, setTab]                       = useState("positions");
  const [startingCapital, setStartingCapital] = useState(null);
  const [riskControls, setRiskControls]     = useState(null);
  const wsRef = useRef(null);

  // WebSocket — API key passed as query param (WS headers not reliably supported in browsers)
  useEffect(() => {
    function connect() {
      const wsUrl = API_KEY ? `${WS_URL}?api_key=${encodeURIComponent(API_KEY)}` : WS_URL;
      const ws = new WebSocket(wsUrl);
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

  // REST: equity curve + trades + starting capital from /status
  useEffect(() => {
    let inFlight = false;
    async function fetchData() {
      // UI-016: guard against overlapping requests — a slow response
      // (backend hiccup) could otherwise let a second poll fire before the
      // first resolves, and an out-of-order response could overwrite newer
      // state with stale data.
      if (inFlight) return;
      inFlight = true;
      try {
        const [eqRes, trRes, mtRes, stRes] = await Promise.all([
          apiFetch("/equity?limit=288"),
          apiFetch("/trades?limit=50"),
          apiFetch("/missed-trades?limit=50"),
          apiFetch("/status"),
        ]);
        if (eqRes.ok) {
          const eq = await eqRes.json();
          setCurve(eq.curve || []);
        }
        if (trRes.ok) {
          const tr = await trRes.json();
          setTrades(tr.trades || []);
        }
        if (mtRes.ok) {
          const mt = await mtRes.json();
          setMissedTrades(mt.missed_trades || []);
        }
        if (stRes.ok) {
          const st = await stRes.json();
          if (st.starting_capital_usd != null) {
            setStartingCapital(st.starting_capital_usd);
          }
        }
      } catch (_) {
        // swallow — next poll retries
      } finally {
        inFlight = false;
      }
    }
    fetchData();
    const id = setInterval(fetchData, 30000);
    return () => clearInterval(id);
  }, []);

  // REST: GAP-013 risk controls (stop-loss / take-profit toggles + thresholds)
  // Polled on its own short interval since it's small and an operator
  // expects a toggle they just flipped (e.g. from a second device) to show
  // up quickly, not wait for the slower 30s equity/trades poll above.
  useEffect(() => {
    let inFlight = false;
    async function fetchRiskControls() {
      if (inFlight) return; // UI-016: same overlapping-request guard as fetchData
      inFlight = true;
      try {
        const res = await apiFetch("/risk-controls");
        if (res.ok) {
          const body = await res.json();
          setRiskControls(body.risk_controls || null);
        }
      } catch (_) {
        // swallow — next poll retries
      } finally {
        inFlight = false;
      }
    }
    fetchRiskControls();
    const id = setInterval(fetchRiskControls, 10000);
    return () => clearInterval(id);
  }, []);

  // operatorId & operatorSecret stored in state — set once, reused for all actions
  const [operatorId, setOperatorId]         = useState("operator");
  const [operatorSecret, setOperatorSecret] = useState("");

  const switchMode = useCallback(async mode => {
    if (!operatorSecret) {
      alert("Enter the operator secret before switching modes.");
      return;
    }
    try {
      await apiFetch("/execution-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, operator: operatorId, operator_secret: operatorSecret }),
      });
    } catch (_) {}
  }, [operatorId, operatorSecret]);

  const resolveApproval = useCallback(async (id, approved, operator) => {
    try {
      await apiFetch(`/approvals/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, operator }),
      });
    } catch (_) {}
  }, []);

  // GAP-013 — update one or more risk-control fields. Pass only the
  // field(s) being changed; undefined fields are omitted from the request
  // body entirely (not sent as null) so the backend's partial-update
  // semantics apply correctly — see SetRiskControlsRequest in main.py.
  const updateRiskControls = useCallback(async (changes) => {
    if (!operatorSecret) {
      alert("Enter the operator secret before changing risk controls.");
      return;
    }
    try {
      const res = await apiFetch("/risk-controls", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...changes,
          operator: operatorId,
          operator_secret: operatorSecret,
        }),
      });
      if (res.ok) {
        const body = await res.json();
        setRiskControls(body.risk_controls || null);
      } else {
        const err = await res.json().catch(() => ({}));
        alert(`Risk control update failed: ${err.detail || res.status}`);
      }
    } catch (_) {}
  }, [operatorId, operatorSecret]);

  const equity      = tick?.equity_usd ?? 0;
  const cash        = tick?.cash_usd ?? 0;
  const capital     = startingCapital ?? tick?.equity_usd ?? 0;  // fallback until loaded
  const pnl         = capital > 0 ? equity - capital : 0;
  const pnlPct      = capital > 0 ? (pnl / capital) * 100 : 0;
  const positions   = tick?.positions ?? [];
  const approvals   = tick?.pending_approvals ?? [];
  const regime      = tick?.regime ?? null;
  const mode        = tick?.execution_mode ?? "manual";

  // Desktop tray badge (Electron only) — mirrors the pending-approvals
  // count so the count is visible without the window in focus.
  useEffect(() => {
    window.tradeBotDesktop?.setPendingApprovals?.(approvals.length);
  }, [approvals.length]);

  return (
    <div className="min-h-screen bg-claude-bg text-claude-text font-sans text-sm">
      {/* Header */}
      <header className="glass sticky top-0 z-10 border-b border-claude-border px-6 py-3 flex items-center justify-between flex-wrap gap-2 shadow-claude">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-bold tracking-tight flex items-center gap-2">
            <span className="inline-flex items-center justify-center w-7 h-7 rounded-md bg-claude-orange text-black text-sm font-black shadow-glow">*</span>
            <span className="text-claude-cream">Trade Bot</span>
          </h1>
          <span className={`text-xs px-2 py-0.5 rounded font-bold ${connected ? "bg-green-900/60 text-green-400" : "bg-red-900/60 text-red-400"} ${connected ? "" : "claude-pulse"}`}>
            {connected ? "● LIVE" : "● DISCONNECTED"}
          </span>
          <span className="text-xs text-claude-muted uppercase tracking-wide">
            {tick?.trading_mode?.toUpperCase() || "PAPER"}
          </span>
        </div>
        {/* Operator identity + secret — required for mode switching */}
        <div className="flex items-center gap-2 text-xs">
          <input
            value={operatorId}
            onChange={e => setOperatorId(e.target.value)}
            placeholder="Operator ID"
            className="bg-claude-surface3 text-white px-2 py-1 rounded w-28"
          />
          <input
            type="password"
            value={operatorSecret}
            onChange={e => setOperatorSecret(e.target.value)}
            placeholder="Operator Secret"
            className="bg-claude-surface3 text-white px-2 py-1 rounded w-36"
          />
          <ModeSwitcher current={mode} onSwitch={switchMode} />
        </div>
      </header>

      <main className="px-6 py-4 space-y-4 claude-fade-in">
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
            color={approvals.length > 0 ? "text-yellow-400" : "text-claude-cream"} />
          <div className="bg-claude-surface rounded-lg p-4 border border-claude-border shadow-claude">
            <div className="text-xs text-claude-muted mb-2 uppercase tracking-wide">Regime</div>
            <RegimeBadge regime={regime} />
          </div>
        </div>

        {/* Equity Chart */}
        <div className="bg-claude-surface rounded-lg border border-claude-border p-4 shadow-claude">
          <h2 className="text-xs font-semibold text-claude-muted uppercase tracking-wide mb-3">
            Equity Curve
          </h2>
          <EquityChart curve={curve} startingCapital={capital} />
        </div>

        {/* Tabs */}
        <div className="bg-claude-surface rounded-lg border border-claude-border shadow-claude">
          <div className="flex border-b border-claude-border">
            {[
              { id: "positions", label: "Positions",     badge: positions.length },
              { id: "approvals", label: "Approvals",     badge: approvals.length },
              { id: "trades",    label: "Trades",        badge: null },
              { id: "missed",    label: "Missed Trades", badge: missedTrades.length },
              { id: "risk",      label: "Risk Controls", badge: null },
            ].map(({ id, label, badge }) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={`px-4 py-2.5 text-xs font-medium border-b-2 transition-colors
                  ${tab === id
                    ? "border-claude-orange text-claude-orange"
                    : "border-transparent text-claude-muted hover:text-white"}`}
              >
                {label}
                {badge > 0 && (
                  <span className="ml-1.5 bg-claude-orange text-white text-xs px-1.5 py-0.5 rounded-full">
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
            {tab === "missed" && <MissedTradesTable missedTrades={missedTrades} />}
            {tab === "risk" && (
              <RiskControlsPanel riskControls={riskControls} onUpdate={updateRiskControls} />
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
