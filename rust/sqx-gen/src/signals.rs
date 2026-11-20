//! Port of signal building and `run_backtest` from `sqx.engine`.
//!
//! A `Cache` is per attempt, like `IndicatorCache()` in the Python runner, and
//! is keyed by (indicator, params, output) so only the output a rule actually
//! uses is computed.

use std::collections::HashMap;
use std::rc::Rc;
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::catalogue::Ind;
use crate::ctx::Ctx;
use crate::indicators::{args_from_params, compute_subset, Series};
use crate::kernel::{simulate, Dist, OrderDist, Sim, Trade};
use crate::metrics::segment_metrics;

pub struct Cache {
    entries: HashMap<(String, Vec<u64>, String), Rc<Vec<f64>>>,
    pub computes: usize,
}

impl Cache {
    pub fn new() -> Cache {
        Cache {
            entries: HashMap::new(),
            computes: 0,
        }
    }

    fn get(&mut self, ind: &Ind, args: &[f64], key: &str, ctx: &Ctx) -> Rc<Vec<f64>> {
        let k = (
            ind.id.clone(),
            args.iter().map(|a| a.to_bits()).collect::<Vec<u64>>(),
            key.to_string(),
        );
        if let Some(v) = self.entries.get(&k) {
            return v.clone();
        }
        self.computes += 1;
        let s = Series {
            open: &ctx.tf_open,
            high: &ctx.tf_high,
            low: &ctx.tf_low,
            close: &ctx.tf_close,
            volume: &ctx.tf_volume,
        };
        let out = Rc::new(compute_subset(&ind.id, args, &s, &ctx.tf_ts, ind, key));
        self.entries.insert(k, out.clone());
        out
    }
}

/// Split of one attempt, filled only when the caller asks for it.
#[derive(Default)]
pub struct Timings {
    pub signals_ns: u128,
    pub kernel_ns: u128,
    pub metrics_ns: u128,
}

pub struct BacktestResult {
    pub metrics_is: Map<String, Value>,
    pub metrics_oos: Map<String, Value>,
    pub metrics_all: Map<String, Value>,
    pub final_equity: f64,
    pub max_dd_m1: f64,
    pub split_day: i64,
    pub signal_counts: (usize, usize, usize),
    pub trades: Vec<Trade>,
    pub daily_eq: Vec<f64>,
}

fn cat<'a>(catalogue: &'a [Ind], id: &str) -> &'a Ind {
    catalogue
        .iter()
        .find(|i| i.id == id)
        .unwrap_or_else(|| panic!("indicator {id} missing from catalogue"))
}

fn eval_condition(c: &Value, catalogue: &[Ind], ctx: &Ctx, cache: &mut Cache) -> Vec<bool> {
    let ind_id = c["indicator"].as_str().unwrap_or("");
    let ind = cat(catalogue, ind_id);
    let params = c["params"].as_object().cloned().unwrap_or_default();
    let args = args_from_params(ind, &params);
    let key = c["output"].as_str().unwrap_or("");
    let a = cache.get(ind, &args, key, ctx);
    let op = c["operator"].as_str().unwrap_or("");
    let n = a.len();

    if op == "is_true" {
        return (0..n).map(|i| !a[i].is_nan() && a[i] > 0.5).collect();
    }

    let kind = c["operand"]["kind"].as_str().unwrap_or("none");
    let level = c["operand"]["value"].as_f64().unwrap_or(0.0);
    let mut sig = vec![false; n];
    match kind {
        "price" => {
            let b = &ctx.tf_close;
            match op {
                "gt" => {
                    for i in 0..n {
                        sig[i] = a[i] > b[i];
                    }
                }
                "ge" | "gte" => {
                    for i in 0..n {
                        sig[i] = a[i] >= b[i];
                    }
                }
                "lt" => {
                    for i in 0..n {
                        sig[i] = a[i] < b[i];
                    }
                }
                "le" | "lte" => {
                    for i in 0..n {
                        sig[i] = a[i] <= b[i];
                    }
                }
                "cross_above" => {
                    for i in 1..n {
                        sig[i] = a[i] > b[i] && a[i - 1] <= b[i - 1];
                    }
                }
                "cross_below" => {
                    for i in 1..n {
                        sig[i] = a[i] < b[i] && a[i - 1] >= b[i - 1];
                    }
                }
                _ => {}
            }
            for i in 0..n {
                if !a[i].is_finite() || !b[i].is_finite() {
                    sig[i] = false;
                }
            }
        }
        "level" => {
            match op {
                "gt" => {
                    for i in 0..n {
                        sig[i] = a[i] > level;
                    }
                }
                "ge" | "gte" => {
                    for i in 0..n {
                        sig[i] = a[i] >= level;
                    }
                }
                "lt" => {
                    for i in 0..n {
                        sig[i] = a[i] < level;
                    }
                }
                "le" | "lte" => {
                    for i in 0..n {
                        sig[i] = a[i] <= level;
                    }
                }
                "cross_above" => {
                    for i in 1..n {
                        sig[i] = a[i] > level && a[i - 1] <= level;
                    }
                }
                "cross_below" => {
                    for i in 1..n {
                        sig[i] = a[i] < level && a[i - 1] >= level;
                    }
                }
                _ => {}
            }
            for i in 0..n {
                if !a[i].is_finite() {
                    sig[i] = false;
                }
            }
        }
        _ => {}
    }
    sig
}

pub fn build_tf_signal(
    conds: &[Value],
    catalogue: &[Ind],
    ctx: &Ctx,
    cache: &mut Cache,
) -> Vec<bool> {
    let n = ctx.tf_close.len();
    if conds.is_empty() {
        return vec![false; n];
    }
    let mut sig = vec![true; n];
    for c in conds {
        let s = eval_condition(c, catalogue, ctx, cache);
        for i in 0..n {
            sig[i] &= s[i];
        }
    }
    sig
}

fn to_m1(tf_sig: &[bool], ctx: &Ctx) -> Vec<u8> {
    let mut out = vec![0u8; ctx.m1_ts.len()];
    for i in 0..ctx.tf_to_m1.len() {
        let idx = ctx.tf_to_m1[i];
        if idx >= 0 && (idx as usize) < out.len() && tf_sig[i] {
            out[idx as usize] = 1;
        }
    }
    out
}

fn num(v: &Value, key: &str) -> f64 {
    v.get(key).and_then(|x| x.as_f64()).unwrap_or(0.0)
}

fn atr_period(p: &Value) -> Option<usize> {
    if p.get("mode").and_then(|m| m.as_str()) == Some("none") {
        return None;
    }
    if p.get("type").and_then(|t| t.as_str()) == Some("fixed") {
        return None;
    }
    Some(num(p, "atr_period") as usize)
}

/// ATR as % of close per timeframe bar, ready to be gathered through
/// `m1_prev_tf` inside the kernel.
fn atr_tf(period: usize, catalogue: &[Ind], ctx: &Ctx, cache: &mut Cache) -> Vec<f64> {
    let ind = cat(catalogue, "atr");
    let pct = cache.get(ind, &[period as f64], "atr_pct", ctx);
    (0..ctx.tf_close.len())
        .map(|t| pct[t] / 100.0 * ctx.tf_close[t])
        .collect()
}

fn make_dist<'a>(p: &Value, atr: &'a HashMap<usize, Vec<f64>>, ctx: &'a Ctx) -> Dist<'a> {
    if p.get("mode").and_then(|m| m.as_str()) == Some("none") {
        return Dist::Zero;
    }
    if p.get("type").and_then(|t| t.as_str()) == Some("fixed") {
        return Dist::Fixed(num(p, "pips") * ctx.pip);
    }
    let period = num(p, "atr_period") as usize;
    let mult = num(p, "atr_mult");
    match atr.get(&period) {
        Some(v) => Dist::Mapped {
            tf: v.as_slice(),
            prev: &ctx.m1_prev_tf,
            mult,
        },
        None => Dist::Zero,
    }
}

fn make_order_dist<'a>(
    order: &Value,
    atr: &'a HashMap<usize, Vec<f64>>,
    ctx: &'a Ctx,
) -> OrderDist<'a> {
    let typ = order.get("type").and_then(|t| t.as_str()).unwrap_or("market");
    if typ == "market" {
        return OrderDist::Const(0.0);
    }
    let dist_type = order
        .get("distance_type")
        .and_then(|t| t.as_str())
        .unwrap_or("fixed");
    if dist_type == "fixed" {
        return OrderDist::Const(num(order, "pips") * ctx.pip);
    }
    let period = num(order, "atr_period") as usize;
    let mult = num(order, "atr_mult");
    match atr.get(&period) {
        Some(v) => OrderDist::Mapped {
            tf: v.as_slice(),
            prev: &ctx.m1_prev_tf,
            mult,
        },
        None => OrderDist::Const(0.0),
    }
}

pub fn run_backtest(
    spec: &Value,
    ctx: &Ctx,
    catalogue: &[Ind],
    cache: &mut Cache,
    mut timings: Option<&mut Timings>,
) -> BacktestResult {
    let ex = &spec["exit"];
    let risk = &spec["risk"];
    let t0 = Instant::now();
    let empty: Vec<Value> = Vec::new();
    let long_sig = to_m1(
        &build_tf_signal(
            spec["entry"]["long"].as_array().unwrap_or(&empty),
            catalogue,
            ctx,
            cache,
        ),
        ctx,
    );
    let short_sig = to_m1(
        &build_tf_signal(
            spec["entry"]["short"].as_array().unwrap_or(&empty),
            catalogue,
            ctx,
            cache,
        ),
        ctx,
    );
    let exit_tf = build_tf_signal(
        ex["conditions"].as_array().unwrap_or(&empty),
        catalogue,
        ctx,
        cache,
    );
    let exit_long = to_m1(&exit_tf, ctx);

    let mut raw_orders = spec
        .get("execution")
        .and_then(|e| e.get("orders"))
        .and_then(|o| o.as_array())
        .cloned()
        .unwrap_or_default();
    if raw_orders.is_empty() {
        raw_orders = vec![json!({"type": "market"})];
    }

    // Every distinct ATR period the spec needs, computed once.
    let mut periods: Vec<usize> = Vec::new();
    for key in ["stop_loss", "take_profit", "trailing"] {
        if let Some(p) = atr_period(&ex[key]) {
            if !periods.contains(&p) {
                periods.push(p);
            }
        }
    }
    for order in &raw_orders {
        let typ = order.get("type").and_then(|t| t.as_str()).unwrap_or("market");
        if typ != "market"
            && order.get("distance_type").and_then(|t| t.as_str()) == Some("atr")
        {
            let p = num(order, "atr_period") as usize;
            if !periods.contains(&p) {
                periods.push(p);
            }
        }
    }
    let mut atr_map: HashMap<usize, Vec<f64>> = HashMap::with_capacity(periods.len());
    for p in periods {
        let series = atr_tf(p, catalogue, ctx, cache);
        atr_map.insert(p, series);
    }

    let sl = make_dist(&ex["stop_loss"], &atr_map, ctx);
    let tp = make_dist(&ex["take_profit"], &atr_map, ctx);
    let tr = &ex["trailing"];
    let trail = if tr.get("mode").and_then(|m| m.as_str()) == Some("none") {
        Dist::Zero
    } else {
        make_dist(tr, &atr_map, ctx)
    };

    let mut order_types: Vec<i64> = Vec::with_capacity(raw_orders.len());
    let mut order_dists: Vec<OrderDist> = Vec::with_capacity(raw_orders.len());
    for order in &raw_orders {
        let typ = order.get("type").and_then(|t| t.as_str()).unwrap_or("market");
        order_types.push(if typ == "market" {
            0
        } else if typ == "limit" {
            1
        } else {
            2
        });
        order_dists.push(make_order_dist(order, &atr_map, ctx));
    }
    let order_expiry = spec
        .get("execution")
        .and_then(|e| e.get("expiry_bars"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    let t1 = Instant::now();
    let n_m1 = ctx.m1_ts.len();
    let no_mask: Vec<u8> = Vec::new();
    let sizing = risk
        .get("sizing")
        .and_then(|s| s.as_str())
        .unwrap_or("fixed_lot");
    let opposite = ex
        .get("opposite_signal")
        .and_then(|s| s.as_str())
        .unwrap_or("ignore");

    let sim = Sim {
        op: &ctx.m1_open,
        hi: &ctx.m1_high,
        lo: &ctx.m1_low,
        cl: &ctx.m1_close,
        day_id: &ctx.m1_day_id,
        n_days: ctx.day_ts.len(),
        entry_long: &long_sig,
        entry_short: &short_sig,
        exit_long: &exit_long,
        exit_short: &exit_long,
        entry_ok: &ctx.entry_ok,
        force_close: &ctx.force_close,
        split_idx: ctx.split_idx as usize,
        sl,
        tp,
        trail,
        lots: num(risk, "fixed_lot"),
        use_risk_pct: if sizing == "risk_percent" { 1 } else { 0 },
        risk_pct: num(risk, "risk_percent"),
        leverage: num(risk, "leverage"),
        contract: 100_000.0,
        pip: ctx.pip,
        half_spread: num(risk, "spread_pips") * ctx.pip / 2.0,
        slippage: num(risk, "slippage_pips") * ctx.pip / 2.0,
        commission: num(risk, "commission_per_lot"),
        opposite_reverse: if opposite == "reverse" { 1 } else { 0 },
        max_bars: num(ex, "max_bars_in_trade") as i64,
        cooldown_bars: num(ex, "cooldown_bars") as i64,
        balance: num(risk, "balance"),
        skip_entry: &no_mask,
        skip_exit: &no_mask,
        random_close: &no_mask,
        order_types: &order_types,
        order_dists: &order_dists,
        order_expiry,
    };
    let _ = n_m1;
    let out = simulate(&sim);
    let t2 = Instant::now();

    let base = num(risk, "balance");
    let split_day = (ctx.split_day as usize).min(ctx.day_ts.len());
    let day_ts = &ctx.day_ts;

    let mut is_pnls: Vec<f64> = Vec::new();
    let mut oos_pnls: Vec<f64> = Vec::new();
    let mut all_pnls: Vec<f64> = Vec::with_capacity(out.trades.len());
    for t in &out.trades {
        let day = ctx.m1_day_id[t.entry_i as usize];
        all_pnls.push(t.pnl);
        if day < ctx.split_day {
            is_pnls.push(t.pnl);
        } else {
            oos_pnls.push(t.pnl);
        }
    }

    let is_metrics = segment_metrics(
        &out.daily_eq[..split_day],
        base,
        &day_ts[..split_day],
        &is_pnls,
        is_pnls.len(),
    );
    let oos_base = if split_day > 0 {
        out.daily_eq[split_day - 1]
    } else {
        base
    };
    let oos_metrics = segment_metrics(
        &out.daily_eq[split_day..],
        oos_base,
        &day_ts[split_day..],
        &oos_pnls,
        oos_pnls.len(),
    );
    let all_metrics = segment_metrics(&out.daily_eq, base, day_ts, &all_pnls, out.trades.len());

    let t3 = Instant::now();
    if let Some(tm) = timings.as_mut() {
        tm.signals_ns += (t1 - t0).as_nanos();
        tm.kernel_ns += (t2 - t1).as_nanos();
        tm.metrics_ns += (t3 - t2).as_nanos();
    }

    BacktestResult {
        metrics_is: is_metrics,
        metrics_oos: oos_metrics,
        metrics_all: all_metrics,
        final_equity: out.final_eq,
        max_dd_m1: out.maxdd * 100.0,
        split_day: ctx.split_day,
        signal_counts: (
            long_sig.iter().filter(|v| **v != 0).count(),
            short_sig.iter().filter(|v| **v != 0).count(),
            exit_long.iter().filter(|v| **v != 0).count(),
        ),
        trades: out.trades,
        daily_eq: out.daily_eq,
    }
}
