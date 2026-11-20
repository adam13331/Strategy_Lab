import React from "react";
import { Num, Section } from "../components";
import type { Config, Gates } from "../types";

const rows: { key: keyof Gates; label: string; hint: string; step: number }[] = [
  { key: "min_trades", label: "Minimum trades", hint: "Closed trades in the segment.", step: 1 },
  { key: "min_sharpe", label: "Minimum Sharpe", hint: "Annualized from daily equity returns, rf = 0.", step: 0.05 },
  { key: "min_return_dd", label: "Minimum return / max DD", hint: "Net return divided by max drawdown.", step: 0.05 },
  { key: "max_stagnation_days", label: "Maximum stagnation", hint: "Longest calendar-day gap without a new equity high.", step: 30 },
  { key: "max_drawdown_pct", label: "Maximum drawdown", hint: "Percent. 100 disables the gate.", step: 5 },
  { key: "min_profit_factor", label: "Minimum profit factor", hint: "Gross wins / gross losses. 0 disables.", step: 0.05 },
  { key: "min_win_rate", label: "Minimum win rate", hint: "Percent. 0 disables.", step: 1 },
];

function GateEditor({ title, subtitle, gates, onChange }: {
  title: string; subtitle: string; gates: Gates; onChange: (g: Gates) => void;
}) {
  return (
    <Section title={title} subtitle={subtitle}>
      <div className="gate-editor">
        {rows.map((r) => (
          <div className="gate-row" key={r.key}>
            <div>
              <span className="field-label">{r.label}</span>
              <span className="hint">{r.hint}</span>
            </div>
            <Num value={gates[r.key]} step={r.step}
              onChange={(v) => onChange({ ...gates, [r.key]: v })} />
          </div>
        ))}
      </div>
    </Section>
  );
}

export function AcceptancePage({ config, setConfig, go }: {
  config: Config; setConfig: (c: Config) => void; go?: (p: string) => void;
}) {
  const upd = (fn: (c: Config) => void) => {
    const c = structuredClone(config); fn(c); setConfig(c);
  };
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Acceptance</h1>
          <p>A strategy must pass every in-sample gate and every out-of-sample gate.</p>
        </div>
        <div className="head-actions">
          {go && <button className="btn" onClick={() => go("generator")}>← Back to generator</button>}
          <div className="badge accent">OOS {config.oos_pct}%</div>
        </div>
      </header>

      <div className="note-box">
        Gates are applied after the backtest. OOS is purged at the boundary; the
        correlation filter only looks at IS returns. Lower thresholds accept more
        strategies and take less time. The run has no maximum attempt count.
      </div>

      <GateEditor title="In sample" subtitle="First part of the history."
        gates={config.acceptance.is_gates}
        onChange={(g) => upd((c) => { c.acceptance.is_gates = g; })} />
      <GateEditor title={`Out of sample (last ${config.oos_pct}%)`} subtitle="Never used for fitting or de-duplication."
        gates={config.acceptance.oos_gates}
        onChange={(g) => upd((c) => { c.acceptance.oos_gates = g; })} />

      <Section title="Metrics reference">
        <div className="metrics-doc">
          <div><h3>Trades</h3><p>Only closed trades count. A trade belongs to the segment where it was opened.</p></div>
          <div><h3>Sharpe</h3><p>mean / stdev of daily equity returns × √252. Marked-to-market between trades.</p></div>
          <div><h3>Return / DD</h3><p>Net return in percent divided by max peak-to-trough drawdown in percent.</p></div>
          <div><h3>Stagnation</h3><p>Calendar days between new equity highs, including the final open-ended period.</p></div>
          <div><h3>OOS split</h3><p>Time ordered: last {config.oos_pct}% of each instrument. A purge blocks entries just before the split.</p></div>
          <div><h3>Costs</h3><p>Spread and slippage are applied on both sides; commission is charged per lot on entry and exit.</p></div>
        </div>
      </Section>

    </div>
  );
}
