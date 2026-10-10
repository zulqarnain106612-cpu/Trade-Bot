// The dashboard workspace: every panel is a grid item that can be dragged by
// its header, resized from its edges, minimized to its header, maximized to
// the visible workspace, hidden and restored.
//
// react-grid-layout owns pointer interaction; layoutStore owns the rules and
// persistence. Maximize is CSS on the same element -- the panel is not
// re-mounted, so its polling, charts and in-progress inputs survive, and
// restoring returns it to the geometry it had.
import { useEffect, useMemo, useState } from 'react';
import ReactGridLayout, { useContainerWidth, verticalCompactor } from 'react-grid-layout';
import 'react-grid-layout/css/styles.css';
import 'react-resizable/css/styles.css';
import {
  GRID_COLS, GRID_MARGIN, ROW_HEIGHT, applyGridLayout, compactLayout, gridItemsFor, setMaximized,
} from '../../workspace/layoutStore';

// Only the header's title area starts a drag. Anything interactive inside it
// -- and every control in the header's action area -- cancels, so a click on
// a button, an input or a link never turns into a move.
export const DRAG_HANDLE = '.ws-drag-handle';
export const DRAG_CANCEL = 'button, input, textarea, select, option, a, label, [contenteditable="true"], .ws-no-drag';
export const STACK_BELOW_PX = 760;

export function Workspace({ specs, layout, onChange, renderPanel, viewportRef }) {
  const { width, containerRef, mounted } = useContainerWidth();
  const stacked = width > 0 && width < STACK_BELOW_PX;
  const items = useMemo(() => gridItemsFor(layout, specs, { stacked }), [layout, specs, stacked]);
  const [frame, setFrame] = useState(null);
  const maximized = layout.maximized;

  // A maximized panel covers exactly the visible workspace (between the
  // header and the terminal/status bar), measured rather than assumed.
  useEffect(() => {
    const viewport = viewportRef?.current;
    if (!maximized || !viewport) {
      setFrame(null);
      return undefined;
    }
    const measure = () => {
      const r = viewport.getBoundingClientRect();
      setFrame({ top: r.top, left: r.left, width: r.width, height: r.height });
    };
    measure();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
    observer?.observe(viewport);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [maximized, viewportRef]);

  useEffect(() => {
    if (!maximized) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onChange((current) => setMaximized(current, null));
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [maximized, onChange]);

  const style = frame
    ? {
      '--ws-max-top': `${frame.top}px`,
      '--ws-max-left': `${frame.left}px`,
      '--ws-max-width': `${frame.width}px`,
      '--ws-max-height': `${frame.height}px`,
    }
    : undefined;

  const interactive = !stacked && !maximized;
  // Only a finished drag or resize writes the grid's geometry back. The
  // stored layout is already compacted the way the grid compacts it, so the
  // grid's other (asynchronous) layout reports carry nothing new -- and one
  // arriving late would undo a change made in the meantime.
  const commit = (next) => onChange((current) => compactLayout(applyGridLayout(current, next), specs));
  return (
    <div
      ref={containerRef}
      className={`workspace${maximized ? ' has-maximized' : ''}${stacked ? ' stacked' : ''}`}
      style={style}
    >
      {mounted && width > 0 && (
        <ReactGridLayout
          width={width}
          layout={items}
          gridConfig={{ cols: GRID_COLS, rowHeight: ROW_HEIGHT, margin: GRID_MARGIN, containerPadding: [0, 0] }}
          dragConfig={{ enabled: interactive, handle: DRAG_HANDLE, cancel: DRAG_CANCEL, threshold: 4 }}
          resizeConfig={{ enabled: interactive, handles: ['se', 'e', 's'] }}
          compactor={verticalCompactor}
          onDragStop={commit}
          onResizeStop={commit}
        >
          {items.map((item) => (
            <div
              key={item.i}
              data-ws-id={item.i}
              className={`ws-item${maximized === item.i ? ' ws-maximized' : ''}`}
            >
              {renderPanel(item.i)}
            </div>
          ))}
        </ReactGridLayout>
      )}
    </div>
  );
}
