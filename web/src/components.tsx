import React, { useMemo } from "react";

export function Section({ title, subtitle, right, children }: {
  title: string; subtitle?: string; right?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="muted">{subtitle}</p>}
        </div>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Field({ label, hint, children, className = "" }: {
  label: string; hint?: string; children: React.ReactNode; className?: string;
}) {
  return (
    <label className={`field ${className}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </label>
  );
}

export function Num({ value, onChange, step = 1, min, max, suffix }: {
  value: number; onChange: (v: number) => void; step?: number;
  min?: number; max?: number; suffix?: string;
}) {
  return (
    <span className="num-wrap">
      <input
        type="number"
        value={Number.isFinite(value) ? value : 0}
        step={step}
        min={min}
        max={max}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {suffix && <span className="suffix">{suffix}</span>}
    </span>
  );
}

export function Toggle({ value, onChange, label }: {
  value: boolean; onChange: (v: boolean) => void; label?: string;
}) {
  return (
    <button
      type="button"
      className={`toggle ${value ? "on" : ""}`}
      onClick={() => onChange(!value)}
      aria-pressed={value}
    >
      <span className="knob" />
      {label && <span>{label}</span>}
    </button>
  );
}

export function Seg<T extends string>({ value, options, onChange, small }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; small?: boolean;
}) {
  return (
    <div className={`seg ${small ? "small" : ""}`}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function RangeRow({ min, max, step, onChange, stepLabel = "step" }: {
  min: number; max: number; step: number;
  onChange: (v: { min: number; max: number; step: number }) => void;
  stepLabel?: string;
}) {
  return (
    <div className="range-row">
      <label>min <input type="number" value={min} step={step}
        onChange={(e) => onChange({ min: Number(e.target.value), max, step })} /></label>
      <label>max <input type="number" value={max} step={step}
        onChange={(e) => onChange({ min, max: Number(e.target.value), step })} /></label>
      <label>{stepLabel} <input type="number" value={step} step={step || 1}
        onChange={(e) => onChange({ min, max, step: Number(e.target.value) })} /></label>
    </div>
  );
}

export function Badge({ children, tone = "neutral" }: {
  children: React.ReactNode; tone?: "neutral" | "good" | "bad" | "warn" | "accent";
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="loading"><span className="spinner" /> {what}…</div>;
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-box">{error}</div>;
}

export function DaysPicker({ days, onChange }: {
  days: number[]; onChange: (d: number[]) => void;
}) {
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return (
    <div className="days">
      {names.map((n, i) => (
        <button key={n} type="button" className={days.includes(i) ? "active" : ""}
          onClick={() => onChange(days.includes(i) ? days.filter((d) => d !== i)
            : [...days, i].sort())}>{n}</button>
      ))}
    </div>
  );
}

export function LineChart({ data, splitTs, height = 280, label = "Equity" }: {
  data: { ts: string; equity: number }[]; splitTs?: string; height?: number; label?: string;
}) {
  const chart = useMemo(() => {
    if (!data.length) return null;
    const W = 1000, H = height, P = { l: 62, r: 16, t: 18, b: 30 };
    const maxPts = 900;
    const step = Math.max(1, Math.floor(data.length / maxPts));
    const pts = data.filter((_, i) => i % step === 0 || i === data.length - 1);
    const vals = pts.map((d) => d.equity);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.08 || Math.max(1, hi * 0.01);
    lo -= pad; hi += pad;
    const x = (i: number) => P.l + (i / Math.max(1, pts.length - 1)) * (W - P.l - P.r);
    const y = (v: number) => P.t + (1 - (v - lo) / Math.max(1e-9, hi - lo)) * (H - P.t - P.b);
    const path = pts.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.equity).toFixed(1)}`).join(" ");
    const area = `${path} L${x(pts.length - 1)},${H - P.b} L${P.l},${H - P.b} Z`;
    let splitX: number | null = null;
    if (splitTs) {
      const idx = pts.findIndex((d) => d.ts >= splitTs);
      if (idx > 0) splitX = x(idx);
    }
    const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      y: y(lo + (hi - lo) * f), v: lo + (hi - lo) * f,
    }));
    return { W, H, P, path, area, grid, splitX, lo, hi, pts };
  }, [data, splitTs, height]);

  if (!chart) return <div className="empty-chart">No equity data</div>;
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${chart.W} ${chart.H}`} role="img" aria-label={label}>
        <defs>
          <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#2dd4bf" stopOpacity="0.26" />
            <stop offset="100%" stopColor="#2dd4bf" stopOpacity="0" />
          </linearGradient>
        </defs>
        {chart.grid.map((g, i) => (
          <g key={i}>
            <line x1={chart.P.l} x2={chart.W - chart.P.r} y1={g.y} y2={g.y}
              className="grid-line" />
            <text x={chart.P.l - 8} y={g.y + 4} textAnchor="end" className="axis">
              {g.v.toFixed(0)}
            </text>
          </g>
        ))}
        <path d={chart.area} fill="url(#eqfill)" />
        <path d={chart.path} className="eq-line" />
        {chart.splitX !== null && (
          <g>
            <line x1={chart.splitX} x2={chart.splitX} y1={chart.P.t}
              y2={chart.H - chart.P.b} className="split-line" />
            <text x={chart.splitX + 6} y={chart.P.t + 14} className="split-label">
              OOS
            </text>
          </g>
        )}
        <text x={chart.P.l} y={chart.H - 8} className="axis">{data[0].ts.slice(0, 10)}</text>
        <text x={chart.W - chart.P.r} y={chart.H - 8} textAnchor="end" className="axis">
          {data[data.length - 1].ts.slice(0, 10)}
        </text>
      </svg>
    </div>
  );
}

export function MetricRow({ label, is, oos, fmt: f = (v: number) => v.toFixed(2) }: {
  label: string; is: number; oos: number; fmt?: (v: number) => string;
}) {
  return (
    <tr>
      <td>{label}</td>
      <td className="mono">{f(is)}</td>
      <td className="mono">{f(oos)}</td>
    </tr>
  );
}
