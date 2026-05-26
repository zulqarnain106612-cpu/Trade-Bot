
import React, { useState, useEffect, useRef, useCallback } from 'react'
import {
  LineChart, Line, AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts'

// ── colour palette ────────────────────────────────────────────────────────────
const C = {
  bg:       '#0a0a0f',
  panel:    '#12121a',
  border:   '#1e1e2e',
  accent:   '#6366f1',
  green:    '#22c55e',
  red:      '#ef4444',
  yellow:   '#f59e0b',
  muted:    '#64748b',
  text:     '#e2e8f0',
  textDim:  '#94a3b8',
}

// ── helpers ───────────────────────────────────────────────────────────────────
const fmt = (n, d=2) => (typeof n === 'number' ? n.toFixed(d) : '—')
const pct  = n => (typeof n === 'number' ? (n >= 0 ? '+' : '') + n.toFixed(2) + '%' : '—')
const clr  = n => n >= 0 ? C.green : C.red

// ── tiny components ───────────────────────────────────────────────────────────
const Panel = ({ children, style={} }) => (
  <div style={{ background: C.panel, border: `1px solid ${C.border}`, borderRadius: 8,
                padding: '12px 16px', ...style }}>
    {children}
  </div>
)

const Badge = ({ label, active, onClick, color=C.accent }) => (
  <button onClick={onClick} style={{
    background:   active ? color : 'transparent',
    border:       `1px solid ${active ? color : C.border}`,
    color:        active ? '#fff' : C.muted,
    borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
    fontSize: 12, fontWeight: 600, transition: 'all .15s',
  }}>{label}</button>
)

const Pill = ({ label, value, color }) => (
  <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:2 }}>
    <span style={{ fontSize:10, color: C.muted, textTransform:'uppercase', letterSpacing:1 }}>{label}</span>
    <span style={{ fontSize:15, fontWeight:700, color: color || C.text }}>{value}</span>
  </div>
)

const Toggle = ({ label, value, onChange }) => (
  <div style={{ display:'flex', alignItems:'center', gap:8 }}>
    <span style={{ fontSize:12, color:C.textDim }}>{label}</span>
    <div onClick={() => onChange(!value)} style={{
      width:36, height:20, borderRadius:10, cursor:'pointer',
      background: value ? C.accent : C.border, position:'relative', transition:'background .2s',
    }}>
      <div style={{
        position:'absolute', top:2, left: value ? 16 : 2,
        width:16, height:16, borderRadius:'50%', background:'#fff', transition:'left .2s',
      }}/>
    </div>
  </div>
)

const Slider = ({ label, value, min, max, step=0.001, onChange }) => (
  <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
    <div style={{ display:'flex', justifyContent:'space-between', fontSize:11 }}>
      <span style={{ color:C.textDim }}>{label}</span>
      <span style={{ color:C.text, fontWeight:600 }}>{(value*100).toFixed(1)}%</span>
    </div>
    <input type="range" min={min} max={max} step={step} value={value}
      onChange={e => onChange(parseFloat(e.target.value))}
      style={{ width:'100%', accentColor: C.accent }} />
  </div>
)

// ── main app ─────────────────────────────────────────────────────────────────
export default function App() {
  const ws        = useRef(null)
  const [status,  setStatus]  = useState({})
  const [trades,  setTrades]  = useState([])
  const [perf,    setPerf]    = useState([])
  const [signals, setSignals] = useState([])
  const [approvals, setApprovals] = useState([])
  const [connected, setConnected] = useState(false)

  // ── fetch initial data ────────────────────────────────────────────────────
  const fetchData = useCallback(async () => {
    try {
      const [t, p, s] = await Promise.all([
        fetch('/api/trades?limit=50').then(r=>r.json()),
        fetch('/api/performance?days=30').then(r=>r.json()),
        fetch('/api/status').then(r=>r.json()),
      ])
      setTrades(t)
      setPerf([...p].reverse())
      setStatus(s)
    } catch {}
  }, [])

  // ── WebSocket ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const connect = () => {
      const sock = new WebSocket(`ws://${location.host}/ws`)
      ws.current = sock

      sock.onopen  = () => { setConnected(true); fetchData() }
      sock.onclose = () => { setConnected(false); setTimeout(connect, 2000) }

      sock.onmessage = e => {
        const msg = JSON.parse(e.data)
        if (msg.type === 'status')          setStatus(msg.data)
        if (msg.type === 'signal')          setSignals(p => [msg.data, ...p].slice(0,50))
        if (msg.type === 'approval_request') setApprovals(p => [...p, msg.data])
        if (msg.type === 'trade_opened')     fetchData()
        if (msg.type === 'config_updated')   fetchData()
      }
    }
    connect()
    const iv = setInterval(fetchData, 10000)
    return () => { clearInterval(iv); ws.current?.close() }
  }, [fetchData])

  // ── config updater ────────────────────────────────────────────────────────
  const setConfig = useCallback(async (key, value) => {
    await fetch('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ key, value }),
    })
  }, [])

  // ── approval handlers ─────────────────────────────────────────────────────
  const resolveApproval = (signal_id, approved) => {
    ws.current?.send(JSON.stringify({ action: approved ? 'approve' : 'reject', signal_id }))
    setApprovals(p => p.filter(a => a.signal_id !== signal_id))
  }

  // ── resume ────────────────────────────────────────────────────────────────
  const resume = async () => {
    await fetch('/api/resume', { method:'POST' })
    fetchData()
  }

  // ── equity curve from trades ──────────────────────────────────────────────
  const equityCurve = perf.map(d => ({
    date:   d.date?.slice(5),
    equity: d.ending_equity,
    pnl:    d.pnl_pct * 100,
  }))

  // ── timeframe multi-select ────────────────────────────────────────────────
  const toggleTimeframe = tf => {
    const cur  = status.active_timeframes || []
    const next = cur.includes(tf) ? cur.filter(x=>x!==tf) : [...cur, tf]
    if (next.length === 0) return
    setConfig('active_timeframes', next)
    setStatus(s => ({ ...s, active_timeframes: next }))
  }

  // ── execution mode ────────────────────────────────────────────────────────
  const setMode = mode => {
    setConfig('execution_mode', mode)
    setStatus(s => ({ ...s, execution_mode: mode }))
  }

  const halted = status.halted

  return (
    <div style={{ minHeight:'100vh', background: C.bg, color: C.text, padding:16 }}>

      {/* ── header ──────────────────────────────────────────────────────── */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:16 }}>
        <div style={{ display:'flex', alignItems:'center', gap:10 }}>
          <div style={{ width:8, height:8, borderRadius:'50%',
            background: connected ? C.green : C.red,
            boxShadow: connected ? `0 0 8px ${C.green}` : 'none' }} />
          <span style={{ fontWeight:700, fontSize:18 }}>Trade-Bot</span>
          <span style={{ fontSize:11, color:C.muted }}>
            {status.trading_mode === 'live' ? '🔴 LIVE' : '🟡 PAPER'}
          </span>
        </div>
        <div style={{ display:'flex', gap:8, alignItems:'center' }}>
          {halted && (
            <button onClick={resume} style={{
              background:'transparent', border:`1px solid ${C.yellow}`,
              color: C.yellow, borderRadius:4, padding:'4px 12px',
              cursor:'pointer', fontSize:12, fontWeight:600,
            }}>⚠ HALTED — Resume</button>
          )}
          <span style={{ fontSize:11, color:C.muted }}>
            {status.engines_active || 0} engines · {status.pending_approvals || 0} pending
          </span>
        </div>
      </div>

      {/* ── approval banners ─────────────────────────────────────────────── */}
      {approvals.map(a => (
        <div key={a.signal_id} style={{
          background:`${C.yellow}18`, border:`1px solid ${C.yellow}`,
          borderRadius:8, padding:'10px 16px', marginBottom:10,
          display:'flex', alignItems:'center', justifyContent:'space-between',
        }}>
          <div style={{ fontSize:12 }}>
            <b style={{ color:C.yellow }}>APPROVAL REQUIRED</b>
            {'  '}{a.symbol} {a.direction?.toUpperCase()} ${a.notional?.toFixed(2)}
            {'  '}confidence: {(a.confidence*100).toFixed(1)}%
            {'  '}meta: {(a.meta_score*100).toFixed(1)}%
            {'  '}regime: {a.regime}
          </div>
          <div style={{ display:'flex', gap:8 }}>
            <button onClick={() => resolveApproval(a.signal_id, true)}
              style={{ background:C.green, border:'none', color:'#fff',
                borderRadius:4, padding:'4px 14px', cursor:'pointer', fontWeight:600 }}>
              Approve
            </button>
            <button onClick={() => resolveApproval(a.signal_id, false)}
              style={{ background:C.red, border:'none', color:'#fff',
                borderRadius:4, padding:'4px 14px', cursor:'pointer', fontWeight:600 }}>
              Skip
            </button>
          </div>
        </div>
      ))}

      {/* ── top metrics row ──────────────────────────────────────────────── */}
      <div style={{ display:'grid', gridTemplateColumns:'repeat(5,1fr)', gap:10, marginBottom:16 }}>
        <Panel><Pill label="Equity" value={`$${fmt(status.equity)}`} /></Panel>
        <Panel><Pill label="Session P&L" value={pct(status.session_pnl_pct)} color={clr(status.session_pnl_pct)} /></Panel>
        <Panel><Pill label="Regime" value={signals[0]?.regime || '—'} /></Panel>
        <Panel><Pill label="Confidence" value={signals[0] ? `${(signals[0].confidence*100).toFixed(1)}%` : '—'} /></Panel>
        <Panel><Pill label="Meta Gate" value={signals[0] ? `${(signals[0].meta_score*100).toFixed(1)}%` : '—'} /></Panel>
      </div>

      {/* ── main grid ────────────────────────────────────────────────────── */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 320px', gap:16 }}>

        {/* left column */}
        <div style={{ display:'flex', flexDirection:'column', gap:16 }}>

          {/* equity curve */}
          <Panel style={{ height:200 }}>
            <div style={{ fontSize:11, color:C.muted, marginBottom:8, fontWeight:600 }}>EQUITY CURVE (30 DAY)</div>
            <ResponsiveContainer width="100%" height={155}>
              <AreaChart data={equityCurve}>
                <defs>
                  <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor={C.accent} stopOpacity={0.3}/>
                    <stop offset="95%" stopColor={C.accent} stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fill:C.muted, fontSize:9 }} />
                <YAxis tick={{ fill:C.muted, fontSize:9 }} />
                <Tooltip contentStyle={{ background:C.panel, border:`1px solid ${C.border}`, fontSize:11 }}/>
                <Area type="monotone" dataKey="equity" stroke={C.accent} fill="url(#eq)" strokeWidth={2} dot={false}/>
              </AreaChart>
            </ResponsiveContainer>
          </Panel>

          {/* daily pnl bars */}
          <Panel style={{ height:160 }}>
            <div style={{ fontSize:11, color:C.muted, marginBottom:8, fontWeight:600 }}>DAILY P&L %</div>
            <ResponsiveContainer width="100%" height={115}>
              <AreaChart data={equityCurve}>
                <CartesianGrid stroke={C.border} strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fill:C.muted, fontSize:9 }} />
                <YAxis tick={{ fill:C.muted, fontSize:9 }} />
                <ReferenceLine y={0} stroke={C.border} />
                <Tooltip contentStyle={{ background:C.panel, border:`1px solid ${C.border}`, fontSize:11 }}/>
                <Area type="monotone" dataKey="pnl" stroke={C.green} fill={`${C.green}22`} strokeWidth={1.5} dot={false}/>
              </AreaChart>
            </ResponsiveContainer>
          </Panel>

          {/* trades table */}
          <Panel>
            <div style={{ fontSize:11, color:C.muted, marginBottom:8, fontWeight:600 }}>RECENT TRADES</div>
            <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11 }}>
              <thead>
                <tr style={{ color:C.muted }}>
                  {['Time','Symbol','TF','Dir','Entry','Exit','P&L','Status','Mode'].map(h => (
                    <th key={h} style={{ textAlign:'left', padding:'4px 6px', fontWeight:500 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.slice(0,20).map(t => (
                  <tr key={t.id} style={{ borderTop:`1px solid ${C.border}` }}>
                    <td style={{ padding:'4px 6px', color:C.muted }}>{t.ts_open?.slice(11,16)}</td>
                    <td style={{ padding:'4px 6px' }}>{t.symbol}</td>
                    <td style={{ padding:'4px 6px', color:C.muted }}>{t.timeframe}</td>
                    <td style={{ padding:'4px 6px', color: t.direction==='long' ? C.green : C.red }}>
                      {t.direction?.toUpperCase()}
                    </td>
                    <td style={{ padding:'4px 6px' }}>{fmt(t.entry_price,2)}</td>
                    <td style={{ padding:'4px 6px', color:C.muted }}>{t.exit_price ? fmt(t.exit_price,2) : '—'}</td>
                    <td style={{ padding:'4px 6px', color: t.pnl >= 0 ? C.green : C.red }}>
                      {t.pnl != null ? pct(t.pnl_pct*100) : '—'}
                    </td>
                    <td style={{ padding:'4px 6px', color:C.muted }}>{t.status}</td>
                    <td style={{ padding:'4px 6px', color: t.mode==='live' ? C.red : C.yellow }}>
                      {t.mode?.toUpperCase()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        </div>

        {/* right column — controls */}
        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>

          {/* execution mode */}
          <Panel>
            <div style={{ fontSize:11, color:C.muted, marginBottom:10, fontWeight:600 }}>EXECUTION MODE</div>
            <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
              {['automatic','restricted','manual'].map(m => (
                <button key={m} onClick={() => setMode(m)} style={{
                  background: status.execution_mode === m ? C.accent : 'transparent',
                  border: `1px solid ${status.execution_mode === m ? C.accent : C.border}`,
                  color: status.execution_mode === m ? '#fff' : C.muted,
                  borderRadius:4, padding:'6px 0', cursor:'pointer',
                  fontSize:12, fontWeight:600, textTransform:'uppercase',
                  letterSpacing:1, transition:'all .15s',
                }}>{m}</button>
              ))}
            </div>
          </Panel>

          {/* timeframes */}
          <Panel>
            <div style={{ fontSize:11, color:C.muted, marginBottom:10, fontWeight:600 }}>TIMEFRAMES</div>
            <div style={{ display:'flex', flexDirection:'column', gap:6 }}>
              {['scalping','intraday','swing'].map(tf => (
                <div key={tf} onClick={() => toggleTimeframe(tf)} style={{
                  display:'flex', alignItems:'center', justifyContent:'space-between',
                  padding:'6px 10px', borderRadius:4, cursor:'pointer',
                  border:`1px solid ${(status.active_timeframes||[]).includes(tf) ? C.accent : C.border}`,
                  background:(status.active_timeframes||[]).includes(tf) ? `${C.accent}18` : 'transparent',
                  transition:'all .15s',
                }}>
                  <span style={{ fontSize:12, fontWeight:600, textTransform:'uppercase', letterSpacing:1 }}>{tf}</span>
                  <div style={{
                    width:10, height:10, borderRadius:'50%',
                    background:(status.active_timeframes||[]).includes(tf) ? C.accent : C.border,
                  }}/>
                </div>
              ))}
              <div style={{ fontSize:10, color:C.muted, marginTop:4 }}>
                Real capital routes only to timeframes with confirmed positive expectancy.
                All active timeframes run in paper simultaneously.
              </div>
            </div>
          </Panel>

          {/* risk sliders */}
          <Panel>
            <div style={{ fontSize:11, color:C.muted, marginBottom:10, fontWeight:600 }}>RISK PARAMETERS</div>
            <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
              <Slider label="Daily Drawdown Halt"
                value={status.daily_drawdown_halt_pct ?? 0.02}
                min={0.005} max={0.1} step={0.005}
                onChange={v => setConfig('daily_drawdown_halt_pct', v)}
              />
              <Slider label="Max Position Size"
                value={status.max_position_pct ?? 0.05}
                min={0.005} max={0.25} step={0.005}
                onChange={v => setConfig('max_position_pct', v)}
              />
              <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:11 }}>
                  <span style={{ color:C.textDim }}>Restricted Limit (USD)</span>
                  <span style={{ color:C.text, fontWeight:600 }}>${status.restricted_notional_limit ?? 50}</span>
                </div>
                <input type="range" min={10} max={500} step={10}
                  value={status.restricted_notional_limit ?? 50}
                  onChange={e => setConfig('restricted_notional_limit', parseFloat(e.target.value))}
                  style={{ width:'100%', accentColor:C.accent }} />
              </div>
              <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
                <div style={{ display:'flex', justifyContent:'space-between', fontSize:11 }}>
                  <span style={{ color:C.textDim }}>Consec. Loss Halt</span>
                  <span style={{ color:C.text, fontWeight:600 }}>{status.consecutive_loss_halt ?? 3} trades</span>
                </div>
                <input type="range" min={2} max={10} step={1}
                  value={status.consecutive_loss_halt ?? 3}
                  onChange={e => setConfig('consecutive_loss_halt', parseInt(e.target.value))}
                  style={{ width:'100%', accentColor:C.accent }} />
              </div>
            </div>
          </Panel>

          {/* live signals feed */}
          <Panel style={{ flex:1 }}>
            <div style={{ fontSize:11, color:C.muted, marginBottom:8, fontWeight:600 }}>SIGNAL FEED</div>
            <div style={{ display:'flex', flexDirection:'column', gap:6, maxHeight:280, overflowY:'auto' }}>
              {signals.slice(0,15).map((s, i) => (
                <div key={i} style={{
                  padding:'6px 8px', borderRadius:4,
                  border:`1px solid ${s.direction==='long' ? `${C.green}44` : `${C.red}44`}`,
                  background: s.direction==='long' ? `${C.green}0a` : `${C.red}0a`,
                  fontSize:10,
                }}>
                  <div style={{ display:'flex', justifyContent:'space-between', marginBottom:2 }}>
                    <span style={{ fontWeight:700, color: s.direction==='long' ? C.green : C.red }}>
                      {s.direction?.toUpperCase()} {s.symbol}
                    </span>
                    <span style={{ color:C.muted }}>{s.timeframe}</span>
                  </div>
                  <div style={{ display:'flex', gap:8, color:C.muted }}>
                    <span>conf {(s.confidence*100).toFixed(0)}%</span>
                    <span>meta {(s.meta_score*100).toFixed(0)}%</span>
                    <span>kelly {(s.kelly_frac*100).toFixed(1)}%</span>
                    <span>{s.regime}</span>
                  </div>
                </div>
              ))}
              {signals.length === 0 && (
                <div style={{ color:C.muted, fontSize:11, textAlign:'center', paddingTop:20 }}>
                  Waiting for signals...
                </div>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}

