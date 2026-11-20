import React, { useEffect, useState } from "react";
import { api, fmt } from "../api";
import { ErrorBox, Field, Loading, Num, Section, Toggle } from "../components";

type Perf = "low_memory" | "balanced" | "high_throughput";

type Settings = {
  max_ram_mb: number;
  performance: Perf;
  workers: number;
  clear_cache_after_run: boolean;
};

type Payload = {
  settings: Settings;
  system: { cpu_count: number; total_ram_mb: number };
  effective: { workers: number; cache_budget_mb: number };
  cache: { entries: number; bytes: number; budget: number };
};

const PROFILES: { value: Perf; label: string; blurb: string }[] = [
  {
    value: "low_memory",
    label: "Low memory",
    blurb: "Fewer workers, small cache. Use on laptops or alongside other apps.",
  },
  {
    value: "balanced",
    label: "Balanced",
    blurb: "Recommended. Leaves some headroom for the desktop and stress runs.",
  },
  {
    value: "high_throughput",
    label: "High throughput",
    blurb: "Uses every core and a larger cache. Fastest, but needs plenty of RAM.",
  },
];

const mb = (bytes: number) => fmt(bytes / (1024 * 1024), 1) + " MB";

export function SettingsPage() {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    try {
      const d = await api.get<Payload>("/api/settings");
      setPayload(d);
      setSettings(d.settings);
    } catch (e) { setError(String(e)); }
  }

  useEffect(() => { load(); }, []);

  function upd(change: (s: Settings) => void) {
    setSettings((s) => {
      if (!s) return s;
      const next = { ...s };
      change(next);
      return next;
    });
  }

  async function save() {
    if (!settings) return;
    setSaving(true); setError(null); setMessage(null);
    try {
      const d = await api.post<Payload>("/api/settings", settings);
      setPayload(d);
      setSettings(d.settings);
      setMessage("Settings saved. They apply to the next run.");
    } catch (e) { setError(String(e)); }
    finally { setSaving(false); }
  }

  async function clearCache() {
    setError(null); setMessage(null);
    try {
      const d = await api.post<Payload & { freed_bytes: number }>("/api/settings/clear-cache");
      setPayload(d);
      setMessage(`Freed ${mb(d.freed_bytes)} of indicator cache.`);
    } catch (e) { setError(String(e)); }
  }

  if (!payload || !settings) return <Loading what="Loading settings" />;

  const total = payload.system.total_ram_mb;
  const unlimited = settings.max_ram_mb === 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Control how much memory the engine uses so generation never runs out of RAM.</p>
        </div>
        <div className="head-actions">
          <button className="btn primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
        </div>
      </header>

      <ErrorBox error={error} />
      {message && <div className="ok-box">{message}</div>}

      <Section
        title="Memory budget"
        subtitle={`This machine reports ${total ? fmt(total, 0) + " MB" : "unknown"} of RAM and ${payload.system.cpu_count} CPU cores.`}
      >
        <div className="preset-grid" style={{ marginBottom: 14 }}>
          <button
            className={`preset-card ${unlimited ? "active" : ""}`}
            onClick={() => upd((s) => { s.max_ram_mb = 0; })}
          >
            <strong>Automatic</strong>
            <span className="muted">Let SQX choose a safe budget from the performance profile.</span>
          </button>
          {total > 0 && (
            <>
              <button
                className="preset-card"
                onClick={() => upd((s) => { s.max_ram_mb = Math.round(total * 0.5); })}
              >
                <strong>Use half ({fmt(total * 0.5, 0)} MB)</strong>
                <span className="muted">Comfortable default for most desktops.</span>
              </button>
              <button
                className="preset-card"
                onClick={() => upd((s) => { s.max_ram_mb = Math.round(total * 0.75); })}
              >
                <strong>Use 75% ({fmt(total * 0.75, 0)} MB)</strong>
                <span className="muted">More speed, still leaves headroom for other apps.</span>
              </button>
            </>
          )}
        </div>

        <div className="form-grid">
          <Field
            label="Available RAM for SQX"
            hint="0 means automatic. The indicator cache is capped at ~35% of this, the rest is for bars and contexts."
          >
            <Num value={settings.max_ram_mb} min={0} step={512} suffix="MB"
              onChange={(v) => upd((s) => { s.max_ram_mb = Math.max(0, v); })} />
          </Field>
          <Field label="Worker threads" hint="0 uses the performance profile. Lower this if the PC feels sluggish.">
            <Num value={settings.workers} min={0} step={1}
              onChange={(v) => upd((s) => { s.workers = Math.max(0, v); })} />
          </Field>
        </div>
        <p className="hint">
          RAM budget {mb(payload.effective.cache_budget_mb * 1024 * 1024)} for the indicator
          cache · {payload.effective.workers} workers effective.
        </p>
      </Section>

      <Section title="Performance profile" subtitle="Profiles set defaults for workers and cache size.">
        <div className="preset-grid">
          {PROFILES.map((p) => (
            <button
              key={p.value}
              className={`preset-card ${settings.performance === p.value ? "active" : ""}`}
              onClick={() => upd((s) => { s.performance = p.value; })}
            >
              <strong>{p.label}</strong>
              <span className="muted">{p.blurb}</span>
            </button>
          ))}
        </div>
      </Section>

      <Section
        title="Cache"
        subtitle="The engine caches indicator values to avoid recomputing them. Clearing it releases memory immediately."
        right={<button className="btn" onClick={clearCache}>Clear cache now</button>}
      >
        <div className="stat-grid">
          <div className="stat">
            <span className="stat-label">Cached entries</span>
            <span className="stat-value">{fmt(payload.cache.entries, 0)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">In use</span>
            <span className="stat-value">{mb(payload.cache.bytes)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Budget</span>
            <span className="stat-value">{mb(payload.cache.budget)}</span>
          </div>
        </div>
        <div className="inline-note" style={{ marginTop: 12 }}>
          <Toggle
            value={settings.clear_cache_after_run}
            onChange={(v) => upd((s) => { s.clear_cache_after_run = v; })}
            label="Clear the cache automatically when a run finishes"
          />
        </div>
      </Section>
    </div>
  );
}
