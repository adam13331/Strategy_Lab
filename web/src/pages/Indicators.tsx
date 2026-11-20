import React, { useMemo, useState } from "react";
import { Badge, RangeRow, Toggle } from "../components";
import type { Config, IndicatorDef } from "../types";

export function IndicatorsPage({ config, setConfig, catalogue }: {
  config: Config; setConfig: (c: Config) => void; catalogue: IndicatorDef[];
}) {
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("All");
  const cats = useMemo(() => ["All", ...Array.from(new Set(catalogue.map((c) => c.category)))], [catalogue]);
  const filtered = catalogue.filter((i) =>
    (cat === "All" || i.category === cat) &&
    (i.label.toLowerCase().includes(q.toLowerCase()) || i.id.includes(q.toLowerCase()) ||
      i.category.toLowerCase().includes(q.toLowerCase())));
  const enabled = catalogue.filter((i) => config.indicators[i.id]?.enabled).length;

  const update = (fn: (c: Config) => void) => {
    const c = structuredClone(config); fn(c); setConfig(c);
  };
  const all = (on: boolean) => update((c) => {
    for (const i of catalogue) {
      if (c.indicators[i.id]) c.indicators[i.id].enabled = on;
    }
  });

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Indicators</h1>
          <p>Turn indicators on or off and constrain the parameter ranges the generator may sample.</p>
        </div>
        <Badge tone={enabled ? "accent" : "warn"}>{enabled} / {catalogue.length} enabled</Badge>
      </header>

      <div className="toolbar">
        <input className="search" placeholder="Search indicators…" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <div className="chips">
          {cats.map((c) => (
            <button key={c} className={`chip ${cat === c ? "active" : ""}`} onClick={() => setCat(c)}>{c}</button>
          ))}
        </div>
        <div className="toolbar-right">
          <button className="btn tiny" onClick={() => all(true)}>Enable all</button>
          <button className="btn tiny" onClick={() => all(false)}>Disable all</button>
        </div>
      </div>

      <div className="indicator-grid">
        {filtered.map((ind) => {
          const st = config.indicators[ind.id];
          if (!st) return null;
          return (
            <div className={`ind-card ${st.enabled ? "" : "off"}`} key={ind.id}>
              <div className="ind-head">
                <div>
                  <div className="ind-title">{ind.label}
                    <Badge>{ind.category}</Badge>
                    {ind.needs_volume && <Badge tone="warn">vol</Badge>}
                  </div>
                  <p>{ind.description}</p>
                </div>
                <Toggle value={st.enabled} onChange={(v) => update((c) => {
                  c.indicators[ind.id].enabled = v;
                })} />
              </div>
              <div className="ind-params">
                {ind.params.map((p) => {
                  if (p.choices?.length) {
                    return (
                      <div className="ind-param" key={p.name}>
                        <span className="param-name">{p.label}</span>
                        <span className="hint">sampled: {p.choices.join(" / ")}</span>
                      </div>
                    );
                  }
                  const r = st.params[p.name] || { min: p.min, max: p.max, step: p.step };
                  return (
                    <div className="ind-param" key={p.name}>
                      <span className="param-name">{p.label}</span>
                      <RangeRow {...r} onChange={(v) => update((c) => {
                        c.indicators[ind.id].params[p.name] = v;
                      })} />
                      <span className="warmup">warm-up ~{ind.warmup}</span>
                    </div>
                  );
                })}
                {ind.params.length === 0 && <span className="muted">No parameters</span>}
              </div>
              <div className="ind-outputs">
                {ind.outputs.map((o) => (
                  <span key={o.key} className="out-chip">
                    {o.label}
                    <em>{o.kind}</em>
                    {o.levels.length > 0 && <small>{o.levels.join(" / ")}</small>}
                  </span>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
