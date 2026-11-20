import React, { useCallback, useEffect, useState } from "react";
import { api, fmt } from "../api";
import { Badge, ErrorBox, Field, Section, Loading } from "../components";
import type { CsvFile, SymbolInfo } from "../types";

const TFS = ["M5", "M15", "M30", "H1", "H4", "D1"];

function fmtBytes(n: number) {
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(1)} KB`;
  return `${n} B`;
}

export function DataPage({ symbols, refresh }: {
  symbols: SymbolInfo[]; refresh: () => Promise<void>;
}) {
  const [path, setPath] = useState("");
  const [symbol, setSymbol] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(symbols[0]?.symbol || null);
  const [files, setFiles] = useState<CsvFile[]>([]);
  const [folder, setFolder] = useState("");
  const [scanning, setScanning] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);
  const info = symbols.find((s) => s.symbol === selected) || symbols[0];

  const loadFiles = useCallback(async () => {
    setScanning(true);
    try {
      const r = await api.get<{ folder: string; files: CsvFile[] }>("/api/data/files");
      setFolder(r.folder);
      setFiles(r.files);
    } catch (e) { setError(String(e)); }
    setScanning(false);
  }, []);

  useEffect(() => { loadFiles(); }, [loadFiles]);

  async function importFile(file: CsvFile) {
    setImporting(file.name); setError(null); setMessage(null);
    try {
      const r = await api.post<any>("/api/data/import", { path: file.path, symbol: null });
      setMessage(`Imported ${r.symbol} from ${file.name}: ${r.rows.toLocaleString()} bars`);
      await Promise.all([refresh(), loadFiles()]);
      setSelected(r.symbol);
    } catch (e) { setError(String(e)); }
    setImporting(null);
  }

  async function importNew() {
    const pending = files.filter((f) => !f.imported && f.symbol);
    if (!pending.length) return;
    setBusy(true); setError(null); setMessage(null);
    const done: string[] = [];
    try {
      for (const file of pending) {
        setImporting(file.name);
        const r = await api.post<any>("/api/data/import", { path: file.path, symbol: null });
        done.push(r.symbol);
      }
      setMessage(`Imported ${done.length} file${done.length === 1 ? "" : "s"}: ${done.join(", ")}`);
      await Promise.all([refresh(), loadFiles()]);
      if (done.length) setSelected(done[done.length - 1]);
    } catch (e) { setError(String(e)); }
    setImporting(null);
    setBusy(false);
  }

  async function doImport() {
    setBusy(true); setError(null); setMessage(null);
    try {
      const r = await api.post<any>("/api/data/import", { path, symbol: symbol || null });
      setMessage(`Imported ${r.symbol}: ${r.rows.toLocaleString()} bars`);
      await Promise.all([refresh(), loadFiles()]);
      setSelected(r.symbol);
    } catch (e) { setError(String(e)); }
    setBusy(false);
  }

  async function doResample(tf: string) {
    if (!info) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const r = await api.post<any>("/api/data/resample", {
        symbol: info.symbol, timeframes: [tf],
      });
      setMessage(`${info.symbol} ${tf}: ${r[0].rows.toLocaleString()} bars`);
      await refresh();
    } catch (e) { setError(String(e)); }
    setBusy(false);
  }

  const built = new Set((info?.bar_caches || []).map((b) => b.timeframe));
  const newCount = files.filter((f) => !f.imported && f.symbol).length;

  return (
    <div className="page">
      <header className="page-head">
        <div><h1>Data</h1><p>Ingest broker CSVs, normalize them to Parquet and build resampled series.</p></div>
        <Badge tone="accent">{symbols.length} instrument{symbols.length === 1 ? "" : "s"}</Badge>
      </header>

      <Section
        title="CSV folder"
        subtitle={folder ? `${folder} · symbols are detected from filenames like AUDJPY-M1-No Session.csv` : "Scanning the raw data folder…"}
        right={
          <div className="section-actions">
            {newCount > 0 && (
              <button className="btn primary tiny" onClick={importNew} disabled={busy || scanning || !!importing}>
                {importing ? `Importing ${importing}…` : `Import ${newCount} new`}
              </button>
            )}
            <button className="btn tiny" onClick={loadFiles} disabled={scanning}>
              {scanning ? "Scanning…" : "Refresh"}
            </button>
          </div>
        }
      >
        {files.length === 0 ? (
          <div className="empty">
            {scanning ? "Scanning…" : "No CSV files found. Drop broker exports into the folder shown above, then press Refresh."}
          </div>
        ) : (
          <div className="csv-list">
            {files.map((file) => (
              <div className="csv-row" key={file.name}>
                <div className="csv-main">
                  <div className="csv-name">{file.name}</div>
                  <div className="csv-meta">
                    {fmtBytes(file.size_bytes)} · modified {file.modified.replace("T", " ")}
                    {file.imported && file.imported_rows != null && (
                      <> · {file.imported_rows.toLocaleString()} bars imported</>
                    )}
                  </div>
                </div>
                <div className="csv-symbol">
                  {file.symbol
                    ? <Badge tone="accent">{file.symbol}</Badge>
                    : <Badge tone="bad">no symbol</Badge>}
                </div>
                <div className="csv-status">
                  {file.error
                    ? <span className="muted">{file.error}</span>
                    : file.imported
                      ? <Badge tone="good">imported</Badge>
                      : <Badge>new</Badge>}
                </div>
                <div className="csv-action">
                  <button className="btn tiny" onClick={() => importFile(file)}
                    disabled={busy || scanning || !!importing || !file.symbol}>
                    {importing === file.name ? "Importing…" : file.imported ? "Re-import" : "Import"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <ErrorBox error={error} />
        {message && <div className="ok-box">{message}</div>}
      </Section>

      <Section title="Import by path" subtitle="For CSVs outside the folder. Date,Time,Open,High,Low,Close,Volume.">
        <div className="form-row">
          <Field label="CSV path" hint="Absolute path, or relative to data/raw or the project root">
            <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/path/to/USDJPY-M1.csv" />
          </Field>
          <Field label="Symbol override" hint="Optional if the filename starts with SYMBOL-">
            <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="USDJPY" />
          </Field>
          <div className="form-action">
            <button className="btn primary" onClick={doImport} disabled={busy || !path}>
              {busy ? "Working…" : "Import"}
            </button>
          </div>
        </div>
      </Section>

      {symbols.length === 0 ? (
        <div className="empty">No instruments yet. Import a CSV from the folder to begin.</div>
      ) : (
        <>
          <div className="checklist">
            <div className={`check ${files.length && files.every((f) => f.imported || !f.symbol) ? "done" : ""}`}>
              <span className="check-mark">{files.length && files.every((f) => f.imported || !f.symbol) ? "✓" : "1"}</span>
              <div><strong>Import your CSV files</strong><span className="muted">Bring broker exports into SQX.</span></div>
            </div>
            <div className={`check ${symbols.some((s) => (s.bar_caches || []).length > 0) ? "done" : ""}`}>
              <span className="check-mark">{symbols.some((s) => (s.bar_caches || []).length > 0) ? "✓" : "2"}</span>
              <div><strong>Build at least one timeframe</strong><span className="muted">Signals need H4/H1/etc. bars.</span></div>
            </div>
            <div className={`check ${symbols.length > 0 ? "done" : ""}`}>
              <span className="check-mark">{symbols.length > 0 ? "✓" : "3"}</span>
              <div><strong>Ready to generate</strong><span className="muted">Go to Generator and start a search.</span></div>
            </div>
          </div>
          <div className="chips">
            {symbols.map((s) => (
              <button key={s.symbol} className={`chip ${info?.symbol === s.symbol ? "active" : ""}`}
                onClick={() => setSelected(s.symbol)}>{s.symbol}</button>
            ))}
          </div>

          {info && (
            <>
              <div className="stat-grid">
                <div className="stat"><span className="stat-label">Bars</span><span className="stat-value">{info.rows.toLocaleString()}</span><span className="stat-sub">M1</span></div>
                <div className="stat"><span className="stat-label">Start</span><span className="stat-value small">{info.start_ts}</span></div>
                <div className="stat"><span className="stat-label">End</span><span className="stat-value small">{info.end_ts}</span></div>
                <div className="stat"><span className="stat-label">Timezone</span><span className="stat-value small">{info.tz_assumed}</span><span className="stat-sub">{info.quality?.inferred_tz}</span></div>
              </div>

              <Section title="Timeframes" subtitle="Signals use the chosen timeframe; fills always use the M1 series.">
                <div className="tf-grid">
                  <div className="tf-card built">
                    <div className="tf-name">M1</div>
                    <div className="tf-meta">{info.rows.toLocaleString()} bars</div>
                    <Badge tone="good">base</Badge>
                  </div>
                  {TFS.map((tf) => {
                    const cache = (info.bar_caches || []).find((b) => b.timeframe === tf);
                    return (
                      <div key={tf} className={`tf-card ${cache ? "built" : ""}`}>
                        <div className="tf-name">{tf}</div>
                        <div className="tf-meta">{cache ? `${cache.rows.toLocaleString()} bars` : "not built"}</div>
                        <button className="btn tiny" onClick={() => doResample(tf)} disabled={busy}>
                          {cache ? "Rebuild" : "Build"}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </Section>

              <Section title="Quality report" subtitle={info.source_file}>
                <div className="quality-grid">
                  <div><span>Source rows</span><b>{info.quality?.rows?.toLocaleString() ?? "—"}</b></div>
                  <div><span>Invalid rows</span><b>{info.quality?.invalid_rows ?? 0}</b></div>
                  <div><span>Duplicate timestamps</span><b>{info.quality?.duplicate_rows ?? 0}</b></div>
                  <div><span>Gaps &gt; 1 min</span><b>{info.quality?.gaps_over_1min?.toLocaleString() ?? "—"}</b></div>
                  <div><span>Gaps &gt; 1 day</span><b>{info.quality?.gaps_over_1day?.toLocaleString() ?? "—"}</b></div>
                  <div><span>Imported</span><b>{String(info.imported_at || "").slice(0, 19)}</b></div>
                </div>
                {info.quality?.years && (
                  <div className="year-bars">
                    {Object.entries(info.quality.years).map(([y, n]) => {
                      const max = Math.max(...Object.values(info.quality.years));
                      return (
                        <div className="year-bar" key={y} title={`${y}: ${n.toLocaleString()} bars`}>
                          <div className="bar" style={{ height: `${Math.max(6, (n / max) * 100)}%` }} />
                          <span>{y.slice(2)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Section>
            </>
          )}
        </>
      )}
    </div>
  );
}
