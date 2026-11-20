//! Port of `sqx.strategy.sample_strategy` for the default generation config.
//!
//! Ranges come from `catalogue.txt` (exported from the Python catalogue), so
//! the Rust sampler draws from the same distribution.  The RNG stream is not
//! MT19937 — see `rng.rs` — so specs are exchanged between engines, not seeds.

use serde_json::{json, Value};

use crate::catalogue::{Ind, Param};
use crate::hash::spec_hash;
use crate::rng::Rng;

const OPERATORS: [&str; 6] = ["gt", "lt", "ge", "le", "cross_above", "cross_below"];
const MAX_ENTRY_INDICATORS: usize = 3;
const MAX_EXIT_INDICATORS: usize = 1;
const ORDER_TYPES: [&str; 3] = ["market", "limit", "stop"];
const MAX_ORDERS: usize = 2;

fn grid(rng: &mut Rng, lo: f64, hi: f64, step: f64, integer: bool) -> f64 {
    let (lo, hi) = if hi < lo { (hi, lo) } else { (lo, hi) };
    let step = if step > 0.0 { step } else { 1.0 };
    let n = ((hi - lo) / step).round() as i64;
    let v = lo + rng.range_inclusive(n.max(0) as u64) as f64 * step;
    let v = v.max(lo).min(hi);
    if integer {
        v.round()
    } else {
        (v * 1e6).round() / 1e6
    }
}

fn sample_params(rng: &mut Rng, ind: &Ind) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    for p in &ind.params {
        if !p.choices.is_empty() {
            let c = rng.choice(&p.choices).clone();
            out.insert(p.name.clone(), json!(c));
            continue;
        }
        let v = grid(rng, p.min, p.max, p.step, p.integer);
        out.insert(
            p.name.clone(),
            if p.integer { json!(v as i64) } else { json!(v) },
        );
    }
    out
}

fn condition(rng: &mut Rng, ind: &Ind) -> Value {
    let out = rng.choice(&ind.outputs).clone();
    let params = sample_params(rng, ind);
    let (operator, operand) = if out.kind == "bool" {
        ("is_true".to_string(), json!({"kind": "none", "value": Value::Null}))
    } else if out.kind == "price" {
        (
            rng.choice(&OPERATORS).to_string(),
            json!({"kind": "price", "value": Value::Null}),
        )
    } else {
        let level = if out.levels.is_empty() {
            json!(0)
        } else {
            let l = *rng.choice(&out.levels);
            if l.fract() == 0.0 {
                json!(l as i64)
            } else {
                json!(l)
            }
        };
        (
            rng.choice(&OPERATORS).to_string(),
            json!({"kind": "level", "value": level}),
        )
    };
    json!({
        "indicator": ind.id,
        "output": out.key,
        "operator": operator,
        "params": params,
        "operand": operand,
    })
}

fn resolve_policy(rng: &mut Rng, kind: &str) -> Value {
    // stop_loss: mode may / type atr ; take_profit: mode may / type fixed
    let always = rng.next_f64() < 0.5;
    if !always {
        return json!({"mode": "none", "type": Value::Null});
    }
    if kind == "take_profit" {
        let pips = grid(rng, 20.0, 80.0, 1.0, false);
        return json!({"mode": kind, "type": "fixed", "pips": pips});
    }
    let period = grid(rng, 7.0, 21.0, 2.0, true);
    let mult = grid(rng, 1.5, 4.0, 0.25, false);
    json!({"mode": kind, "type": "atr", "atr_period": period as i64, "atr_mult": mult})
}

fn resolve_orders(rng: &mut Rng) -> Value {
    let n = rng.range_inclusive((MAX_ORDERS - 1) as u64) as usize + 1;
    let mut chosen: Vec<String> = rng.sample(&ORDER_TYPES, n).iter().map(|s| s.to_string()).collect();
    if chosen.len() > 1 && chosen.iter().any(|t| t == "market") {
        let filtered: Vec<String> = chosen.iter().filter(|t| *t != "market").cloned().collect();
        chosen = if filtered.is_empty() { vec!["market".to_string()] } else { filtered };
    }
    let mut orders: Vec<Value> = Vec::new();
    for typ in &chosen {
        if typ == "market" {
            orders.push(json!({"type": "market"}));
            continue;
        }
        let period = grid(rng, 7.0, 21.0, 2.0, true);
        let mult = grid(rng, 0.25, 1.5, 0.25, false);
        orders.push(json!({
            "type": typ,
            "distance_type": "atr",
            "atr_period": period as i64,
            "atr_mult": mult,
        }));
    }
    let mut expiry = grid(rng, 1.0, 60.0, 1.0, true) as i64;
    if orders.iter().all(|o| o["type"] == "market") {
        expiry = 0;
    }
    json!({"orders": orders, "expiry_bars": expiry, "oco": true})
}

pub fn sample_strategy(rng: &mut Rng, catalogue: &[Ind], symbol: &str, timeframe: &str) -> Value {
    let mut entry_long: Vec<Value> = Vec::new();
    let mut entry_short: Vec<Value> = Vec::new();
    for side in 0..2 {
        let hi = MAX_ENTRY_INDICATORS.max(1);
        let lo = if hi >= 2 { hi - 1 } else { 1 };
        let n = (rng.range_inclusive((hi - lo) as u64) as usize) + lo;
        let mut used: Vec<(String, String, String, String)> = Vec::new();
        let target = if side == 0 { &mut entry_long } else { &mut entry_short };
        for _ in 0..n {
            for _try in 0..12 {
                let ind = rng.choice(catalogue).clone();
                let c = condition(rng, &ind);
                let key = (
                    c["indicator"].as_str().unwrap_or("").to_string(),
                    c["output"].as_str().unwrap_or("").to_string(),
                    c["operator"].as_str().unwrap_or("").to_string(),
                    c["operand"]["value"].to_string(),
                );
                if !used.contains(&key) {
                    used.push(key);
                    target.push(c);
                    break;
                }
            }
        }
    }

    let mut exits: Vec<Value> = Vec::new();
    let n_exit = rng.range_inclusive(MAX_EXIT_INDICATORS as u64) as usize;
    let mut used: Vec<(String, String, String, String)> = Vec::new();
    for _ in 0..n_exit {
        for _try in 0..12 {
            let ind = rng.choice(catalogue).clone();
            let c = condition(rng, &ind);
            let key = (
                c["indicator"].as_str().unwrap_or("").to_string(),
                c["output"].as_str().unwrap_or("").to_string(),
                c["operator"].as_str().unwrap_or("").to_string(),
                c["operand"]["value"].to_string(),
            );
            if !used.contains(&key) {
                used.push(key);
                exits.push(c);
                break;
            }
        }
    }

    let opposite = if rng.next_f64() < 0.5 { "reverse" } else { "ignore" };
    let mut sl = resolve_policy(rng, "stop_loss");
    let tp = resolve_policy(rng, "take_profit");
    let orders = resolve_orders(rng);
    let trailing = json!({"mode": "none"});

    if sl["mode"] == "none" && tp["mode"] == "none" && exits.is_empty() && opposite == "ignore" {
        // guarantee an exit path, same as the Python sampler
        sl = json!({"mode": "stop_loss", "type": "atr", "atr_period": 14, "atr_mult": 3.0});
    }

    let max_bars = *rng.choice(&[60i64, 120, 240, 480]);
    let cooldown = *rng.choice(&[0i64, 24, 96, 240, 480]);

    json!({
        "schema": 1,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": "both",
        "entry": {"long": entry_long, "short": entry_short},
        "execution": orders,
        "exit": {
            "conditions": exits,
            "opposite_signal": opposite,
            "stop_loss": sl,
            "take_profit": tp,
            "trailing": trailing,
            "max_bars_in_trade": max_bars,
            "cooldown_bars": cooldown,
            "close_at_session_end": true,
        },
        "session": {
            "start_hour": 0, "end_hour": 23, "close_at_end": true,
            "days": [0, 1, 2, 3, 4],
        },
        "risk": {
            "sizing": "fixed_lot", "fixed_lot": 0.1, "risk_percent": 1.0,
            "leverage": 30.0, "balance": 10000.0, "spread_pips": 0.3,
            "commission_per_lot": 0.0, "slippage_pips": 0.0,
        },
        "meta": {"seed_source": "run", "generator": "1.0.0"},
    })
}

pub fn hash_spec(spec: &Value) -> String {
    spec_hash(&serde_json::to_string(spec).unwrap_or_default())
}

pub fn _param_unused(_p: &Param) {}
