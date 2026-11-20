//! Port of `segment_metrics`, `_max_dd` and `passes` from `sqx.engine`.

use serde_json::{json, Map, Value};

pub fn round_to(x: f64, dp: i32) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let m = 10f64.powi(dp);
    (x * m).round() / m
}

pub fn max_dd(eq: &[f64]) -> f64 {
    if eq.len() < 2 {
        return 0.0;
    }
    let mut peak = eq[0];
    let mut out: f64 = 0.0;
    for &v in eq {
        if v > peak {
            peak = v;
        }
        let dd = if peak > 0.0 { (peak - v) / peak } else { 0.0 };
        if dd > out {
            out = dd;
        }
    }
    out
}

fn empty_metrics() -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("trades".into(), json!(0));
    m.insert("net_return".into(), json!(0.0));
    m.insert("max_dd".into(), json!(0.0));
    m.insert("return_dd".into(), json!(0.0));
    m.insert("sharpe".into(), json!(0.0));
    m.insert("stagnation_days".into(), json!(0));
    m.insert("profit_factor".into(), json!(0.0));
    m.insert("win_rate".into(), json!(0.0));
    m.insert("avg_trade".into(), json!(0.0));
    m
}

pub fn segment_metrics(
    eq_segment: &[f64],
    base: f64,
    day_ts: &[i64],
    trade_pnls: &[f64],
    trades: usize,
) -> Map<String, Value> {
    if eq_segment.is_empty() {
        return empty_metrics();
    }
    let mut eq: Vec<f64> = Vec::with_capacity(eq_segment.len() + 1);
    eq.push(base);
    eq.extend_from_slice(eq_segment);

    let mut rets: Vec<f64> = Vec::with_capacity(eq.len());
    for k in 1..eq.len() {
        let den = if eq[k - 1] == 0.0 { f64::NAN } else { eq[k - 1] };
        let r = (eq[k] - eq[k - 1]) / den;
        if r.is_finite() {
            rets.push(r);
        }
    }
    let mut sharpe = 0.0;
    if rets.len() > 2 {
        let n = rets.len() as f64;
        let mean = rets.iter().sum::<f64>() / n;
        let var = rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
        let sd = var.sqrt();
        if sd > 0.0 {
            sharpe = mean / sd * 252f64.sqrt();
        }
    }

    let mdd = max_dd(&eq);
    let net = if base > 0.0 { eq[eq.len() - 1] / base - 1.0 } else { 0.0 };
    let retdd = if mdd > 0.0 {
        net * 100.0 / (mdd * 100.0)
    } else if net > 0.0 {
        999.0
    } else {
        0.0
    };

    // stagnation: longest calendar-day gap without a new equity high
    let mut peak = eq[0];
    let mut last_high_ts: Option<i64> = None;
    let mut stag: i64 = 0;
    for k in 1..eq.len() {
        let day = day_ts[k - 1];
        if eq[k] > peak {
            peak = eq[k];
            if let Some(last) = last_high_ts {
                let gap = (day - last) / 86400;
                if gap > stag {
                    stag = gap;
                }
            }
            last_high_ts = Some(day);
        }
    }
    if eq.len() > 1 {
        match last_high_ts {
            None => stag = (day_ts[day_ts.len() - 1] - day_ts[0]) / 86400,
            Some(last) => {
                let tail = (day_ts[day_ts.len() - 1] - last) / 86400;
                if tail > stag {
                    stag = tail;
                }
            }
        }
    }

    let wins: Vec<f64> = trade_pnls.iter().copied().filter(|p| *p > 0.0).collect();
    let losses: Vec<f64> = trade_pnls.iter().copied().filter(|p| *p < 0.0).collect();
    let loss_sum: f64 = losses.iter().sum();
    let win_sum: f64 = wins.iter().sum();
    let pf = if !losses.is_empty() && loss_sum != 0.0 {
        win_sum / loss_sum.abs()
    } else if !wins.is_empty() {
        999.0
    } else {
        0.0
    };

    let mut m = Map::new();
    m.insert("trades".into(), json!(trades as i64));
    m.insert("net_return".into(), json!(round_to(net * 100.0, 4)));
    m.insert("max_dd".into(), json!(round_to(mdd * 100.0, 4)));
    m.insert("return_dd".into(), json!(round_to(retdd, 4)));
    m.insert("sharpe".into(), json!(round_to(sharpe, 4)));
    m.insert("stagnation_days".into(), json!(stag));
    m.insert("profit_factor".into(), json!(round_to(pf, 4)));
    m.insert(
        "win_rate".into(),
        json!(if trade_pnls.is_empty() {
            0.0
        } else {
            round_to(wins.len() as f64 / trade_pnls.len() as f64 * 100.0, 2)
        }),
    );
    m.insert(
        "avg_trade".into(),
        json!(if trade_pnls.is_empty() {
            0.0
        } else {
            round_to(trade_pnls.iter().sum::<f64>() / trade_pnls.len() as f64, 4)
        }),
    );
    m
}

/// Same gate order and reason strings as `sqx.engine.passes`.
///
/// The engine reports metrics; a caller decides acceptance. The bench harness
/// applies these gates to the emitted metrics on both sides, so the binary
/// itself does not call this yet.
#[allow(dead_code)]
pub fn passes(metrics: &Map<String, Value>, gates: &Map<String, Value>) -> (bool, String) {
    let m = |k: &str| metrics.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
    let g = |k: &str| gates.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
    if m("trades") < g("min_trades") {
        return (false, "min_trades".into());
    }
    if m("sharpe") < g("min_sharpe") {
        return (false, "min_sharpe".into());
    }
    if m("stagnation_days") > g("max_stagnation_days") {
        return (false, "stagnation".into());
    }
    if m("return_dd") < g("min_return_dd") {
        return (false, "return_dd".into());
    }
    if m("max_dd") > g("max_drawdown_pct") {
        return (false, "max_dd".into());
    }
    if m("profit_factor") < g("min_profit_factor") {
        return (false, "profit_factor".into());
    }
    if m("win_rate") < g("min_win_rate") {
        return (false, "win_rate".into());
    }
    (true, String::new())
}
