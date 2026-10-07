/**
 * RES-018 — the runtime panel offers only what the change manager accepts and
 * shows desired/actual gaps, transitions and trace outcomes as served.
 */
import { describe, expect, it } from 'vitest';

import {
  HEALTH_COLOR,
  STATE_COLOR,
  changeRequestBody,
  countPairs,
  decisionsFor,
  describeTrace,
  describeTransition,
  desiredActualGaps,
} from './runtimeModel.js';

describe('decisionsFor', () => {
  it.each([
    [{ status: 'AWAITING_APPROVAL' }, ['approve', 'cancel']],
    [{ status: 'APPROVED', classification: 'LIVE_GATED' }, ['execute', 'cancel']],
    [{ status: 'APPROVED', classification: 'SHADOW_REQUIRED' }, ['cancel']],
    [{ status: 'APPROVED', classification: 'CANARY_REQUIRED' }, ['cancel']],
    [{ status: 'STAGED' }, ['execute', 'cancel']],
    [{ status: 'EXECUTED' }, ['promote', 'rollback']],
    [{ status: 'PROMOTED' }, []],
    [{ status: 'REJECTED' }, []],
  ])('%o -> %o', (change, verbs) => {
    expect(decisionsFor(change)).toEqual(verbs);
  });

  it('offers nothing without a change', () => {
    expect(decisionsFor(null)).toEqual([]);
  });
});

describe('desiredActualGaps', () => {
  it('lists state and version gaps, and nothing else', () => {
    const components = [
      { component_id: 'worker:a', state: 'ACTIVE', version: '1', desired_state: null },
      {
        component_id: 'worker:b',
        state: 'ACTIVE',
        version: '1',
        desired_state: { target_state: 'ACTIVE', target_version: null },
      },
      {
        component_id: 'worker:c',
        state: 'FAILED',
        version: '1',
        desired_state: { target_state: 'ACTIVE', target_version: null },
      },
      {
        component_id: 'model:m',
        state: 'ACTIVE',
        version: '2',
        desired_state: { target_state: 'ACTIVE', target_version: '1' },
      },
    ];
    expect(desiredActualGaps(components)).toEqual([
      { component_id: 'worker:c', desired: 'ACTIVE@1', actual: 'FAILED@1' },
      { component_id: 'model:m', desired: 'ACTIVE@1', actual: 'ACTIVE@2' },
    ]);
    expect(desiredActualGaps(undefined)).toEqual([]);
  });
});

describe('describeTransition / describeTrace', () => {
  it('names the step, its actor and a failure', () => {
    expect(describeTransition(null)).toBe('—');
    expect(
      describeTransition({ from_state: 'ACTIVE', to_state: 'DRAINED', action: 'DRAIN', actor: 'alice' }),
    ).toBe('ACTIVE → DRAINED (DRAIN by alice)');
    expect(
      describeTransition({ from_state: null, to_state: 'FAILED', action: 'START', ok: false }),
    ).toBe('new → FAILED (START FAILED)');
  });

  it('says what stopped a decision', () => {
    expect(describeTrace(null)).toBe('');
    expect(describeTrace({ final_decision: 'FILLED', first_blocking: null })).toBe('FILLED');
    expect(
      describeTrace({
        final_decision: 'REJECTED',
        first_blocking: { stage: 'risk', gate: 'drawdown', reason: 'dd 9%' },
      }),
    ).toBe('REJECTED at risk (drawdown: dd 9%)');
    expect(
      describeTrace({ final_decision: 'NO_TRADE', first_blocking: { stage: 'signal', gate: 'signal' } }),
    ).toBe('NO_TRADE at signal (signal)');
  });
});

describe('changeRequestBody', () => {
  it.each([
    [{}, 'component id is required'],
    [{ component_id: 'worker:w' }, 'action is required'],
    [{ component_id: 'worker:w', action: 'drain' }, 'a change needs a reason'],
    [{ component_id: 'worker:w', action: 'replace', reason: 'x' }, 'REPLACE needs a target version'],
  ])('refuses %o', (form, error) => {
    expect(changeRequestBody(form)).toEqual({ error });
  });

  it('builds the body the endpoint takes', () => {
    expect(
      changeRequestBody({
        component_id: ' worker:w ',
        action: 'replace',
        reason: ' upgrade ',
        target_version: '2',
        expected_version: '1',
      }),
    ).toEqual({
      body: {
        component_id: 'worker:w',
        action: 'REPLACE',
        reason: 'upgrade',
        target_version: '2',
        expected_version: '1',
      },
    });
    expect(changeRequestBody({ component_id: 'w', action: 'drain', reason: 'r' })).toEqual({
      body: { component_id: 'w', action: 'DRAIN', reason: 'r' },
    });
  });
});

describe('palette and counts', () => {
  it('colours every state and health the server reports', () => {
    for (const s of ['DISCOVERED', 'VALIDATED', 'INITIALIZED', 'STANDBY', 'ACTIVE', 'PAUSED',
      'DRAINED', 'STOPPED', 'FAILED', 'QUARANTINED']) {
      expect(STATE_COLOR[s]).toBeTruthy();
    }
    for (const h of ['UNKNOWN', 'HEALTHY', 'DEGRADED', 'UNHEALTHY']) {
      expect(HEALTH_COLOR[h]).toBeTruthy();
    }
  });

  it('sorts count pairs', () => {
    expect(countPairs({ worker: 2, model: 1 })).toEqual(['model: 1', 'worker: 2']);
    expect(countPairs(null)).toEqual([]);
  });
});
