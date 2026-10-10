// One xterm.js terminal bound to one daemon session.
//
// xterm.js is a real terminal emulator: it interprets the bytes the PTY
// produced (colours, cursor movement, alternate screen, Unicode widths), and
// everything typed -- including Ctrl+C, Ctrl+D, arrows and paste -- goes to
// the PTY unchanged, where the kernel's line discipline acts on it. Nothing
// here interprets commands.
//
// The view stays mounted while hidden so a session keeps its screen and
// scrollback when the operator switches tabs or collapses the dock.
import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

const THEME = {
  background: '#050507',
  foreground: '#e8e5df',
  cursor: '#da7756',
  selectionBackground: 'rgba(0, 212, 255, 0.25)',
};

export function XtermView({
  sid,
  active,
  client,
  TerminalImpl = Terminal,
  FitAddonImpl = FitAddon,
}) {
  const containerRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);

  useEffect(() => {
    const term = new TerminalImpl({
      cursorBlink: true,
      scrollback: 5000,
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: 12,
      theme: THEME,
      allowProposedApi: false,
    });
    const fit = new FitAddonImpl();
    term.loadAddon(fit);
    term.open(containerRef.current);
    termRef.current = term;
    fitRef.current = fit;

    const offOutput = client.on('output', (event) => {
      if (event.sid === sid) term.write(event.bytes);
    });
    const offGap = client.on('gap', (event) => {
      if (event.sid === sid) term.write('\r\n\x1b[33m[earlier output was not retained]\x1b[0m\r\n');
    });
    const onData = term.onData((data) => client.send('session.input', { sid, data }));
    const onResize = term.onResize(({ cols, rows }) => client.send('session.resize', { sid, rows, cols }));
    client.attach(sid);

    let observer = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => {
        const el = containerRef.current;
        // A hidden view has no size; fitting it would shrink the PTY to 1x1.
        if (el && el.clientWidth > 0 && el.clientHeight > 0) {
          try { fit.fit(); } catch { /* not yet laid out */ }
        }
      });
      observer.observe(containerRef.current);
    }

    return () => {
      observer?.disconnect();
      offOutput();
      offGap();
      onData.dispose();
      onResize.dispose();
      client.detach(sid);
      term.dispose();
      termRef.current = null;
    };
  }, [sid, client, TerminalImpl, FitAddonImpl]);

  useEffect(() => {
    if (!active || !termRef.current) return;
    const el = containerRef.current;
    if (el && el.clientWidth > 0 && el.clientHeight > 0) {
      try { fitRef.current.fit(); } catch { /* not yet laid out */ }
    }
    termRef.current.focus();
  }, [active]);

  return (
    <div
      ref={containerRef}
      className="xterm-host"
      data-session={sid}
      style={{ display: active ? 'block' : 'none' }}
    />
  );
}
