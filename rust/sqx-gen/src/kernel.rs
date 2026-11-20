//! Port of `sqx.engine._simulate` (the numba `nogil` kernel), branch for branch.
//!
//! Reason codes, exit order and commission handling are kept literally:
//! forced exit 6/8, max bars 7, random close 10, indicator exit 4, opposite
//! reversal 5, stop 1, take profit 2, trailing 3, end of data 9.
//!
//! Distance series are *not* materialised per M1 bar: an ATR distance is a
//! gather through `m1_prev_tf` into the 147k-bar timeframe array, so building a
//! 4.4M-element array per policy only costs bandwidth. `Dist` evaluates it at
//! the bar the kernel is already touching.

pub struct Trade {
    pub entry_i: i64,
    pub exit_i: i64,
    pub side: i64,
    pub entry_px: f64,
    pub exit_px: f64,
    pub pnl: f64,
    pub reason: i64,
}

/// A stop/target/trailing distance, evaluated per M1 bar.
pub enum Dist<'a> {
    /// Policy disabled.
    Zero,
    /// Fixed pips converted to price.
    Fixed(f64),
    /// ATR (% of close, per timeframe bar) times a multiplier, mapped to M1.
    Mapped {
        tf: &'a [f64],
        prev: &'a [i64],
        mult: f64,
    },
}

impl Dist<'_> {
    #[inline(always)]
    pub fn is_zero(&self) -> bool {
        matches!(self, Dist::Zero)
    }

    #[inline(always)]
    pub fn at(&self, i: usize) -> f64 {
        match self {
            Dist::Zero => 0.0,
            Dist::Fixed(v) => *v,
            Dist::Mapped { tf, prev, mult } => {
                let v = tf[prev[i] as usize] * *mult;
                if v.is_finite() {
                    v
                } else {
                    0.0
                }
            }
        }
    }
}

/// A pending-order distance, evaluated per M1 bar.
pub enum OrderDist<'a> {
    Const(f64),
    Mapped {
        tf: &'a [f64],
        prev: &'a [i64],
        mult: f64,
    },
}

impl OrderDist<'_> {
    #[inline(always)]
    pub fn at(&self, i: usize) -> f64 {
        match self {
            OrderDist::Const(v) => *v,
            OrderDist::Mapped { tf, prev, mult } => {
                let v = tf[prev[i] as usize] * *mult;
                if v.is_finite() {
                    v
                } else {
                    0.0
                }
            }
        }
    }
}

#[inline(always)]
fn flag(mask: &[u8], i: usize) -> bool {
    !mask.is_empty() && mask[i] != 0
}

pub struct Sim<'a> {
    pub op: &'a [f64],
    pub hi: &'a [f64],
    pub lo: &'a [f64],
    pub cl: &'a [f64],
    pub day_id: &'a [i64],
    pub n_days: usize,
    pub entry_long: &'a [u8],
    pub entry_short: &'a [u8],
    pub exit_long: &'a [u8],
    pub exit_short: &'a [u8],
    pub entry_ok: &'a [u8],
    pub force_close: &'a [u8],
    pub split_idx: usize,
    pub sl: Dist<'a>,
    pub tp: Dist<'a>,
    pub trail: Dist<'a>,
    pub lots: f64,
    pub use_risk_pct: i64,
    pub risk_pct: f64,
    pub leverage: f64,
    pub contract: f64,
    pub pip: f64,
    pub half_spread: f64,
    pub slippage: f64,
    pub commission: f64,
    pub opposite_reverse: i64,
    pub max_bars: i64,
    pub cooldown_bars: i64,
    pub balance: f64,
    pub skip_entry: &'a [u8],
    pub skip_exit: &'a [u8],
    pub random_close: &'a [u8],
    pub order_types: &'a [i64],
    pub order_dists: &'a [OrderDist<'a>],
    pub order_expiry: i64,
}

pub struct SimOut {
    pub final_eq: f64,
    pub maxdd: f64,
    pub trades: Vec<Trade>,
    pub daily_eq: Vec<f64>,
}

pub fn simulate(s: &Sim) -> SimOut {
    let n = s.cl.len();
    let n_days = s.n_days;
    let mut eq = s.balance;
    let mut peak = s.balance;
    let mut maxdd = 0.0f64;
    let mut pos: i64 = 0;
    let mut entry_px = 0.0f64;
    let mut entry_i: usize = 0;
    let mut lots_cur = 0.0f64;
    let mut sl = 0.0f64;
    let mut tp = 0.0f64;
    let mut trail = 0.0f64;
    let mut cooldown_until: i64 = -1;
    let mut daily_eq = vec![s.balance; n_days];
    let mut trades: Vec<Trade> = Vec::with_capacity(1024);
    let n_orders = s.order_types.len();
    let mut pending_side = vec![0i64; n_orders];
    let mut pending_px = vec![0.0f64; n_orders];
    let mut pending_until = vec![0i64; n_orders];
    let mut has_pending = false;

    for i in 0..n {
        // forced exits at the bar open
        if pos != 0 {
            let mut reason = 0i64;
            let mut raw = 0.0f64;
            if s.force_close[i] != 0 || i == s.split_idx {
                reason = if s.force_close[i] != 0 { 6 } else { 8 };
                raw = s.op[i];
            } else if s.max_bars > 0 && (i - entry_i) as i64 >= s.max_bars {
                reason = 7;
                raw = s.op[i];
            } else if flag(s.random_close, i) && i > entry_i {
                reason = 10;
                raw = s.op[i];
            }
            if reason != 0 {
                let exit_px;
                let pnl;
                if pos == 1 {
                    exit_px = raw - s.half_spread - s.slippage;
                    pnl = (exit_px - entry_px) * s.contract * lots_cur / exit_px;
                } else {
                    exit_px = raw + s.half_spread + s.slippage;
                    pnl = (entry_px - exit_px) * s.contract * lots_cur / exit_px;
                }
                eq += pnl - s.commission * lots_cur;
                trades.push(Trade {
                    entry_i: entry_i as i64,
                    exit_i: i as i64,
                    side: pos,
                    entry_px,
                    exit_px,
                    pnl: pnl - 2.0 * s.commission * lots_cur,
                    reason,
                });
                cooldown_until = i as i64 + s.cooldown_bars;
                pos = 0;
            }
        }

        // indicator exit at the open
        if pos != 0 && !flag(s.skip_exit, i) {
            let hit = (pos == 1 && s.exit_long[i] != 0) || (pos == -1 && s.exit_short[i] != 0);
            if hit {
                let raw = s.op[i];
                let exit_px;
                let pnl;
                if pos == 1 {
                    exit_px = raw - s.half_spread - s.slippage;
                    pnl = (exit_px - entry_px) * s.contract * lots_cur / exit_px;
                } else {
                    exit_px = raw + s.half_spread + s.slippage;
                    pnl = (entry_px - exit_px) * s.contract * lots_cur / exit_px;
                }
                eq += pnl - s.commission * lots_cur;
                trades.push(Trade {
                    entry_i: entry_i as i64,
                    exit_i: i as i64,
                    side: pos,
                    entry_px,
                    exit_px,
                    pnl: pnl - 2.0 * s.commission * lots_cur,
                    reason: 4,
                });
                cooldown_until = i as i64 + s.cooldown_bars;
                pos = 0;
            }
        }

        // opposite signal reversal / close at open
        if pos != 0 && s.opposite_reverse == 1 && s.entry_ok[i] == 1 && !flag(s.skip_exit, i) {
            let do_rev =
                (pos == 1 && s.entry_short[i] == 1) || (pos == -1 && s.entry_long[i] == 1);
            if do_rev {
                let raw = s.op[i];
                let exit_px;
                let pnl;
                if pos == 1 {
                    exit_px = raw - s.half_spread - s.slippage;
                    pnl = (exit_px - entry_px) * s.contract * lots_cur / exit_px;
                } else {
                    exit_px = raw + s.half_spread + s.slippage;
                    pnl = (entry_px - exit_px) * s.contract * lots_cur / exit_px;
                }
                eq += pnl - s.commission * lots_cur;
                trades.push(Trade {
                    entry_i: entry_i as i64,
                    exit_i: i as i64,
                    side: pos,
                    entry_px,
                    exit_px,
                    pnl: pnl - 2.0 * s.commission * lots_cur,
                    reason: 5,
                });
                // allow the reverse entry on this same bar
                cooldown_until = i as i64 - 1;
                pos = 0;
            }
        }

        // pending limit/stop orders trigger when price reaches the level
        let mut pending_fill_side = 0i64;
        let mut pending_fill_px = 0.0f64;
        if pos == 0 && has_pending && s.entry_ok[i] == 1 && !flag(s.skip_entry, i) {
            for k in 0..n_orders {
                if pending_side[k] == 0 {
                    continue;
                }
                if pending_until[k] >= 0 && (i as i64) > pending_until[k] {
                    pending_side[k] = 0;
                    continue;
                }
                let typ = s.order_types[k];
                let px = pending_px[k];
                let mut hit = false;
                let mut fill = 0.0;
                if typ == 1 && pending_side[k] == 1 && s.lo[i] <= px {
                    fill = s.op[i].min(px);
                    hit = true;
                } else if typ == 1 && pending_side[k] == -1 && s.hi[i] >= px {
                    fill = s.op[i].max(px);
                    hit = true;
                } else if typ == 2 && pending_side[k] == 1 && s.hi[i] >= px {
                    fill = s.op[i].max(px);
                    hit = true;
                } else if typ == 2 && pending_side[k] == -1 && s.lo[i] <= px {
                    fill = s.op[i].min(px);
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
        if pos == 0 && (i as i64) > cooldown_until && !flag(s.skip_entry, i) {
            let mut side = 0i64;
            let mut raw = 0.0f64;
            if pending_fill_side != 0 {
                side = pending_fill_side;
                raw = pending_fill_px;
            } else if s.entry_ok[i] == 1 {
                if s.entry_long[i] == 1 && s.entry_short[i] == 1 {
                    side = 1;
                } else if s.entry_long[i] == 1 {
                    side = 1;
                } else if s.entry_short[i] == 1 {
                    side = -1;
                }
                if side != 0 {
                    // a fresh signal replaces any still-pending orders
                    for k in 0..n_orders {
                        pending_side[k] = 0;
                    }
                    has_pending = false;
                    let mut has_market = false;
                    for k in 0..n_orders {
                        if s.order_types[k] == 0 {
                            has_market = true;
                        }
                    }
                    if has_market {
                        raw = s.op[i];
                    } else {
                        let mut placed = 0;
                        for k in 0..n_orders {
                            let d = s.order_dists[k].at(i);
                            if d <= 0.0 {
                                continue;
                            }
                            if s.order_types[k] == 1 {
                                pending_px[k] = if side == 1 { s.op[i] - d } else { s.op[i] + d };
                            } else {
                                pending_px[k] = if side == 1 { s.op[i] + d } else { s.op[i] - d };
                            }
                            pending_side[k] = side;
                            pending_until[k] = if s.order_expiry > 0 {
                                i as i64 + s.order_expiry
                            } else {
                                -1
                            };
                            placed += 1;
                        }
                        if placed > 0 {
                            has_pending = true;
                            side = 0;
                        } else {
                            // no usable distance: fall back to a market fill
                            raw = s.op[i];
                        }
                    }
                }
            }
            if side != 0 {
                let dist_sl = s.sl.at(i);
                let dist_tp = s.tp.at(i);
                let dist_tr = s.trail.at(i);
                if side == 1 {
                    entry_px = raw + s.half_spread + s.slippage;
                    if !s.sl.is_zero() && dist_sl <= 0.0 {
                        side = 0;
                    }
                    if !s.tp.is_zero() && dist_tp <= 0.0 {
                        side = 0;
                    }
                    if !s.trail.is_zero() && dist_tr <= 0.0 {
                        side = 0;
                    }
                    if side != 0 {
                        sl = if !s.sl.is_zero() { entry_px - dist_sl } else { 0.0 };
                        tp = if !s.tp.is_zero() { entry_px + dist_tp } else { 0.0 };
                        trail = if !s.trail.is_zero() { entry_px - dist_tr } else { 0.0 };
                    }
                } else {
                    entry_px = raw - s.half_spread - s.slippage;
                    if !s.sl.is_zero() && dist_sl <= 0.0 {
                        side = 0;
                    }
                    if !s.tp.is_zero() && dist_tp <= 0.0 {
                        side = 0;
                    }
                    if !s.trail.is_zero() && dist_tr <= 0.0 {
                        side = 0;
                    }
                    if side != 0 {
                        sl = if !s.sl.is_zero() { entry_px + dist_sl } else { 0.0 };
                        tp = if !s.tp.is_zero() { entry_px - dist_tp } else { 0.0 };
                        trail = if !s.trail.is_zero() { entry_px + dist_tr } else { 0.0 };
                    }
                }
                if side != 0 {
                    if s.use_risk_pct == 1 && !s.sl.is_zero() {
                        let risk_money = eq * s.risk_pct / 100.0;
                        let pv = if entry_px > 0.0 {
                            s.pip * s.contract / entry_px
                        } else {
                            0.0
                        };
                        let sl_pips = if s.pip > 0.0 {
                            (entry_px - sl).abs() / s.pip
                        } else {
                            0.0
                        };
                        lots_cur = if sl_pips > 0.0 && pv > 0.0 {
                            risk_money / (sl_pips * pv)
                        } else {
                            s.lots
                        };
                    } else {
                        lots_cur = s.lots;
                    }
                    let max_lots = if s.contract > 0.0 {
                        eq * s.leverage / s.contract
                    } else {
                        0.0
                    };
                    if lots_cur > max_lots {
                        lots_cur = max_lots;
                    }
                    if lots_cur > 0.0 {
                        pos = side;
                        entry_i = i;
                        eq -= s.commission * lots_cur;
                    } else {
                        pos = 0;
                    }
                }
            }
        }

        // intrabar exits (prices adjusted to the side of the book)
        if pos != 0 {
            let mut reason = 0i64;
            let mut exit_px = 0.0f64;
            if pos == 1 {
                let om = s.op[i] - s.half_spread - s.slippage;
                let lm = s.lo[i] - s.half_spread - s.slippage;
                let hm = s.hi[i] - s.half_spread - s.slippage;
                if !s.sl.is_zero() && om <= sl {
                    exit_px = om;
                    reason = 1;
                } else if !s.tp.is_zero() && om >= tp {
                    exit_px = om;
                    reason = 2;
                } else if !s.sl.is_zero() && lm <= sl {
                    exit_px = sl;
                    reason = 1;
                } else if !s.tp.is_zero() && hm >= tp {
                    exit_px = tp;
                    reason = 2;
                } else if !s.trail.is_zero() && lm <= trail {
                    exit_px = trail;
                    reason = 3;
                }
            } else {
                let om = s.op[i] + s.half_spread + s.slippage;
                let hm = s.hi[i] + s.half_spread + s.slippage;
                let lm = s.lo[i] + s.half_spread + s.slippage;
                if !s.sl.is_zero() && om >= sl {
                    exit_px = om;
                    reason = 1;
                } else if !s.tp.is_zero() && om <= tp {
                    exit_px = om;
                    reason = 2;
                } else if !s.sl.is_zero() && hm >= sl {
                    exit_px = sl;
                    reason = 1;
                } else if !s.tp.is_zero() && lm <= tp {
                    exit_px = tp;
                    reason = 2;
                } else if !s.trail.is_zero() && hm >= trail {
                    exit_px = trail;
                    reason = 3;
                }
            }
            if reason != 0 {
                let pnl = if pos == 1 {
                    (exit_px - entry_px) * s.contract * lots_cur / exit_px
                } else {
                    (entry_px - exit_px) * s.contract * lots_cur / exit_px
                };
                eq += pnl - s.commission * lots_cur;
                trades.push(Trade {
                    entry_i: entry_i as i64,
                    exit_i: i as i64,
                    side: pos,
                    entry_px,
                    exit_px,
                    pnl: pnl - 2.0 * s.commission * lots_cur,
                    reason,
                });
                cooldown_until = i as i64 + s.cooldown_bars;
                pos = 0;
            }
            if pos != 0 && !s.trail.is_zero() {
                let dist_tr = s.trail.at(i);
                if pos == 1 {
                    let cand = s.cl[i] - s.half_spread - s.slippage - dist_tr;
                    if cand > trail {
                        trail = cand;
                    }
                    if trail < sl && !s.sl.is_zero() {
                        trail = sl;
                    }
                } else {
                    let cand = s.cl[i] + s.half_spread + s.slippage + dist_tr;
                    if trail == 0.0 || cand < trail {
                        trail = cand;
                    }
                    if trail > sl && !s.sl.is_zero() {
                        trail = sl;
                    }
                }
            }
        }

        // mark to market
        let eq_mark = if pos == 1 {
            eq + (s.cl[i] - entry_px) * s.contract * lots_cur / s.cl[i]
        } else if pos == -1 {
            eq + (entry_px - s.cl[i]) * s.contract * lots_cur / s.cl[i]
        } else {
            eq
        };
        let d = s.day_id[i] as usize;
        if d < n_days {
            daily_eq[d] = eq_mark;
        }
        if eq_mark > peak {
            peak = eq_mark;
        }
        let dd = if peak > 0.0 {
            (peak - eq_mark) / peak
        } else {
            0.0
        };
        if dd > maxdd {
            maxdd = dd;
        }
    }

    if pos != 0 {
        let i = n - 1;
        let (exit_px, pnl) = if pos == 1 {
            let x = s.cl[i] - s.half_spread - s.slippage;
            (x, (x - entry_px) * s.contract * lots_cur / x)
        } else {
            let x = s.cl[i] + s.half_spread + s.slippage;
            (x, (entry_px - x) * s.contract * lots_cur / x)
        };
        eq += pnl - s.commission * lots_cur;
        trades.push(Trade {
            entry_i: entry_i as i64,
            exit_i: i as i64,
            side: pos,
            entry_px,
            exit_px,
            pnl: pnl - 2.0 * s.commission * lots_cur,
            reason: 9,
        });
        let d = s.day_id[i] as usize;
        if d < n_days {
            daily_eq[d] = eq;
        }
    }

    SimOut {
        final_eq: eq,
        maxdd,
        trades,
        daily_eq,
    }
}
