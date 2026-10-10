// Panel chrome for one workspace item: a header that is the drag handle and
// carries the workspace controls, and a body that fills the rest.
//
// Size and position belong to the workspace grid, not to the panel. Minimize
// hides the body but keeps it mounted, so a panel's own state (form inputs,
// selected tabs, subscriptions) is exactly where the operator left it.
//
// Keyboard: focus the grip (⠿) and use the arrow keys to move the panel,
// Shift+arrows to resize it; the buttons beside it minimize, maximize and
// hide. Escape leaves a maximized panel.
const MOVE_KEYS = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

export function Panel({
  title,
  icon,
  children,
  accentColor,
  badge,
  headerExtra,
  className = '',
  minimized = false,
  maximized = false,
  onMinimize,
  onMaximize,
  onHide,
  onMove,
  onResize,
}) {
  const onGripKey = (e) => {
    const delta = MOVE_KEYS[e.key];
    if (!delta) return;
    e.preventDefault();
    if (e.shiftKey) onResize?.(delta[0], delta[1]);
    else onMove?.(delta[0], delta[1]);
  };

  return (
    <section
      className={`panel${minimized ? ' panel-minimized' : ''}${maximized ? ' panel-maximized' : ''} ${className}`}
      style={{ borderColor: accentColor ? `${accentColor}30` : undefined }}
      aria-label={title}
    >
      <div className="panel-header">
        <div className="ws-drag-handle" title="Drag to move">
          {(onMove || onResize) && (
            <span
              role="button"
              tabIndex={0}
              className="ws-grip"
              aria-label={`Move or resize ${title}`}
              aria-describedby="ws-keyboard-help"
              onKeyDown={onGripKey}
            >
              ⠿
            </span>
          )}
          {icon && <span style={{ color: accentColor || 'var(--c-cyan)', fontSize: 13 }} aria-hidden="true">{icon}</span>}
          <span className="panel-title" style={{ color: accentColor || undefined }}>{title}</span>
          {badge != null && badge > 0 && (
            <span className="badge" style={{ background: 'rgba(0,212,255,0.12)', color: 'var(--c-cyan)', fontSize: 9 }}>
              {badge}
            </span>
          )}
        </div>
        <div className="panel-actions">
          {headerExtra}
          {onMinimize && (
            <button
              type="button"
              className="panel-ctl"
              onClick={onMinimize}
              aria-label={minimized ? `Restore ${title}` : `Minimize ${title}`}
              title={minimized ? 'Restore' : 'Minimize'}
            >
              {minimized ? '▢' : '–'}
            </button>
          )}
          {onMaximize && (
            <button
              type="button"
              className="panel-ctl"
              onClick={onMaximize}
              aria-label={maximized ? `Restore ${title} size` : `Maximize ${title}`}
              aria-pressed={maximized}
              title={maximized ? 'Restore size (Esc)' : 'Maximize'}
            >
              {maximized ? '❐' : '□'}
            </button>
          )}
          {onHide && (
            <button
              type="button"
              className="panel-ctl"
              onClick={onHide}
              aria-label={`Hide ${title}`}
              title="Hide (restore it from Panels)"
            >
              ×
            </button>
          )}
        </div>
      </div>
      <div className="panel-body" hidden={minimized}>{children}</div>
    </section>
  );
}
