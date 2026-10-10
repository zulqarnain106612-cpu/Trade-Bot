/**
 * TERM-010 — the workspace layout is versioned, validated and repairable.
 *
 * Every rule the dashboard relies on lives in pure functions over one plain
 * object, so they are decided here without a DOM: a corrupt or older stored
 * layout must never leave a panel unreachable, maximize/minimize must restore
 * the previous geometry, and no operation may push a panel outside the grid
 * or below its minimum size.
 */
import { describe, expect, it } from 'vitest';

import {
  GRID_COLS,
  LAYOUT_VERSION,
  LEGACY_VISIBILITY_KEY,
  MAX_H,
  MINIMIZED_H,
  WORKSPACE_STORAGE_KEY,
  applyGridLayout,
  compactLayout,
  defaultLayout,
  gridItemsFor,
  loadLayout,
  moveBy,
  resizeBy,
  sanitizeLayout,
  saveLayout,
  setAllHidden,
  setHidden,
  setMaximized,
  toggleMinimized,
} from './layoutStore.js';
import { WORKSPACE_ITEMS } from './items.js';

const SPECS = [
  { id: 'a', w: 12, h: 4, minW: 6, minH: 3 },
  { id: 'b', w: 12, h: 6, minW: 6, minH: 4 },
  { id: 'c', w: 24, h: 3, minW: 8, minH: 2 },
];
const spec = (id) => SPECS.find((s) => s.id === id);

/** A Storage stand-in; `failing` makes every call throw like a locked-down profile. */
function memoryStorage(initial = {}, { failing = false } = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem(key) {
      if (failing) throw new Error('SecurityError');
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      if (failing) throw new Error('QuotaExceededError');
      data.set(key, String(value));
    },
  };
}

describe('defaultLayout', () => {
  it('fills rows left to right and wraps when a panel does not fit', () => {
    const layout = defaultLayout(SPECS);
    expect(layout.version).toBe(LAYOUT_VERSION);
    expect(layout.maximized).toBeNull();
    expect(layout.items.a).toMatchObject({ x: 0, y: 0, w: 12, h: 4, hidden: false });
    expect(layout.items.b).toMatchObject({ x: 12, y: 0, w: 12, h: 6 });
    // The tallest panel of the first row decides where the second starts.
    expect(layout.items.c).toMatchObject({ x: 0, y: 6, w: 24, h: 3 });
  });

  it('gives every real panel a place inside the grid', () => {
    const layout = defaultLayout(WORKSPACE_ITEMS);
    for (const item of WORKSPACE_ITEMS) {
      const placed = layout.items[item.id];
      expect(placed.x + placed.w).toBeLessThanOrEqual(GRID_COLS);
      expect(placed.w).toBeGreaterThanOrEqual(item.minW);
      expect(placed.h).toBeGreaterThanOrEqual(item.minH);
    }
  });
});

describe('sanitizeLayout', () => {
  it.each([null, 'text', 42, { version: 0, items: {} }, { version: LAYOUT_VERSION + 1, items: {} }])(
    'falls back to the defaults for %j',
    (raw) => {
      expect(sanitizeLayout(raw, SPECS)).toEqual(defaultLayout(SPECS));
    },
  );

  it('repairs one bad entry without losing the others', () => {
    const raw = {
      version: LAYOUT_VERSION,
      items: {
        a: { x: 'left', y: 0, w: 12, h: 4 },
        b: { x: 0, y: 30, w: 8, h: 5, hidden: true },
      },
      maximized: null,
    };
    const layout = sanitizeLayout(raw, SPECS);
    expect(layout.items.a).toEqual(defaultLayout(SPECS).items.a);
    expect(layout.items.b).toMatchObject({ x: 0, y: 30, w: 8, h: 5, hidden: true });
    // A panel missing from the stored layout (added in a later build) appears.
    expect(layout.items.c).toEqual(defaultLayout(SPECS).items.c);
  });

  it('clamps sizes and positions back inside the grid and above the minimums', () => {
    const raw = {
      version: LAYOUT_VERSION,
      items: { a: { x: 40, y: -5, w: 1, h: 9999 }, b: { x: 3, y: 0, w: 99, h: 1 } },
    };
    const layout = sanitizeLayout(raw, SPECS);
    expect(layout.items.a).toMatchObject({ x: GRID_COLS - 6, y: 0, w: 6, h: MAX_H });
    expect(layout.items.b).toMatchObject({ x: 0, w: GRID_COLS, h: 4 });
  });

  it('keeps a minimized panel a header strip that remembers a usable height', () => {
    const raw = {
      version: LAYOUT_VERSION,
      items: {
        a: { x: 0, y: 0, w: 12, h: 30, minimized: true },
        b: { x: 12, y: 0, w: 12, h: 2, minimized: true, restoreH: 1 },
        c: { x: 0, y: 9, w: 24, h: 3, restoreH: 10 },
      },
    };
    const layout = sanitizeLayout(raw, SPECS);
    expect(layout.items.a).toMatchObject({ h: MINIMIZED_H, minimized: true, restoreH: 4 });
    expect(layout.items.b).toMatchObject({ h: MINIMIZED_H, restoreH: 4 });
    // restoreH only means something while minimized.
    expect(layout.items.c.restoreH).toBeNull();
  });

  it.each([
    ['an unknown panel', 'zzz'],
    ['a hidden panel', 'b'],
    ['a non-string', 7],
  ])('drops a maximized reference to %s', (_label, maximized) => {
    const raw = {
      version: LAYOUT_VERSION,
      items: { b: { x: 0, y: 0, w: 12, h: 6, hidden: true } },
      maximized,
    };
    expect(sanitizeLayout(raw, SPECS).maximized).toBeNull();
  });

  it('keeps a valid maximized panel', () => {
    const raw = { ...defaultLayout(SPECS), maximized: 'a' };
    expect(sanitizeLayout(raw, SPECS).maximized).toBe('a');
  });
});

describe('loadLayout and saveLayout', () => {
  it('round-trips what was saved', () => {
    const storage = memoryStorage();
    const layout = moveBy(defaultLayout(SPECS), 'a', 0, 3);
    expect(saveLayout(layout, storage)).toBe(true);
    expect(JSON.parse(storage.data.get(WORKSPACE_STORAGE_KEY)).version).toBe(LAYOUT_VERSION);
    expect(loadLayout(SPECS, storage)).toEqual(layout);
  });

  it('survives an unparseable stored value', () => {
    const storage = memoryStorage({ [WORKSPACE_STORAGE_KEY]: '{not json' });
    expect(loadLayout(SPECS, storage)).toEqual(defaultLayout(SPECS));
  });

  it('survives storage that refuses every call', () => {
    const storage = memoryStorage({}, { failing: true });
    expect(loadLayout(SPECS, storage)).toEqual(defaultLayout(SPECS));
    expect(saveLayout(defaultLayout(SPECS), storage)).toBe(false);
  });

  it('works without any storage at all', () => {
    expect(loadLayout(SPECS, null)).toEqual(defaultLayout(SPECS));
  });

  it('carries panels hidden in the older visibility map over', () => {
    const storage = memoryStorage({ [LEGACY_VISIBILITY_KEY]: JSON.stringify({ b: false, a: true }) });
    const layout = loadLayout(SPECS, storage);
    expect(layout.items.b.hidden).toBe(true);
    expect(layout.items.a.hidden).toBe(false);
  });

  it.each(['{broken', 'null', '"text"'])('ignores an unreadable legacy value %j', (legacy) => {
    const storage = memoryStorage({ [LEGACY_VISIBILITY_KEY]: legacy });
    expect(loadLayout(SPECS, storage)).toEqual(defaultLayout(SPECS));
  });

  it('prefers the versioned layout over the legacy map', () => {
    const saved = setHidden(defaultLayout(SPECS), 'c', true);
    const storage = memoryStorage({
      [WORKSPACE_STORAGE_KEY]: JSON.stringify(saved),
      [LEGACY_VISIBILITY_KEY]: JSON.stringify({ a: false }),
    });
    const layout = loadLayout(SPECS, storage);
    expect(layout.items.c.hidden).toBe(true);
    expect(layout.items.a.hidden).toBe(false);
  });
});

describe('applyGridLayout', () => {
  it('returns the same object when nothing moved, so a render cannot loop', () => {
    const layout = defaultLayout(SPECS);
    const same = gridItemsFor(layout, SPECS);
    expect(applyGridLayout(layout, same)).toBe(layout);
  });

  it('records a moved panel and ignores unknown or hidden ones', () => {
    const layout = setHidden(defaultLayout(SPECS), 'c', true);
    const next = applyGridLayout(layout, [
      { i: 'a', x: 2, y: 1, w: 10, h: 5 },
      { i: 'c', x: 0, y: 0, w: 24, h: 9 },
      { i: 'ghost', x: 0, y: 0, w: 1, h: 1 },
    ]);
    expect(next.items.a).toMatchObject({ x: 2, y: 1, w: 10, h: 5 });
    expect(next.items.c).toEqual(layout.items.c);
    expect(next.items.ghost).toBeUndefined();
  });

  it('never lets the grid resize a minimized panel', () => {
    const layout = toggleMinimized(defaultLayout(SPECS), 'a', spec('a'));
    const next = applyGridLayout(layout, [{ i: 'a', x: 0, y: 0, w: 12, h: 9 }]);
    expect(next.items.a.h).toBe(MINIMIZED_H);
  });
});

describe('hide, minimize and maximize', () => {
  it('hiding the maximized panel leaves maximized mode', () => {
    const layout = setMaximized(defaultLayout(SPECS), 'a');
    expect(setHidden(layout, 'a', true).maximized).toBeNull();
    expect(setHidden(layout, 'b', true).maximized).toBe('a');
    expect(setHidden(layout, 'ghost', true)).toBe(layout);
  });

  it('hides and recovers every panel at once', () => {
    const hidden = setAllHidden(setMaximized(defaultLayout(SPECS), 'a'), true);
    expect(Object.values(hidden.items).every((item) => item.hidden)).toBe(true);
    expect(hidden.maximized).toBeNull();
    const shown = setAllHidden(hidden, false);
    expect(Object.values(shown.items).some((item) => item.hidden)).toBe(false);
  });

  it('minimizing and restoring returns the previous height', () => {
    const resized = resizeBy(defaultLayout(SPECS), 'b', 0, 5, spec('b'));
    const minimized = toggleMinimized(resized, 'b', spec('b'));
    expect(minimized.items.b).toMatchObject({ minimized: true, h: MINIMIZED_H, restoreH: 11 });
    const restored = toggleMinimized(minimized, 'b', spec('b'));
    expect(restored.items.b).toMatchObject({ minimized: false, h: 11, restoreH: null });
    expect(toggleMinimized(resized, 'ghost', spec('b'))).toBe(resized);
  });

  it('restoring without a remembered height uses the default height', () => {
    const layout = defaultLayout(SPECS);
    layout.items.a = { ...layout.items.a, minimized: true, h: MINIMIZED_H, restoreH: null };
    expect(toggleMinimized(layout, 'a', spec('a')).items.a.h).toBe(4);
  });

  it('minimizing the maximized panel leaves maximized mode', () => {
    const layout = setMaximized(defaultLayout(SPECS), 'a');
    expect(toggleMinimized(layout, 'a', spec('a')).maximized).toBeNull();
    expect(toggleMinimized(layout, 'b', spec('b')).maximized).toBe('a');
  });

  it('maximizing keeps the stored geometry, so un-maximizing restores it', () => {
    const layout = moveBy(defaultLayout(SPECS), 'b', -4, 2);
    const maximized = setMaximized(layout, 'b');
    expect(maximized.maximized).toBe('b');
    expect(maximized.items).toEqual(layout.items);
    expect(setMaximized(maximized, null)).toEqual({ ...layout, maximized: null });
  });

  it('maximizing a minimized panel shows it at its usable height', () => {
    const minimized = toggleMinimized(defaultLayout(SPECS), 'b', spec('b'));
    const maximized = setMaximized(minimized, 'b');
    expect(maximized.items.b).toMatchObject({ minimized: false, h: 6 });
  });

  it('refuses to maximize a hidden or unknown panel', () => {
    const layout = setHidden(defaultLayout(SPECS), 'c', true);
    expect(setMaximized(layout, 'c')).toBe(layout);
    expect(setMaximized(layout, 'ghost')).toBe(layout);
  });
});

describe('keyboard move and resize', () => {
  it('moves within the grid and never past its edges', () => {
    const layout = defaultLayout(SPECS);
    expect(moveBy(layout, 'a', 3, 2).items.a).toMatchObject({ x: 3, y: 2 });
    expect(moveBy(layout, 'a', -5, -5).items.a).toMatchObject({ x: 0, y: 0 });
    expect(moveBy(layout, 'b', 50, 0).items.b.x).toBe(GRID_COLS - 12);
    expect(moveBy(layout, 'ghost', 1, 1)).toBe(layout);
  });

  it('resizes down to the minimums and no wider than the space to the right', () => {
    const layout = defaultLayout(SPECS);
    expect(resizeBy(layout, 'b', -20, -20, spec('b')).items.b).toMatchObject({ w: 6, h: 4 });
    expect(resizeBy(layout, 'b', 20, 0, spec('b')).items.b.w).toBe(GRID_COLS - 12);
    expect(resizeBy(layout, 'ghost', 1, 1, spec('b'))).toBe(layout);
  });

  it('a minimized panel keeps its header height', () => {
    const minimized = toggleMinimized(defaultLayout(SPECS), 'a', spec('a'));
    expect(resizeBy(minimized, 'a', 0, 5, spec('a')).items.a.h).toBe(MINIMIZED_H);
  });
});

describe('compactLayout', () => {
  it('pushes an overlapping panel down instead of stacking them', () => {
    const layout = moveBy(defaultLayout(SPECS), 'c', 0, -6);
    const compacted = compactLayout(layout, SPECS);
    const { a, b, c } = compacted.items;
    for (const [one, two] of [[a, c], [b, c]]) {
      const overlapX = one.x < two.x + two.w && two.x < one.x + one.w;
      const overlapY = one.y < two.y + two.h && two.y < one.y + one.h;
      expect(overlapX && overlapY).toBe(false);
    }
  });

  it('floats a panel up into the gap above it', () => {
    const layout = moveBy(defaultLayout(SPECS), 'c', 0, 40);
    expect(compactLayout(layout, SPECS).items.c.y).toBe(6);
  });

  it('is a no-op on a compact layout and on an empty workspace', () => {
    const layout = defaultLayout(SPECS);
    expect(compactLayout(layout, SPECS)).toBe(layout);
    const empty = setAllHidden(layout, true);
    expect(compactLayout(empty, SPECS)).toBe(empty);
  });
});

describe('gridItemsFor', () => {
  it('passes only visible panels with their constraints', () => {
    const layout = toggleMinimized(setHidden(defaultLayout(SPECS), 'c', true), 'a', spec('a'));
    const items = gridItemsFor(layout, SPECS);
    expect(items.map((item) => item.i)).toEqual(['a', 'b']);
    expect(items[0]).toMatchObject({ minH: MINIMIZED_H, maxH: MINIMIZED_H, isResizable: false });
    expect(items[1]).toMatchObject({ minW: 6, minH: 4, isResizable: true });
    expect(items[1].maxH).toBeUndefined();
  });

  it('stacks into one full-width column in reading order on a narrow screen', () => {
    const layout = moveBy(defaultLayout(SPECS), 'b', 0, 20);
    const items = gridItemsFor(layout, SPECS, { stacked: true });
    expect(items.map((item) => item.i)).toEqual(['a', 'c', 'b']);
    expect(items.every((item) => item.x === 0 && item.w === GRID_COLS)).toBe(true);
    expect(items.every((item) => !item.isDraggable && !item.isResizable)).toBe(true);
    expect(items.map((item) => item.y)).toEqual([0, 4, 7]);
    // Stacking is derived for rendering; the stored layout is untouched.
    expect(layout.items.b.y).toBe(20);
  });
});
