import React, { useEffect, useMemo, useState } from "react";
import { api, fmt, pct } from "../api";
import { Badge, ErrorBox, LineChart, Loading, Section } from "../components";
import type { Metrics, RunInfo, StrategyDetail, StrategyRow } from "../types";

const REASONS: Record<number, string> = {
  1: "Stop loss", 2: "Take profit", 3: "Trailing", 4: "Exit signal",
  5: "Opposite signal", 6: "Session end", 7: "Max bars", 8: "OOS split",
  9: "Data end",
};

function MetricTable({ is, oos }: { is: Metrics; oos: Metrics }) {
  const rows: { l: string; tip: string; k: keyof Metrics; f?: (n: number) => string }[] = [
    { l: "Trades", tip: "Number of trades taken.", k: "trades", f: (n) => fmt(n, 0) },
    { l: "Net return", tip: "Profit or loss over the period.", k: "net_return", f: (n) => pct(n) },
    { l: "Max drawdown", tip: "Largest peak-to-trough loss.", k: "max_dd", f: (n) => pct(n) },
    { l: "Return / max DD", tip: "How much profit per unit of worst loss.", k: "return_dd", f: (n) => fmt(n) },
    { l: "Sharpe", tip: "Higher means steadier profits.", k: "sharpe", f: (n) => fmt(n) },
    { l: "Stagnation (days)", tip: "Days without a new equity high.", k: "stagnation_days", f: (n) => fmt(n, 0) },
    { l: "Profit factor", tip: "Gross profit divided by gross loss.", k: "profit_factor", f: (n) => fmt(n) },
    { l: "Win rate", tip: "Share of trades that won.", k: "win_rate", f: (n) => pct(n, 1) },
    { l: "Avg trade", tip: "Mean profit per trade.", k: "avg_trade", f: (n) => fmt(n) },
  ];
  return (
    <table className="table metrics-table">
      <thead><tr><th>Metric</th><th>In sample</th><th>Out of sample</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.k}><td title={r.tip}>{r.l}</td><td className="mono">{r.f ? r.f(is[r.k]) : is[r.k]}</td><td className="mono">{r.f ? r.f(oos[r.k]) : oos[r.k]}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

export function ResultsPage({ runs, runId, setRunId, selectedId, setSelectedId }: {
  runs: RunInfo[]; runId?: string; setRunId: (id: string) => void;
  selectedId?: string; setSelectedId: (id?: string) => void;
}) {
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<StrategyDetail | null>(null);
  const [equity, setEquity] = useState<{ ts: string; equity: number; segment?: string }[]>([]);
  const [trades, setTrades] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const limit = 50;

  useEffect(() => {
    if (!runId && runs.length) {
      const withResults = runs.find((r) => (r.stats?.accepted ?? 0) > 0);
      setRunId((withResults || runs[0]).run_id);
    }
  }, [runs.length]);

  useEffect(() => {
    if (!runId) return;
    setLoading(true); setError(null);
    api.get<any>(`/api/strategies?run_id=${runId}&limit=${limit}&offset=${offset}`)
      .then((r) => { setRows(r.items); setTotal(r.total); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [runId, offset]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    setLoading(true); setError(null);
    Promise.all([
      api.get<StrategyDetail>(`/api/strategies/${selectedId}`),
      api.get<any[]>(`/api/strategies/${selectedId}/equity`),
      api.get<any[]>(`/api/strategies/${selectedId}/trades?limit=500`),
    ]).then(([d, e, t]) => { setDetail(d); setEquity(e); setTrades(t); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selectedId]);

  const filtered = useMemo(() => {
    if (!query) return rows;
    const q = query.toLowerCase();
    return rows.filter((r) => r.strategy_id.toLowerCase().includes(q) ||
      r.symbol.toLowerCase().includes(q) || r.direction.includes(q));
  }, [rows, query]);

  const splitTs = useMemo(() => equity.find((e) => e.segment === "OOS")?.ts, [equity]);
  const currentRun = runs.find((r) => r.run_id === runId);

  return (
    <div className="page">
      <header className="page-head">
        <div><h1>Results</h1><p>Survivors of the IS/OOS gates and the correlation filter.</p></div>
        <div className="head-actions">
          <select value={runId || ""} onChange={(e) => { setRunId(e.target.value); setOffset(0); setSelectedId(undefined); }}>
            <option value="">Select run…</option>
            {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.status} · {r.name || ""}</option>)}
          </select>
          <Badge tone="accent">{fmt(total, 0)} strategies</Badge>
        </div>
      </header>
      <ErrorBox error={error} />

      {!runId ? <div className="empty">Choose a run.</div> : (
        <>
          <div className="toolbar">
            <input className="search" placeholder="Filter by id, symbol or direction…" value={query}
              onChange={(e) => setQuery(e.target.value)} />
            <div className="toolbar-right">
              <span className="muted">{currentRun?.name || ""}</span>
              <button className="btn tiny" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Prev</button>
              <span className="mono">{offset + 1}–{Math.min(offset + limit, total)}</span>
              <button className="btn tiny" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Next</button>
            </div>
          </div>

          {loading && rows.length === 0 ? <Loading what="Loading strategies" /> : (
            <table className="table strategy-table">
              <thead>
                <tr>
                  <th>Strategy</th><th>Market</th><th>Direction</th>
                  <th title="Consistency on learned data">IS Sharpe</th>
                  <th title="Profit vs worst drop on learned data">IS R/DD</th>
                  <th title="Trades on learned data">IS trades</th>
                  <th title="Consistency on unseen data">OOS Sharpe</th>
                  <th title="Profit vs worst drop on unseen data">OOS R/DD</th>
                  <th title="Trades on unseen data">OOS trades</th>
                  <th title="Similarity to other strategies">Corr</th>
                  <th title="Overall score">Score</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={r.strategy_id} className={`clickable ${selectedId === r.strategy_id ? "selected" : ""}`}
                    onClick={() => setSelectedId(r.strategy_id)}>
                    <td className="mono">{r.strategy_id}</td>
                    <td>{r.symbol} <span className="muted">{r.timeframe}</span></td>
                    <td>{r.direction}</td>
                    <td className="mono">{fmt(r.metrics_is.sharpe)}</td>
                    <td className="mono">{fmt(r.metrics_is.return_dd)}</td>
                    <td className="mono">{fmt(r.metrics_is.trades, 0)}</td>
                    <td className="mono">{fmt(r.metrics_oos.sharpe)}</td>
                    <td className="mono">{fmt(r.metrics_oos.return_dd)}</td>
                    <td className="mono">{fmt(r.metrics_oos.trades, 0)}</td>
                    <td className="mono">{r.correlation_max === null || r.correlation_max === undefined ? "—" : fmt(r.correlation_max, 3)}</td>
                    <td className="mono">{fmt(r.score, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {filtered.length === 0 && !loading && <div className="empty small">No strategies yet. A run may still be searching.</div>}
        </>
      )}

      {selectedId && detail && (
        <div className="detail">
          <div className="detail-head">
            <div>
              <div className="eyebrow">{detail.symbol} · {detail.timeframe} · {detail.direction}</div>
              <h2 className="mono">{detail.strategy_id}</h2>
            </div>
            <div className="head-actions">
              <Badge tone="accent">score {fmt(detail.score, 3)}</Badge>
              <button className="btn small" onClick={() => setSelectedId(undefined)}>Close</button>
            </div>
          </div>

          <div className="detail-grid">
            <section className="section">
              <h2>Rules</h2>
              <div className="rules">
                {(detail.rules.long.length > 0) && <div><span className="rule-side long">LONG</span><ul>{detail.rules.long.map((r) => <li key={r}>{r}</li>)}</ul></div>}
                {(detail.rules.short.length > 0) && <div><span className="rule-side short">SHORT</span><ul>{detail.rules.short.map((r) => <li key={r}>{r}</li>)}</ul></div>}
                <div><span className="rule-side exit">EXIT</span>
                  <ul>
                    {detail.rules.exits.map((r) => <li key={r}>{r}</li>)}
                    {detail.rules.exits.length === 0 && <li className="muted">No indicator exit</li>}
                  </ul>
                </div>
              </div>
              <div className="exit-summary">
                <div><span>{detail.rules.stop_loss}</span></div>
                <div><span>{detail.rules.take_profit}</span></div>
                <div><span>{detail.rules.trailing}</span></div>
                <div><span>Opposite signal: {detail.rules.opposite_signal}</span></div>
                <div><span>Entry orders: {detail.rules.orders}</span></div>
                <div><span>Max bars in trade: {detail.rules.max_bars}</span></div>
              </div>
            </section>
            <section className="section">
              <h2>Metrics</h2>
              <MetricTable is={detail.metrics_is} oos={detail.metrics_oos} />
            </section>
          </div>

          <Section title="Equity" subtitle="Daily mark-to-market. The dashed line marks the OOS boundary.">
            <LineChart data={equity} splitTs={splitTs} height={320} />
          </Section>

          <Section title="Trades" subtitle={`${trades.length} shown`}>
            <div className="trades-wrap">
              <table className="table">
                <thead><tr><th>#</th><th>Entry</th><th>Exit</th><th>Side</th><th>Entry px</th><th>Exit px</th><th>PnL</th><th>Exit reason</th></tr></thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i}>
                      <td className="mono">{i + 1}</td>
                      <td className="mono">{String(t.entry_ts).slice(0, 16)}</td>
                      <td className="mono">{String(t.exit_ts).slice(0, 16)}</td>
                      <td><span className={`side ${t.side}`}>{t.side}</span></td>
                      <td className="mono">{t.entry}</td>
                      <td className="mono">{t.exit}</td>
                      <td className={`mono ${t.pnl >= 0 ? "pos" : "neg"}`}>{fmt(t.pnl)}</td>
                      <td>{REASONS[t.reason] || t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      )}
    </div>
  );
}
