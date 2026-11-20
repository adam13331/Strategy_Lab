import React from "react";
import { api, fmt, pct } from "../api";
import { ErrorBox, LineChart, Loading, Section } from "../components";
import type { BankInfo, BankStrategy, Metrics } from "../types";

function RobustTable({ metrics }: { metrics: Record<string, { median: number; p05: number }> }) {
  const rows: { l: string; k: string; f?: (n: number) => string }[] = [
    { l: "Trades", k: "trades", f: (n) => fmt(n, 0) },
    { l: "Net return", k: "net_return", f: (n) => pct(n) },
    { l: "Max drawdown", k: "max_dd", f: (n) => pct(n) },
    { l: "Return / DD", k: "return_dd", f: (n) => fmt(n) },
    { l: "Sharpe", k: "sharpe", f: (n) => fmt(n) },
    { l: "Stagnation (d)", k: "stagnation_days", f: (n) => fmt(n, 0) },
    { l: "Profit factor", k: "profit_factor", f: (n) => fmt(n) },
    { l: "Win rate", k: "win_rate", f: (n) => pct(n, 1) },
  ];
  return (
    <table className="table metrics-table">
      <thead><tr><th>Metric</th><th>Median scenario</th><th>5th percentile</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.k}>
            <td>{r.l}</td>
            <td className="mono">{r.f ? r.f(metrics[r.k]?.median ?? 0) : "—"}</td>
            <td className="mono">{r.f ? r.f(metrics[r.k]?.p05 ?? 0) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function BanksPage({ initialBankId }: { initialBankId?: string }) {
  const [banks, setBanks] = React.useState<BankInfo[]>([]);
  const [bankId, setBankId] = React.useState<string | undefined>(initialBankId);
  const [rows, setRows] = React.useState<BankStrategy[]>([]);
  const [selected, setSelected] = React.useState<string | undefined>(undefined);
  const [detail, setDetail] = React.useState<any | null>(null);
  const [equity, setEquity] = React.useState<{ ts: string; equity: number; segment?: string }[]>([]);
  const [trades, setTrades] = React.useState<any[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);

  async function exportEa(strategyId: string) {
    if (!bankId) return;
    setError(null);
    try {
      const r = await fetch(`/api/banks/${bankId}/strategies/${strategyId}/export-ea`);
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${strategyId}.mq5`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
  }

  React.useEffect(() => {
    api.get<BankInfo[]>("/api/banks").then((b) => {
      setBanks(b);
      setBankId((id) => id || b[0]?.bank_id);
    }).catch((e) => setError(String(e)));
  }, []);

  React.useEffect(() => {
    if (!bankId) { setRows([]); return; }
    setLoading(true);
    api.get<BankInfo & { strategies: BankStrategy[] }>(`/api/banks/${bankId}`)
      .then((b) => setRows(b.strategies || []))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [bankId]);

  React.useEffect(() => {
    if (!bankId || !selected) { setDetail(null); setEquity([]); setTrades([]); return; }
    setLoading(true);
    Promise.all([
      api.get<any>(`/api/banks/${bankId}/strategies/${selected}`),
      api.get<any[]>(`/api/banks/${bankId}/strategies/${selected}/equity`),
      api.get<any[]>(`/api/banks/${bankId}/strategies/${selected}/trades?limit=500`),
    ]).then(([d, e, t]) => { setDetail(d); setEquity(e); setTrades(t); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [bankId, selected]);

  const bank = banks.find((b) => b.bank_id === bankId);
  const splitTs = React.useMemo(() => equity.find((e) => e.segment === "OOS")?.ts, [equity]);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Strategy banks</h1>
          <p>Stress-tested strategies that survived every validation stage. This is the output of the stress pipeline.</p>
        </div>
        <div className="head-actions">
          <select value={bankId || ""} onChange={(e) => { setBankId(e.target.value); setSelected(undefined); }}>
            {banks.length === 0 && <option value="">No banks yet</option>}
            {banks.map((b) => (
              <option key={b.bank_id} value={b.bank_id}>{b.bank_id} · {b.name} · {b.count}</option>
            ))}
          </select>
        </div>
      </header>
      <ErrorBox error={error} />

      {banks.length === 0 && (
        <div className="empty">
          No strategy banks yet. Run the stress pipeline and let at least one strategy survive.
        </div>
      )}

      {bank && (
        <>
          <div className="stat-grid">
            <div className="stat"><span className="stat-label">Bank</span>
              <span className="stat-value small mono">{bank.bank_id}</span>
              <span className="stat-sub">{bank.name}</span></div>
            <div className="stat"><span className="stat-label">Strategies</span>
              <span className="stat-value">{bank.count}</span></div>
            <div className="stat"><span className="stat-label">Source run</span>
              <span className="stat-value small mono">{bank.source_run_id || "—"}</span></div>
            <div className="stat"><span className="stat-label">Test run</span>
              <span className="stat-value small mono">{bank.rob_id || "—"}</span></div>
          </div>

          <Section title="Surviving strategies"
            subtitle="Robustness columns show the median and 5th percentile across all stress scenarios.">
            {loading && !rows.length ? <Loading what="Loading bank" /> : (
              <table className="table strategy-table">
                <thead><tr>
                  <th>Strategy</th><th>Market</th><th>Dir</th>
                  <th>IS Sharpe</th><th>OOS Sharpe</th>
                  <th>Rob. Sharpe</th><th>Rob. return p05</th><th>Rob. R/DD</th><th>Score</th>
                </tr></thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.strategy_id}
                      className={`clickable ${selected === r.strategy_id ? "selected" : ""}`}
                      onClick={() => setSelected(r.strategy_id)}>
                      <td className="mono">{r.strategy_id}</td>
                      <td>{r.symbol} {r.timeframe}</td>
                      <td>{r.direction}</td>
                      <td className="mono">{fmt(r.metrics_is?.sharpe ?? 0)}</td>
                      <td className="mono">{fmt(r.metrics_oos?.sharpe ?? 0)}</td>
                      <td className="mono">{fmt(r.metrics_all?.sharpe?.median ?? 0)}</td>
                      <td className="mono">{pct(r.metrics_all?.net_return?.p05 ?? 0)}</td>
                      <td className="mono">{fmt(r.metrics_all?.return_dd?.median ?? 0)}</td>
                      <td className="mono">{fmt(r.score ?? 0)}</td>
                    </tr>
                  ))}
                  {!rows.length && <tr><td colSpan={9} className="muted">This bank is empty.</td></tr>}
                </tbody>
              </table>
            )}
          </Section>

          {detail && (
            <div className="detail">
              <Section title={`Strategy ${detail.strategy_id}`}
                subtitle="Full rules, scenario-robust metrics, equity and trades."
                right={<button className="btn small" onClick={() => exportEa(detail.strategy_id)}>Export EA</button>}>
                <div className="two-col">
                  <div>
                    <h3 className="subhead">Rules</h3>
                    <div className="rules">
                      {detail.rules?.long?.length > 0 && (
                        <div><span className="rule-side long">LONG</span>
                          <ul>{detail.rules.long.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>
                      )}
                      {detail.rules?.short?.length > 0 && (
                        <div><span className="rule-side short">SHORT</span>
                          <ul>{detail.rules.short.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>
                      )}
                      {detail.rules?.exits?.length > 0 && (
                        <div><span className="rule-side exit">EXIT</span>
                          <ul>{detail.rules.exits.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>
                      )}
                      <div className="rule-line">{detail.rules?.stop_loss}</div>
                      <div className="rule-line">{detail.rules?.take_profit}</div>
                      <div className="rule-line">{detail.rules?.trailing}</div>
                      <div className="rule-line">Opposite signal: {detail.rules?.opposite_signal}</div>
                      <div className="rule-line">Max bars in trade: {detail.rules?.max_bars}</div>
                    </div>
                  </div>
                  <div>
                    <h3 className="subhead">Scenario metrics</h3>
                    <RobustTable metrics={detail.metrics_all || {}} />
                  </div>
                </div>
              </Section>

              <Section title="Equity" subtitle="Daily mark-to-market, dashed line marks the source run's OOS boundary.">
                <LineChart data={equity} splitTs={splitTs} />
              </Section>

              <Section title="Trades" subtitle={`${trades.length} shown`}>
                <table className="table">
                  <thead><tr><th>#</th><th>Entry</th><th>Exit</th><th>Side</th>
                    <th>Entry px</th><th>Exit px</th><th>PnL</th></tr></thead>
                  <tbody>
                    {trades.map((t: any, i: number) => (
                      <tr key={i}>
                        <td className="muted">{i + 1}</td>
                        <td className="mono">{String(t.entry_ts).slice(0, 16)}</td>
                        <td className="mono">{String(t.exit_ts).slice(0, 16)}</td>
                        <td>{t.side}</td>
                        <td className="mono">{fmt(t.entry, 5)}</td>
                        <td className="mono">{fmt(t.exit, 5)}</td>
                        <td className={`mono ${t.pnl >= 0 ? "pos" : "neg"}`}>{fmt(t.pnl)}</td>
                      </tr>
                    ))}
                    {!trades.length && <tr><td colSpan={7} className="muted">No trades stored.</td></tr>}
                  </tbody>
                </table>
              </Section>
            </div>
          )}
        </>
      )}
    </div>
  );
}
