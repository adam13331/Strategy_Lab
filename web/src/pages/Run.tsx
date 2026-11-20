import React, { useEffect, useRef, useState } from "react";
import { api, fmt, pct } from "../api";
import { Badge, ErrorBox, Loading } from "../components";
import type { RunInfo, StrategyRow } from "../types";

const FUNNEL: { key: string; label: string; tone: string }[] = [
  { key: "generated", label: "Generated", tone: "neutral" },
  { key: "is_fail", label: "Failed IS", tone: "bad" },
  { key: "oos_fail", label: "Failed OOS", tone: "warn" },
  { key: "correlation", label: "Correlated", tone: "warn" },
  { key: "accepted", label: "Accepted", tone: "good" },
];

export function RunPage({ runId, setRunId, runs, refreshRuns, goResults, openStrategy }: {
  runId?: string;
  setRunId: (id: string) => void;
  runs: RunInfo[];
  refreshRuns: () => Promise<void>;
  goResults: () => void;
  openStrategy: (id: string) => void;
}) {
  const [run, setRun] = useState<RunInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const es = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!runId) { setRun(null); return; }
    let alive = true;
    setLoading(true);
    api.get<RunInfo>(`/api/runs/${runId}`).then((r) => {
      if (!alive) return;
      setRun(r); setLoading(false);
    }).catch((e) => { if (alive) { setError(String(e)); setLoading(false); } });

    es.current?.close();
    const source = new EventSource(`/api/runs/${runId}/events`);
    es.current = source;
    source.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        setRun((prev) => ({ ...(prev || {}), ...data } as RunInfo));
        if (data.done) { source.close(); refreshRuns(); }
      } catch {}
    };
    source.onerror = () => { source.close(); };
    return () => { alive = false; source.close(); };
  }, [runId]);

  async function stop() {
    if (!runId) return;
    try { await api.post(`/api/runs/${runId}/stop`); }
    catch (e) { setError(String(e)); }
  }

  const status = run?.status || "—";
  const isLive = status === "running" || status === "queued" || status === "stopping";
  const accepted = run?.accepted ?? run?.stats?.accepted ?? 0;
  const target = run?.target ?? run?.stats?.target ?? run?.config?.target_accepted ?? 0;
  const attempts = run?.attempts ?? run?.stats?.attempts ?? 0;
  const funnel = run?.funnel ?? run?.stats?.funnel ?? {};
  const progress = target ? Math.min(100, (accepted / target) * 100) : 0;
  const recent: StrategyRow[] = (run?.recent || []) as StrategyRow[];

  if (!runId) {
    return (
      <div className="page">
        <header className="page-head"><div><h1>Runs</h1><p>Pick a run to monitor, or start a new search.</p></div></header>
        <div className="run-list">
          {runs.length === 0 && <div className="empty">No runs yet.</div>}
          {runs.map((r) => (
            <button className="run-card" key={r.run_id} onClick={() => setRunId(r.run_id)}>
              <div>
                <div className="mono">{r.run_id} <span className="muted">{r.name}</span></div>
                <div className="muted">{String(r.created_at || "").slice(0, 19)}</div>
              </div>
              <div className="run-card-right">
                <Badge tone={r.status === "completed" ? "good" : r.status === "running" ? "accent" : "neutral"}>{r.status}</Badge>
                <span className="mono">{r.stats?.accepted ?? 0}/{r.stats?.target ?? 0}</span>
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Run <span className="mono">{runId}</span></h1>
          <p>{run?.name || "Strategy generation"} {run?.config && <>· {run.config.timeframe} · {run.config.symbols?.join(", ")} · OOS {run.config.oos_pct}%</>}</p>
        </div>
        <div className="head-actions">
          {isLive && <button className="btn danger" onClick={stop}>Stop run</button>}
          {runs.length > 0 && <select value={runId} onChange={(e) => setRunId(e.target.value)}>
            {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.status}</option>)}
          </select>}
          <button className="btn" onClick={goResults}>Results</button>
        </div>
      </header>
      <ErrorBox error={error} />
      {loading && !run && <Loading what="Loading run" />}

      {run && (
        <>
          <div className="run-hero">
            <div className="run-progress">
              <div className="run-progress-head">
                <Badge tone={status === "completed" ? "good" : status === "failed" ? "bad" : status === "stopped" ? "warn" : "accent"}>{status}</Badge>
                <span className="mono">{fmt(accepted, 0)} / {fmt(target, 0)} good strategies</span>
              </div>
              <div className="progress"><div style={{ width: `${progress}%` }} /></div>
              <div className="run-sub">
                <span>{fmt(attempts, 0)} ideas tested</span>
                <span>{fmt(run.throughput ?? 0, 2)} / s</span>
                <span>{fmt(run.elapsed ?? 0, 0)} s elapsed</span>
                <span>target {fmt(target, 0)}</span>
              </div>
            </div>
            <div className="run-funnel">
              {FUNNEL.map((f) => {
                const v = Number(funnel[f.key] || 0);
                const w = attempts ? Math.max(2, (v / attempts) * 100) : 0;
                return (
                  <div className="funnel-row" key={f.key}>
                    <span>{f.label}</span>
                    <div className="funnel-track"><div className={`funnel-fill ${f.tone}`} style={{ width: `${w}%` }} /></div>
                    <b className="mono">{fmt(v, 0)}</b>
                  </div>
                );
              })}
            </div>
          </div>

          {run.error && <ErrorBox error={run.error} />}

          <div className="two-col">
            <section className="section">
              <div className="section-head"><div><h2>Recent survivors</h2><p className="muted">Latest strategies that cleared all gates.</p></div></div>
              {recent.length === 0 ? <div className="empty small">Waiting for the first survivor…</div> : (
                <table className="table">
                  <thead><tr><th>Strategy</th><th>IS Sharpe</th><th>OOS Sharpe</th><th>OOS Return/DD</th><th>Trades</th></tr></thead>
                  <tbody>
                    {recent.slice().reverse().map((s: any) => (
                      <tr key={s.strategy_id} className="clickable" onClick={() => openStrategy(s.strategy_id)}>
                        <td className="mono">{s.strategy_id}</td>
                        <td className="mono">{fmt(s.is_sharpe)}</td>
                        <td className="mono">{fmt(s.oos_sharpe)}</td>
                        <td className="mono">{fmt(s.return_dd)}</td>
                        <td className="mono">{fmt(s.trades, 0)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="section">
              <div className="section-head"><div><h2>Configuration</h2><p className="muted">Snapshot stored with the run.</p></div></div>
              {run.config ? (
                <div className="config-list">
                  <div><span>Symbols</span><b>{run.config.symbols.join(", ")}</b></div>
                  <div><span>Timeframe</span><b>{run.config.timeframe}</b></div>
                  <div><span>Direction</span><b>{run.config.direction}</b></div>
                  <div><span>OOS</span><b>{run.config.oos_pct}%</b></div>
                  <div><span>Sizing</span><b>{run.config.risk.sizing === "fixed_lot" ? `${run.config.risk.fixed_lot} lots` : `${run.config.risk.risk_percent}% risk`}</b></div>
                  <div><span>Spread</span><b>{run.config.risk.spread_pips} pips</b></div>
                  <div><span>Session</span><b>{String(run.config.session.start_hour).padStart(2, "0")}:00 → {String(run.config.session.end_hour).padStart(2, "0")}:00</b></div>
                  <div><span>Days</span><b>{run.config.session.days.map((d) => ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]).join(" ")}</b></div>
                  <div><span>Autocorrelation</span><b>{run.config.autocorrelation.enabled ? `${run.config.autocorrelation.method} ≥ ${run.config.autocorrelation.threshold}` : "off"}</b></div>
                  <div><span>IS gates</span><b>{run.config.acceptance.is_gates.min_trades} trades · Sharpe ≥ {run.config.acceptance.is_gates.min_sharpe} · R/DD ≥ {run.config.acceptance.is_gates.min_return_dd}</b></div>
                  <div><span>OOS gates</span><b>{run.config.acceptance.oos_gates.min_trades} trades · Sharpe ≥ {run.config.acceptance.oos_gates.min_sharpe} · R/DD ≥ {run.config.acceptance.oos_gates.min_return_dd}</b></div>
                </div>
              ) : <p className="muted">Unavailable</p>}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
