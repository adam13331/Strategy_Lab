import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ErrorBox } from "./components";
import type { Config, IndicatorDef, RunInfo, SymbolInfo } from "./types";
import { Overview } from "./pages/Overview";
import { DataPage } from "./pages/Data";
import { IndicatorsPage } from "./pages/Indicators";
import { GeneratorPage } from "./pages/Generator";
import { AcceptancePage } from "./pages/Acceptance";
import { RunPage } from "./pages/Run";
import { ResultsPage } from "./pages/Results";
import { StressPage } from "./pages/Stress";
import { MT5BacktesterPage } from "./pages/MT5Backtester";
import { PortfolioPage } from "./pages/Portfolio";
import { BanksPage } from "./pages/Banks";
import { SettingsPage } from "./pages/Settings";

type Page = "overview" | "data" | "indicators" | "generator" | "acceptance" | "run" | "results" | "stress" | "mt5" | "portfolio" | "banks" | "settings";

const NAV: { id: Page; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "◈" },
  { id: "data", label: "Data", icon: "▤" },
  { id: "generator", label: "Generation", icon: "⌁" },
  { id: "stress", label: "Stress test", icon: "⌾" },
  { id: "mt5", label: "MT5 Backtester", icon: " “" },
  { id: "portfolio", label: "Portfolio Composer", icon: "▦" },
  { id: "banks", label: "Strategy Banks", icon: "▣" },
  { id: "settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [config, setConfig] = useState<Config | null>(null);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [catalogue, setCatalogue] = useState<IndicatorDef[]>([]);
  const [runId, setRunId] = useState<string | undefined>(undefined);
  const [selectedId, setSelectedId] = useState<string | undefined>(undefined);
  const [bankId, setBankId] = useState<string | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  const refreshSymbols = useCallback(async () => {
    const s = await api.get<SymbolInfo[]>("/api/symbols");
    setSymbols(s);
    setConfig((c) => {
      if (!c) return c;
      if (c.symbols.length === 0 && s.length) return { ...c, symbols: [s[0].symbol] };
      return c;
    });
  }, []);

  const refreshRuns = useCallback(async () => {
    setRuns(await api.get<RunInfo[]>("/api/runs"));
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [cfg, syms, rs, inds] = await Promise.all([
          api.get<Config>("/api/config/default"),
          api.get<SymbolInfo[]>("/api/symbols"),
          api.get<RunInfo[]>("/api/runs"),
          api.get<{ catalogue: IndicatorDef[] }>("/api/indicators"),
        ]);
        setConfig(cfg); setSymbols(syms); setRuns(rs); setCatalogue(inds.catalogue);
        setReady(true);
      } catch (e) { setError(String(e)); }
    })();
  }, []);

  useEffect(() => {
    if (runs.length && !runId) setRunId(runs[0].run_id);
  }, [runs.length]);

  async function openStrategy(id: string) {
    try {
      const d = await api.get<any>(`/api/strategies/${id}`);
      setRunId(d.run_id);
      setSelectedId(id);
      setPage("results");
    } catch (e) { setError(String(e)); }
  }

  function startRun(id: string) {
    setRunId(id);
    setSelectedId(undefined);
    setPage("run");
    refreshRuns();
  }

  function go(p: string) { setPage(p as Page); }

  if (!ready) {
    return (
      <div className="boot">
        <div className="boot-logo">SQX</div>
        {error ? <ErrorBox error={error} /> : <div className="muted">Connecting to the local engine…</div>}
        {error && <p className="muted">Start the backend with <code>uv run sqx serve</code>.</p>}
      </div>
    );
  }
  if (!config) return null;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand" onClick={() => setPage("overview")}>
          <div className="brand-mark">SQX</div>
          <div>
            <strong>Strategy Lab</strong>
            <span>phase 1 · local</span>
          </div>
        </div>
        <nav>
          {NAV.map((n) => (
            <button key={n.id} className={page === n.id ? "active" : ""}
              onClick={() => setPage(n.id)}>
              <span className="nav-icon">{n.icon}</span>{n.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="dot-row"><span className="live-dot" /> engine connected</div>
          <div className="muted">{symbols.length} instruments</div>
        </div>
      </aside>

      <main className="main">
        {error && <div className="global-error"><ErrorBox error={error} /></div>}

        {page === "overview" && (
          <Overview symbols={symbols} runs={runs} go={go} openRun={(id) => { setRunId(id); setPage("run"); }} />
        )}
        {page === "data" && <DataPage symbols={symbols} refresh={async () => { await refreshSymbols(); }} />}
        {page === "indicators" && (
          <IndicatorsPage config={config} setConfig={setConfig} catalogue={catalogue} />
        )}
        {page === "generator" && (
          <GeneratorPage config={config} setConfig={setConfig} catalogue={catalogue}
            symbols={symbols} refresh={async () => { await refreshSymbols(); }}
            onStarted={startRun} go={go} />
        )}
        {page === "acceptance" && <AcceptancePage config={config} setConfig={setConfig} go={go} />}
        {page === "run" && (
          <RunPage runId={runId} setRunId={(id) => { setRunId(id); setSelectedId(undefined); }}
            runs={runs} refreshRuns={refreshRuns}
            goResults={() => { setSelectedId(undefined); setPage("results"); }}
            openStrategy={openStrategy} />
        )}
        {page === "results" && (
          <ResultsPage runs={runs} runId={runId} setRunId={setRunId}
            selectedId={selectedId} setSelectedId={setSelectedId} />
        )}
        {page === "stress" && (
          <StressPage runs={runs} goBanks={(id) => { setBankId(id); setPage("banks"); }} />
        )}
        {page === "mt5" && <MT5BacktesterPage />}
        {page === "portfolio" && <PortfolioPage />}
        {page === "banks" && <BanksPage key={bankId} initialBankId={bankId} />}
        {page === "settings" && <SettingsPage key="settings" />}
      </main>
    </div>
  );
}
