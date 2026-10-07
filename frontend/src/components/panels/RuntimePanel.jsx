import { useState } from 'react';
import { usePolling } from '../../hooks/useApi';
import {
  HEALTH_COLOR,
  STATE_COLOR,
  changeRequestBody,
  countPairs,
  decisionsFor,
  describeTrace,
  describeTransition,
  desiredActualGaps,
} from './runtimeModel';

/**
 * The runtime platform (src/runtime), as GET /runtime* serves it: components
 * with desired vs actual state, the dependency order, change history with the
 * decisions the change manager will accept, decision traces, and the Claude
 * session task and branch-audit ledgers.
 *
 * Every write goes through `operatorAction` -- the operator second factor --
 * to /runtime/changes, which the change manager classifies and gates. The
 * panel has no path to a component of its own.
 */

const VIEWS = ['overview', 'components', 'dependencies', 'changes', 'traces', 'agent'];

const cell = { padding: '3px 6px', fontSize: 11, borderBottom: '1px solid var(--c-border)' };
const head = { ...cell, color: 'var(--c-faint)', textAlign: 'left', fontWeight: 600 };

function Dot({ color }) {
  return (
    <span
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: 4,
        background: color ?? 'var(--c-faint)',
        marginRight: 6,
      }}
    />
  );
}

function Empty({ children }) {
  return <div style={{ color: 'var(--c-faint)', fontSize: 12, padding: 12 }}>{children}</div>;
}

function Overview({ overview, components }) {
  if (!overview) return <Empty>Runtime platform not reachable.</Empty>;
  const gaps = desiredActualGaps(components);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: 12 }}>
      <div>{overview.components} components — {countPairs(overview.by_state).join(' · ')}</div>
      <div style={{ color: 'var(--c-muted)' }}>{countPairs(overview.by_type).join(' · ')}</div>
      <div>
        <strong>Desired ≠ actual:</strong> {gaps.length === 0 ? 'none' : ''}
        {gaps.map((g) => (
          <div key={g.component_id} style={{ fontFamily: 'monospace', fontSize: 11 }}>
            {g.component_id}: wants {g.desired}, is {g.actual}
          </div>
        ))}
      </div>
      <div>
        <strong>Open changes:</strong> {overview.open_changes.length}
        {' · '}
        <strong>Dependency issues:</strong> {overview.dependency_issues.length}
      </div>
      {overview.dependency_issues.map((issue) => (
        <div key={issue} style={{ color: 'var(--c-red)', fontSize: 11 }}>{issue}</div>
      ))}
    </div>
  );
}

function Components({ components }) {
  const [selected, setSelected] = useState(null);
  if (!components || components.length === 0) return <Empty>No runtime components.</Empty>;
  const chosen = components.find((c) => c.component_id === selected);
  return (
    <div>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            {['component', 'version', 'state', 'desired', 'health', 'restarts', 'last transition'].map(
              (h) => <th key={h} style={head}>{h}</th>,
            )}
          </tr>
        </thead>
        <tbody>
          {components.map((c) => (
            <tr
              key={c.component_id}
              onClick={() => setSelected(c.component_id)}
              style={{ cursor: 'pointer', background: c.component_id === selected ? 'var(--c-surface2)' : undefined }}
            >
              <td style={cell}>{c.component_id}</td>
              <td style={cell}>{c.version}</td>
              <td style={cell}><Dot color={STATE_COLOR[c.state]} />{c.state}</td>
              <td style={cell}>{c.desired_state ? c.desired_state.target_state : '—'}</td>
              <td style={cell} title={c.health.detail}>
                <Dot color={HEALTH_COLOR[c.health.state]} />{c.health.state}
              </td>
              <td style={cell}>{c.restart_count}</td>
              <td style={cell}>{describeTransition(c.last_transition)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {chosen && (
        <div style={{ marginTop: 8, fontSize: 11, fontFamily: 'monospace' }}>
          <div>implementation: {chosen.implementation}</div>
          <div>capabilities: {chosen.capabilities.actions.join(', ') || 'observe-only'}</div>
          <div>depends on: {chosen.dependencies.map((d) => d.component_id).join(', ') || '—'}</div>
          <div>depended on by: {chosen.dependents.join(', ') || '—'}</div>
          {chosen.last_error && <div style={{ color: 'var(--c-red)' }}>last error: {chosen.last_error}</div>}
        </div>
      )}
    </div>
  );
}

function Dependencies({ graph }) {
  if (!graph) return <Empty>No dependency data.</Empty>;
  return (
    <div style={{ fontSize: 11, fontFamily: 'monospace' }}>
      {graph.cycle && <div style={{ color: 'var(--c-red)' }}>cycle: {graph.cycle.join(' → ')}</div>}
      <div>start order: {graph.order.join(' → ') || '—'}</div>
      {graph.edges.map((e) => (
        <div key={`${e.from}->${e.to}`}>
          {e.from} → {e.to}{e.version ? ` @${e.version}` : ''}
        </div>
      ))}
      {graph.issues.map((i) => <div key={i} style={{ color: 'var(--c-red)' }}>{i}</div>)}
    </div>
  );
}

function ChangeConsole({ onSubmit }) {
  const [form, setForm] = useState({ component_id: '', action: '', reason: '', target_version: '' });
  const [error, setError] = useState('');
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const submit = async () => {
    const { body, error: problem } = changeRequestBody(form);
    setError(problem ?? '');
    if (body) await onSubmit(body);
  };
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
      <input placeholder="component id" value={form.component_id} onChange={set('component_id')} />
      <input placeholder="action" value={form.action} onChange={set('action')} />
      <input placeholder="target version" value={form.target_version} onChange={set('target_version')} />
      <input placeholder="reason" value={form.reason} onChange={set('reason')} />
      <button className="btn" onClick={submit}>REQUEST</button>
      {error && <span style={{ color: 'var(--c-red)', fontSize: 11 }}>{error}</span>}
    </div>
  );
}

function Changes({ changes, onDecide, onSubmit }) {
  return (
    <div>
      <ChangeConsole onSubmit={onSubmit} />
      {(!changes || changes.length === 0) && <Empty>No runtime changes yet.</Empty>}
      {(changes ?? []).slice().reverse().map((c) => (
        <div key={c.change_id} style={{ ...cell, display: 'flex', justifyContent: 'space-between', gap: 8 }}>
          <span>
            <strong>{c.status}</strong> {c.action} {c.component_id} [{c.classification}]
            {' '}by {c.actor.name} ({c.actor.kind}) — {c.reason}
            {c.result && <span style={{ color: 'var(--c-muted)' }}> · {c.result}</span>}
            {c.rollback && <span style={{ color: 'var(--c-red)' }}> · {c.rollback}</span>}
          </span>
          <span style={{ display: 'flex', gap: 4 }}>
            {decisionsFor(c).map((verb) => (
              <button key={verb} className="btn" onClick={() => onDecide(c.change_id, verb)}>
                {verb.toUpperCase()}
              </button>
            ))}
          </span>
        </div>
      ))}
    </div>
  );
}

function Traces({ traces }) {
  if (!traces || traces.length === 0) return <Empty>No decision traces yet.</Empty>;
  return (
    <div>
      {traces.map((t) => (
        <div key={t.trace_id} style={cell}>
          <span style={{ fontFamily: 'monospace' }}>{t.trace_id}</span>{' '}
          {describeTrace(t)} — {t.stages.join(' → ')}
        </div>
      ))}
    </div>
  );
}

function AgentLedgers({ ledgers }) {
  if (!ledgers) return <Empty>No agent ledgers.</Empty>;
  return (
    <div style={{ fontSize: 11 }}>
      {ledgers.tasks.map((t) => (
        <div key={t.task_id} style={cell}>
          <strong>{t.task_id}</strong> {t.status}/{t.phase} on {t.branch} @{t.current_sha.slice(0, 8)} —
          {' '}{t.steps_done}/{t.steps_total} steps · next: {t.next_action || '—'}
        </div>
      ))}
      {ledgers.branch_audit.map((e) => (
        <div key={e.audit_id ?? e.branch_name} style={cell}>
          {e.branch_name} @{String(e.audited_head_sha ?? '').slice(0, 8)}: {e.completion_status}
        </div>
      ))}
      {ledgers.unreadable.map((u) => <div key={u} style={{ color: 'var(--c-red)' }}>{u}</div>)}
    </div>
  );
}

export function RuntimePanel({ operatorAction }) {
  const [view, setView] = useState('overview');
  const overview = usePolling('/runtime', 5000);
  const components = usePolling('/runtime/components', 5000);
  const graph = usePolling('/runtime/dependencies', 30000);
  const changes = usePolling('/runtime/changes', 5000);
  const traces = usePolling('/runtime/traces?limit=20', 10000);
  const ledgers = usePolling('/runtime/agent', 30000);

  const onDecide = (changeId, verb) =>
    operatorAction(`/runtime/changes/${encodeURIComponent(changeId)}/${verb}`, 'POST', { reason: '' });
  const onSubmit = (body) => operatorAction('/runtime/changes', 'POST', body);

  return (
    <div>
      <div style={{ display: 'flex', gap: 4, marginBottom: 8 }}>
        {VIEWS.map((v) => (
          <button
            key={v}
            className="btn"
            style={{ opacity: v === view ? 1 : 0.6 }}
            onClick={() => setView(v)}
          >
            {v}
          </button>
        ))}
      </div>
      {view === 'overview' && <Overview overview={overview} components={components} />}
      {view === 'components' && <Components components={components} />}
      {view === 'dependencies' && <Dependencies graph={graph} />}
      {view === 'changes' && <Changes changes={changes} onDecide={onDecide} onSubmit={onSubmit} />}
      {view === 'traces' && <Traces traces={traces} />}
      {view === 'agent' && <AgentLedgers ledgers={ledgers} />}
    </div>
  );
}
