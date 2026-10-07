/**
 * RES-018 — what the runtime panel shows, computed without React.
 *
 * Everything here reads the /runtime payloads as the server renders them
 * (src/runtime, src/api/runtime_control.py) and decides nothing the server
 * has not: which decision buttons a change offers follows its status and
 * classification exactly as the change manager will accept them, so the
 * panel never offers a click the backend is going to refuse.
 */

export const STATE_COLOR = {
  ACTIVE: 'var(--c-green)',
  STANDBY: 'var(--c-cyan)',
  PAUSED: 'var(--c-yellow, #eab308)',
  DRAINED: 'var(--c-muted)',
  STOPPED: 'var(--c-faint)',
  FAILED: 'var(--c-red)',
  QUARANTINED: 'var(--c-red)',
  DISCOVERED: 'var(--c-faint)',
  VALIDATED: 'var(--c-faint)',
  INITIALIZED: 'var(--c-faint)',
};

export const HEALTH_COLOR = {
  HEALTHY: 'var(--c-green)',
  DEGRADED: 'var(--c-yellow, #eab308)',
  UNHEALTHY: 'var(--c-red)',
  UNKNOWN: 'var(--c-faint)',
};

const STAGED_CLASSES = new Set(['SHADOW_REQUIRED', 'CANARY_REQUIRED']);

/** The decision verbs the change manager accepts for a change right now. */
export function decisionsFor(change) {
  if (!change) return [];
  switch (change.status) {
    case 'AWAITING_APPROVAL':
      return ['approve', 'cancel'];
    case 'APPROVED':
      // A staged class waits for its shadow/canary result, recorded by the
      // evaluator that ran it -- not by a button here.
      return STAGED_CLASSES.has(change.classification) ? ['cancel'] : ['execute', 'cancel'];
    case 'STAGED':
      return ['execute', 'cancel'];
    case 'EXECUTED':
      return ['promote', 'rollback'];
    default:
      return [];
  }
}

/** Components whose desired state differs from what they are doing. */
export function desiredActualGaps(components) {
  return (components ?? [])
    .filter((c) => c.desired_state && (
      c.desired_state.target_state !== c.state
      || (c.desired_state.target_version && c.desired_state.target_version !== c.version)
    ))
    .map((c) => ({
      component_id: c.component_id,
      desired: `${c.desired_state.target_state}@${c.desired_state.target_version ?? c.version}`,
      actual: `${c.state}@${c.version}`,
    }));
}

/** "ACTIVE → DRAINED (DRAIN by alice)" for the last transition, or "—". */
export function describeTransition(t) {
  if (!t) return '—';
  const from = t.from_state ?? 'new';
  const by = t.actor ? ` by ${t.actor}` : '';
  const what = t.ok === false ? `${t.action} FAILED` : t.action;
  return `${from} → ${t.to_state} (${what}${by})`;
}

/** One line per trace: final decision, and what stopped it when something did. */
export function describeTrace(trace) {
  if (!trace) return '';
  const block = trace.first_blocking;
  if (!block) return trace.final_decision;
  const reason = block.reason ? `: ${block.reason}` : '';
  return `${trace.final_decision} at ${block.stage} (${block.gate}${reason})`;
}

/**
 * The POST /runtime/changes body for a console form, or an error string.
 * Mirrors the server's own refusals so a typo is caught before a round trip.
 */
export function changeRequestBody(form) {
  const componentId = (form.component_id ?? '').trim();
  const action = (form.action ?? '').trim().toUpperCase();
  const reason = (form.reason ?? '').trim();
  if (!componentId) return { error: 'component id is required' };
  if (!action) return { error: 'action is required' };
  if (!reason) return { error: 'a change needs a reason' };
  const targetVersion = (form.target_version ?? '').trim();
  if (action === 'REPLACE' && !targetVersion) return { error: 'REPLACE needs a target version' };
  const body = { component_id: componentId, action, reason };
  if (targetVersion) body.target_version = targetVersion;
  const expected = (form.expected_version ?? '').trim();
  if (expected) body.expected_version = expected;
  return { body };
}

/** Counts as "label: n" pairs, sorted by label, for the overview strip. */
export function countPairs(counts) {
  return Object.entries(counts ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([label, n]) => `${label}: ${n}`);
}
