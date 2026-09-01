import { useState, useRef } from 'react';
import { useResizable } from '../hooks/useResizable';

export function Panel({
  title,
  icon,
  children,
  defaultWidth,
  defaultHeight,
  visible = true,
  onToggleVisible,
  accentColor,
  badge,
  headerExtra,
  className = '',
}) {
  const { size, onMouseDown, setSizeManual } = useResizable(
    defaultWidth || 400,
    defaultHeight || 300
  );
  const [editingSize, setEditingSize] = useState(false);
  const [wInput, setWInput] = useState('');
  const [hInput, setHInput] = useState('');

  if (!visible) return null;

  const borderColor = accentColor ? `1px solid ${accentColor}20` : undefined;

  return (
    <div
      className={`panel claude-fade-in ${className}`}
      style={{
        width: defaultWidth === 'auto' ? '100%' : size.w,
        height: defaultHeight === 'auto' ? 'auto' : size.h,
        borderColor: accentColor ? `${accentColor}30` : undefined,
      }}
    >
      <div className="panel-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {icon && <span style={{ color: accentColor || 'var(--c-cyan)', fontSize: 13 }}>{icon}</span>}
          <span className="panel-title" style={{ color: accentColor || undefined }}>{title}</span>
          {badge != null && badge > 0 && (
            <span
              className="badge"
              style={{
                background: 'rgba(0,212,255,0.12)',
                color: 'var(--c-cyan)',
                fontSize: 9,
              }}
            >
              {badge}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          {headerExtra}
          {editingSize ? (
            <form
              style={{ display: 'flex', gap: 3 }}
              onSubmit={(e) => {
                e.preventDefault();
                const w = parseInt(wInput) || null;
                const h = parseInt(hInput) || null;
                setSizeManual(w, h);
                setEditingSize(false);
              }}
            >
              <input
                className="input-sm"
                style={{ width: 48, textAlign: 'right' }}
                value={wInput}
                onChange={(e) => setWInput(e.target.value)}
                placeholder="W"
                autoFocus
              />
              <span style={{ color: 'var(--c-faint)', fontSize: 10 }}>x</span>
              <input
                className="input-sm"
                style={{ width: 48, textAlign: 'right' }}
                value={hInput}
                onChange={(e) => setHInput(e.target.value)}
                placeholder="H"
              />
              <button type="submit" className="btn btn-blue" style={{ padding: '2px 6px', fontSize: 9 }}>OK</button>
            </form>
          ) : (
            <button
              onClick={() => {
                setWInput(String(Math.round(size.w)));
                setHInput(String(Math.round(size.h)));
                setEditingSize(true);
              }}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--c-faint)',
                cursor: 'pointer',
                fontSize: 10,
                padding: '2px 4px',
              }}
              title="Type size (W x H)"
            >
              {Math.round(size.w)}x{Math.round(size.h)}
            </button>
          )}
          {onToggleVisible && (
            <button
              onClick={onToggleVisible}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--c-faint)',
                cursor: 'pointer',
                fontSize: 14,
                padding: '0 2px',
                lineHeight: 1,
              }}
              title="Hide panel"
            >
              ×
            </button>
          )}
        </div>
      </div>
      <div className="panel-body">{children}</div>
      {defaultWidth !== 'auto' && (
        <>
          <div
            className="resize-handle resize-handle-right"
            onMouseDown={(e) => onMouseDown(e, 'x')}
          />
          <div
            className="resize-handle resize-handle-bottom"
            onMouseDown={(e) => onMouseDown(e, 'y')}
          />
          <div
            className="resize-handle resize-handle-corner"
            onMouseDown={(e) => onMouseDown(e, 'xy')}
          />
        </>
      )}
    </div>
  );
}
