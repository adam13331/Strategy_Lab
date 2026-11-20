import React from "react";
import { api, fmt, pct } from "../api";
import { Badge, ErrorBox, Num, Section, Seg, Toggle } from "../components";
import type {
  RunInfo, StressConfig, StressResult, StressRun, StressStageResult,
} from "../types";

const head = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 0 });

const STAGE_KEYS = [
  "parameter_sensitivity", "price_cost_sensitivity", "trade_shuffling",
  "monte_carlo", "cross_market_validation",
] as const;

const STAGE_SHORT: Record<string, string> = {
  parameter_sensitivity: "Params",
  price_cost_sensitivity: "Price / cost",
  trade_shuffling: "Sequence",
  monte_carlo: "Monte Carlo",
  cross_market_validation: "Cross-market",
};

function stageTone(status?: string): "good" | "bad" | "warn" | "neutral" | "accent" {
  if (status === "passed") return "good";
  if (status === "failed") return "bad";
  if (status === "stopped") return "warn";
  if (status === "skipped") return "neutral";
  return "neutral";
}

export function StressPage({ runs, goBanks }: {
  runs: RunInfo[]; goBanks: (bankId?: string) => void;
}) {
  const withAccepted = runs.filter((r) => (r.stats?.accepted ?? 0) > 0);
  const initialSource = (withAccepted.find((r) => r.status === "completed")
    || withAccepted[0] || runs[0])?.run_id || "";
  const [sourceRunId, setSourceRunId] = React.useState(initialSource);
  const [config, setConfig] = React.useState<StressConfig | null>(null);
  const [stressRuns, setStressRuns] = React.useState<StressRun[]>([]);
  const [stressId, setStressId] = React.useState<string | undefined>(undefined);
  const [current, setCurrent] = React.useState<StressRun | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [starting, setStarting] = React.useState(false);
  const es = React.useRef<EventSource | null>(null);

  const refreshList = React.useCallback(async () => {
    try { setStressRuns(await api.get<StressRun[]>("/api/stress")); }
    catch (e) { setError(String(e)); }
  }, []);

  React.useEffect(() => { refreshList(); }, [refreshList]);

  React.useEffect(() => {
    if (!sourceRunId) { setConfig(null); return; }
    let alive = true;
    api.get<StressConfig>(`/api/stress/defaults?source_run_id=${encodeURIComponent(sourceRunId)}`)
      .then((c) => { if (alive) setConfig({ ...c, source_run_id: sourceRunId }); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [sourceRunId]);

  React.useEffect(() => {
    if (!stressId) { setCurrent(null); return; }
    let alive = true;
    es.current?.close();
    api.get<StressRun>(`/api/stress/${stressId}`)
      .then((r) => { if (alive) setCurrent(r); })
      .catch((e) => { if (alive) setError(String(e)); });
    const source = new EventSource(`/api/stress/${stressId}/events`);
    es.current = source;
    source.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        setCurrent((prev) => ({ ...(prev || {}), ...data } as StressRun));
        if (data.done) { source.close(); refreshList(); }
      } catch {}
    };
    source.onerror = () => source.close();
    return () => { alive = false; source.close(); };
  }, [stressId]);

  const sourceRun = runs.find((r) => r.run_id === sourceRunId);
  const strategies = sourceRun?.stats?.accepted ?? sourceRun?.accepted ?? 0;

  const altCount = config?.cross_market.alternate_symbols?.length ?? 0;
  const perStrategy = config ? (
    (config.parameter.enabled ? config.parameter.rounds : 0)
    + (config.price_cost.enabled ? config.price_cost.rounds * 2 : 0)
    + (config.shuffle.enabled ? config.shuffle.rounds : 0)
    + (config.monte_carlo.enabled ? config.monte_carlo.rounds : 0)
    + (config.cross_market.enabled ? altCount : 0)
  ) : 0;
  const totalTests = strategies * perStrategy;

  function patch<K extends keyof StressConfig>(key: K, value: StressConfig[K]) {
    setConfig((c) => (c ? { ...c, [key]: value } : c));
  }
  function patchStage(stage: keyof StressConfig, key: string,
    value: number | boolean | string | string[] | null) {
    setConfig((c) => (c ? { ...c, [stage]: { ...(c[stage] as any), [key]: value } } : c));
  }

  async function start() {
    if (!config) return;
    setStarting(true); setError(null);
    try {
      const r = await api.post<{ stress_id: string }>("/api/stress",
        { config, name: config.bank_name || undefined });
      setStressId(r.stress_id);
      await refreshList();
    } catch (e) { setError(String(e)); }
    finally { setStarting(false); }
  }

  async function stop() {
    if (!stressId) return;
    try { await api.post(`/api/stress/${stressId}/stop`); }
    catch (e) { setError(String(e)); }
  }

  const live = current && ["queued", "running", "stopping"].includes(current.status);
  const done = current?.strategies_done ?? 0;
  const total = current?.strategies_total ?? 0;
  const progress = total ? Math.min(100, (done / total) * 100) : 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Stress test</h1>
          <p>Sequential five-stage robustness validation. A strategy must clear every stage before the next one runs.</p>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={() => goBanks(current?.bank_id || undefined)}>Strategy banks</button>
          {live && <button className="btn danger" onClick={stop}>Stop</button>}
        </div>
      </header>
      <ErrorBox error={error} />

      {!config ? (
        <div className="empty">Import data and accept some strategies first.</div>
      ) : (
        <Section title="Pipeline configuration"
          subtitle="Stages run in order. The first failure rejects the strategy; later stages are recorded as not run.">
          <div className="form-grid">
            <label className="field span-2">
              <span className="field-label">Source run</span>
              <select value={sourceRunId} onChange={(e) => setSourceRunId(e.target.value)}>
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id} · {r.name || r.status} · {r.stats?.accepted ?? 0} accepted
                  </option>
                ))}
              </select>
              <span className="hint">{strategies} accepted strategies will be tested.</span>
            </label>
            <label className="field">
              <span className="field-label">Bank name (optional)</span>
              <input value={config.bank_name || ""} placeholder="Stress survivors"
                onChange={(e) => patch("bank_name", e.target.value || null)} />
            </label>
            <label className="field">
              <span className="field-label">Random seed</span>
              <Num value={config.seed} step={1} onChange={(v) => patch("seed", v)} />
            </label>
          </div>

          <div className="two-col" style={{ marginTop: 20 }}>
            <article className="section inner">
              <div className="section-head">
                <div><h2>1 · Parameter sensitivity</h2>
                  <p className="muted">Perturb optimised parameters by ±10–20%.</p></div>
                <Toggle value={config.parameter.enabled}
                  onChange={(v) => patchStage("parameter", "enabled", v)} />
              </div>
              <div className="form-grid">
                <label className="field"><span className="field-label">Rounds</span>
                  <Num value={config.parameter.rounds} min={15} max={50} step={1}
                    onChange={(v) => patchStage("parameter", "rounds", v)} /></label>
                <label className="field"><span className="field-label">Perturb min</span>
                  <Num value={config.parameter.perturb_min_pct} min={0} max={100} step={1}
                    suffix="% ±" onChange={(v) => patchStage("parameter", "perturb_min_pct", v)} /></label>
                <label className="field"><span className="field-label">Perturb max</span>
                  <Num value={config.parameter.perturb_max_pct} min={0} max={100} step={1}
                    suffix="% ±" onChange={(v) => patchStage("parameter", "perturb_max_pct", v)} /></label>
                <label className="field"><span className="field-label">Max Sharpe drop</span>
                  <Num value={config.parameter.max_sharpe_drop_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("parameter", "max_sharpe_drop_pct", v)} /></label>
                <label className="field"><span className="field-label">Max PF drop</span>
                  <Num value={config.parameter.max_profit_factor_drop_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("parameter", "max_profit_factor_drop_pct", v)} /></label>
                <label className="field"><span className="field-label">Profitable variants</span>
                  <Num value={config.parameter.min_profitable_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("parameter", "min_profitable_pct", v)} /></label>
              </div>
            </article>

            <article className="section inner">
              <div className="section-head">
                <div><h2>2 · Price &amp; cost sensitivity</h2>
                  <p className="muted">Gaussian H/L/C noise plus 2–3× slippage and commission.</p></div>
                <Toggle value={config.price_cost.enabled}
                  onChange={(v) => patchStage("price_cost", "enabled", v)} />
              </div>
              <div className="form-grid">
                <label className="field"><span className="field-label">Rounds</span>
                  <Num value={config.price_cost.rounds} min={1} max={50} step={1}
                    onChange={(v) => patchStage("price_cost", "rounds", v)} /></label>
                <label className="field"><span className="field-label">Noise min</span>
                  <Num value={config.price_cost.noise_min_pct} min={0} max={5} step={0.01}
                    suffix="% σ" onChange={(v) => patchStage("price_cost", "noise_min_pct", v)} /></label>
                <label className="field"><span className="field-label">Noise max</span>
                  <Num value={config.price_cost.noise_max_pct} min={0} max={5} step={0.01}
                    suffix="% σ" onChange={(v) => patchStage("price_cost", "noise_max_pct", v)} /></label>
                <label className="field"><span className="field-label">Max Sharpe drop</span>
                  <Num value={config.price_cost.max_sharpe_drop_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("price_cost", "max_sharpe_drop_pct", v)} /></label>
                <label className="field"><span className="field-label">Cost multiplier min</span>
                  <Num value={config.price_cost.cost_multiplier_min} min={1} max={10} step={0.1}
                    suffix="×" onChange={(v) => patchStage("price_cost", "cost_multiplier_min", v)} /></label>
                <label className="field"><span className="field-label">Cost multiplier max</span>
                  <Num value={config.price_cost.cost_multiplier_max} min={1} max={10} step={0.1}
                    suffix="×" onChange={(v) => patchStage("price_cost", "cost_multiplier_max", v)} /></label>
                <label className="field"><span className="field-label">Cost-stress profitable</span>
                  <Num value={config.price_cost.min_profitable_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("price_cost", "min_profitable_pct", v)} /></label>
                <label className="field"><span className="field-label">Baseline slippage</span>
                  <Num value={config.price_cost.baseline_slippage_pips ?? 0} min={0} step={0.1}
                    suffix="pips" onChange={(v) => patchStage("price_cost", "baseline_slippage_pips", v)} />
                  <span className="hint">0 uses the strategy; if both costs are zero a small stress floor is applied.</span></label>
                <label className="field"><span className="field-label">Baseline commission</span>
                  <Num value={config.price_cost.baseline_commission_per_lot ?? 0} min={0} step={0.5}
                    suffix="per lot" onChange={(v) => patchStage("price_cost", "baseline_commission_per_lot", v)} /></label>
              </div>
            </article>

            <article className="section inner">
              <div className="section-head">
                <div><h2>3 · Trade shuffling</h2>
                  <p className="muted">Reshuffle the trade return vector and measure the drawdown distribution.</p></div>
                <Toggle value={config.shuffle.enabled}
                  onChange={(v) => patchStage("shuffle", "enabled", v)} />
              </div>
              <div className="form-grid">
                <label className="field"><span className="field-label">Rounds</span>
                  <Num value={config.shuffle.rounds} min={1000} max={5000} step={100}
                    onChange={(v) => patchStage("shuffle", "rounds", v)} /></label>
                <label className="field"><span className="field-label">p95 MaxDD multiple</span>
                  <Num value={config.shuffle.p95_max_dd_multiple} min={1} max={10} step={0.1}
                    suffix="×" onChange={(v) => patchStage("shuffle", "p95_max_dd_multiple", v)} /></label>
                <label className="field"><span className="field-label">p99 MaxDD multiple</span>
                  <Num value={config.shuffle.p99_max_dd_multiple} min={1} max={10} step={0.1}
                    suffix="×" onChange={(v) => patchStage("shuffle", "p99_max_dd_multiple", v)} /></label>
              </div>
            </article>

            <article className="section inner">
              <div className="section-head">
                <div><h2>4 · Monte Carlo price permutation</h2>
                  <p className="muted">Block-bootstrap or GARCH synthetic M1 price paths.</p></div>
                <Toggle value={config.monte_carlo.enabled}
                  onChange={(v) => patchStage("monte_carlo", "enabled", v)} />
              </div>
              <div className="form-grid">
                <label className="field"><span className="field-label">Rounds</span>
                  <Num value={config.monte_carlo.rounds} min={500} max={1000} step={50}
                    onChange={(v) => patchStage("monte_carlo", "rounds", v)} /></label>
                <label className="field"><span className="field-label">Method</span>
                  <Seg value={config.monte_carlo.method}
                    options={[{ value: "block_bootstrap", label: "Block bootstrap" },
                      { value: "garch", label: "GARCH(1,1)" }]}
                    onChange={(v) => patchStage("monte_carlo", "method", v)} /></label>
                <label className="field"><span className="field-label">Block size</span>
                  <Num value={config.monte_carlo.block_size} min={10} max={5000} step={10}
                    suffix="bars" onChange={(v) => patchStage("monte_carlo", "block_size", v)} /></label>
                <label className="field"><span className="field-label">Profitable paths</span>
                  <Num value={config.monte_carlo.min_profitable_pct} min={0} max={100} step={1}
                    suffix="%" onChange={(v) => patchStage("monte_carlo", "min_profitable_pct", v)} /></label>
              </div>
            </article>

            <article className="section inner span-2">
              <div className="section-head">
                <div><h2>5 · Cross-market validation</h2>
                  <p className="muted">Run the unchanged rules on alternate instruments.</p></div>
                <Toggle value={config.cross_market.enabled}
                  onChange={(v) => patchStage("cross_market", "enabled", v)} />
              </div>
              <div className="form-grid">
                <label className="field span-2"><span className="field-label">Alternate symbols</span>
                  <input value={(config.cross_market.alternate_symbols || []).join(", ")}
                    placeholder="EURUSD, GBPUSD"
                    onChange={(e) => patchStage("cross_market", "alternate_symbols",
                      e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
                  <span className="hint">Leave empty to use every other imported symbol with the same timeframe.</span>
                </label>
                <label className="field"><span className="field-label">Minimum average PF</span>
                  <Num value={config.cross_market.min_avg_profit_factor} min={0} step={0.05}
                    onChange={(v) => patchStage("cross_market", "min_avg_profit_factor", v)} /></label>
              </div>
            </article>
          </div>

          <div className="callout" style={{ marginTop: 20 }}>
            <strong>{head(totalTests)}</strong> scenarios up front
            <span className="muted"> · {perStrategy} per strategy × {strategies} strategies.
              Stages run sequentially, so a failing strategy stops early.</span>
          </div>
          <div className="head-actions" style={{ marginTop: 16 }}>
            <button className="btn primary" disabled={!strategies || !perStrategy || starting}
              onClick={start}>{starting ? "Starting…" : "Start stress pipeline"}</button>
            {!strategies && <span className="hint">The source run has no accepted strategies.</span>}
          </div>
        </Section>
      )}

      <Section title="Stress runs" subtitle="Select a run to inspect stage verdicts and survivors.">
        <div className="run-list">
          {stressRuns.length === 0 && <div className="empty">No stress pipeline runs yet.</div>}
          {stressRuns.map((r) => {
            const stats = (r.stats || {}) as any;
            return (
              <button key={r.stress_id}
                className={`run-card ${stressId === r.stress_id ? "selected" : ""}`}
                onClick={() => setStressId(r.stress_id)}>
                <div>
                  <div className="mono">{r.stress_id} <span className="muted">{r.name}</span></div>
                  <div className="muted">source {r.source_run_id} · {String(r.created_at || "").slice(0, 19)}</div>
                </div>
                <div className="run-card-right">
                  <Badge tone={r.status === "completed" ? "good" : r.status === "running" ? "accent"
                    : r.status === "failed" ? "bad" : "neutral"}>{r.status}</Badge>
                  <span className="mono">{stats.survivors ?? 0}/{stats.strategies_total ?? 0} survived</span>
                  {r.bank_id && <Badge tone="accent">{r.bank_id}</Badge>}
                </div>
              </button>
            );
          })}
        </div>
      </Section>

      {current && (
        <>
          <div className="run-hero">
            <div className="run-progress">
              <div className="run-progress-head">
                <Badge tone={current.status === "completed" ? "good"
                  : current.status === "failed" ? "bad"
                  : current.status === "stopped" ? "warn" : "accent"}>{current.status}</Badge>
                <span className="mono">{done} / {total} strategies</span>
              </div>
              <div className="progress"><div style={{ width: `${progress}%` }} /></div>
              <div className="run-sub">
                <span>{head(current.tests_run ?? 0)} / {head(current.tests_requested ?? 0)} scenarios</span>
                <span>{fmt(current.throughput ?? 0, 2)} / s</span>
                <span>{fmt(current.elapsed ?? 0, 0)} s</span>
                <span>{current.survivors ?? 0} survivors</span>
              </div>
              {current.current && (
                <div className="callout" style={{ marginTop: 12 }}>
                  Testing <span className="mono">{current.current.strategy_id}</span>
                  {current.current.stage && <> · stage {current.current.stage_index} · {current.current.stage}</>}
                  {current.current.stage && <> · {current.current.stage_tests_run ?? 0} scenarios run</>}
                </div>
              )}
            </div>
            <div className="stats4">
              <div className="stat"><span className="stat-label">Strategies</span>
                <span className="stat-value">{total}</span></div>
              <div className="stat"><span className="stat-label">Survivors</span>
                <span className="stat-value">{current.survivors ?? 0}</span></div>
              <div className="stat"><span className="stat-label">Stages</span>
                <span className="stat-value">{current.stages_requested ?? 5}</span></div>
              <div className="stat"><span className="stat-label">Strategies left</span>
                <span className="stat-value">{Math.max(0, total - done)}</span></div>
            </div>
          </div>

          {current.error && <ErrorBox error={current.error} />}

          {current.bank_id && (
            <div className="callout" style={{ marginBottom: 18 }}>
              Survivors were written to bank <span className="mono">{current.bank_id}</span>
              <button className="btn small" style={{ marginLeft: 12 }}
                onClick={() => goBanks(current.bank_id || undefined)}>Open bank</button>
            </div>
          )}

          <Section title="Strategy verdicts"
            subtitle="Sequential walk: a failed stage marks the remaining stages as not run.">
            <table className="table">
              <thead><tr>
                <th>Strategy</th><th>Verdict</th><th>Checks</th>
                {STAGE_KEYS.map((k) => <th key={k}>{STAGE_SHORT[k]}</th>)}
              </tr></thead>
              <tbody>
                {(current.results || []).slice(0, 30).map((r: StressResult) => (
                  <tr key={r.strategy_id}>
                    <td className="mono">{r.strategy_id}</td>
                    <td><Badge tone={r.survival ? "good" : "bad"}>
                      {r.survival ? "survived" : "failed"}</Badge></td>
                    <td className="mono">{pct(r.pass_rate * 100, 0)}</td>
                    {STAGE_KEYS.map((k) => {
                      const s: StressStageResult | undefined = r.stages?.[k];
                      return <td key={k}>{s ? (
                        <Badge tone={stageTone(s.status)}>{s.status.replace("_", " ")}</Badge>
                      ) : <span className="muted">—</span>}</td>;
                    })}
                  </tr>
                ))}
                {!(current.results || []).length && (
                  <tr><td colSpan={8} className="muted">Waiting for the first verdict…</td></tr>
                )}
              </tbody>
            </table>
          </Section>
        </>
      )}
    </div>
  );
}
