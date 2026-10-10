/**
 * TERM-010 — the workspace grid and the panel chrome.
 *
 * react-grid-layout owns pointer interaction and is replaced here by a stub
 * that records what it was given; what is decided is the contract around it:
 * only the header's title area starts a drag and every control inside it
 * cancels one, a narrow screen stacks panels without touching the saved
 * layout, maximize disables dragging and fills the measured viewport, Escape
 * restores, and a finished drag is written back compacted. Panels are fully
 * operable from the keyboard.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GRID_COLS, defaultLayout, setHidden, setMaximized } from '../../workspace/layoutStore.js';
import { Panel } from '../Panel.jsx';
import { DRAG_CANCEL, DRAG_HANDLE, STACK_BELOW_PX, Workspace } from './Workspace.jsx';

const grid = vi.hoisted(() => ({ width: 1200, props: null }));
vi.mock('react-grid-layout', async () => {
  const { createElement } = await import('react');
  return {
    default: (props) => {
      grid.props = props;
      return createElement('div', { 'data-testid': 'grid' }, props.children);
    },
    useContainerWidth: () => ({ width: grid.width, containerRef: () => {}, mounted: true }),
    verticalCompactor: { type: 'vertical' },
  };
});

const SPECS = [
  { id: 'a', w: 12, h: 4, minW: 6, minH: 3 },
  { id: 'b', w: 12, h: 6, minW: 6, minH: 4 },
  { id: 'c', w: 24, h: 3, minW: 8, minH: 2 },
];

function renderWorkspace(layout, extra = {}) {
  const onChange = vi.fn();
  const view = render(
    <Workspace
      specs={SPECS}
      layout={layout}
      onChange={onChange}
      renderPanel={(id) => <Panel title={`Panel ${id}`} onMove={() => {}}><p>body {id}</p></Panel>}
      {...extra}
    />,
  );
  return { onChange, ...view };
}

beforeEach(() => {
  grid.width = 1200;
  grid.props = null;
});

describe('Workspace', () => {
  it('lays out every visible panel with its constraints and drag rules', () => {
    const { container } = renderWorkspace(setHidden(defaultLayout(SPECS), 'c', true));
    expect([...container.querySelectorAll('[data-ws-id]')].map((el) => el.dataset.wsId)).toEqual(['a', 'b']);
    expect(grid.props.layout.map((item) => [item.i, item.x, item.y, item.w, item.h])).toEqual([
      ['a', 0, 0, 12, 4],
      ['b', 12, 0, 12, 6],
    ]);
    expect(grid.props.gridConfig.cols).toBe(GRID_COLS);
    expect(grid.props.dragConfig).toMatchObject({ enabled: true, handle: DRAG_HANDLE, cancel: DRAG_CANCEL });
    expect(grid.props.resizeConfig.enabled).toBe(true);
  });

  it('a drag starts only from the title area, never from a control inside the header', () => {
    const header = document.createElement('div');
    header.innerHTML = `
      <div class="ws-drag-handle"><span class="panel-title">t</span><input><a href="#">l</a></div>
      <button>b</button><select><option>o</option></select><label>x</label>
      <div contenteditable="true"></div><span class="ws-no-drag"></span>`;
    const cancels = (selector) => header.querySelector(selector).matches(DRAG_CANCEL);
    expect(cancels('.panel-title')).toBe(false);
    expect(cancels('.ws-drag-handle')).toBe(false);
    for (const selector of ['input', 'a', 'button', 'select', 'option', 'label', '[contenteditable]', '.ws-no-drag']) {
      expect(cancels(selector)).toBe(true);
    }
  });

  it('a finished drag is stored compacted; nothing else writes the layout', () => {
    const layout = defaultLayout(SPECS);
    const { onChange } = renderWorkspace(layout);
    expect(onChange).not.toHaveBeenCalled();
    // Drop "c" on top of "a": the stored result must not overlap.
    act(() => grid.props.onDragStop([
      { i: 'a', x: 0, y: 0, w: 12, h: 4 },
      { i: 'b', x: 12, y: 0, w: 12, h: 6 },
      { i: 'c', x: 0, y: 0, w: 24, h: 3 },
    ]));
    const next = onChange.mock.calls[0][0](layout);
    const { a, b, c } = next.items;
    for (const other of [a, b]) {
      expect(other.y + other.h <= c.y || c.y + c.h <= other.y).toBe(true);
    }
    act(() => grid.props.onResizeStop([{ i: 'b', x: 12, y: 0, w: 12, h: 9 }]));
    expect(onChange.mock.calls[1][0](layout).items.b.h).toBe(9);
  });

  it('stacks into one column on a narrow screen without changing the saved layout', () => {
    grid.width = STACK_BELOW_PX - 1;
    const layout = defaultLayout(SPECS);
    const { container } = renderWorkspace(layout);
    expect(container.querySelector('.workspace').className).toContain('stacked');
    expect(grid.props.layout.every((item) => item.x === 0 && item.w === GRID_COLS)).toBe(true);
    expect(grid.props.dragConfig.enabled).toBe(false);
    expect(layout.items.b.x).toBe(12);
  });

  it('renders nothing until the container has a width', () => {
    grid.width = 0;
    renderWorkspace(defaultLayout(SPECS));
    expect(screen.queryByTestId('grid')).toBeNull();
  });

  it('a maximized panel fills the measured viewport and Escape restores it', () => {
    const viewport = document.createElement('div');
    viewport.getBoundingClientRect = () => ({ top: 48, left: 0, width: 1000, height: 600 });
    const { container, onChange } = renderWorkspace(setMaximized(defaultLayout(SPECS), 'b'), {
      viewportRef: { current: viewport },
    });
    const workspace = container.querySelector('.workspace');
    expect(workspace.className).toContain('has-maximized');
    expect(workspace.style.getPropertyValue('--ws-max-top')).toBe('48px');
    expect(workspace.style.getPropertyValue('--ws-max-height')).toBe('600px');
    expect(container.querySelector('[data-ws-id="b"]').className).toContain('ws-maximized');
    expect(grid.props.dragConfig.enabled).toBe(false);
    expect(grid.props.resizeConfig.enabled).toBe(false);
    // Maximize is CSS on the same element: every panel stays mounted.
    expect(screen.getByText('body a')).not.toBeNull();

    fireEvent.keyDown(document, { key: 'Enter' });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: 'Escape' });
    const restored = onChange.mock.calls[0][0](setMaximized(defaultLayout(SPECS), 'b'));
    expect(restored.maximized).toBeNull();
  });

  it('stops listening for Escape when nothing is maximized', () => {
    const { onChange } = renderWorkspace(defaultLayout(SPECS));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('Panel', () => {
  function renderPanel(props = {}) {
    const handlers = {
      onMove: vi.fn(), onResize: vi.fn(), onMinimize: vi.fn(), onMaximize: vi.fn(), onHide: vi.fn(),
    };
    const view = render(<Panel title="Positions" {...handlers} {...props}><input aria-label="filter" /></Panel>);
    return { handlers, ...view };
  }

  it('moves with the arrow keys and resizes with Shift+arrows', () => {
    const { handlers } = renderPanel();
    const grip = screen.getByRole('button', { name: 'Move or resize Positions' });
    fireEvent.keyDown(grip, { key: 'ArrowRight' });
    fireEvent.keyDown(grip, { key: 'ArrowUp', shiftKey: true });
    fireEvent.keyDown(grip, { key: 'a' });
    expect(handlers.onMove.mock.calls).toEqual([[1, 0]]);
    expect(handlers.onResize.mock.calls).toEqual([[0, -1]]);
  });

  it('every workspace control is a labelled button', () => {
    const { handlers } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Minimize Positions' }));
    fireEvent.click(screen.getByRole('button', { name: 'Maximize Positions' }));
    fireEvent.click(screen.getByRole('button', { name: 'Hide Positions' }));
    expect(handlers.onMinimize).toHaveBeenCalledOnce();
    expect(handlers.onMaximize).toHaveBeenCalledOnce();
    expect(handlers.onHide).toHaveBeenCalledOnce();
  });

  it('minimized keeps the body mounted, so its state survives', () => {
    const { container, rerender } = renderPanel({ minimized: true });
    const input = screen.getByLabelText('filter');
    fireEvent.change(input, { target: { value: 'BTC' } });
    expect(container.querySelector('.panel-body').hidden).toBe(true);
    expect(screen.getByRole('button', { name: 'Restore Positions' })).not.toBeNull();
    rerender(<Panel title="Positions" minimized={false}><input aria-label="filter" /></Panel>);
    expect(screen.getByLabelText('filter').value).toBe('BTC');
  });

  it('shows the maximized state', () => {
    renderPanel({ maximized: true });
    const toggle = screen.getByRole('button', { name: 'Restore Positions size' });
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
  });

  it('a panel outside the workspace has no grip or controls', () => {
    render(<Panel title="Plain" badge={3}>x</Panel>);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText('3')).not.toBeNull();
  });
});
