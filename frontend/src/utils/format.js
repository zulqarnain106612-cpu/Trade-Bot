export function fmt(value, decimals = 2) {
  if (value == null || isNaN(value)) return '—';
  return Number(value).toFixed(decimals);
}

export function tsToTime(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

export function tsToDateTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
  } catch {
    return String(ts);
  }
}

export function pnlColor(v) {
  if (v == null) return 'var(--c-muted)';
  return v >= 0 ? 'var(--c-green)' : 'var(--c-red)';
}

export function abbreviate(n) {
  if (n == null || isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return fmt(n, 2);
}
