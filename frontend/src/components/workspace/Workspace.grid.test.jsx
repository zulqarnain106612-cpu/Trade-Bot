/**
 * TERM-011 — pointer placement through the real grid.
 *
 * Workspace.test.jsx replaces react-grid-layout with a stub, so it decides
 * the contract around the grid but never the pointer path itself. Here the
 * installed react-grid-layout, react-draggable and react-resizable run for
 * real: a mouse press on the header, moves on the document and a release go
 * through the grid's drag threshold, collision handling and compaction into
 * Workspace's commit, the App-style state and localStorage.
 *
 * jsdom has no layout engine, so exactly two measurements are supplied the
 * way a browser would: the container width (react-grid-layout's own
 * useContainerWidth reads offsetWidth, always 0 here) and offsetParent
 * (always null in jsdom, which makes the grid ignore every drag). Item
 * rectangles are read back from the transform the grid itself rendered.
 */
import { useCallback, useEffect, useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  GRID_COLS, WORKSPACE_STORAGE_KEY, compactLayout, loadLayout, moveBy, resizeBy, saveLayout,
  toggleMinimized,
} from '../../workspace/layoutStore.js';
import { Panel } from '../Panel.jsx';
import { STACK_BELOW_PX, Workspace } from './Workspace.jsx';

const grid = vi.hoisted(() => ({ width: 1200 }));
vi.mock('react-grid-layout', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useContainerWidth: () => ({ width: grid.width, containerRef: () => {}, mounted: true }),
  };
});

const SPECS = [
  { id: 'a', w: 12, h: 4, minW: 6, minH: 3 },
  { id: 'b', w: 12, h: 6, minW: 6, minH: 4 },
  { id: 'c', w: 24, h: 3, minW: 8, minH: 2 },
];
// At 1200px: (1200 - 23 * 12) / 24 = 38.5px columns, so one column step is
// 50.5px; one row step is ROW_HEIGHT + margin = 32px.
const COL_STEP = 50.5;
const ROW_STEP = 32;

const TRANSLATE = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/;
const parentDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent');
const rectDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'getBoundingClientRect');

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get() { return this.parentElement; },
  });
  Object.defineProperty(Element.prototype, 'getBoundingClientRect', {
    configurable: true,
    value() {
      const match = TRANSLATE.exec(this.style?.transform ?? '');
      const left = match ? Number(match[1]) : 0;
      const top = match ? Number(match[2]) : 0;
      const width = parseFloat(this.style?.width) || 0;
      const height = parseFloat(this.style?.height) || 0;
      return { left, top, x: left, y: top, width, height, right: left + width, bottom: top + height };
    },
  });
});

afterAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', parentDescriptor);
  Object.defineProperty(Element.prototype, 'getBoundingClientRect', rectDescriptor);
});

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)); },
    saved: () => JSON.parse(values.get(WORKSPACE_STORAGE_KEY)),
  };
}

// The same wiring as App.jsx's Dashboard: one layout state, compacted on
// every change, saved whenever it changes, the grid's commits passed to the
// raw setter and the keyboard/controls through updateLayout.
function Harness({ storage }) {
  const [layout, setLayout] = useState(() => compactLayout(loadLayout(SPECS, storage), SPECS));
  const updateLayout = useCallback(
    (change) => setLayout((current) => compactLayout(change(current), SPECS)),
    [],
  );
  useEffect(() => { saveLayout(layout, storage); }, [layout, storage]);
  return (
    <Workspace
      specs={SPECS}
      layout={layout}
      onChange={setLayout}
      renderPanel={(id) => {
        const spec = SPECS.find((s) => s.id === id);
        return (
          <Panel
            title={`Panel ${id}`}
            minimized={layout.items[id].minimized}
            onMinimize={() => updateLayout((l) => toggleMinimized(l, id, spec))}
            onMove={(dx, dy) => updateLayout((l) => moveBy(l, id, dx, dy))}
            onResize={(dw, dh) => updateLayout((l) => resizeBy(l, id, dw, dh, spec))}
          >
            <input aria-label={`notes ${id}`} />
          </Panel>
        );
      }}
    />
  );
}

const item = (container, id) => container.querySelector(`[data-ws-id="${id}"]`);
const handleOf = (container, id) => item(container, id).querySelector('.panel-title');

function position(element) {
  const match = TRANSLATE.exec(element.style.transform);
  return match ? [Number(match[1]), Number(match[2])] : null;
}

// Press, travel in whole-pixel steps (the way pointer events arrive), release.
function drag(target, [x0, y0], [dx, dy], steps = 3) {
  fireEvent.mouseDown(target, { button: 0, clientX: x0, clientY: y0 });
  for (let i = 1; i <= steps; i += 1) {
    fireEvent.mouseMove(document, {
      clientX: x0 + Math.round((dx * i) / steps),
      clientY: y0 + Math.round((dy * i) / steps),
    });
  }
  fireEvent.mouseUp(document, { clientX: x0 + dx, clientY: y0 + dy });
}

function overlaps(items) {
  const list = Object.values(items).filter((i) => !i.hidden);
  return list.some((p, n) => list.slice(n + 1).some((q) => (
    p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h
  )));
}

beforeEach(() => {
  grid.width = 1200;
});

describe('Workspace pointer placement (real react-grid-layout)', () => {
  it('dragging a panel by its header moves it, commits the drop and saves it', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    expect(position(item(container, 'a'))).toEqual([0, 0]);

    drag(handleOf(container, 'a'), [40, 10], [12 * COL_STEP, 0]);

    const saved = storage.saved().items;
    expect([saved.a.x, saved.a.y, saved.a.w, saved.a.h]).toEqual([12, 0, 12, 4]);
    expect(overlaps(saved)).toBe(false);
    expect(position(item(container, 'a'))).toEqual([606, 0]);
    // The panel it was dropped on moved out of the way, not under it.
    expect(saved.b.x).toBe(12);
    expect(saved.b.y).toBeGreaterThanOrEqual(4);
  });

  it('a reload restores the saved placement', () => {
    const storage = memoryStorage();
    const first = render(<Harness storage={storage} />);
    drag(handleOf(first.container, 'c'), [40, 6 * ROW_STEP + 10], [0, -6 * ROW_STEP]);
    const placed = storage.saved().items;
    expect(placed.c.y).toBe(0);
    first.unmount();

    const { container } = render(<Harness storage={storage} />);
    expect(position(item(container, 'c'))).toEqual([0, 0]);
    expect(storage.saved().items).toEqual(placed);
  });

  it('a press on a header control, the body or an input never moves the panel', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    const before = storage.saved();
    for (const target of [
      screen.getByRole('button', { name: 'Minimize Panel a' }),
      screen.getByLabelText('notes a'),
      item(container, 'a').querySelector('.panel-body'),
    ]) {
      drag(target, [40, 10], [12 * COL_STEP, 3 * ROW_STEP]);
    }
    expect(storage.saved()).toEqual(before);
    expect(position(item(container, 'a'))).toEqual([0, 0]);
  });

  it('the keyboard grip is part of the handle and starts a drag too', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    drag(screen.getByRole('button', { name: 'Move or resize Panel a' }), [4, 10], [12 * COL_STEP, 0]);
    expect(storage.saved().items.a.x).toBe(12);
    expect(position(item(container, 'a'))).toEqual([606, 0]);
  });

  it('a press that stays inside the drag threshold is a click, not a move', () => {
    const storage = memoryStorage();
    const onChange = vi.fn();
    const { container } = render(
      <Workspace
        specs={SPECS}
        layout={loadLayout(SPECS, storage)}
        onChange={onChange}
        renderPanel={(id) => <Panel title={`Panel ${id}`} onMove={() => {}}>{id}</Panel>}
      />,
    );
    drag(handleOf(container, 'a'), [40, 10], [2, 2], 1);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('the panel content is the same element before and after a drag', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    const input = screen.getByLabelText('notes a');
    fireEvent.change(input, { target: { value: 'BTC' } });
    drag(handleOf(container, 'a'), [40, 10], [12 * COL_STEP, 0]);
    expect(storage.saved().items.a.x).toBe(12);
    expect(screen.getByLabelText('notes a')).toBe(input);
    expect(input.value).toBe('BTC');
  });

  it('resizing from the south-east handle commits the new size', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    const handle = item(container, 'a').querySelector(':scope > .react-resizable-handle-se');
    // 594x116px is 12x4; +101/+64px is two more columns and rows.
    drag(handle, [594, 116], [2 * COL_STEP, 2 * ROW_STEP], 1);
    const saved = storage.saved().items;
    expect([saved.a.x, saved.a.y, saved.a.w, saved.a.h]).toEqual([0, 0, 14, 6]);
    expect(overlaps(saved)).toBe(false);
  });

  it('keyboard placement still works, and a later drag does not revert it', () => {
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    const grip = screen.getByRole('button', { name: 'Move or resize Panel a' });
    fireEvent.keyDown(grip, { key: 'ArrowRight' });
    fireEvent.keyDown(grip, { key: 'ArrowRight', shiftKey: true });
    let saved = storage.saved().items;
    expect([saved.a.x, saved.a.w]).toEqual([1, 13]);
    expect(position(item(container, 'a'))).toEqual([Math.round(COL_STEP), 0]);

    // The grid's own commit carries every item; the keyboard change made
    // just before must be in it rather than reverted by a stale report.
    const [cx, cy] = position(item(container, 'c'));
    drag(handleOf(container, 'c'), [cx + 40, cy + 10], [0, ROW_STEP]);
    saved = storage.saved().items;
    expect([saved.a.x, saved.a.y, saved.a.w]).toEqual([1, 0, 13]);
    expect(overlaps(saved)).toBe(false);
  });

  it('stacked on a narrow screen: full width, and a drag does nothing', () => {
    grid.width = STACK_BELOW_PX - 60;
    const storage = memoryStorage();
    const { container } = render(<Harness storage={storage} />);
    const before = storage.saved();
    for (const id of ['a', 'b', 'c']) {
      const element = item(container, id);
      expect(position(element)[0]).toBe(0);
      expect(element.style.width).toBe(`${grid.width}px`);
      // react-resizable keeps its handles in the DOM and hides them by class.
      expect(element.classList.contains('react-resizable-hide')).toBe(true);
      expect(element.classList.contains('react-draggable')).toBe(false);
    }
    drag(handleOf(container, 'b'), [40, 10], [300, 200]);
    expect(storage.saved()).toEqual(before);
    expect(before.items.b.x).toBe(GRID_COLS / 2);
  });
});
