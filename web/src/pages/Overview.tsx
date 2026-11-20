import React, { useEffect, useState } from "react";
import { api, fmt } from "../api";
import { Badge, Stat } from "../components";
import type { BankInfo, RunInfo, StressRun, SymbolInfo } from "../types";

const FUNNEL_KEYS: { key: string; label: string; tone: "good" | "bad" | "warn" | "neutral" | "accent" }[] = [
  { key: "generated", label: "Generated", tone: "neutral" },
  { key: "structural_duplicate", label: "Structural dup", tone: "warn" },
  { key: "error", label: "Errors", tone: "bad" },
  { key: "is_fail", label: "Failed IS", tone: "bad" },
  { key: "oos_fail", label: "Failed OOS", tone: "warn" },
  { key: "correlation", label: "Correlated", tone: "warn" },
  { key: "accepted", label: "Accepted", tone: "good" },
];

const TONE_COLOR: Record<string, string> = {
  good: "#4ade80", bad: "#f87171", warn: "#fbbf24", neutral: "#7e939b", accent: "#2dd4bf",
};

function FunnelChart({ data }: { data: { key: string; label: string; value: number; tone: string }[] }) {
  const max = Math.max(1, ...data.map((d) => d.value));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {data.map((d) => {
        const w = (d.value / max) * 100;
        return (
          <div key={d.key} style={{ display: "grid", gridTemplateColumns: "128px 1fr 64px", alignItems: "center", gap: 10, fontSize: 12 }}>
            <span style={{ color: "#9db0b7" }}>{d.label}</span>
            <div style={{ height: 10, background: "#0a1216", borderRadius: 999, overflow: "hidden", border: "1px solid #1d2d35" }}>
              <div style={{ width: `${Math.max(d.value > 0 ? 2 : 0, w)}%`, height: "100%", background: TONE_COLOR[d.tone] || "#3b5560", borderRadius: 999, transition: "width .4s" }} />
            </div>
            <b className="mono" style={{ textAlign: "right" }}>{fmt(d.value, 0)}</b>
          </div>
        );
      })}
    </div>
  );
}

function VBarChart({ items, valueKey = "value", labelKey = "label", color = "#2dd4bf", height = 150 }: {
  items: { label: string; value: number }[]; valueKey?: string; labelKey?: string; color?: string; height?: number;
}) {
  if (!items.length) return <div className="empty small">No data yet.</div>;
  const max = Math.max(1, ...items.map((i) => i.value));
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height, paddingTop: 8 }}>
      {items.map((it, i) => {
        const h = (it.value / max) * (height - 24);
        return (
          <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end", gap: 4, minWidth: 0 }}>
            <span className="mono" style={{ fontSize: 9.5, color: "#9db0b7" }}>{fmt(it.value, 0)}</span>
            <div style={{ width: "100%", maxWidth: 34, height: Math.max(2, h), background: `linear-gradient(180deg, ${color}, ${color}55)`, borderRadius: "3px 3px 0 0" }} title={`${it.label}: ${it.value}`} />
            <span style={{ fontSize: 9, color: "#7e939b", fontFamily: "var(--mono)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: "100%" }}>{it.label}</span>
          </div>
        );
      })}
    </div>
  );
}

export function Overview({ symbols, runs, go, openRun }: {
  symbols: SymbolInfo[]; runs: RunInfo[];
  go: (p: string) => void; openRun: (id: string) => void;
}) {
  const [stressRuns, setStressRuns] = useState<StressRun[]>([]);
  const [banks, setBanks] = useState<BankInfo[]>([]);

  useEffect(() => {
    let alive = true;
    api.get<StressRun[]>("/api/stress").then((d) => { if (alive) setStressRuns(d); }).catch(() => {});
    api.get<BankInfo[]>("/api/banks").then((d) => { if (alive) setBanks(d); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  const bars = symbols.reduce((a, s) => a + (s.rows || 0), 0);
  const accepted = runs.reduce((a, r) => a + Number(r.stats?.accepted || r.accepted || 0), 0);
  const attempts = runs.reduce((a, r) => a + Number(r.stats?.attempts || r.attempts || 0), 0);
  const stressSurvivors = stressRuns.reduce((a, r) => a + Number(r.survivors || r.stats?.survivors || 0), 0);
  const stressTested = stressRuns.reduce((a, r) => a + Number(r.strategies_total || r.stats?.strategies_total || 0), 0);
  const bankStrategies = banks.reduce((a, b) => a + Number(b.count || 0), 0);

  // aggregate funnel across runs
  const funnelAgg: Record<string, number> = {};
  for (const k of FUNNEL_KEYS) funnelAgg[k.key] = 0;
  for (const r of runs) {
    const f = (r.funnel || r.stats?.funnel || {}) as Record<string, number>;
    for (const k of FUNNEL_KEYS) {
      funnelAgg[k.key] += Number(f[k.key] || 0);
    }
  }
  // if generated empty but attempts exist, fall back
  if (funnelAgg.generated === 0 && attempts > 0) funnelAgg.generated = attempts;

  const funnelData = FUNNEL_KEYS.map((k) => ({ key: k.key, label: k.label, value: funnelAgg[k.key] || 0, tone: k.tone }));

  // accepted per run (last 10)
  const runBars = runs.slice(0, 10).reverse().map((r) => ({
    label: (r.run_id || "").slice(0, 6),
    value: Number(r.stats?.accepted || r.accepted || 0),
  }));

  const stressBars = stressRuns.slice(0, 8).reverse().map((r) => ({
    label: (r.stress_id || "").slice(0, 6),
    value: Number(r.survivors || r.stats?.survivors || 0),
  }));

  const bankBars = banks.slice(0, 8).reverse().map((b) => ({
    label: (b.bank_id || b.name || "").slice(0, 8),
    value: Number(b.count || 0),
  }));

  const completedRuns = runs.filter((r) => r.status === "completed").length;
  const runningRuns = runs.filter((r) => r.status === "running" || r.status === "queued" || r.status === "stopping").length;

  return (
    <div className="page">
      <header className="hero">
        <div>
          <div className="eyebrow">LOCAL STRATEGY RESEARCH</div>
          <h1>Find strategies that survive.<br />Not just fit.</h1>
          <p className="hero-copy">
            Generate rule-based strategies on any timeframe, execute them on
            1-minute bars, reject them in-sample and out-of-sample, remove
            duplicates by structure and correlation, then stress-test the
            survivors and bank the ones that hold up.
          </p>
          <div className="hero-actions">
            <button className="btn primary" onClick={() => go("generator")}>Configure a run</button>
            <button className="btn" onClick={() => go("data")}>Manage data</button>
            <button className="btn" onClick={() => go("stress")}>Stress test</button>
          </div>
        </div>
        <div className="hero-panel">
          <div className="hero-panel-row"><span>Data</span><strong>{bars.toLocaleString()} bars</strong></div>
          <div className="hero-panel-row"><span>Instruments</span><strong>{symbols.length}</strong></div>
          <div className="hero-panel-row"><span>Runs</span><strong>{runs.length}</strong></div>
          <div className="hero-panel-row"><span>Accepted</span><strong>{accepted}</strong></div>
          <div className="hero-panel-row"><span>Stress survivors</span><strong>{stressSurvivors}</strong></div>
          <div className="hero-panel-row"><span>Banked</span><strong>{bankStrategies}</strong></div>
          <div className="hero-note">Execution: M1 · OOS default 40% · dedup on · 5-stage stress</div>
        </div>
      </header>

      <div className="stat-grid">
        <Stat label="Imported bars" value={bars.toLocaleString()} sub={symbols.map((s) => s.symbol).join(" · ") || "no data yet"} />
        <Stat label="Indicators" value="53" sub="trend · momentum · volatility · volume · price action" />
        <Stat label="Runs" value={runs.length} sub={`${completedRuns} completed · ${runningRuns} live`} />
        <Stat label="Accepted" value={accepted} sub={`${attempts.toLocaleString()} ideas tested`} />
        <Stat label="Stress survivors" value={stressSurvivors} sub={`${stressTested} tested across ${stressRuns.length} runs`} />
        <Stat label="Banked strategies" value={bankStrategies} sub={`${banks.length} bank${banks.length === 1 ? "" : "s"}`} />
      </div>

      {/* Process pipeline */}
      <section className="section">
        <div className="section-head">
          <div>
            <h2>Working process</h2>
            <p className="muted">The full pipeline from raw data to banked, stress-tested strategies.</p>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0,1fr))", gap: 12 }}>
          {[
            { key: "data", label: "1. Ingest data", desc: "Broker M1 CSV → normalized Parquet + quality report.", stat: `${symbols.length} instruments · ${bars.toLocaleString()} bars`, action: "Manage data", go: "data", tone: symbols.length ? "good" : "warn" },
            { key: "gen", label: "2. Generate", desc: "Seed-controlled sampling, 1-minute fills, IS/OOS gates, dedup.", stat: `${runs.length} runs · ${accepted} accepted`, action: "Configure a run", go: "generator", tone: runs.length ? "good" : "neutral" },
            { key: "stress", label: "3. Stress test", desc: "Params, price/cost, shuffle, Monte Carlo, cross-market.", stat: `${stressRuns.length} runs · ${stressSurvivors} survivors`, action: "Open stress test", go: "stress", tone: stressRuns.length ? "good" : "neutral" },
            { key: "bank", label: "4. Bank survivors", desc: "Curated strategy banks for portfolio building.", stat: `${banks.length} banks · ${bankStrategies} strategies`, action: "Open banks", go: "banks", tone: banks.length ? "good" : "neutral" },
          ].map((p) => (
            <div key={p.key} style={{ background: "#0b1317", border: "1px solid #1d2d35", borderRadius: 10, padding: 14, display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <strong style={{ fontSize: 13 }}>{p.label}</strong>
                <Badge tone={p.tone as any}>{p.tone === "good" ? "ready" : p.tone === "warn" ? "action" : "idle"}</Badge>
              </div>
              <p className="muted" style={{ fontSize: 11.5, lineHeight: 1.45 }}>{p.desc}</p>
              <span className="mono" style={{ fontSize: 11, color: "#9db0b7" }}>{p.stat}</span>
              <button className="btn small" style={{ marginTop: 4, alignSelf: "flex-start" }} onClick={() => go(p.go)}>{p.action}</button>
            </div>
          ))}
        </div>
      </section>

      {/* Graphs */}
      <div className="two-col">
        <section className="section">
          <div className="section-head">
            <div>
              <h2>Generation funnel</h2>
              <p className="muted">Aggregate across {runs.length} run{runs.length === 1 ? "" : "s"}.</p>
            </div>
          </div>
          {runs.length === 0 ? (
            <div className="empty small">No runs yet. Start a generation to see the funnel.</div>
          ) : (
            <FunnelChart data={funnelData} />
          )}
        </section>
        <section className="section">
          <div className="section-head">
            <div>
              <h2>Accepted per run</h2>
              <p className="muted">Last {Math.min(10, runs.length)} runs.</p>
            </div>
          </div>
          <VBarChart items={runBars} color="#2dd4bf" />
        </section>
      </div>

      <div className="two-col">
        <section className="section">
          <div className="section-head">
            <div>
              <h2>Stress survivors</h2>
              <p className="muted">Survivors per stress run.</p>
            </div>
            <button className="btn small" onClick={() => go("stress")}>Open</button>
          </div>
          <VBarChart items={stressBars} color="#4ade80" />
        </section>
        <section className="section">
          <div className="section-head">
            <div>
              <h2>Bank sizes</h2>
              <p className="muted">Strategies per bank.</p>
            </div>
            <button className="btn small" onClick={() => go("banks")}>Open</button>
          </div>
          <VBarChart items={bankBars} color="#fbbf24" />
        </section>
      </div>

      {/* Recent runs */}
      <section className="section">
        <div className="section-head">
          <div>
            <h2>Recent runs</h2>
            <p className="muted">Runs continue until the accepted target is reached or you stop them.</p>
          </div>
          <button className="btn small" onClick={() => go("run")}>All runs</button>
        </div>
        {runs.length === 0 ? (
          <div className="empty">
            <p>No runs yet. Import data, choose your indicators and acceptance gates, then start a search.</p>
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>Run</th><th>Status</th><th>Accepted</th><th>Attempts</th><th>Created</th></tr></thead>
            <tbody>
              {runs.slice(0, 8).map((r) => {
                const acc = r.stats?.accepted ?? r.accepted ?? 0;
                const att = r.stats?.attempts ?? r.attempts ?? 0;
                return (
                  <tr key={r.run_id} className="clickable" onClick={() => openRun(r.run_id)}>
                    <td><span className="mono">{r.run_id}</span><span className="muted"> {r.name || ""}</span></td>
                    <td><Badge tone={r.status === "completed" ? "good" : r.status === "running" ? "accent" : "neutral"}>{r.status}</Badge></td>
                    <td className="mono">{fmt(acc, 0)} / {fmt(r.stats?.target ?? r.target ?? 0, 0)}</td>
                    <td className="mono">{fmt(att, 0)}</td>
                    <td className="muted">{String(r.created_at || "").slice(0, 19)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <div className="two-col">
        <section className="section">
          <h2>Workflow</h2>
          <ol className="steps">
            <li><b>Ingest</b><span>Broker M1 CSV → normalized Parquet + quality report.</span></li>
            <li><b>Configure</b><span>Timeframe, direction, exits, sessions, indicator ranges.</span></li>
            <li><b>Generate</b><span>Seed-controlled sampling, 1-minute fills, IS/OOS gates, dedup.</span></li>
            <li><b>Stress test</b><span>Params, price/cost, shuffle, Monte Carlo, cross-market.</span></li>
            <li><b>Bank</b><span>Curate survivors into strategy banks.</span></li>
            <li><b>Explore</b><span>Inspect rules, equity, drawdown and trades of survivors.</span></li>
          </ol>
        </section>
        <section className="section">
          <h2>Notes</h2>
          <ul className="notes">
            <li>Indicators are evaluated on completed bars; entries fill on the next M1 open.</li>
            <li>SL/TP are checked against M1 highs/lows, with SL taking precedence inside one bar.</li>
            <li>The OOS segment is never used for correlation filtering.</li>
            <li>Raw CSVs stay untouched. Every run stores an immutable config and seed.</li>
            <li>Stress tests never modify the source run; survivors are banked separately.</li>
          </ul>
        </section>
      </div>
    </div>
  );
}