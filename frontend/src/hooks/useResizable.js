import { useState, useCallback, useRef, useEffect } from 'react';

export function useResizable(initialWidth, initialHeight, minW = 200, minH = 120) {
  const [size, setSize] = useState({ w: initialWidth, h: initialHeight });
  const dragging = useRef(null);

  const onMouseDown = useCallback((e, axis) => {
    e.preventDefault();
    e.stopPropagation();
    dragging.current = {
      axis,
      startX: e.clientX,
      startY: e.clientY,
      startW: size.w,
      startH: size.h,
    };

    const onMove = (ev) => {
      if (!dragging.current) return;
      const d = dragging.current;
      const dx = ev.clientX - d.startX;
      const dy = ev.clientY - d.startY;
      setSize(prev => ({
        w: d.axis !== 'y' ? Math.max(minW, d.startW + dx) : prev.w,
        h: d.axis !== 'x' ? Math.max(minH, d.startH + dy) : prev.h,
      }));
    };

    const onUp = () => {
      dragging.current = null;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [size, minW, minH]);

  const setSizeManual = useCallback((w, h) => {
    setSize({
      w: w != null ? Math.max(minW, w) : size.w,
      h: h != null ? Math.max(minH, h) : size.h,
    });
  }, [size, minW, minH]);

  return { size, onMouseDown, setSizeManual, setSize };
}
