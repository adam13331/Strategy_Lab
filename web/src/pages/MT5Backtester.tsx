import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, fmt, pct } from "../api";
import { Badge, ErrorBox, Field, Loading, Section } from "../components";
import type { BankInfo, BankStrategy, SymbolInfo } from "../types";

const FOLDER_KEY = "sqx.mt5.folder";

type Part = { ok: boolean; path: string | null };
type Check = {
  path: string;
  exists: boolean;
  ok: boolean;
  terminal: Part;
  metaeditor: Part;
  mql5: { ok: boolean; path: string | null; candidates: string[] };
  experts: { ok: boolean; path: string | null; target: string | null };
  indicators: {
    ok: boolean; path: string | null; folder: string;
    expected: number; found: number; missing: string[];
  };
  symbols: string[];
  notes: string[];
};

type BuildResult = {
  strategy_id: string;
  file: string | null;
  expert: string | null;
  compiled: boolean;
  errors: number | null;
  warnings: number | null;
  log: string;
  error: string | null;
};

type TesterResult = {
  strategy_id: string; expert: string; symbol: string; symbol_known: boolean;
  timeframe: string; model: number; from: string; to: string;
  config: string; report: string; report_ready: boolean;
  compiled: boolean; launched: boolean; pid: number | null; note: string | null;
};

type CompareRow = {
  key: string; label: string; sqx: number | null; mt5: number | null;
  ratio: number | null; similarity: number | null; within: boolean;
};
type Compare = {
  ready: boolean; message?: string | null; report: string;
  baseline: "is" | "oos";
  rows: CompareRow[]; similar_count: number; compared: number; threshold: number;
  mt5: Record<string, number | null>;
};

const QUALITY: { value: number; label: string }[] = [
  { value: 0, label: "Every tick" },
  { value: 1, label: "1 minute OHLC" },
  { value: 2, label: "Open prices only" },
  { value: 3, label: "Math calculations" },
  { value: 4, label: "Every tick based on real ticks" },
];

function CheckRow({ ok, label, value, detail }: {
  ok: boolean; label: string; value?: string | null; detail?: string;
}) {
  return (
    <div>
      <span>
        <Badge tone={ok ? "good" : "bad"}>{ok ? "✓" : "✗"}</Badge>{" "}
        {label}
        {detail && <span className="muted small"> · {detail}</span>}
      </span>
      <b className="mono" style={{ wordBreak: "break-all" }}>{value || "—"}</b>
    </div>
  );
}

export function MT5BacktesterPage() {
  const [folder, setFolder] = useState(() => {
    try { return localStorage.getItem(FOLDER_KEY) || ""; } catch { return ""; }
  });
  const [check, setCheck] = useState<Check | null>(null);
  const [checking, setChecking] = useState(false);

  const [banks, setBanks] = useState<BankInfo[]>([]);
  const [bankId, setBankId] = useState("");
  const [rows, setRows] = useState<BankStrategy[]>([]);
  const [rowsLoading, setRowsLoading] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [symbol, setSymbol] = useState("");
  const [fromDate, setFromDate] = useState("2020-01-01");
  const [toDate, setToDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [model, setModel] = useState(0);

  const [building, setBuilding] = useState(false);
  const [results, setResults] = useState<BuildResult[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [tester, setTester] = useState<TesterResult | null>(null);
  const [comparison, setComparison] = useState<Compare | null>(null);
  const [comparing, setComparing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runCheck = useCallback(async (raw: string, remember: boolean) => {
    setChecking(true); setError(null);
    try {
      const r = await api.post<Check>("/api/mt5/check", { path: raw });
      setCheck(r);
      if (remember) {
        try { localStorage.setItem(FOLDER_KEY, raw); } catch {}
      }
    } catch (e) { setError(String(e)); setCheck(null); }
    setChecking(false);
  }, []);

  useEffect(() => {
    if (folder.trim()) runCheck(folder, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [b, s] = await Promise.all([
          api.get<BankInfo[]>("/api/banks"),
          api.get<SymbolInfo[]>("/api/symbols"),
        ]);
        setBanks(b);
        setSymbols(s);
        setSymbol((cur) => cur || s[0]?.symbol || "");
        const preferred = b.find((x) => x.group === "stress_testing") || b[0];
        setBankId((cur) => cur || preferred?.bank_id || "");
      } catch (e) { setError(String(e)); }
    })();
  }, []);

  useEffect(() => {
    if (!bankId) { setRows([]); return; }
    setRowsLoading(true);
    setSelected([]);
    setResults([]);
    api.get<BankInfo & { strategies: BankStrategy[] }>(`/api/banks/${bankId}`)
      .then((b) => setRows(b.strategies || []))
      .catch((e) => setError(String(e)))
      .finally(() => setRowsLoading(false));
  }, [bankId]);

  const terminalSymbols = check?.symbols || [];
  const symbolKnown = !symbol.trim() || terminalSymbols.length === 0
    || terminalSymbols.includes(symbol.trim());
  const symbolChoices = useMemo(() => {
    const out = [...terminalSymbols];
    for (const s of symbols) if (!out.includes(s.symbol)) out.push(s.symbol);
    return out;
  }, [terminalSymbols, symbols]);

  // Fill the tester symbol from what the terminal already knows, only when the
  // operator has not typed one yet; a typed symbol always wins.
  useEffect(() => {
    if (symbol.trim() || !terminalSymbols.length) return;
    const preferred = terminalSymbols.find((s) => symbols.some((x) => x.symbol === s));
    setSymbol(preferred || terminalSymbols[0]);
  }, [terminalSymbols, symbols, symbol]);

  const orderedBanks = useMemo(() => {
    const list = [...banks];
    list.sort((a, b) =>
      Number(b.group === "stress_testing") - Number(a.group === "stress_testing"));
    return list;
  }, [banks]);

  function toggle(id: string) {
    setSelected((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  async function build() {
    if (!folder.trim()) { setError("Set the MetaTrader 5 folder first"); return; }
    if (!bankId) { setError("Choose a strategy bank"); return; }
    if (!selected.length) { setError("Select at least one strategy"); return; }
    setBuilding(true); setError(null); setNote(null); setResults([]); setTester(null);
    try {
      const r = await api.post<{ check: Check; results: BuildResult[] }>("/api/mt5/build", {
        path: folder, bank_id: bankId, strategy_ids: selected,
      });
      setCheck(r.check);
      setResults(r.results);
      const ok = r.results.filter((x) => x.compiled).length;
      setNote(`${ok}/${r.results.length} expert advisors compiled into Experts\\SQX.`);
    } catch (e) { setError(String(e)); }
    setBuilding(false);
  }

  async function runTester(strategyId: string) {
    if (!symbol.trim()) { setError("Enter the symbol to test on"); return; }
    setRunning(strategyId); setError(null); setNote(null);
    setTester(null); setComparison(null);
    try {
      const r = await api.post<TesterResult>("/api/mt5/tester", {
        path: folder, bank_id: bankId, strategy_id: strategyId,
        symbol: symbol.trim(), from_date: fromDate, to_date: toDate,
        model, launch: true,
      });
      setTester(r);
      setNote(r.note || `Tester config written: ${r.config}`);
    } catch (e) { setError(String(e)); }
    setRunning(null);
  }

  async function checkResult() {
    if (!tester) return;
    setComparing(true); setError(null);
    try {
      const r = await api.post<Compare>("/api/mt5/compare", {
        path: folder, bank_id: bankId, strategy_id: tester.strategy_id,
      });
      setComparison(r);
      if (!r.ready && r.message) setNote(r.message);
    } catch (e) { setError(String(e)); }
    setComparing(false);
  }

  const missingPreview = check?.indicators.missing.slice(0, 10).join(", ") || "";
  const failedLogs = results.filter((r) => !r.compiled);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>MT5 Backtester</h1>
          <p>
            Export stress-tested strategies as MQL5 experts, compile them with
            MetaEditor and run them in the MT5 strategy tester on a chosen symbol.
          </p>
        </div>
        <div className="head-actions">
          {check && (
            <Badge tone={check.ok ? "good" : "bad"}>
              {check.ok ? "terminal ready" : "terminal incomplete"}
            </Badge>
          )}
        </div>
      </header>

      <ErrorBox error={error} />
      {note && <div className="ok-box">{note}</div>}

      <Section title="MetaTrader 5 terminal"
        subtitle="Point at the folder that holds terminal64.exe (on Linux this is the Wine folder, for example ~/.wine/drive_c/Program Files/MetaTrader 5).">
        <div className="form-grid">
          <Field label="Terminal folder" className="span-2"
            hint="The folder is remembered in this browser.">
            <div className="head-actions" style={{ flexWrap: "nowrap" }}>
              <input style={{ flex: 1, minWidth: 260 }} value={folder}
                placeholder="/home/user/.wine/drive_c/Program Files/MetaTrader 5"
                onChange={(e) => setFolder(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") runCheck(folder, true); }} />
              <button className="btn primary" onClick={() => runCheck(folder, true)}
                disabled={checking || !folder.trim()}>
                {checking ? "Checking…" : "Check terminal"}
              </button>
            </div>
          </Field>
        </div>

        {check && (
          <div className="config-list" style={{ marginTop: 6 }}>
            <CheckRow ok={check.terminal.ok} label="Terminal executable"
              value={check.terminal.path} />
            <CheckRow ok={check.metaeditor.ok} label="MetaEditor (compile)"
              value={check.metaeditor.path}
              detail={check.metaeditor.ok ? undefined : "needed to compile EAs"} />
            <CheckRow ok={check.mql5.ok} label="MQL5 data folder"
              value={check.mql5.path}
              detail={check.mql5.candidates.length > 1
                ? `${check.mql5.candidates.length} candidates` : undefined} />
            <CheckRow ok={check.experts.ok} label="Experts folder"
              value={check.experts.target} />
            <CheckRow ok={check.indicators.found === check.indicators.expected}
              label="SQX indicators pasted"
              value={check.indicators.path}
              detail={`${check.indicators.found}/${check.indicators.expected} found` +
                (check.indicators.folder ? ` in "${check.indicators.folder}"` : "")} />
          </div>
        )}

        {check && !check.indicators.ok && (
          <div className="inline-note" style={{ marginTop: 12 }}>
            <Badge tone="warn">no indicators</Badge>
            <span className="muted">
              Export the indicator pack from the Generation page and paste the
              .mq5 files into {check.indicators.path || "MQL5/Indicators"}.
            </span>
          </div>
        )}
        {check && check.indicators.ok && check.indicators.missing.length > 0 && (
          <p className="muted small" style={{ marginTop: 10 }}>
            Missing indicators: {missingPreview}
            {check.indicators.missing.length > 10
              ? ` … +${check.indicators.missing.length - 10} more` : ""}
          </p>
        )}
        {check?.notes.map((n) => (
          <p key={n} className="muted small" style={{ marginTop: 6 }}>· {n}</p>
        ))}
      </Section>

      <Section title="Stress-tested strategies"
        subtitle="Survivors of the stress pipeline. Selected strategies are exported as SQX\*.mq5 and compiled.">
        <div className="form-grid">
          <Field label="Strategy bank" className="span-2"
            hint="Banks created by the stress pipeline are listed first. Each strategy runs on its own timeframe.">
            <select value={bankId} onChange={(e) => setBankId(e.target.value)}>
              {orderedBanks.length === 0 && <option value="">No banks yet</option>}
              {orderedBanks.map((b) => (
                <option key={b.bank_id} value={b.bank_id}>
                  {b.bank_id} · {b.name} · {b.count} · {b.group || "—"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Symbol for the tester" className="span-2"
            hint={`Detected from the terminal's downloaded history (${terminalSymbols.length} symbols). A typed symbol is always used as-is.`}>
            <input list="mt5-symbol-list" value={symbol}
              placeholder="EURUSD" onChange={(e) => setSymbol(e.target.value)} />
            <datalist id="mt5-symbol-list">
              {symbolChoices.map((s) => <option key={s} value={s} />)}
            </datalist>
            {!symbolKnown && (
              <div className="inline-note">
                <Badge tone="warn">not in terminal history</Badge>
                <span className="muted">
                  {symbol.trim()} was not found among the terminal's symbols; the
                  tester may need to download its history first.
                </span>
              </div>
            )}
          </Field>
          <Field label="Tester from">
            <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} />
          </Field>
          <Field label="Tester to">
            <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} />
          </Field>
          <Field label="Backtest quality" className="span-2"
            hint="MT5 tick-generation model. Higher quality is slower but tracks the SQX M1 fills more closely; real ticks need broker tick history.">
            <select value={model} onChange={(e) => setModel(Number(e.target.value))}>
              {QUALITY.map((q) => <option key={q.value} value={q.value}>{q.label}</option>)}
            </select>
          </Field>
        </div>

        {orderedBanks.length === 0 && (
          <div className="empty small" style={{ marginTop: 12 }}>
            No strategy banks yet. Run the stress pipeline and let strategies survive.
          </div>
        )}
        {orderedBanks.length > 0 && rowsLoading && <Loading what="Loading bank" />}
        {orderedBanks.length > 0 && !rowsLoading && rows.length === 0 && (
          <div className="empty small" style={{ marginTop: 12 }}>This bank has no strategies.</div>
        )}

        {rows.length > 0 && (
          <>
            <div className="head-actions" style={{ margin: "14px 0" }}>
              <button className="btn tiny"
                onClick={() => setSelected(rows.map((r) => r.strategy_id))}>Select all</button>
              <button className="btn tiny" onClick={() => setSelected([])}>Clear</button>
              <span className="muted small">{selected.length} selected</span>
              <button className="btn primary" style={{ marginLeft: "auto" }}
                onClick={build}
                disabled={building || !selected.length || !folder.trim()}>
                {building ? "Exporting & compiling…" : "Export & compile"}
              </button>
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th />
                  <th>Strategy</th>
                  <th>Symbol</th>
                  <th>TF</th>
                  <th>OOS Sharpe</th>
                  <th>Score</th>
                  <th>EA</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const res = results.find((x) => x.strategy_id === r.strategy_id);
                  return (
                    <tr key={r.strategy_id}>
                      <td>
                        <input type="checkbox"
                          checked={selected.includes(r.strategy_id)}
                          onChange={() => toggle(r.strategy_id)} />
                      </td>
                      <td className="mono">{r.strategy_id}</td>
                      <td>{r.symbol}</td>
                      <td>{r.timeframe}</td>
                      <td className="mono">{fmt(r.metrics_oos?.sharpe)}</td>
                      <td className="mono">{fmt(r.score)}</td>
                      <td>
                        {res
                          ? <Badge tone={res.compiled ? "good" : "bad"}>
                              {res.compiled ? "compiled" : "not compiled"}
                            </Badge>
                          : <span className="muted">—</span>}
                      </td>
                      <td>
                        <button className="btn tiny" onClick={() => runTester(r.strategy_id)}
                          disabled={running !== null || !check?.mql5.ok || !symbol.trim()}>
                          {running === r.strategy_id ? "Starting…" : "Test"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}

        {failedLogs.length > 0 && (
          <div style={{ marginTop: 14 }}>
            {failedLogs.map((r) => (
              <div className="error-box" key={r.strategy_id} style={{ marginTop: 8 }}>
                <strong>{r.strategy_id}</strong>{" "}
                {r.error || `compilation failed${r.errors ? ` (${r.errors} errors)` : ""}`}
                {r.log && (
                  <div className="mono small" style={{
                    whiteSpace: "pre-wrap", marginTop: 6,
                    maxHeight: 140, overflow: "auto",
                  }}>{r.log}</div>
                )}
              </div>
            ))}
          </div>
        )}

        {tester && (
          <div className="config-list" style={{ marginTop: 16 }}>
            <div><span>Expert</span><b className="mono">{tester.expert}</b></div>
            <div><span>Backtest quality</span>
              <b>{QUALITY.find((q) => q.value === tester.model)?.label || tester.model}</b></div>
            <div><span>Tester symbol</span>
              <b className="mono">{tester.symbol}
                {tester.symbol_known
                  ? <Badge tone="good">in terminal</Badge>
                  : <Badge tone="warn">unknown</Badge>}
              </b></div>
            <div><span>Timeframe (from strategy)</span>
              <b className="mono">{tester.timeframe}</b></div>
            <div><span>Period</span>
              <b className="mono">{tester.from} → {tester.to}</b></div>
            <div><span>Config</span>
              <b className="mono" style={{ wordBreak: "break-all" }}>{tester.config}</b></div>
            <div><span>Report</span>
              <b className="mono" style={{ wordBreak: "break-all" }}>{tester.report}</b></div>
            <div><span>Compiled (.ex5)</span>
              <b>{tester.compiled
                ? <Badge tone="good">yes</Badge>
                : <Badge tone="bad">no</Badge>}</b></div>
          </div>
        )}

        {tester && (
          <div className="head-actions" style={{ marginTop: 14 }}>
            <button className="btn" onClick={checkResult} disabled={comparing}>
              {comparing ? "Reading report…" : "Check MT5 result"}
            </button>
            <span className="muted small">
              MT5 writes the report after the tester finishes; the SQX side is the
              strategy's out-of-sample metrics, so align the tester dates with it.
            </span>
          </div>
        )}

        {comparison?.ready && comparison.rows.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="head-actions" style={{ marginBottom: 8 }}>
              <Badge tone={comparison.similar_count >= comparison.compared - 1 ? "good" : "warn"}>
                {comparison.similar_count}/{comparison.compared} metrics within
                {" "}{Math.round(comparison.threshold * 100)}% of SQX
              </Badge>
              <span className="muted small mono" style={{ wordBreak: "break-all" }}>
                {comparison.report}
              </span>
            </div>
            <table className="table">
              <thead>
                <tr><th>Metric</th><th>SQX ({comparison.baseline.toUpperCase()})</th>
                  <th>MT5</th><th>MT5 / SQX</th><th>Similarity</th><th /></tr>
              </thead>
              <tbody>
                {comparison.rows.map((r) => (
                  <tr key={r.key}>
                    <td>{r.label}</td>
                    <td className="mono">{fmt(r.sqx)}</td>
                    <td className="mono">{fmt(r.mt5)}</td>
                    <td className="mono">{fmt(r.ratio)}</td>
                    <td className="mono">
                      {r.similarity === null ? "—" : pct(r.similarity * 100, 1)}
                    </td>
                    <td>
                      {r.similarity === null
                        ? <Badge tone="neutral">no data</Badge>
                        : r.within
                          ? <Badge tone="good">match</Badge>
                          : <Badge tone="bad">off</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
