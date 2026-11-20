use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2, PyArrayMethods, PyUntypedArrayMethods};
use pyo3::prelude::*;

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn simulate(
    op: PyReadonlyArray1<f64>,
    hi: PyReadonlyArray1<f64>,
    lo: PyReadonlyArray1<f64>,
    cl: PyReadonlyArray1<f64>,
    day_id: PyReadonlyArray1<i64>,
    n_days: i64,
    entry_long: PyReadonlyArray1<u8>,
    entry_short: PyReadonlyArray1<u8>,
    exit_long: PyReadonlyArray1<u8>,
    exit_short: PyReadonlyArray1<u8>,
    entry_ok: PyReadonlyArray1<u8>,
    force_close: PyReadonlyArray1<u8>,
    split_idx: i64,
    sl_atr: PyReadonlyArray1<f64>,
    tp_atr: PyReadonlyArray1<f64>,
    trail_atr: PyReadonlyArray1<f64>,
    sl_mode: i64,
    sl_fixed: f64,
    tp_mode: i64,
    tp_fixed: f64,
    trail_mode: i64,
    lots: f64,
    use_risk_pct: i64,
    risk_pct: f64,
    leverage: f64,
    contract: f64,
    pip: f64,
    half_spread: f64,
    slippage: f64,
    commission: f64,
    opposite_reverse: i64,
    max_bars: i64,
    cooldown_bars: i64,
    balance: f64,
    daily_eq: &Bound<'_, PyArray1<f64>>,
    t_entry_i: &Bound<'_, PyArray1<i64>>,
    t_exit_i: &Bound<'_, PyArray1<i64>>,
    t_side: &Bound<'_, PyArray1<i64>>,
    t_entry_px: &Bound<'_, PyArray1<f64>>,
    t_exit_px: &Bound<'_, PyArray1<f64>>,
    t_pnl: &Bound<'_, PyArray1<f64>>,
    t_reason: &Bound<'_, PyArray1<i64>>,
    skip_entry: PyReadonlyArray1<u8>,
    skip_exit: PyReadonlyArray1<u8>,
    random_close: PyReadonlyArray1<u8>,
    order_types: PyReadonlyArray1<i64>,
    order_dists: PyReadonlyArray2<'_, f64>,
    order_expiry: i64,
) -> PyResult<(f64, f64, i64)> {
    // immutable slices
    let op = op.as_slice()?;
    let hi = hi.as_slice()?;
    let lo = lo.as_slice()?;
    let cl = cl.as_slice()?;
    let day_id = day_id.as_slice()?;
    let entry_long = entry_long.as_slice()?;
    let entry_short = entry_short.as_slice()?;
    let exit_long = exit_long.as_slice()?;
    let exit_short = exit_short.as_slice()?;
    let entry_ok = entry_ok.as_slice()?;
    let force_close = force_close.as_slice()?;
    let sl_atr = sl_atr.as_slice()?;
    let tp_atr = tp_atr.as_slice()?;
    let trail_atr = trail_atr.as_slice()?;
    let skip_entry = skip_entry.as_slice()?;
    let skip_exit = skip_exit.as_slice()?;
    let random_close = random_close.as_slice()?;
    let order_types = order_types.as_slice()?;
    let order_dists = order_dists.as_slice()?;
    let n_orders = order_types.len();

    // mutable slices
    let daily_eq = unsafe { daily_eq.as_slice_mut()? };
    let t_entry_i = unsafe { t_entry_i.as_slice_mut()? };
    let t_exit_i = unsafe { t_exit_i.as_slice_mut()? };
    let t_side = unsafe { t_side.as_slice_mut()? };
    let t_entry_px = unsafe { t_entry_px.as_slice_mut()? };
    let t_exit_px = unsafe { t_exit_px.as_slice_mut()? };
    let t_pnl = unsafe { t_pnl.as_slice_mut()? };
    let t_reason = unsafe { t_reason.as_slice_mut()? };

    let n = cl.len();
    let max_trades = t_pnl.len();

    let mut eq = balance;
    let mut peak = balance;
    let mut maxdd = 0.0f64;
    let mut pos: i64 = 0;
    let mut entry_px = 0.0f64;
    let mut entry_i: i64 = 0;
    let mut lots_cur = 0.0f64;
    let mut sl = 0.0f64;
    let mut tp = 0.0f64;
    let mut trail = 0.0f64;
    let mut trade_n: i64 = 0;
    let mut cooldown_until: i64 = -1;

    let mut pending_side = vec![0i64; n_orders];
    let mut pending_px = vec![0.0f64; n_orders];
    let mut pending_until = vec![0i64; n_orders];
    let mut has_pending = false;

    for d in 0..n_days as usize {
        daily_eq[d] = balance;
    }

    for i in 0..n {
        let ii = i as i64;

        // forced exits at bar open
        if pos != 0 {
            let mut reason: i64 = 0;
            let mut raw = 0.0;
            if force_close[i] == 1 || ii == split_idx {
                reason = if force_close[i] == 1 { 6 } else { 8 };
                raw = op[i];
            } else if max_bars > 0 && (ii - entry_i) >= max_bars {
                reason = 7;
                raw = op[i];
            } else if random_close[i] == 1 && ii > entry_i {
                reason = 10;
                raw = op[i];
            }
            if reason != 0 {
                let exit_px = if pos == 1 {
                    raw - half_spread - slippage
                } else {
                    raw + half_spread + slippage
                };
                let pnl = if pos == 1 {
                    (exit_px - entry_px) * contract * lots_cur / exit_px
                } else {
                    (entry_px - exit_px) * contract * lots_cur / exit_px
                };
                eq += pnl - commission * lots_cur;
                if trade_n < max_trades as i64 {
                    t_entry_i[trade_n as usize] = entry_i;
                    t_exit_i[trade_n as usize] = ii;
                    t_side[trade_n as usize] = pos;
                    t_entry_px[trade_n as usize] = entry_px;
                    t_exit_px[trade_n as usize] = exit_px;
                    t_pnl[trade_n as usize] = pnl - 2.0 * commission * lots_cur;
                    t_reason[trade_n as usize] = reason;
                    trade_n += 1;
                    cooldown_until = ii + cooldown_bars;
                }
                pos = 0;
            }
        }

        // indicator exit at open
        if pos != 0 {
            if skip_exit[i] == 0
                && ((pos == 1 && exit_long[i] == 1) || (pos == -1 && exit_short[i] == 1))
            {
                let raw = op[i];
                let exit_px = if pos == 1 {
                    raw - half_spread - slippage
                } else {
                    raw + half_spread + slippage
                };
                let pnl = if pos == 1 {
                    (exit_px - entry_px) * contract * lots_cur / exit_px
                } else {
                    (entry_px - exit_px) * contract * lots_cur / exit_px
                };
                eq += pnl - commission * lots_cur;
                if trade_n < max_trades as i64 {
                    t_entry_i[trade_n as usize] = entry_i;
                    t_exit_i[trade_n as usize] = ii;
                    t_side[trade_n as usize] = pos;
                    t_entry_px[trade_n as usize] = entry_px;
                    t_exit_px[trade_n as usize] = exit_px;
                    t_pnl[trade_n as usize] = pnl - 2.0 * commission * lots_cur;
                    t_reason[trade_n as usize] = 4;
                    trade_n += 1;
                    cooldown_until = ii + cooldown_bars;
                }
                pos = 0;
            }
        }

        // opposite signal reversal
        if pos != 0 && opposite_reverse == 1 && entry_ok[i] == 1 && skip_exit[i] == 0 {
            let do_rev = (pos == 1 && entry_short[i] == 1) || (pos == -1 && entry_long[i] == 1);
            if do_rev {
                let raw = op[i];
                let exit_px = if pos == 1 {
                    raw - half_spread - slippage
                } else {
                    raw + half_spread + slippage
                };
                let pnl = if pos == 1 {
                    (exit_px - entry_px) * contract * lots_cur / exit_px
                } else {
                    (entry_px - exit_px) * contract * lots_cur / exit_px
                };
                eq += pnl - commission * lots_cur;
                if trade_n < max_trades as i64 {
                    t_entry_i[trade_n as usize] = entry_i;
                    t_exit_i[trade_n as usize] = ii;
                    t_side[trade_n as usize] = pos;
                    t_entry_px[trade_n as usize] = entry_px;
                    t_exit_px[trade_n as usize] = exit_px;
                    t_pnl[trade_n as usize] = pnl - 2.0 * commission * lots_cur;
                    t_reason[trade_n as usize] = 5;
                    trade_n += 1;
                    cooldown_until = ii - 1;
                }
                pos = 0;
            }
        }

        // pending orders
        let mut pending_fill_side: i64 = 0;
        let mut pending_fill_px = 0.0;
        if pos == 0 && has_pending && entry_ok[i] == 1 && skip_entry[i] == 0 {
            for k in 0..n_orders {
                if pending_side[k] == 0 {
                    continue;
                }
                if pending_until[k] >= 0 && ii > pending_until[k] {
                    pending_side[k] = 0;
                    continue;
                }
                let typ = order_types[k];
                let px = pending_px[k];
                let mut hit = false;
                let mut fill = 0.0;
                if typ == 1 && pending_side[k] == 1 && lo[i] <= px {
                    fill = op[i].min(px);
                    hit = true;
                } else if typ == 1 && pending_side[k] == -1 && hi[i] >= px {
                    fill = op[i].max(px);
                    hit = true;
                } else if typ == 2 && pending_side[k] == 1 && hi[i] >= px {
                    fill = op[i].max(px);
                    hit = true;
                } else if typ == 2 && pending_side[k] == -1 && lo[i] <= px {
                    fill = op[i].min(px);
                    hit = true;
                }
                if hit {
                    pending_fill_side = pending_side[k];
                    pending_fill_px = fill;
                    break;
                }
            }
            let mut still_pending = 0;
            for k in 0..n_orders {
                if pending_side[k] != 0 {
                    still_pending += 1;
                }
            }
            if pending_fill_side != 0 {
                for k in 0..n_orders {
                    pending_side[k] = 0;
                }
                has_pending = false;
            } else if still_pending == 0 {
                has_pending = false;
            }
        }

        // pending fills and fresh entries
        if pos == 0 && ii > cooldown_until && skip_entry[i] == 0 {
            let mut side: i64 = 0;
            let mut raw = 0.0;
            if pending_fill_side != 0 {
                side = pending_fill_side;
                raw = pending_fill_px;
            } else if entry_ok[i] == 1 {
                if entry_long[i] == 1 && entry_short[i] == 1 {
                    side = 1;
                } else if entry_long[i] == 1 {
                    side = 1;
                } else if entry_short[i] == 1 {
                    side = -1;
                }
                if side != 0 {
                    for k in 0..n_orders {
                        pending_side[k] = 0;
                    }
                    has_pending = false;
                    let mut has_market = false;
                    for k in 0..n_orders {
                        if order_types[k] == 0 {
                            has_market = true;
                        }
                    }
                    if has_market {
                        raw = op[i];
                    } else {
                        let mut placed = 0;
                        for k in 0..n_orders {
                            let d = order_dists[k * n + i];
                            if d <= 0.0 {
                                continue;
                            }
                            if order_types[k] == 1 {
                                pending_px[k] = if side == 1 { op[i] - d } else { op[i] + d };
                            } else {
                                pending_px[k] = if side == 1 { op[i] + d } else { op[i] - d };
                            }
                            pending_side[k] = side;
                            pending_until[k] = if order_expiry > 0 { ii + order_expiry } else { -1 };
                            placed += 1;
                        }
                        if placed > 0 {
                            has_pending = true;
                            side = 0;
                        } else {
                            raw = op[i];
                        }
                    }
                }
            }
            if side != 0 {
                if side == 1 {
                    entry_px = raw + half_spread + slippage;
                    let dist_sl = if sl_mode == 1 { sl_fixed } else { sl_atr[i] };
                    let dist_tp = if tp_mode == 1 { tp_fixed } else { tp_atr[i] };
                    let dist_tr = trail_atr[i];
                    if sl_mode != 0 && dist_sl <= 0.0 { side = 0; }
                    if tp_mode != 0 && dist_tp <= 0.0 { side = 0; }
                    if trail_mode != 0 && dist_tr <= 0.0 { side = 0; }
                    if side != 0 {
                        sl = if sl_mode != 0 { entry_px - dist_sl } else { 0.0 };
                        tp = if tp_mode != 0 { entry_px + dist_tp } else { 0.0 };
                        trail = if trail_mode != 0 { entry_px - dist_tr } else { 0.0 };
                    }
                } else {
                    entry_px = raw - half_spread - slippage;
                    let dist_sl = if sl_mode == 1 { sl_fixed } else { sl_atr[i] };
                    let dist_tp = if tp_mode == 1 { tp_fixed } else { tp_atr[i] };
                    let dist_tr = trail_atr[i];
                    if sl_mode != 0 && dist_sl <= 0.0 { side = 0; }
                    if tp_mode != 0 && dist_tp <= 0.0 { side = 0; }
                    if trail_mode != 0 && dist_tr <= 0.0 { side = 0; }
                    if side != 0 {
                        sl = if sl_mode != 0 { entry_px + dist_sl } else { 0.0 };
                        tp = if tp_mode != 0 { entry_px - dist_tp } else { 0.0 };
                        trail = if trail_mode != 0 { entry_px + dist_tr } else { 0.0 };
                    }
                }
                if side != 0 {
                    if use_risk_pct == 1 && sl_mode != 0 {
                        let risk_money = eq * risk_pct / 100.0;
                        let pv = if entry_px > 0.0 { pip * contract / entry_px } else { 0.0 };
                        let sl_pips = if pip > 0.0 { (entry_px - sl).abs() / pip } else { 0.0 };
                        if sl_pips > 0.0 && pv > 0.0 {
                            lots_cur = risk_money / (sl_pips * pv);
                        } else {
                            lots_cur = lots;
                        }
                    } else {
                        lots_cur = lots;
                    }
                    let max_lots = if contract > 0.0 { eq * leverage / contract } else { 0.0 };
                    if lots_cur > max_lots {
                        lots_cur = max_lots;
                    }
                    if lots_cur > 0.0 {
                        pos = side;
                        entry_i = ii;
                        eq -= commission * lots_cur;
                    } else {
                        pos = 0;
                    }
                }
            }
        }

        // intrabar exits
        if pos != 0 {
            let mut reason: i64 = 0;
            let mut exit_px = 0.0;
            if pos == 1 {
                let om = op[i] - half_spread - slippage;
                let lm = lo[i] - half_spread - slippage;
                let hm = hi[i] - half_spread - slippage;
                if sl_mode != 0 && om <= sl {
                    exit_px = om; reason = 1;
                } else if tp_mode != 0 && om >= tp {
                    exit_px = om; reason = 2;
                } else if sl_mode != 0 && lm <= sl {
                    exit_px = sl; reason = 1;
                } else if tp_mode != 0 && hm >= tp {
                    exit_px = tp; reason = 2;
                } else if trail_mode != 0 && lm <= trail {
                    exit_px = trail; reason = 3;
                }
            } else {
                let om = op[i] + half_spread + slippage;
                let hm = hi[i] + half_spread + slippage;
                let lm = lo[i] + half_spread + slippage;
                if sl_mode != 0 && om >= sl {
                    exit_px = om; reason = 1;
                } else if tp_mode != 0 && om <= tp {
                    exit_px = om; reason = 2;
                } else if sl_mode != 0 && hm >= sl {
                    exit_px = sl; reason = 1;
                } else if tp_mode != 0 && lm <= tp {
                    exit_px = tp; reason = 2;
                } else if trail_mode != 0 && hm >= trail {
                    exit_px = trail; reason = 3;
                }
            }
            if reason != 0 {
                let pnl = if pos == 1 {
                    (exit_px - entry_px) * contract * lots_cur / exit_px
                } else {
                    (entry_px - exit_px) * contract * lots_cur / exit_px
                };
                eq += pnl - commission * lots_cur;
                if trade_n < max_trades as i64 {
                    t_entry_i[trade_n as usize] = entry_i;
                    t_exit_i[trade_n as usize] = ii;
                    t_side[trade_n as usize] = pos;
                    t_entry_px[trade_n as usize] = entry_px;
                    t_exit_px[trade_n as usize] = exit_px;
                    t_pnl[trade_n as usize] = pnl - 2.0 * commission * lots_cur;
                    t_reason[trade_n as usize] = reason;
                    trade_n += 1;
                    cooldown_until = ii + cooldown_bars;
                }
                pos = 0;
            }
            if pos != 0 && trail_mode != 0 {
                if pos == 1 {
                    let cand = cl[i] - half_spread - slippage - trail_atr[i];
                    if cand > trail {
                        trail = cand;
                    }
                    if sl_mode != 0 && trail < sl {
                        trail = sl;
                    }
                } else {
                    let cand = cl[i] + half_spread + slippage + trail_atr[i];
                    if trail == 0.0 || cand < trail {
                        trail = cand;
                    }
                    if sl_mode != 0 && trail > sl {
                        trail = sl;
                    }
                }
            }
        }

        // mark to market
        let eq_mark = if pos == 1 {
            eq + (cl[i] - entry_px) * contract * lots_cur / cl[i]
        } else if pos == -1 {
            eq + (entry_px - cl[i]) * contract * lots_cur / cl[i]
        } else {
            eq
        };
        daily_eq[day_id[i] as usize] = eq_mark;
        if eq_mark > peak {
            peak = eq_mark;
        }
        let dd = if peak > 0.0 { (peak - eq_mark) / peak } else { 0.0 };
        if dd > maxdd {
            maxdd = dd;
        }
    }

    if pos != 0 {
        let i = n - 1;
        let ii = i as i64;
        let exit_px = if pos == 1 {
            cl[i] - half_spread - slippage
        } else {
            cl[i] + half_spread + slippage
        };
        let pnl = if pos == 1 {
            (exit_px - entry_px) * contract * lots_cur / exit_px
        } else {
            (entry_px - exit_px) * contract * lots_cur / exit_px
        };
        eq += pnl - commission * lots_cur;
        if trade_n < max_trades as i64 {
            t_entry_i[trade_n as usize] = entry_i;
            t_exit_i[trade_n as usize] = ii;
            t_side[trade_n as usize] = pos;
            t_entry_px[trade_n as usize] = entry_px;
            t_exit_px[trade_n as usize] = exit_px;
            t_pnl[trade_n as usize] = pnl - 2.0 * commission * lots_cur;
            t_reason[trade_n as usize] = 9;
            trade_n += 1;
        }
        daily_eq[day_id[i] as usize] = eq;
    }

    Ok((eq, maxdd, trade_n))
}

fn pearson(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len();
    if n == 0 { return f64::NAN; }
    let mean_x = x.iter().sum::<f64>() / n as f64;
    let mean_y = y.iter().sum::<f64>() / n as f64;
    let mut cov = 0.0;
    let mut vx = 0.0;
    let mut vy = 0.0;
    for i in 0..n {
        let dx = x[i] - mean_x;
        let dy = y[i] - mean_y;
        cov += dx * dy;
        vx += dx * dx;
        vy += dy * dy;
    }
    if vx <= 0.0 || vy <= 0.0 { return f64::NAN; }
    cov / (vx.sqrt() * vy.sqrt())
}

fn rank_average(a: &[f64]) -> Vec<f64> {
    let n = a.len();
    let mut idx: Vec<usize> = (0..n).collect();
    idx.sort_by(|&i, &j| a[i].partial_cmp(&a[j]).unwrap_or(std::cmp::Ordering::Equal));
    let mut ranks = vec![0.0; n];
    let mut i = 0;
    while i < n {
        let mut j = i;
        while j + 1 < n && a[idx[j + 1]] == a[idx[i]] {
            j += 1;
        }
        let avg_rank = (i + j) as f64 / 2.0 + 1.0;
        for k in i..=j {
            ranks[idx[k]] = avg_rank;
        }
        i = j + 1;
    }
    ranks
}

#[pyfunction]
fn best_correlation(
    candidate: PyReadonlyArray1<f64>,
    accepted: PyReadonlyArray2<'_, f64>,
    method: &str,
) -> PyResult<(f64, i64)> {
    let cand = candidate.as_slice()?;
    let acc = accepted.as_slice()?;
    let shape = accepted.shape();
    let n_rows = shape[0];
    let n_cols = shape[1];
    if n_rows == 0 || n_cols == 0 || cand.len() != n_cols {
        return Ok((-2.0, -1));
    }

    let cm = cand.iter().sum::<f64>() / n_cols as f64;
    let cvar: f64 = cand.iter().map(|v| (v - cm).powi(2)).sum();
    if cvar <= 0.0 {
        return Ok((-2.0, -1));
    }

    let cand_ranks = if method == "spearman" {
        Some(rank_average(cand))
    } else {
        None
    };

    let mut best = -2.0f64;
    let mut best_idx: i64 = -1;
    for r in 0..n_rows {
        let row = &acc[r * n_cols..(r + 1) * n_cols];
        let mean = row.iter().sum::<f64>() / n_cols as f64;
        let var: f64 = row.iter().map(|v| (v - mean).powi(2)).sum();
        if var <= 0.0 { continue; }
        let c = if method == "spearman" {
            let yr = rank_average(row);
            let xr = cand_ranks.as_ref().unwrap();
            pearson(xr, &yr)
        } else {
            pearson(cand, row)
        };
        if c.is_finite() && c > best {
            best = c;
            best_idx = r as i64;
        }
    }
    Ok((best, best_idx))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn build_context(
    tf_ts: PyReadonlyArray1<i64>,
    tf_close_ts: PyReadonlyArray1<i64>,
    tf_seconds: i64,
    base_ts: PyReadonlyArray1<i64>,
    start_hour: i64,
    end_hour: i64,
    days: Vec<i64>,
    close_at_end: bool,
    oos_pct: i64,
) -> PyResult<(
    Vec<i64>, Vec<i64>, Vec<i64>, Vec<i64>, Vec<u8>, Vec<u8>, i64, i64, Vec<i64>, i64, i64,
)> {
    let tf_ts = tf_ts.as_slice()?;
    let tf_close_ts = tf_close_ts.as_slice()?;
    let base_ts = base_ts.as_slice()?;
    let n = base_ts.len();
    if n == 0 {
        return Ok((vec![], vec![], vec![], vec![], vec![], vec![], 0, 0, vec![], 0, 0));
    }

    // tf_to_base
    let mut tf_to_base = vec![0i64; tf_ts.len()];
    for i in 0..tf_ts.len() {
        // searchsorted base_ts for tf_close_ts[i], side left
        let v = tf_close_ts[i];
        let mut lo = 0i64;
        let mut hi = n as i64;
        while lo < hi {
            let mid = (lo + hi) / 2;
            if base_ts[mid as usize] < v {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        tf_to_base[i] = lo;
    }

    // base_prev_tf
    let mut base_prev_tf = vec![0i64; n];
    for i in 0..n {
        let v = base_ts[i];
        // searchsorted tf_close_ts for v, side right
        let mut lo = 0i64;
        let mut hi = tf_close_ts.len() as i64;
        while lo < hi {
            let mid = (lo + hi) / 2;
            if tf_close_ts[mid as usize] <= v {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let mut idx = lo - 1;
        if idx < 0 { idx = 0; }
        base_prev_tf[i] = idx;
    }

    // day id
    let mut day_ints = vec![0i64; n];
    for i in 0..n {
        day_ints[i] = base_ts[i] / 86400;
    }
    // unique sorted
    let mut uniq: Vec<i64> = day_ints.clone();
    uniq.sort();
    uniq.dedup();
    let mut day_id = vec![0i64; n];
    for i in 0..n {
        // binary search in uniq
        let v = day_ints[i];
        let mut lo = 0usize;
        let mut hi = uniq.len();
        while lo < hi {
            let mid = (lo + hi) / 2;
            if uniq[mid] < v {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        day_id[i] = lo as i64;
    }
    let day_ts: Vec<i64> = uniq.iter().map(|d| d * 86400).collect();

    // session
    let day_set: std::collections::HashSet<i64> = days.into_iter().collect();
    let mut entry_ok = vec![0u8; n];
    let mut force_close = vec![0u8; n];
    for i in 0..n {
        let ts = base_ts[i];
        let hours = ((ts % 86400) / 3600) as i64;
        let minutes = ((ts % 3600) / 60) as i64;
        let weekdays = ((ts / 86400) + 3) % 7;
        let day_ok = day_set.contains(&weekdays);
        let hour_ok = if start_hour == end_hour {
            true
        } else if start_hour < end_hour {
            hours >= start_hour && hours < end_hour
        } else {
            hours >= start_hour || hours < end_hour
        };
        if day_ok && hour_ok {
            entry_ok[i] = 1;
        }
        if close_at_end && start_hour != end_hour {
            if hours == end_hour && minutes == 0 && day_ok {
                force_close[i] = 1;
            }
        }
    }
    if n > 0 {
        entry_ok[n - 1] = 0;
    }

    // split
    let start_ts = base_ts[0];
    let end_ts = base_ts[n - 1];
    let split_ts = start_ts + ((end_ts - start_ts) * (100 - oos_pct) / 100);
    // searchsorted base_ts for split_ts, side left
    let mut lo = 0i64;
    let mut hi = n as i64;
    while lo < hi {
        let mid = (lo + hi) / 2;
        if base_ts[mid as usize] < split_ts {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    let mut split_idx = lo;
    if split_idx < 1 { split_idx = 1; }
    if split_idx > n as i64 - 2 { split_idx = n as i64 - 2; }
    let split_day = day_id[split_idx as usize];
    force_close[split_idx as usize] = 1;
    // embargo
    let embargo_ts = base_ts[split_idx as usize] - tf_seconds;
    let mut embargo_start = 0i64;
    let mut lo = 0i64;
    let mut hi = n as i64;
    while lo < hi {
        let mid = (lo + hi) / 2;
        if base_ts[mid as usize] < embargo_ts {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    embargo_start = lo;
    for i in embargo_start as usize..split_idx as usize {
        entry_ok[i] = 0;
    }
    entry_ok[split_idx as usize] = 0;

    // base_tf_idx
    let mut base_tf_idx = vec![0i64; n];
    for i in 0..n {
        let v = base_ts[i];
        // searchsorted tf_ts for v, side right
        let mut lo = 0i64;
        let mut hi = tf_ts.len() as i64;
        while lo < hi {
            let mid = (lo + hi) / 2;
            if tf_ts[mid as usize] <= v {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let mut idx = lo - 1;
        if idx < 0 { idx = 0; }
        if idx > tf_ts.len() as i64 - 1 { idx = tf_ts.len() as i64 - 1; }
        base_tf_idx[i] = idx;
    }

    Ok((
        tf_to_base, base_prev_tf, day_id, day_ts, entry_ok, force_close,
        split_idx, split_day, base_tf_idx, start_ts, end_ts,
    ))
}

#[pymodule]
fn sqx_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(simulate, m)?)?;
    m.add_function(wrap_pyfunction!(best_correlation, m)?)?;
    m.add_function(wrap_pyfunction!(build_context, m)?)?;
    Ok(())
}
