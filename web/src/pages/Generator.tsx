import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, fmt } from "../api";
import {
  Badge, DaysPicker, ErrorBox, Field, Num, RangeRow, Section, Seg, Toggle,
} from "../components";
import type {
  Config, CsvFile, IndicatorDef, Policy, RunInfo, SymbolInfo, Trailing,
} from "../types";

const TFS = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function PolicyEditor({ title, subtitle, policy, onChange }: {
  title: string; subtitle: string; policy: Policy; onChange: (p: Policy) => void;
}) {
  const set = (patch: Partial<Policy>) => onChange({ ...policy, ...patch });
  return (
    <div className="policy">
      <div className="policy-head">
        <div><h3>{title}</h3><p>{subtitle}</p></div>
        <Seg value={policy.mode}
          options={[{ value: "always", label: "Always" }, { value: "never", label: "Never" },
            { value: "may", label: "May use" }]}
          onChange={(v) => set({ mode: v })} />
      </div>
      {policy.mode !== "never" && (
        <div className="policy-body">
          <Seg value={policy.type} small
            options={[{ value: "fixed", label: "Fixed pips" }, { value: "atr", label: "ATR based" }]}
            onChange={(v) => set({ type: v })} />
          {policy.type === "fixed" ? (
            <div className="param-block">
              <span className="param-title">Pips range</span>
              <RangeRow min={policy.pips_min} max={policy.pips_max} step={policy.pips_step}
                onChange={(v) => set({ pips_min: v.min, pips_max: v.max, pips_step: v.step })} />
            </div>
          ) : (
            <div className="param-block">
              <span className="param-title">ATR period range</span>
              <RangeRow min={policy.atr_period_min} max={policy.atr_period_max} step={policy.atr_period_step}
                onChange={(v) => set({ atr_period_min: v.min, atr_period_max: v.max, atr_period_step: v.step })} />
              <span className="param-title">ATR multiplier range</span>
              <RangeRow min={policy.atr_mult_min} max={policy.atr_mult_max} step={policy.atr_mult_step}
                onChange={(v) => set({ atr_mult_min: v.min, atr_mult_max: v.max, atr_mult_step: v.step })} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function GeneratorPage({ config, setConfig, catalogue, symbols, refresh, onStarted, go }: {
  config: Config; setConfig: (c: Config) => void; catalogue: IndicatorDef[];
  symbols: SymbolInfo[]; refresh: () => Promise<void>;
  onStarted: (id: string) => void; go: (p: string) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [pre, setPre] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<CsvFile[]>([]);
  const [importing, setImporting] = useState<string | null>(null);
  const [buildingTf, setBuildingTf] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [active, setActive] = useState<RunInfo | null>(null);
  const [profiles, setProfiles] = useState<{ name: string; config: Config }[]>(() => {
    try { return JSON.parse(localStorage.getItem("sqx.profiles") || "[]"); } catch { return []; }
  });

  function persistProfiles(next: { name: string; config: Config }[]) {
    setProfiles(next);
    try { localStorage.setItem("sqx.profiles", JSON.stringify(next)); } catch {}
  }

  function saveProfile() {
    const name = window.prompt("Profile name");
    if (!name) return;
    const next = [...profiles.filter((p) => p.name !== name), { name, config: structuredClone(config) }];
    persistProfiles(next);
  }

  function applyProfile(name: string) {
    const p = profiles.find((x) => x.name === name);
    if (p) setConfig(structuredClone(p.config));
  }

  function deleteProfile(name: string) {
    persistProfiles(profiles.filter((p) => p.name !== name));
  }

  function applyPreset(name: "conservative" | "balanced" | "aggressive") {
    upd((c) => {
      if (name === "conservative") {
        c.target_accepted = 25;
        c.autocorrelation.enabled = true;
        c.autocorrelation.threshold = 0.97;
        c.acceptance.is_gates.min_trades = 30;
        c.acceptance.oos_gates.min_trades = 15;
      } else if (name === "aggressive") {
        c.target_accepted = 100;
        c.autocorrelation.enabled = true;
        c.autocorrelation.threshold = 0.9;
        c.acceptance.is_gates.min_trades = 10;
        c.acceptance.oos_gates.min_trades = 5;
      } else {
        c.target_accepted = 50;
        c.autocorrelation.enabled = true;
        c.autocorrelation.threshold = 0.95;
        c.acceptance.is_gates.min_trades = 20;
        c.acceptance.oos_gates.min_trades = 10;
      }
    });
  }
  const upd = (fn: (c: Config) => void) => {
    const c = structuredClone(config); fn(c); setConfig(c);
  };
  async function exportIndicators() {
    setError(null); setMessage(null);
    try {
      const r = await fetch("/api/indicators/export");
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "SQX_indicators.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setMessage("Exported indicators to SQX_indicators.zip");
    } catch (e) { setError(String(e)); }
  }

  const enabled = catalogue.filter((i) => config.indicators[i.id]?.enabled).length;

  const loadFiles = useCallback(async () => {
    try {
      const r = await api.get<{ files: CsvFile[] }>("/api/data/files");
      setFiles(r.files);
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { loadFiles(); }, [loadFiles, symbols]);

  // The backend runs one generation at a time. While one is live this page
  // shows its progress instead of the form, so leaving and returning here does
  // not look like the run was lost.
  useEffect(() => {
    let alive = true;
    async function probe() {
      try {
        const run = await api.get<RunInfo | null>("/api/runs/active");
        if (alive) setActive(run);
      } catch { /* the form still works without the live probe */ }
    }
    probe();
    const timer = setInterval(probe, 2000);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  // Symbols from the Data tab plus detected CSVs that have not been imported yet.
  const available = useMemo(() => {
    const out: { symbol: string; imported: boolean; file?: CsvFile }[] = [];
    const seen = new Set<string>();
    for (const s of symbols) {
      out.push({ symbol: s.symbol, imported: true });
      seen.add(s.symbol);
    }
    for (const f of files) {
      if (!f.symbol || seen.has(f.symbol)) continue;
      out.push({ symbol: f.symbol, imported: false, file: f });
      seen.add(f.symbol);
    }
    return out;
  }, [symbols, files]);

  async function toggleSymbol(entry: { symbol: string; imported: boolean; file?: CsvFile }) {
    const sym = entry.symbol;
    if (config.symbols.includes(sym)) {
      upd((c) => { c.symbols = c.symbols.filter((x) => x !== sym); });
      return;
    }
    if (!entry.imported) {
      if (!entry.file) return;
      setImporting(sym); setError(null); setMessage(null);
      try {
        const r = await api.post<any>("/api/data/import", {
          path: entry.file.path, symbol: entry.file.symbol,
        });
        let built = "";
        if (config.timeframe !== "M1") {
          await api.post<any>("/api/data/resample", {
            symbol: r.symbol, timeframes: [config.timeframe],
          });
          built = ` and built ${config.timeframe}`;
        }
        setMessage(`Imported ${r.symbol}: ${r.rows.toLocaleString()} bars${built}`);
        await refresh();
        await loadFiles();
      } catch (e) {
        setError(`Could not import ${sym}: ${e}`);
        setImporting(null);
        return;
      }
      setImporting(null);
    }
    upd((c) => { if (!c.symbols.includes(sym)) c.symbols = [...c.symbols, sym]; });
  }

  // Selected instruments that have no bars cached for the chosen timeframe.
  const missingTf = useMemo(() => {
    if (config.timeframe === "M1") return [];
    return config.symbols.filter((sym) => {
      const info = symbols.find((s) => s.symbol === sym);
      return info && !(info.bar_caches || []).some((b) => b.timeframe === config.timeframe);
    });
  }, [config.symbols, config.timeframe, symbols]);

  async function buildTimeframe() {
    setBuildingTf(true); setError(null); setMessage(null);
    try {
      for (const sym of missingTf) {
        await api.post<any>("/api/data/resample", { symbol: sym, timeframes: [config.timeframe] });
      }
      setMessage(`Built ${config.timeframe} bars for ${missingTf.join(", ")}`);
      await refresh();
    } catch (e) { setError(String(e)); }
    setBuildingTf(false);
  }

  async function preflight() {
    setError(null);
    try { setPre(await api.post<any>("/api/preflight", config)); }
    catch (e) { setError(String(e)); }
  }

  async function requestStart() {
    setBusy(true); setError(null);
    try {
      const p = await api.post<any>("/api/preflight", config);
      setPre(p);
      setConfirming(true);
      if (!p.ok) setError(p.errors.join(" · "));
    } catch (e) { setError(String(e)); }
    setBusy(false);
  }

  async function confirmStart() {
    setBusy(true); setError(null);
    try {
      const p = pre ?? await api.post<any>("/api/preflight", config);
      setPre(p);
      if (!p.ok) { setError(p.errors.join(" · ")); setBusy(false); return; }
      const r = await api.post<any>("/api/runs", { config });
      onStarted(r.run_id);
      setConfirming(false);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  }

  async function stopActive() {
    if (!active) return;
    try { await api.post(`/api/runs/${active.run_id}/stop`); }
    catch (e) { setError(String(e)); }
  }

  if (active) {
    return (
      <div className="page">
        <header className="page-head">
          <div>
            <h1>Generator</h1>
            <p>A generation is in progress. Only one can run at a time.</p>
          </div>
          <div className="head-actions">
            <button className="btn" onClick={() => onStarted(active.run_id)}>Open run view</button>
          </div>
        </header>
        <ErrorBox error={error} />
        <ActiveRun run={active} onOpen={() => onStarted(active.run_id)} onStop={stopActive} />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div><h1>Generator</h1><p>Everything the search will sample, and every gate it must clear.</p></div>
        <div className="head-actions">
          <button className="btn" onClick={preflight}>Preflight</button>
          <button className="btn primary" onClick={requestStart} disabled={busy}>
            {busy ? "Checking…" : "Start run"}
          </button>
        </div>
      </header>

      <ErrorBox error={error} />
      <div className="profile-bar">
        <select value="" onChange={(e) => { if (e.target.value) applyProfile(e.target.value); e.target.value = ""; }}>
          <option value="">Load profile…</option>
          {profiles.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
        </select>
        <button className="btn tiny" onClick={saveProfile}>Save current as profile</button>
        {profiles.length > 0 && (
          <select value="" onChange={(e) => { if (e.target.value) deleteProfile(e.target.value); e.target.value = ""; }}>
            <option value="">Delete profile…</option>
            {profiles.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select>
        )}
      </div>
      {message && <div className="ok-box">{message}</div>}
      {pre && (
        <div className={`preflight ${pre.ok ? "ok" : "bad"}`}>
          <div className="preflight-main">
            {pre.ok ? "Configuration ready" : "Fix the configuration first"}
            <span>{enabled} indicators enabled · OOS {pre.oos_pct}% · {pre.indicator_variants.toLocaleString()} indicator variants</span>
          </div>
          <div className="preflight-side">estimated ~{Math.max(1, Math.round(pre.rough_seconds / 60))} min for the target</div>
          {pre.errors?.length > 0 && <ul>{pre.errors.map((e: string) => <li key={e}>{e}</li>)}</ul>}
          {pre.warnings?.length > 0 && <ul className="warn">{pre.warnings.map((e: string) => <li key={e}>{e}</li>)}</ul>}
        </div>
      )}

      {/* confirmation panel rendered at the bottom near the start button */}

      <Section title="Instrument & split" subtitle="Signals on the selected timeframe. Every fill is simulated on the M1 series.">
        <div className="form-grid">
          <Field label="Symbols" className="span-2"
            hint="Every instrument from the Data tab is selectable. A detected CSV that is not imported yet is imported the first time you select it.">
            <div className="chips">
              {available.map((s) => (
                <button key={s.symbol}
                  className={`chip ${config.symbols.includes(s.symbol) ? "active" : ""} ${!s.imported ? "pending" : ""}`}
                  title={s.imported ? s.symbol : `${s.symbol} — not imported yet, selecting imports it`}
                  disabled={importing !== null}
                  onClick={() => toggleSymbol(s)}>
                  {importing === s.symbol && <span className="chip-dot" />}
                  {s.symbol}
                  {!s.imported && importing !== s.symbol && <span className="chip-tag">new</span>}
                </button>
              ))}
              {available.length === 0 && <span className="muted">Import a CSV first</span>}
            </div>
            {missingTf.length > 0 && (
              <div className="inline-note">
                <Badge tone="warn">{config.timeframe} not built</Badge>
                <span className="muted">{missingTf.join(", ")}</span>
                <button className="btn tiny" onClick={buildTimeframe} disabled={buildingTf || importing !== null}>
                  {buildingTf ? "Building…" : `Build ${config.timeframe}`}
                </button>
              </div>
            )}
          </Field>
          <Field label="Timeframe">
            <select value={config.timeframe} onChange={(e) => upd((c) => { c.timeframe = e.target.value; })}>
              {TFS.map((t) => <option key={t}>{t}</option>)}
            </select>
          </Field>
          <Field label="Trade direction">
            <Seg value={config.direction}
              options={[{ value: "long", label: "Long" }, { value: "short", label: "Short" },
                { value: "both", label: "Both" }]}
              onChange={(v) => upd((c) => { c.direction = v; })} />
          </Field>
          <Field label="Out-of-sample share" hint="Last N% of each instrument's history is held out." className="span-2">
            <div className="slider-row">
              <input type="range" min={10} max={60} step={5} value={config.oos_pct}
                onChange={(e) => upd((c) => { c.oos_pct = Number(e.target.value); })} />
              <Num value={config.oos_pct} min={10} max={60} step={5} suffix="%" onChange={(v) => upd((c) => { c.oos_pct = v; })} />
              {[10, 20, 30, 40, 50].map((v) => (
                <button key={v} className={`mini ${config.oos_pct === v ? "active" : ""}`}
                  onClick={() => upd((c) => { c.oos_pct = v; })}>{v}%</button>
              ))}
            </div>
          </Field>
          <Field label="Accepted target" hint="The run loops until this count is reached or you stop it. No attempt cap.">
            <Num value={config.target_accepted} min={1} step={10}
              onChange={(v) => upd((c) => { c.target_accepted = v; })} />
          </Field>
          <Field label="Random seed" hint="Same seed + config + data = same accepted set.">
            <Num value={config.seed} onChange={(v) => upd((c) => { c.seed = v; })} />
          </Field>
        </div>
      </Section>

      <Section title="Entry execution" subtitle="Market fills on the next M1 bar. Limit and stop entries become pending orders at a sampled distance.">
        <div className="form-grid">
          <Field label="Allowed order types" className="span-2" hint="Pick one or more. Pending orders are sampled per strategy; market cannot be combined with pending orders on the same signal.">
            <div className="chips">
              {(["market", "limit", "stop"] as const).map((t) => {
                const active = config.orders.types.includes(t);
                return (
                  <button key={t} className={`chip ${active ? "active" : ""}`}
                    onClick={() => upd((c) => {
                      const set = new Set(c.orders.types);
                      if (set.has(t)) set.delete(t); else set.add(t);
                      c.orders.types = (["market", "limit", "stop"] as const)
                        .filter((x) => set.has(x));
                      if (c.orders.types.length === 0) c.orders.types = ["market"];
                      if (c.orders.types.includes("market") && c.orders.types.length > 1) {
                        c.orders.types = c.orders.types.filter((x) => x !== "market");
                      }
                    })}>{t[0].toUpperCase() + t.slice(1)}</button>
                );
              })}
            </div>
          </Field>
          <Field label="Max simultaneous orders" hint="Limit/stop orders are placed together and the first fill cancels the rest.">
            <Num value={config.orders.max_orders} min={1} max={3} step={1}
              onChange={(v) => upd((c) => { c.orders.max_orders = v; })} />
          </Field>
          {config.orders.types.includes("market") ? (
            <Field label="Fill" className="span-2">
              <span className="hint">Market-only: the entry signal fills at the next M1 open.</span>
            </Field>
          ) : (
            <>
              <Field label="Distance">
                <Seg value={config.orders.distance_type}
                  options={[{ value: "fixed", label: "Fixed pips" }, { value: "atr", label: "ATR based" }]}
                  onChange={(v) => upd((c) => { c.orders.distance_type = v; })} />
              </Field>
              <Field label="Expiry">
                <div className="param-block">
                  <span className="param-title">Pending order lifetime (bars)</span>
                  <RangeRow min={config.orders.expiry_bars_min} max={config.orders.expiry_bars_max}
                    step={config.orders.expiry_bars_step}
                    onChange={(v) => upd((c) => {
                      c.orders.expiry_bars_min = v.min;
                      c.orders.expiry_bars_max = v.max;
                      c.orders.expiry_bars_step = v.step;
                    })} />
                </div>
              </Field>
              {config.orders.distance_type === "fixed" ? (
                <Field label="Distance range" className="span-2">
                  <div className="param-block">
                    <span className="param-title">Pips from signal price</span>
                    <RangeRow min={config.orders.pips_min} max={config.orders.pips_max}
                      step={config.orders.pips_step}
                      onChange={(v) => upd((c) => {
                        c.orders.pips_min = v.min; c.orders.pips_max = v.max;
                        c.orders.pips_step = v.step;
                      })} />
                  </div>
                </Field>
              ) : (
                <Field label="Distance range" className="span-2">
                  <div className="param-block">
                    <span className="param-title">ATR period range</span>
                    <RangeRow min={config.orders.atr_period_min} max={config.orders.atr_period_max}
                      step={config.orders.atr_period_step}
                      onChange={(v) => upd((c) => {
                        c.orders.atr_period_min = v.min;
                        c.orders.atr_period_max = v.max;
                        c.orders.atr_period_step = v.step;
                      })} />
                    <span className="param-title">ATR multiplier range</span>
                    <RangeRow min={config.orders.atr_mult_min} max={config.orders.atr_mult_max}
                      step={config.orders.atr_mult_step}
                      onChange={(v) => upd((c) => {
                        c.orders.atr_mult_min = v.min;
                        c.orders.atr_mult_max = v.max;
                        c.orders.atr_mult_step = v.step;
                      })} />
                  </div>
                </Field>
              )}
            </>
          )}
        </div>
      </Section>

      <Section title="Account & costs" subtitle="Position sizing and the execution cost model.">
        <div className="form-grid">
          <Field label="Sizing">
            <Seg value={config.risk.sizing}
              options={[{ value: "fixed_lot", label: "Fixed lot" }, { value: "risk_percent", label: "Risk %" }]}
              onChange={(v) => upd((c) => { c.risk.sizing = v; })} />
          </Field>
          {config.risk.sizing === "fixed_lot" ? (
            <Field label="Lot size">
              <Num value={config.risk.fixed_lot} step={0.01} min={0.01}
                onChange={(v) => upd((c) => { c.risk.fixed_lot = v; })} />
            </Field>
          ) : (
            <Field label="Risk per trade">
              <Num value={config.risk.risk_percent} step={0.1} min={0.01} suffix="%"
                onChange={(v) => upd((c) => { c.risk.risk_percent = v; })} />
            </Field>
          )}
          <Field label="Initial balance">
            <Num value={config.risk.balance} step={1000} min={100}
              onChange={(v) => upd((c) => { c.risk.balance = v; })} />
          </Field>
          <Field label="Leverage cap">
            <Num value={config.risk.leverage} step={1} min={1} suffix="x"
              onChange={(v) => upd((c) => { c.risk.leverage = v; })} />
          </Field>
          <Field label="Spread" hint="Round-turn spread in pips.">
            <Num value={config.risk.spread_pips} step={0.1} min={0} suffix="pips"
              onChange={(v) => upd((c) => { c.risk.spread_pips = v; })} />
          </Field>
          <Field label="Commission">
            <Num value={config.risk.commission_per_lot} step={1} min={0} suffix="/lot"
              onChange={(v) => upd((c) => { c.risk.commission_per_lot = v; })} />
          </Field>
          <Field label="Slippage">
            <Num value={config.risk.slippage_pips} step={0.1} min={0} suffix="pips"
              onChange={(v) => upd((c) => { c.risk.slippage_pips = v; })} />
          </Field>
        </div>
      </Section>

      <Section title="Session & weekdays" subtitle="No new entries outside the window. Open trades are closed at the end hour.">
        <div className="form-grid">
          <Field label="Start hour (UTC)">
            <Num value={config.session.start_hour} min={0} max={23}
              onChange={(v) => upd((c) => { c.session.start_hour = v; })} />
          </Field>
          <Field label="End hour (UTC)">
            <Num value={config.session.end_hour} min={0} max={23}
              onChange={(v) => upd((c) => { c.session.end_hour = v; })} />
          </Field>
          <Field label="Close at end">
            <Toggle value={config.session.close_at_end}
              onChange={(v) => upd((c) => { c.session.close_at_end = v; })} label="Force close" />
          </Field>
          <Field label="Days traded" className="span-2">
            <DaysPicker days={config.session.days}
              onChange={(d) => upd((c) => { c.session.days = d; })} />
          </Field>
        </div>
        <p className="hint">Wrap-around windows are supported (e.g. 22 → 06). Hours use the session clock, default UTC.</p>
      </Section>

      <Section title="Rules" subtitle="How many conditions and how positions are allowed to exit.">
        <div className="form-grid">
          <Field label="Max entry indicators">
            <Num value={config.max_entry_indicators} min={1} max={6}
              onChange={(v) => upd((c) => { c.max_entry_indicators = v; })} />
          </Field>
          <Field label="Max exit indicators">
            <Num value={config.max_exit_indicators} min={0} max={6}
              onChange={(v) => upd((c) => { c.max_exit_indicators = v; })} />
          </Field>
          <Field label="Opposite entry signal" className="span-2">
            <Seg value={config.opposite_signal}
              options={[{ value: "ignore", label: "Ignore" }, { value: "reverse", label: "Reverse" },
                { value: "ignore_or_reverse", label: "Ignore or reverse" }]}
              onChange={(v) => upd((c) => { c.opposite_signal = v; })} />
          </Field>
        </div>
      </Section>

      <div className="policy-grid">
        <PolicyEditor title="Stop loss" subtitle="Always, never, or sampled per strategy."
          policy={config.stop_loss}
          onChange={(p) => upd((c) => { c.stop_loss = p; })} />
        <PolicyEditor title="Take profit" subtitle="Fixed pips or an ATR multiple."
          policy={config.take_profit}
          onChange={(p) => upd((c) => { c.take_profit = p; })} />
        <div className="policy">
          <div className="policy-head">
            <div><h3>Trailing stop</h3><p>ATR based, only when enabled.</p></div>
            <Toggle value={config.trailing.mode === "use"} label={config.trailing.mode === "use" ? "On" : "Off"}
              onChange={(v) => upd((c) => {
                c.trailing = { ...c.trailing, mode: v ? "use" : "none" } as Trailing;
              })} />
          </div>
          {config.trailing.mode === "use" && (
            <div className="policy-body">
              <div className="param-block">
                <span className="param-title">ATR period range</span>
                <RangeRow min={config.trailing.atr_period_min} max={config.trailing.atr_period_max}
                  step={config.trailing.atr_period_step}
                  onChange={(v) => upd((c) => {
                    c.trailing.atr_period_min = v.min; c.trailing.atr_period_max = v.max;
                    c.trailing.atr_period_step = v.step;
                  })} />
                <span className="param-title">ATR multiplier range</span>
                <RangeRow min={config.trailing.atr_mult_min} max={config.trailing.atr_mult_max}
                  step={config.trailing.atr_mult_step}
                  onChange={(v) => upd((c) => {
                    c.trailing.atr_mult_min = v.min; c.trailing.atr_mult_max = v.max;
                    c.trailing.atr_mult_step = v.step;
                  })} />
              </div>
            </div>
          )}
        </div>
      </div>

      <Section title="De-duplication" subtitle="Structural de-dupe always runs. Return correlation is optional.">
        <div className="form-grid">
          <Field label="Return correlation filter">
            <Toggle value={config.autocorrelation.enabled}
              onChange={(v) => upd((c) => { c.autocorrelation.enabled = v; })} label="Enabled" />
          </Field>
          <Field label="Correlation threshold" hint="Reject if correlation with any accepted strategy is at or above this.">
            <div className="slider-row">
              <input type="range" min={0.5} max={0.999} step={0.01}
                value={config.autocorrelation.threshold}
                onChange={(e) => upd((c) => { c.autocorrelation.threshold = Number(e.target.value); })} />
              <Num value={config.autocorrelation.threshold} step={0.01} min={0} max={1}
                onChange={(v) => upd((c) => { c.autocorrelation.threshold = v; })} />
            </div>
          </Field>
          <Field label="Method">
            <Seg value={config.autocorrelation.method}
              options={[{ value: "pearson", label: "Pearson" }, { value: "spearman", label: "Spearman" }]}
              onChange={(v) => upd((c) => { c.autocorrelation.method = v; })} />
          </Field>
        </div>
      </Section>

      <Section title="Indicators" subtitle="The indicator pool is shared with the Indicators page."
        right={<div className="head-actions">
          <button className="btn small" onClick={exportIndicators}>export</button>
          <button className="btn small" onClick={() => go("indicators")}>Edit pool</button>
        </div>}>
        <div className="pool-summary">
          <Badge tone="accent">{enabled} enabled</Badge>
          <span className="muted">{catalogue.length - enabled} disabled</span>
          <span className="muted">·</span>
          <span className="muted">Entry conditions are AND-joined; the generator samples 1–{config.max_entry_indicators} per side.</span>
        </div>
      </Section>

      <Section title="Acceptance gates" subtitle="IS and OOS thresholds are applied independently.">
        <div className="gate-preview">
          <GatePreview title="In sample" g={config.acceptance.is_gates} />
          <GatePreview title={`Out of sample (${config.oos_pct}%)`} g={config.acceptance.oos_gates} />
          <button className="btn" onClick={() => go("acceptance")}>Edit gates</button>
        </div>
      </Section>

      {confirming ? (
        <div className="sticky-actions" style={{ flexDirection: "column", alignItems: "stretch", gap: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <div>
              <strong>Confirm parameters and start generation</strong>
              <div className="muted" style={{ marginTop: 4 }}>
                {config.symbols.join(", ") || "no symbols"} · {config.timeframe} signals · OOS {config.oos_pct}% · target {config.target_accepted} accepted · seed {config.seed}
              </div>
              <div className="muted" style={{ marginTop: 2 }}>
                IS gates: trades≥{config.acceptance.is_gates.min_trades}, Sharpe≥{config.acceptance.is_gates.min_sharpe}, R/DD≥{config.acceptance.is_gates.min_return_dd}, stagnation≤{config.acceptance.is_gates.max_stagnation_days}d
              </div>
              <div className="muted" style={{ marginTop: 2 }}>
                OOS gates: trades≥{config.acceptance.oos_gates.min_trades}, Sharpe≥{config.acceptance.oos_gates.min_sharpe}, R/DD≥{config.acceptance.oos_gates.min_return_dd}, stagnation≤{config.acceptance.oos_gates.max_stagnation_days}d
              </div>
            </div>
            <div className="muted">
              {pre ? `estimated ~${Math.max(1, Math.round(pre.rough_seconds / 60))} min` : "checking…"}
            </div>
          </div>
          {pre?.errors?.length > 0 && <ul style={{ color: "#fca5a5", margin: 0, paddingLeft: 18 }}>{pre.errors.map((e: string) => <li key={e}>{e}</li>)}</ul>}
          {pre?.warnings?.length > 0 && <ul className="warn" style={{ margin: 0, paddingLeft: 18 }}>{pre.warnings.map((e: string) => <li key={e}>{e}</li>)}</ul>}
          <div className="head-actions" style={{ justifyContent: "flex-end" }}>
            <button className="btn" onClick={() => setConfirming(false)} disabled={busy}>Back</button>
            <button className="btn primary" onClick={confirmStart} disabled={busy || !pre?.ok}>
              {busy ? "Starting…" : "Confirm & start"}
            </button>
          </div>
        </div>
      ) : (
        <div className="sticky-actions">
          <div>
            <strong>{config.target_accepted}</strong> accepted strategies · <strong>{config.timeframe}</strong> signals · <strong>{config.oos_pct}%</strong> OOS
          </div>
          <div className="head-actions">
            <button className="btn" onClick={preflight}>Preflight</button>
            <button className="btn primary" onClick={requestStart} disabled={busy}>Start run</button>
          </div>
        </div>
      )}
    </div>
  );
}

function ActiveRun({ run, onOpen, onStop }: {
  run: RunInfo; onOpen: () => void; onStop: () => void;
}) {
  const accepted = run.accepted ?? run.stats?.accepted ?? 0;
  const target = run.target ?? run.stats?.target ?? run.config?.target_accepted ?? 0;
  const attempts = run.attempts ?? run.stats?.attempts ?? 0;
  const progress = target ? Math.min(100, (accepted / target) * 100) : 0;
  const stopping = run.status === "stopping";
  return (
    <section className="section">
      <div className="run-progress">
        <div className="run-progress-head">
          <Badge tone="accent">{run.status}</Badge>
          <span className="mono">{fmt(accepted, 0)} / {fmt(target, 0)} good strategies</span>
        </div>
        <div className="progress"><div style={{ width: `${progress}%` }} /></div>
        <div className="run-sub">
          <span>{fmt(attempts, 0)} ideas tested</span>
          <span>{fmt(run.throughput ?? 0, 2)} / s</span>
          <span>{fmt(run.elapsed ?? 0, 0)} s elapsed</span>
          {run.config && <span>{run.config.timeframe} · {run.config.symbols?.join(", ")}</span>}
        </div>
        <div className="head-actions" style={{ marginTop: 16 }}>
          <button className="btn" onClick={onOpen}>Open run view</button>
          <button className="btn danger" onClick={onStop} disabled={stopping}>
            {stopping ? "Stopping…" : "Stop generation"}
          </button>
        </div>
        <p className="hint">
          Run <span className="mono">{run.run_id}</span>{run.name ? ` · ${run.name}` : ""}. This form
          comes back when the generation stops, so there is never more than one running.
        </p>
      </div>
    </section>
  );
}

function GatePreview({ title, g }: { title: string; g: any }) {
  return (
    <div className="gate-preview-card">
      <h3>{title}</h3>
      <div><span>Trades ≥</span><b>{g.min_trades}</b></div>
      <div><span>Sharpe ≥</span><b>{g.min_sharpe}</b></div>
      <div><span>Return/DD ≥</span><b>{g.min_return_dd}</b></div>
      <div><span>Stagnation ≤</span><b>{g.max_stagnation_days} d</b></div>
    </div>
  );
}
