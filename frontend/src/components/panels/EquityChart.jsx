import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Area, AreaChart,
} from 'recharts';
import { fmt } from '../../utils/format';

export function EquityChart({ curve, startingCapital }) {
  if (!curve || curve.length === 0)
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 180, color: 'var(--c-faint)', fontSize: 12 }}>
        No equity data yet
      </div>
    );

  const data = curve.map((p) => ({
    t: new Date(p.ts).toLocaleTimeString(),
    equity: p.equity_usd,
    dd: p.drawdown_pct,
    dailyPnl: p.daily_pnl_usd,
  }));

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#00d4ff" stopOpacity={0.25} />
            <stop offset="100%" stopColor="#00d4ff" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1a181e" />
        <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#5e5a55' }} interval="preserveStartEnd" />
        <YAxis
          tick={{ fontSize: 9, fill: '#5e5a55' }}
          domain={['auto', 'auto']}
          tickFormatter={(v) => `$${fmt(v, 0)}`}
        />
        <Tooltip
          contentStyle={{
            background: '#0c0b0f',
            border: '1px solid #262329',
            fontSize: 11,
            borderRadius: 6,
          }}
          formatter={(v, n) => [n === 'equity' ? `$${fmt(v)}` : `${fmt(v, 3)}%`, n]}
        />
        {startingCapital && (
          <ReferenceLine y={startingCapital} stroke="#5e5a55" strokeDasharray="4 2" />
        )}
        <Area
          type="monotone"
          dataKey="equity"
          stroke="#00d4ff"
          fill="url(#equityGrad)"
          strokeWidth={2}
          dot={false}
          name="equity"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function DrawdownChart({ curve }) {
  if (!curve || curve.length === 0) return null;

  const data = curve.map((p) => ({
    t: new Date(p.ts).toLocaleTimeString(),
    dd: -(p.drawdown_pct || 0),
  }));

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ef4444" stopOpacity={0.3} />
            <stop offset="100%" stopColor="#ef4444" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1a181e" />
        <XAxis dataKey="t" tick={{ fontSize: 9, fill: '#5e5a55' }} interval="preserveStartEnd" />
        <YAxis
          tick={{ fontSize: 9, fill: '#5e5a55' }}
          tickFormatter={(v) => `${v.toFixed(2)}%`}
        />
        <Tooltip
          contentStyle={{ background: '#0c0b0f', border: '1px solid #262329', fontSize: 11, borderRadius: 6 }}
          formatter={(v) => [`${v.toFixed(3)}%`, 'Drawdown']}
        />
        <Area
          type="monotone"
          dataKey="dd"
          stroke="#ef4444"
          fill="url(#ddGrad)"
          strokeWidth={1.5}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
