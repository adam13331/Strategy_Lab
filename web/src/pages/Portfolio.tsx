import React, { useEffect, useMemo, useState } from "react";
import { api, fmt, pct } from "../api";
import {
  Badge, ErrorBox, Field, LineChart, Loading, Num, Section,
} from "../components";
import type { BankInfo, BankStrategy } from "../types";

type StrategyItem = {
  strategy_id: string;
  symbol: string;
  timeframe: string;
  trades: number | null;
  sharpe: number | null;
  return_dd: number | null;
  has_data: boolean;
  months: number;
  monthly_vol: number;
  monthly_win_rate: number;
  included: boolean;
  reason: string;
};

type ProfileRule = {
  key: string; label: string; op: string; threshold: number;
  unit: string; weight: number; rule: string;
};
type StageCfg = Record<string, number | string | null>;
type StageMap = Record<string, StageCfg>;
type ProfileDef = {
  id: string; label: string; subtitle: string;
  rules: ProfileRule[]; notes: string[]; sources: string[];
  stage_defaults?: StageMap;
};
type ProfileCheck = {
  key: string; label: string; value: number | null; op: string;
  threshold: number; default_threshold: number; custom: boolean;
  unit: string; weight: number; rule: string;
  ok: boolean; points: number;
};
type ChallengeStage = {
  id: string; label: string; pass_rate: number; attempts: number;
  avg_days: number; breaches: Record<string, number>;
};
type Challenge = {
  attempts: number; overall_pass_rate: number; stages: ChallengeStage[];
  monthly: {
    mean: number; median: number; positive_pct: number; best: number;
    worst: number; payout: number; profit_split_pct: number; months: number;
  };
  heatmap: { years: number[]; values: (number | null)[][] };
};
type ProfileEval = {
  id: string; label: string; score: number;
  rules_passed: number; rules_total: number; all_passed: boolean;
  checks: ProfileCheck[];
};

type Portfolio = {
  id: string;
  label: string;
  weights: Record<string, number>;
  metrics: Record<string, number>;
  score: number;
  profile: ProfileEval;
  challenge?: Challenge;
  equity: { ts: string; equity: number }[];
};

type Composed = {
  bank_id: string;
  bank_name: string;
  base_balance: number;
  window: { start: string; end: string; months: number };
  max_corr: number;
  vol_target: number;
  strategies: StrategyItem[];
  selected: string[];
  avg_corr: number;
  correlation: { ids: string[]; matrix: (number | null)[][] };
  portfolios: Portfolio[];
  best: string;
  profile: ProfileDef;
  stages: StageMap | null;
};

function breachSummary(breaches: Record<string, number>): string {
  const names: Record<string, string> = {
    daily_loss: "daily loss", max_loss: "max loss", timeout: "timeout",
    consistency: "consistency", end_of_data: "ran out of data",
  };
  const entries = Object.entries(breaches).filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!entries.length) return "no breaches";
  return "failed: " + entries.slice(0, 2)
    .map(([k, v]) => `${names[k] || k} ${v}`).join(" · ");
}

function MonthlyHeatmap({ years, values }:
  { years: number[]; values: (number | null)[][] }) {
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="table">
        <thead>
          <tr><th>Year</th>{months.map((m) => <th key={m}>{m}</th>)}<th>Year</th></tr>
        </thead>
        <tbody>
          {years.map((year, i) => {
            const row = values[i] || [];
            let comp = 1;
            row.forEach((v) => { if (v !== null && v !== undefined) comp *= 1 + v / 100; });
            return (
              <tr key={year}>
                <td className="mono">{year}</td>
                {row.map((v, j) => (
                  <td key={j} className="mono" style={{
                    background: v === null || v === undefined ? undefined
                      : v >= 0
                        ? `rgba(34, 197, 94, ${Math.min(0.45, Math.abs(v) / 12 * 0.45)})`
                        : `rgba(239, 68, 68, ${Math.min(0.45, Math.abs(v) / 12 * 0.45)})`,
                  }}>
                    {v === null || v === undefined ? "—" : pct(v, 1)}
                  </td>
                ))}
                <td className="mono">{pct((comp - 1) * 100, 1)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function checkValue(c: ProfileCheck): string {
  if (c.value === null || c.value === undefined) return "—";
  const digits = ["active_days", "months"].includes(c.key) ? 0
    : c.key.endsWith("_pass_rate") ? 1 : 2;
  const text = fmt(c.value, digits);
  if (c.unit === "%") return `${text}%`;
  if (c.unit) return `${text} ${c.unit}`;
  return text;
}

const METRICS: { key: string; label: string; pct?: boolean; digits?: number }[] = [
  { key: "net_return", label: "Net return", pct: true },
  { key: "ann_return", label: "Annualised return", pct: true },
  { key: "ann_vol", label: "Annualised volatility", pct: true },
  { key: "sharpe", label: "Sharpe ratio" },
  { key: "sortino", label: "Sortino ratio" },
  { key: "max_dd", label: "Max drawdown", pct: true },
  { key: "return_dd", label: "Return / drawdown" },
  { key: "calmar", label: "Calmar ratio" },
  { key: "monthly_win_rate", label: "Monthly win rate", pct: true, digits: 1 },
  { key: "best_month", label: "Best month", pct: true },
  { key: "worst_month", label: "Worst month", pct: true },
];

function scoreTone(score: number): "good" | "warn" | "bad" {
  return score >= 70 ? "good" : score >= 50 ? "warn" : "bad";
}

export function PortfolioPage() {
  const [banks, setBanks] = useState<BankInfo[]>([]);
  const [bankId, setBankId] = useState("");
  const [rows, setRows] = useState<BankStrategy[]>([]);
  const [rowsLoading, setRowsLoading] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const [profiles, setProfiles] = useState<ProfileDef[]>([]);
  const [profileId, setProfileId] = useState("personal");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [stages, setStages] = useState<StageMap>({});
  const [maxCorr, setMaxCorr] = useState(0.5);
  const [volTarget, setVolTarget] = useState(12);
  const [composing, setComposing] = useState(false);
  const [result, setResult] = useState<Composed | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [b, ps] = await Promise.all([
          api.get<BankInfo[]>("/api/banks"),
          api.get<ProfileDef[]>("/api/portfolio/profiles"),
        ]);
        setBanks(b);
        setProfiles(ps);
        const preferred = b.find((x) => x.group === "stress_testing") || b[0];
        setBankId((cur) => cur || preferred?.bank_id || "");
      } catch (e) { setError(String(e)); }
    })();
  }, []);

  useEffect(() => {
    if (!bankId) { setRows([]); return; }
    setRowsLoading(true);
    setResult(null);
    api.get<BankInfo & { strategies: BankStrategy[] }>(`/api/banks/${bankId}`)
      .then((b) => {
        const list = b.strategies || [];
        setRows(list);
        setSelected(list.map((r) => r.strategy_id));
      })
      .catch((e) => setError(String(e)))
      .finally(() => setRowsLoading(false));
  }, [bankId]);

  const orderedBanks = useMemo(() => {
    const list = [...banks];
    list.sort((a, b) =>
      Number(b.group === "stress_testing") - Number(a.group === "stress_testing"));
    return list;
  }, [banks]);

  function toggle(id: string) {
    setSelected((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  const numericOverrides = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [key, raw] of Object.entries(overrides)) {
      const n = Number(raw);
      if (raw.trim() !== "" && Number.isFinite(n)) out[key] = n;
    }
    return out;
  }, [overrides]);

  async function compose(profile = profileId,
                        ov: Record<string, number> = numericOverrides,
                        st: StageMap = stages) {
    if (!bankId) { setError("Choose a strategy bank"); return; }
    if (!selected.length) { setError("Select at least one strategy"); return; }
    setComposing(true); setError(null); setResult(null);
    try {
      const r = await api.post<Composed>("/api/portfolio/compose", {
        bank_id: bankId,
        strategy_ids: selected,
        max_corr: maxCorr,
        vol_target: volTarget / 100,
        profile,
        profile_overrides: ov,
        stages: profile === "propfirm" ? st : {},
      });
      setResult(r);
    } catch (e) { setError(String(e)); }
    setComposing(false);
  }

  function defaultsFor(id: string): StageMap {
    const p = profiles.find((x) => x.id === id);
    return p?.stage_defaults ? JSON.parse(JSON.stringify(p.stage_defaults)) : {};
  }

  function chooseProfile(id: string) {
    const st = defaultsFor(id);
    setProfileId(id);
    setOverrides({});
    setStages(st);
    if (result) compose(id, {}, st);
  }

  function updateStage(sid: string, key: string, value: string) {
    setStages((prev) => {
      const next: StageMap = { ...prev };
      next[sid] = { ...(prev[sid] || {}) };
      next[sid][key] = value === "" ? null : value;
      return next;
    });
  }

  const activeProfile = profiles.find((p) => p.id === profileId);

  const excluded = result?.strategies.filter((s) => !s.included) || [];
  const metricValue = (p: Portfolio, key: string, isPct?: boolean, digits = 2) => {
    const v = p.metrics[key];
    if (v === undefined || v === null) return "—";
    return isPct ? pct(v, digits) : fmt(v, digits);
  };

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Portfolio Composer</h1>
          <p>
            Build a low-correlation portfolio from stress-tested strategies, with
            percent-based sizing instead of the fixed lot used during the search.
          </p>
        </div>
        <div className="head-actions">
          {result && (
            <Badge tone="accent">
              {result.selected.length} strategies · avg corr {fmt(result.avg_corr, 2)}
            </Badge>
          )}
        </div>
      </header>

      <ErrorBox error={error} />

      <Section title="Profile"
        subtitle="Each profile weights the metrics that decide success for that capital source.">
        <div className="head-actions">
          {profiles.map((p) => (
            <button key={p.id} className={`btn ${profileId === p.id ? "primary" : ""}`}
              onClick={() => chooseProfile(p.id)}>{p.label}</button>
          ))}
          {Object.keys(overrides).length > 0 && (
            <button className="btn tiny" style={{ marginLeft: "auto" }}
              onClick={() => setOverrides({})}>Reset thresholds</button>
          )}
        </div>
        {activeProfile && (
          <>
            <p className="muted small" style={{ marginTop: 10 }}>{activeProfile.subtitle}</p>
            <table className="table" style={{ marginTop: 10 }}>
              <thead>
                <tr><th>Metric</th><th>Threshold</th><th>Weight</th><th>Why</th></tr>
              </thead>
              <tbody>
                {activeProfile.rules.map((r) => (
                  <tr key={r.key}>
                    <td>{r.label}</td>
                    <td className="mono">
                      {r.op}{" "}
                      <input type="number" step={0.5} className="mono"
                        style={{ width: 84 }}
                        value={overrides[r.key] ?? String(r.threshold)}
                        onChange={(e) => setOverrides((prev) => ({
                          ...prev, [r.key]: e.target.value,
                        }))} />
                      <span className="muted"> {r.unit}</span>
                      {overrides[r.key] !== undefined && (
                        <span className="muted small"> (default {fmt(r.threshold, 2)})</span>
                      )}
                    </td>
                    <td className="mono">{r.weight}</td>
                    <td className="muted small">{r.rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {profileId === "propfirm" && Object.keys(stages).length > 0 && (
              <>
                <div className="head-actions" style={{ marginTop: 16 }}>
                  <h3 className="eyebrow" style={{ margin: 0 }}>Prop firm stages</h3>
                  <button className="btn tiny"
                    onClick={() => setStages(defaultsFor("propfirm"))}>Reset stages</button>
                </div>
                <p className="muted small">
                  Daily basis: fixed = % of the stage's starting balance, trailing = %
                  of that day's opening equity. Overall basis: fixed = % of the
                  starting balance, trailing = % of the running peak. Leave max days
                  or consistency empty to disable that rule.
                </p>
                <div style={{ overflowX: "auto" }}>
                  <table className="table">
                    <thead>
                      <tr><th>Stage</th><th>Target %</th><th>Daily loss %</th>
                        <th>Daily basis</th><th>Overall loss %</th><th>Overall basis</th>
                        <th>Min days</th><th>Max days</th><th>Consistency %</th>
                        <th>Payout split %</th></tr>
                    </thead>
                    <tbody>
                      {["stage1", "stage2", "funded"].map((sid) => {
                        const c = stages[sid] || {};
                        const num = (key: string) => (
                          <input type="number" className="mono"
                            step={key === "consistency_pct" ? 1 : 0.5}
                            style={{ width: 68 }}
                            value={String(c[key] ?? "")}
                            onChange={(e) => updateStage(sid, key, e.target.value)} />
                        );
                        return (
                          <tr key={sid}>
                            <td>{String(c.label ?? sid)}</td>
                            <td>{sid === "funded"
                              ? <span className="muted">—</span>
                              : num("profit_target_pct")}</td>
                            <td>{num("daily_loss_pct")}</td>
                            <td>
                              <select value={String(c.daily_mode ?? "fixed")}
                                onChange={(e) => updateStage(sid, "daily_mode", e.target.value)}>
                                <option value="fixed">fixed</option>
                                <option value="trailing">trailing</option>
                              </select>
                            </td>
                            <td>{num("max_loss_pct")}</td>
                            <td>
                              <select value={String(c.max_mode ?? "fixed")}
                                onChange={(e) => updateStage(sid, "max_mode", e.target.value)}>
                                <option value="fixed">fixed</option>
                                <option value="trailing">trailing</option>
                              </select>
                            </td>
                            <td>{num("min_days")}</td>
                            <td>{num("max_days")}</td>
                            <td>{num("consistency_pct")}</td>
                            <td>{sid === "funded"
                              ? num("profit_split_pct")
                              : <span className="muted">—</span>}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="muted small">
                  Stage and threshold edits apply to the next composition.
                </p>
              </>
            )}
            {activeProfile.notes.length > 0 && (
              <p className="muted small" style={{ marginTop: 8 }}>
                {activeProfile.notes.join(" ")}
              </p>
            )}
          </>
        )}
      </Section>

      <Section title="Universe & constraints"
        subtitle="Correlation is measured on monthly profit/loss. Strategies that breach the limit are left out of the portfolio.">
        <div className="form-grid">
          <Field label="Strategy bank" className="span-2"
            hint="Stress-pipeline banks are listed first.">
            <select value={bankId} onChange={(e) => setBankId(e.target.value)}>
              {orderedBanks.length === 0 && <option value="">No banks yet</option>}
              {orderedBanks.map((b) => (
                <option key={b.bank_id} value={b.bank_id}>
                  {b.bank_id} · {b.name} · {b.count} · {b.group || "—"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Max monthly correlation"
            hint="Highest allowed absolute correlation of monthly P&L with the strategies already in the portfolio.">
            <div className="slider-row">
              <input type="range" min={0.05} max={0.95} step={0.05}
                value={maxCorr} onChange={(e) => setMaxCorr(Number(e.target.value))} />
              <Num value={maxCorr} min={0} max={0.99} step={0.05}
                onChange={setMaxCorr} />
            </div>
          </Field>
          <Field label="Volatility target"
            hint="Dynamic sizing scales the portfolio to this annualised volatility (capped at 2× exposure).">
            <Num value={volTarget} min={1} max={100} step={1} suffix="%"
              onChange={setVolTarget} />
          </Field>
        </div>

        {orderedBanks.length === 0 && (
          <div className="empty small" style={{ marginTop: 12 }}>
            No strategy banks yet. Run the stress pipeline first.
          </div>
        )}
        {rowsLoading && <Loading what="Loading strategies" />}

        {rows.length > 0 && !rowsLoading && (
          <>
            <div className="head-actions" style={{ margin: "14px 0" }}>
              <button className="btn tiny"
                onClick={() => setSelected(rows.map((r) => r.strategy_id))}>Select all</button>
              <button className="btn tiny" onClick={() => setSelected([])}>Clear</button>
              <span className="muted small">{selected.length} of {rows.length} selected</span>
                              <button className="btn primary" style={{ marginLeft: "auto" }}
                onClick={() => compose()} disabled={composing || !selected.length}>
                {composing ? "Composing…" : "Compose portfolio"}
              </button>
            </div>
            <table className="table">
              <thead>
                <tr><th /><th>Strategy</th><th>Symbol</th><th>TF</th>
                  <th>OOS Sharpe</th><th>OOS R/DD</th><th>Trades</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.strategy_id}>
                    <td>
                      <input type="checkbox" checked={selected.includes(r.strategy_id)}
                        onChange={() => toggle(r.strategy_id)} />
                    </td>
                    <td className="mono">{r.strategy_id}</td>
                    <td>{r.symbol}</td>
                    <td>{r.timeframe}</td>
                    <td className="mono">{fmt(r.metrics_oos?.sharpe)}</td>
                    <td className="mono">{fmt(r.metrics_oos?.return_dd)}</td>
                    <td className="mono">{fmt(r.metrics_oos?.trades, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </Section>

      {result && (
        <>
          <Section title="Selection"
            subtitle={`Monthly P&L from ${result.window.start} to ${result.window.end} (${result.window.months} months), correlation limit ${fmt(result.max_corr, 2)}.`}
            right={<Badge tone={result.avg_corr <= result.max_corr ? "good" : "warn"}>
              avg corr {fmt(result.avg_corr, 2)}</Badge>}>
            <table className="table">
              <thead>
                <tr><th>Strategy</th><th>Symbol</th><th>Months</th>
                  <th>Monthly vol</th><th>Monthly win</th><th>Decision</th></tr>
              </thead>
              <tbody>
                {result.strategies.map((s) => (
                  <tr key={s.strategy_id}>
                    <td className="mono">{s.strategy_id}</td>
                    <td>{s.symbol}</td>
                    <td className="mono">{s.months || "—"}</td>
                    <td className="mono">{s.months ? pct(s.monthly_vol) : "—"}</td>
                    <td className="mono">{s.months ? pct(s.monthly_win_rate, 1) : "—"}</td>
                    <td>
                      {s.included
                        ? <Badge tone="good">in portfolio</Badge>
                        : <span className="muted small">{s.reason}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {excluded.length > 0 && (
              <p className="muted small" style={{ marginTop: 8 }}>
                {excluded.length} strateg{excluded.length === 1 ? "y" : "ies"} excluded
                by the correlation limit or missing history.
              </p>
            )}
          </Section>

          {result.correlation.ids.length > 1 && (
            <Section title="Monthly P&L correlation"
              subtitle="Pairwise Pearson correlation of monthly returns for the selected strategies.">
              <div style={{ overflowX: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th />
                      {result.correlation.ids.map((id) => (
                        <th key={id} className="mono">{id.slice(-8)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.correlation.matrix.map((row, i) => (
                      <tr key={result.correlation.ids[i]}>
                        <td className="mono">{result.correlation.ids[i]}</td>
                        {row.map((v, j) => (
                          <td key={j} className="mono" style={{
                            background: v === null ? undefined
                              : `rgba(220, 38, 38, ${Math.min(0.45, Math.abs(v) * 0.45)})`,
                          }}>
                            {v === null ? "—" : fmt(v, 2)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          )}

          {result.portfolios.map((p) => (
            <Section key={p.id} title={p.label}
              right={
                <div className="head-actions">
                  {result.best === p.id && <Badge tone="accent">best</Badge>}
                  <Badge tone={p.profile.all_passed ? "good" : "warn"}>
                    {p.profile.rules_passed}/{p.profile.rules_total} rules
                  </Badge>
                  <Badge tone={scoreTone(p.score)}>score {fmt(p.score, 1)}/100</Badge>
                </div>
              }
              subtitle={`Exposure ${fmt(p.metrics.exposure ?? 1, 2)}× · ${p.metrics.trades ?? 0} trades across ${result.selected.length} strategies.`}>
              <div className="stat-grid">
                <div className="stat"><span className="stat-label">Net return</span>
                  <span className="stat-value">{pct(p.metrics.net_return)}</span>
                  <span className="stat-sub">{pct(p.metrics.ann_return)} annualised</span></div>
                <div className="stat"><span className="stat-label">Sharpe</span>
                  <span className="stat-value">{fmt(p.metrics.sharpe)}</span>
                  <span className="stat-sub">Sortino {fmt(p.metrics.sortino)}</span></div>
                <div className="stat"><span className="stat-label">Max drawdown</span>
                  <span className="stat-value">{pct(p.metrics.max_dd)}</span>
                  <span className="stat-sub">Return/DD {fmt(p.metrics.return_dd)}</span></div>
                <div className="stat"><span className="stat-label">Monthly win rate</span>
                  <span className="stat-value">{pct(p.metrics.monthly_win_rate, 1)}</span>
                  <span className="stat-sub">
                    {pct(p.metrics.worst_month)} worst · {pct(p.metrics.best_month)} best
                  </span></div>
              </div>

              <div className="two-col">
                <div>
                  <h3 className="eyebrow">Metrics</h3>
                  <div className="config-list">
                    {METRICS.map((m) => (
                      <div key={m.key}>
                        <span>{m.label}</span>
                        <b className="mono">{metricValue(p, m.key, m.pct, m.digits ?? 2)}</b>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <h3 className="eyebrow">Equity (monthly, base 100)</h3>
                  {p.equity.length > 1
                    ? <LineChart data={p.equity} height={210} label={p.label} />
                    : <div className="empty small">Not enough history for a curve.</div>}
                  <h3 className="eyebrow" style={{ marginTop: 14 }}>Weights</h3>
                  <div className="chips">
                    {Object.entries(p.weights).map(([sid, w]) => (
                      <Badge key={sid} tone="neutral">
                        {sid.slice(-8)} · {pct(w * 100, 1)}
                      </Badge>
                    ))}
                  </div>
                </div>
              </div>

              <h3 className="eyebrow" style={{ marginTop: 14 }}>
                {result.profile.label} checks
              </h3>
              <table className="table">
                <thead>
                  <tr><th>Metric</th><th>Portfolio</th><th>Threshold</th>
                    <th>Points</th><th /></tr>
                </thead>
                <tbody>
                  {p.profile.checks.map((c) => (
                    <tr key={c.key}>
                      <td>{c.label}</td>
                      <td className="mono">{checkValue(c)}</td>
                      <td className="mono">{c.op} {fmt(c.threshold, 2)}{c.unit}
                        {c.custom && <span className="muted small"> custom</span>}</td>
                      <td className="mono">{fmt(c.points, 1)}/{c.weight}</td>
                      <td>
                        {c.ok
                          ? <Badge tone="good">pass</Badge>
                          : <Badge tone="bad">breach</Badge>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {p.challenge && (
                <>
                  <h3 className="eyebrow" style={{ marginTop: 16 }}>
                    Prop firm simulation
                  </h3>
                  <div className="stat-grid">
                    {p.challenge.stages.map((s) => (
                      <div className="stat" key={s.id}>
                        <span className="stat-label">{s.label}</span>
                        <span className="stat-value">{pct(s.pass_rate, 1)}</span>
                        <span className="stat-sub">
                          {s.attempts} attempts{s.avg_days ? ` · ${fmt(s.avg_days, 1)} d avg` : ""}
                        </span>
                        <span className="stat-sub muted small">{breachSummary(s.breaches)}</span>
                      </div>
                    ))}
                  </div>
                  <p className="muted small" style={{ marginTop: 8 }}>
                    Overall pass rate {pct(p.challenge.overall_pass_rate, 1)} across
                    {" "}{p.challenge.attempts} start dates · funded monthly mean
                    {" "}{pct(p.challenge.monthly.mean)} (median {pct(p.challenge.monthly.median)},
                    {" "}{pct(p.challenge.monthly.positive_pct, 1)} positive months) · best
                    {" "}{pct(p.challenge.monthly.best)} / worst {pct(p.challenge.monthly.worst)}
                    {" "}· est. payout {fmt(p.challenge.monthly.payout, 0)} per month at
                    {" "}{pct(p.challenge.monthly.profit_split_pct, 0)} split
                    on {fmt(result.base_balance, 0)}.
                  </p>
                  <h3 className="eyebrow" style={{ marginTop: 14 }}>
                    Funded monthly returns
                  </h3>
                  <MonthlyHeatmap years={p.challenge.heatmap.years}
                    values={p.challenge.heatmap.values} />
                  <p className="muted small">
                    The funded account trades the same curve, so these are the
                    portfolio's compounded monthly returns; the payout estimate is
                    the mean month × profit split.
                  </p>
                </>
              )}
            </Section>
          ))}
        </>
      )}
    </div>
  );
}
