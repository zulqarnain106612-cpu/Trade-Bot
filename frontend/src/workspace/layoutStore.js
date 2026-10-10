// Workspace layout: what the operator arranged, persisted and validated.
//
// Pure functions over one plain object so every rule is testable without a
// DOM. The persisted shape is versioned; anything that does not parse, names
// a version this build does not know, or carries values out of range is
// repaired field by field (an invalid entry falls back to that panel's
// default, nothing else is lost) -- a corrupt localStorage value can never
// leave a panel unreachable or crash the dashboard.
//
//   { version: 1,
//     items: { <id>: { x, y, w, h, hidden, minimized, restoreH } },
//     maximized: <id> | null }
//
// Grid units: GRID_COLS columns across the workspace, ROW_HEIGHT px rows.
//
// The stored layout is always kept compacted with the grid's own
// verticalCompactor, so what is stored is exactly what the grid renders. That
// lets the grid's asynchronous layout reports be ignored except at the end of
// a drag or resize: applying every report let one that described the
// previous state land after the next keyboard move and undo it.
import { verticalCompactor } from 'react-grid-layout/core';

export const WORKSPACE_STORAGE_KEY = 'tradebot.workspace';
export const LEGACY_VISIBILITY_KEY = 'panel-visibility';
export const LAYOUT_VERSION = 1;
export const GRID_COLS = 24;
export const ROW_HEIGHT = 20;
export const GRID_MARGIN = [12, 12];
// Header only: two rows is 52px, which is the panel header plus its border.
export const MINIMIZED_H = 2;
export const MAX_Y = 2000;
export const MAX_H = 200;

const isInt = (v) => Number.isInteger(v);

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

export function defaultLayout(specs) {
  const items = {};
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const spec of specs) {
    const w = clamp(spec.w, spec.minW ?? 1, GRID_COLS);
    if (x + w > GRID_COLS) {
      x = 0;
      y += rowHeight;
      rowHeight = 0;
    }
    items[spec.id] = { x, y, w, h: spec.h, hidden: false, minimized: false, restoreH: null };
    x += w;
    rowHeight = Math.max(rowHeight, spec.h);
  }
  return { version: LAYOUT_VERSION, items, maximized: null };
}

function sanitizeItem(raw, spec, fallback) {
  if (!raw || typeof raw !== 'object') return fallback;
  const { x, y, w, h } = raw;
  if (![x, y, w, h].every(isInt)) return fallback;
  const minW = spec.minW ?? 1;
  const minH = spec.minH ?? 1;
  const width = clamp(w, minW, GRID_COLS);
  const minimized = raw.minimized === true;
  const restoreH = isInt(raw.restoreH) ? clamp(raw.restoreH, minH, MAX_H) : null;
  return {
    x: clamp(x, 0, GRID_COLS - width),
    y: clamp(y, 0, MAX_Y),
    w: width,
    h: minimized ? MINIMIZED_H : clamp(h, minH, MAX_H),
    hidden: raw.hidden === true,
    minimized,
    restoreH: minimized ? restoreH ?? spec.h : null,
  };
}

export function sanitizeLayout(raw, specs) {
  const defaults = defaultLayout(specs);
  if (!raw || typeof raw !== 'object' || raw.version !== LAYOUT_VERSION) return defaults;
  const items = {};
  for (const spec of specs) {
    items[spec.id] = sanitizeItem(raw.items?.[spec.id], spec, defaults.items[spec.id]);
  }
  const maximized = typeof raw.maximized === 'string'
    && items[raw.maximized] && !items[raw.maximized].hidden ? raw.maximized : null;
  return { version: LAYOUT_VERSION, items, maximized };
}

export function loadLayout(specs, storage = globalThis.localStorage) {
  let saved = null;
  let legacy = null;
  try {
    saved = storage?.getItem(WORKSPACE_STORAGE_KEY) ?? null;
    legacy = storage?.getItem(LEGACY_VISIBILITY_KEY) ?? null;
  } catch {
    return defaultLayout(specs);
  }
  if (saved != null) {
    try {
      return sanitizeLayout(JSON.parse(saved), specs);
    } catch {
      return defaultLayout(specs);
    }
  }
  // Version 0 was a bare { id: visible } map. Carry the operator's hidden
  // panels over rather than resurrecting everything they closed.
  const layout = defaultLayout(specs);
  if (legacy != null) {
    try {
      const visibility = JSON.parse(legacy);
      if (visibility && typeof visibility === 'object') {
        for (const spec of specs) {
          if (visibility[spec.id] === false) layout.items[spec.id].hidden = true;
        }
      }
    } catch { /* unreadable legacy value: defaults */ }
  }
  return layout;
}

export function saveLayout(layout, storage = globalThis.localStorage) {
  try {
    storage?.setItem(WORKSPACE_STORAGE_KEY, JSON.stringify(layout));
    return true;
  } catch {
    return false;
  }
}

function withItem(layout, id, patch) {
  if (!layout.items[id]) return layout;
  return { ...layout, items: { ...layout.items, [id]: { ...layout.items[id], ...patch } } };
}

// Positions reported by the grid at the end of a drag or resize. Returns the
// same object when nothing moved, so a re-render cannot loop.
export function applyGridLayout(layout, gridItems) {
  let changed = false;
  const items = { ...layout.items };
  for (const g of gridItems) {
    const current = items[g.i];
    if (!current || current.hidden) continue;
    const next = { ...current, x: g.x, y: g.y, w: g.w, h: current.minimized ? current.h : g.h };
    if (next.x !== current.x || next.y !== current.y || next.w !== current.w || next.h !== current.h) {
      items[g.i] = next;
      changed = true;
    }
  }
  return changed ? { ...layout, items } : layout;
}

export function setHidden(layout, id, hidden) {
  const next = withItem(layout, id, { hidden });
  return hidden && next.maximized === id ? { ...next, maximized: null } : next;
}

export function setAllHidden(layout, hidden) {
  const items = {};
  for (const [id, item] of Object.entries(layout.items)) items[id] = { ...item, hidden };
  return { ...layout, items, maximized: hidden ? null : layout.maximized };
}

export function toggleMinimized(layout, id, spec) {
  const item = layout.items[id];
  if (!item) return layout;
  if (item.minimized) {
    return withItem(layout, id, { minimized: false, h: item.restoreH ?? spec.h, restoreH: null });
  }
  const next = withItem(layout, id, { minimized: true, restoreH: item.h, h: MINIMIZED_H });
  return next.maximized === id ? { ...next, maximized: null } : next;
}

export function setMaximized(layout, id) {
  if (id === null) return { ...layout, maximized: null };
  const item = layout.items[id];
  if (!item || item.hidden) return layout;
  // A maximized panel shows its content; restore a minimized one first so
  // un-maximizing returns it to a usable size rather than a header strip.
  const restored = item.minimized
    ? withItem(layout, id, { minimized: false, h: item.restoreH ?? item.h, restoreH: null })
    : layout;
  return { ...restored, maximized: id };
}

export function moveBy(layout, id, dx, dy) {
  const item = layout.items[id];
  if (!item) return layout;
  return withItem(layout, id, {
    x: clamp(item.x + dx, 0, GRID_COLS - item.w),
    y: clamp(item.y + dy, 0, MAX_Y),
  });
}

export function resizeBy(layout, id, dw, dh, spec) {
  const item = layout.items[id];
  if (!item) return layout;
  const w = clamp(item.w + dw, spec.minW ?? 1, GRID_COLS - item.x);
  const h = item.minimized ? item.h : clamp(item.h + dh, spec.minH ?? 1, MAX_H);
  return withItem(layout, id, { w, h });
}

// Settle visible items the way the grid will: float up into gaps, push down
// out of overlaps. Deterministic, so the grid's own pass is then a no-op.
export function compactLayout(layout, specs) {
  const items = gridItemsFor(layout, specs);
  if (items.length === 0) return layout;
  return applyGridLayout(layout, verticalCompactor.compact(items, GRID_COLS));
}

// What the grid renders: visible items only, with their constraints. In
// stacked mode (a narrow screen) everything is one full-width column in
// reading order -- derived, never saved, so the desktop layout survives.
export function gridItemsFor(layout, specs, { stacked = false } = {}) {
  const visible = specs
    .filter((spec) => layout.items[spec.id] && !layout.items[spec.id].hidden)
    .map((spec) => ({ spec, item: layout.items[spec.id] }));
  if (stacked) {
    visible.sort((a, b) => a.item.y - b.item.y || a.item.x - b.item.x);
    let y = 0;
    return visible.map(({ spec, item }) => {
      const entry = {
        i: spec.id, x: 0, y, w: GRID_COLS, h: item.h,
        minW: GRID_COLS, minH: item.minimized ? MINIMIZED_H : spec.minH ?? 1,
        isResizable: false, isDraggable: false,
      };
      y += item.h;
      return entry;
    });
  }
  return visible.map(({ spec, item }) => ({
    i: spec.id,
    x: item.x,
    y: item.y,
    w: item.w,
    h: item.h,
    minW: spec.minW ?? 1,
    minH: item.minimized ? MINIMIZED_H : spec.minH ?? 1,
    ...(item.minimized ? { maxH: MINIMIZED_H } : {}),
    isResizable: !item.minimized,
  }));
}
