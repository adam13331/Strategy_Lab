//! Port of `sqx.indicators` — all 52 catalogue indicators.
//!
//! NaN is used for "not available", matching pandas.  The helpers below
//! reproduce the exact pandas semantics the Python library relies on:
//!
//! * `ewm(..., adjust=False, min_periods=n)`: `y[0]=x[0]`,
//!   `y[i]=a*x[i]+(1-a)*y[i-1]`, outputs masked to NaN for the first `n-1`
//!   observations; a leading NaN run seeds the recursion at the first value.
//! * `rolling(n, min_periods=n)`: NaN unless the window holds `n` numbers.
//! * `rolling(n, min_periods=m<n)`: aggregates the numbers in the window when
//!   there are at least `m` of them.
//! * `.replace(0, nan)` is applied wherever the Python source does it, so a
//!   zero denominator yields NaN rather than inf.

use crate::catalogue::Ind;

pub struct Series<'a> {
    pub open: &'a [f64],
    pub high: &'a [f64],
    pub low: &'a [f64],
    pub close: &'a [f64],
    pub volume: &'a [f64],
}

pub const NAN: f64 = f64::NAN;

// ------------------------------------------------------------------ helpers
fn ewm(x: &[f64], alpha: f64, minp: usize) -> Vec<f64> {
    let mut out = vec![NAN; x.len()];
    let mut avg = NAN;
    let mut count = 0usize;
    for i in 0..x.len() {
        let v = x[i];
        if v.is_nan() {
            continue;
        }
        count += 1;
        if avg.is_nan() {
            avg = v;
        } else {
            avg = alpha * v + (1.0 - alpha) * avg;
        }
        if count >= minp {
            out[i] = avg;
        }
    }
    out
}

fn ewm_span(x: &[f64], n: usize) -> Vec<f64> {
    ewm(x, 2.0 / (n as f64 + 1.0), n)
}

fn ewm_alpha_n(x: &[f64], n: usize) -> Vec<f64> {
    ewm(x, 1.0 / n as f64, n)
}

fn roll_sum(x: &[f64], n: usize, minp: usize) -> Vec<f64> {
    let len = x.len();
    let mut out = vec![NAN; len];
    let mut sum = 0.0;
    let mut count = 0usize;
    for i in 0..len {
        if !x[i].is_nan() {
            sum += x[i];
            count += 1;
        }
        if i >= n && !x[i - n].is_nan() {
            sum -= x[i - n];
            count -= 1;
        }
        if i + 1 >= n && count >= minp {
            out[i] = sum;
        }
    }
    out
}

/// Window sum recomputed from scratch.
///
/// The sliding sum leaves a ~1e-12 residue where the true window sum is
/// zero (add/remove of unequal magnitudes does not cancel exactly), which
/// matters wherever a zero denominator means NaN: `mfi` would report ~100
/// instead of NaN on a window with no down-ticks, and `cmf` likewise.
fn roll_sum_exact(x: &[f64], n: usize, minp: usize) -> Vec<f64> {
    let len = x.len();
    let mut out = vec![NAN; len];
    for i in 0..len {
        if i + 1 < n {
            continue;
        }
        let mut sum = 0.0;
        let mut count = 0usize;
        for j in (i + 1 - n)..=i {
            if !x[j].is_nan() {
                sum += x[j];
                count += 1;
            }
        }
        if count >= minp {
            out[i] = sum;
        }
    }
    out
}

fn roll_mean(x: &[f64], n: usize, minp: usize) -> Vec<f64> {
    let len = x.len();
    let mut out = vec![NAN; len];
    let mut sum = 0.0;
    let mut count = 0usize;
    for i in 0..len {
        if !x[i].is_nan() {
            sum += x[i];
            count += 1;
        }
        if i >= n && !x[i - n].is_nan() {
            sum -= x[i - n];
            count -= 1;
        }
        if i + 1 >= n && count >= minp {
            out[i] = sum / count as f64;
        }
    }
    out
}

/// Welford add/remove, the same accumulation order pandas uses in
/// `roll_var`, so rolling std agrees with the Python engine to the last bits
/// rather than merely to mathematical equality (the two differ by up to 5e-9,
/// which %B and the width ratios then amplify).
fn roll_std0(x: &[f64], n: usize, minp: usize) -> Vec<f64> {
    let len = x.len();
    let mut out = vec![NAN; len];
    let mut mean = 0.0f64;
    let mut ssq = 0.0f64;
    let mut nobs = 0usize;
    for i in 0..len {
        let v = x[i];
        if !v.is_nan() {
            nobs += 1;
            let delta = v - mean;
            mean += delta / nobs as f64;
            ssq += delta * (v - mean);
        }
        if i >= n {
            let old = x[i - n];
            if !old.is_nan() && nobs > 1 {
                nobs -= 1;
                let delta = old - mean;
                mean -= delta / nobs as f64;
                ssq -= delta * (old - mean);
            } else if !old.is_nan() && nobs == 1 {
                nobs = 0;
                mean = 0.0;
                ssq = 0.0;
            }
        }
        if i + 1 >= n && nobs >= minp && nobs > 0 {
            let var = (ssq / nobs as f64).max(0.0);
            out[i] = var.sqrt();
        }
    }
    out
}

fn roll_extreme(x: &[f64], n: usize, minp: usize, want_max: bool) -> Vec<f64> {
    // Monotonic queue in a plain Vec: indices are pushed once and popped once,
    // `head` only advances, so no ring arithmetic and no per-bar allocation.
    let len = x.len();
    let mut out = vec![NAN; len];
    let mut q: Vec<usize> = Vec::with_capacity(len);
    let mut head = 0usize;
    let mut nan_count = 0usize;
    for i in 0..len {
        let v = x[i];
        if v.is_nan() {
            nan_count += 1;
        } else {
            while q.len() > head {
                let b = *q.last().unwrap();
                let worse = if want_max { x[b] <= v } else { x[b] >= v };
                if worse {
                    q.pop();
                } else {
                    break;
                }
            }
            q.push(i);
        }
        if i >= n && x[i - n].is_nan() {
            nan_count -= 1;
        }
        while q.len() > head && q[head] + n <= i {
            head += 1;
        }
        if i + 1 >= n && n - nan_count >= minp && q.len() > head {
            out[i] = x[q[head]];
        }
    }
    out
}

fn roll_max(x: &[f64], n: usize) -> Vec<f64> {
    roll_extreme(x, n, n, true)
}

fn roll_min(x: &[f64], n: usize) -> Vec<f64> {
    roll_extreme(x, n, n, false)
}

// pandas' rolling median keeps a sorted window; n is <= 100 here.
fn roll_median(x: &[f64], n: usize, minp: usize) -> Vec<f64> {
    let len = x.len();
    let mut out = vec![NAN; len];
    let mut win: Vec<f64> = Vec::with_capacity(n + 1);
    let mut nan_count = 0usize;
    let insert = |win: &mut Vec<f64>, v: f64| {
        let pos = win.partition_point(|&w| w < v);
        win.insert(pos, v);
    };
    for i in 0..len {
        if x[i].is_nan() {
            nan_count += 1;
        } else {
            insert(&mut win, x[i]);
        }
        if i >= n {
            let old = x[i - n];
            if old.is_nan() {
                nan_count -= 1;
            } else {
                let pos = win.partition_point(|&w| w < old);
                win.remove(pos);
            }
        }
        if i + 1 >= n {
            let count = n - nan_count;
            if count >= minp && !win.is_empty() {
                let m = win.len();
                out[i] = if m % 2 == 1 {
                    win[m / 2]
                } else {
                    (win[m / 2 - 1] + win[m / 2]) / 2.0
                };
            }
        }
    }
    out
}

fn shift(x: &[f64], k: usize) -> Vec<f64> {
    let mut out = vec![NAN; x.len()];
    for i in k..x.len() {
        out[i] = x[i - k];
    }
    out
}

fn diff(x: &[f64]) -> Vec<f64> {
    let mut out = vec![NAN; x.len()];
    for i in 1..x.len() {
        out[i] = x[i] - x[i - 1];
    }
    out
}

fn map2(a: &[f64], b: &[f64], f: impl Fn(f64, f64) -> f64) -> Vec<f64> {
    a.iter().zip(b).map(|(x, y)| f(*x, *y)).collect()
}

fn div_num(a: &[f64], b: &[f64]) -> Vec<f64> {
    map2(a, b, |x, y| x / y)
}

fn div_zero_nan(a: &[f64], b: &[f64]) -> Vec<f64> {
    map2(a, b, |x, y| if y == 0.0 { NAN } else { x / y })
}

fn bools(b: impl IntoIterator<Item = bool>, n: usize) -> Vec<f64> {
    let mut out = vec![0.0; n];
    for (i, v) in b.into_iter().enumerate() {
        out[i] = if v { 1.0 } else { 0.0 };
    }
    out
}

fn ratio(s: &[f64], n: usize) -> Vec<f64> {
    let minp = (5usize).max(n / 2);
    let base = roll_median(s, n, minp);
    div_zero_nan(s, &base)
}

fn atr(high: &[f64], low: &[f64], close: &[f64], n: usize) -> Vec<f64> {
    let pc = shift(close, 1);
    let mut tr = vec![0.0; high.len()];
    for i in 0..high.len() {
        let hl = high[i] - low[i];
        let mut m = hl;
        let a = (high[i] - pc[i]).abs();
        let b = (low[i] - pc[i]).abs();
        if !a.is_nan() && a > m {
            m = a;
        }
        if !b.is_nan() && b > m {
            m = b;
        }
        if pc[i].is_nan() {
            m = hl;
        }
        tr[i] = m;
    }
    ewm_alpha_n(&tr, n)
}

fn pct_change(x: &[f64]) -> Vec<f64> {
    let prev = shift(x, 1);
    map2(x, &prev, |c, p| c / p - 1.0)
}

fn wma(x: &[f64], n: usize) -> Vec<f64> {
    // Sliding weighted sum: W[i] = W[i-1] - sum(W[i-1]) + n*x[i].
    let len = x.len();
    let mut out = vec![NAN; len];
    if n == 0 || len < n {
        return out;
    }
    let wsum = (n * (n + 1)) as f64 / 2.0;
    let mut nanp = vec![0i32; len + 1];
    for i in 0..len {
        nanp[i + 1] = nanp[i] + i32::from(x[i].is_nan());
    }
    // seed with the first full window, then slide
    let mut w = 0.0;
    let mut s = 0.0;
    for j in 0..n {
        let v = x[j];
        w += (j + 1) as f64 * v;
        s += v;
    }
    // `valid` tracks whether w/s describe the previous window.  A window that
    // contained a NaN leaves them NaN, so the next clean window has to be
    // summed from scratch — otherwise the NaN propagates to the end of the
    // series (this is exactly what `hma` hits: its input starts with NaNs).
    let mut valid = nanp[n] == 0;
    if valid {
        out[n - 1] = w / wsum;
    }
    for i in n..len {
        if nanp[i + 1] - nanp[i + 1 - n] == 0 {
            // Reseed every 512 bars: the recurrence drifts by ~1e-10 per step
            // relative to its own magnitude, which is 2e-7 on the HMA output by
            // the end of a 147k-bar series and enough to flip price comparisons.
            if valid && (i - n) % 512 != 0 {
                w = w - s + n as f64 * x[i];
                s = s - x[i - n] + x[i];
            } else {
                w = 0.0;
                s = 0.0;
                for j in 0..n {
                    let v = x[i + 1 - n + j];
                    w += (j + 1) as f64 * v;
                    s += v;
                }
                valid = true;
            }
            out[i] = w / wsum;
        } else {
            valid = false;
        }
    }
    out
}

// HTF helpers -------------------------------------------------------------
const HTF_CHOICES: [&str; 5] = ["H1", "H4", "D1", "W1", "MN1"];

fn days_to_ymd(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn tf_group(ts: &[i64], tf: &str) -> Vec<i64> {
    let mut out = Vec::with_capacity(ts.len());
    if tf == "MN1" {
        for &t in ts {
            let (y, m, _) = days_to_ymd(t.div_euclid(86400));
            out.push(y * 12 + m - 1);
        }
        return out;
    }
    if tf == "W1" {
        for &t in ts {
            let days = t.div_euclid(86400);
            out.push((days + 3).div_euclid(7));
        }
        return out;
    }
    let secs: i64 = match tf {
        "M1" => 60,
        "M5" => 300,
        "M15" => 900,
        "M30" => 1800,
        "H1" => 3600,
        "H4" => 14_400,
        _ => 86_400,
    };
    for &t in ts {
        out.push(t.div_euclid(secs));
    }
    out
}

fn group_bounds(group: &[i64]) -> (Vec<usize>, Vec<usize>) {
    let mut starts = vec![0usize];
    for i in 1..group.len() {
        if group[i] != group[i - 1] {
            starts.push(i);
        }
    }
    let mut ends = starts[1..].to_vec();
    ends.push(group.len());
    (starts, ends)
}

fn group_ordinals(starts: &[usize], ends: &[usize]) -> Vec<usize> {
    let mut out = Vec::with_capacity(*ends.last().unwrap_or(&0));
    for (g, (&s, &e)) in starts.iter().zip(ends).enumerate() {
        for _ in s..e {
            out.push(g);
        }
    }
    out
}

fn group_running(values: &[f64], ordinals: &[usize], want_max: bool) -> Vec<f64> {
    let mut out = vec![NAN; values.len()];
    let mut cur_g = usize::MAX;
    let mut cur = NAN;
    for i in 0..values.len() {
        if ordinals[i] != cur_g {
            cur_g = ordinals[i];
            cur = NAN;
        }
        let v = values[i];
        if cur.is_nan() {
            cur = v;
        } else if !v.is_nan() && ((want_max && v > cur) || (!want_max && v < cur)) {
            cur = v;
        }
        out[i] = cur;
    }
    out
}

fn group_final(values: &[f64], starts: &[usize], ends: &[usize], want_max: bool) -> Vec<f64> {
    let mut out = Vec::with_capacity(starts.len());
    for (&s, &e) in starts.iter().zip(ends) {
        let mut cur = NAN;
        for v in &values[s..e] {
            if v.is_nan() {
                continue;
            }
            if cur.is_nan() || (want_max && *v > cur) || (!want_max && *v < cur) {
                cur = *v;
            }
        }
        out.push(cur);
    }
    out
}

fn htf_rolling(ts: &[i64], values: &[f64], tf: &str, lookback: usize, want_max: bool) -> Vec<f64> {
    let n = values.len();
    if n == 0 {
        return values.to_vec();
    }
    let group = tf_group(ts, tf);
    let (starts, ends) = group_bounds(&group);
    let ordinals = group_ordinals(&starts, &ends);
    let running = group_running(values, &ordinals, want_max);
    let lookback = lookback.max(1);
    if lookback == 1 {
        return running;
    }
    let finals = group_final(values, &starts, &ends, want_max);
    let w = lookback - 1;
    // prior[g] = extreme(finals[g-w .. g]) with a full window, then shifted by one
    let mut prior = vec![NAN; finals.len()];
    for g in w..finals.len() {
        let mut cur = NAN;
        for k in (g - w)..g {
            let v = finals[k];
            if v.is_nan() {
                continue;
            }
            if cur.is_nan() || (want_max && v > cur) || (!want_max && v < cur) {
                cur = v;
            }
        }
        prior[g] = cur;
    }
    let mut out = running.clone();
    for i in 0..n {
        let p = prior[ordinals[i]];
        if p.is_nan() {
            continue;
        }
        out[i] = if want_max { out[i].max(p) } else { out[i].min(p) };
    }
    for i in 0..n {
        if ordinals[i] < w {
            out[i] = NAN;
        }
    }
    out
}

fn htf_offset(ts: &[i64], values: &[f64], tf: &str, offset: usize, want_max: bool) -> Vec<f64> {
    let n = values.len();
    if n == 0 {
        return values.to_vec();
    }
    let group = tf_group(ts, tf);
    let (starts, ends) = group_bounds(&group);
    let ordinals = group_ordinals(&starts, &ends);
    if offset == 0 {
        return group_running(values, &ordinals, want_max);
    }
    let finals = group_final(values, &starts, &ends, want_max);
    let mut out = vec![NAN; n];
    for i in 0..n {
        let ord = ordinals[i];
        if ord >= offset {
            out[i] = finals[ord - offset];
        }
    }
    out
}

// ------------------------------------------------------------------ dispatch
pub fn compute(id: &str, args: &[f64], s: &Series, ts: &[i64], ind: &Ind) -> Vec<Vec<f64>> {
    let ip = |k: usize| -> usize {
        let v = args.get(k).copied().unwrap_or_else(|| {
            ind.params
                .get(k)
                .map(|p| p.default)
                .unwrap_or(0.0)
        });
        v.round() as usize
    };
    let fp = |k: usize| -> f64 {
        args.get(k)
            .copied()
            .unwrap_or_else(|| ind.params.get(k).map(|p| p.default).unwrap_or(0.0))
    };
    let c = s.close;
    let h = s.high;
    let l = s.low;
    let o = s.open;
    let v = s.volume;
    match id {
        // ------------------------------------------------------------ trend
        "sma" => vec![roll_mean(c, ip(0), ip(0))],
        "ema" => vec![ewm_span(c, ip(0))],
        "wma" => vec![wma(c, ip(0))],
        "hma" => {
            let period = ip(0);
            let half = (period / 2).max(2);
            let root = ((period as f64).sqrt().round() as usize).max(2);
            let a = wma(c, half);
            let b = wma(c, period);
            let inner: Vec<f64> = a.iter().zip(&b).map(|(x, y)| 2.0 * x - y).collect();
            vec![wma(&inner, root)]
        }
        "dema" => {
            let p = ip(0);
            let e1 = ewm_span(c, p);
            let e2 = ewm_span(&e1, p);
            vec![map2(&e1, &e2, |a, b| 2.0 * a - b)]
        }
        "tema" => {
            let p = ip(0);
            let e1 = ewm_span(c, p);
            let e2 = ewm_span(&e1, p);
            let e3 = ewm_span(&e2, p);
            vec![map2(&map2(&e1, &e2, |a, b| 3.0 * a - 3.0 * b), &e3, |a, b| a + b)]
        }
        "kama" => {
            let period = ip(0);
            let fast = ip(1);
            let slow = ip(2);
            let n = c.len();
            let mut out = vec![NAN; n];
            if n > period {
                let fs = 2.0 / (fast as f64 + 1.0);
                let ss = 2.0 / (slow as f64 + 1.0);
                let mut prev = c[period - 1];
                out[period - 1] = prev;
                for i in period..n {
                    let mut vol = 0.0;
                    for j in (i - period)..i {
                        vol += (c[j + 1] - c[j]).abs();
                    }
                    let change = (c[i] - c[i - period]).abs();
                    let sc = if vol == 0.0 { NAN } else { change / vol };
                    let mut sm = (sc * (fs - ss) + ss).powi(2);
                    if !sm.is_finite() {
                        sm = 0.0;
                    }
                    prev += sm * (c[i] - prev);
                    out[i] = prev;
                }
            }
            vec![out]
        }
        "vwma" => {
            let p = ip(0);
            let cv: Vec<f64> = c.iter().zip(v).map(|(x, y)| x * y).collect();
            vec![div_num(&roll_sum(&cv, p, p), &roll_sum(v, p, p))]
        }
        "macd" => {
            let (fast, slow, sig) = (ip(0), ip(1), ip(2));
            let line = map2(&ewm_span(c, fast), &ewm_span(c, slow), |a, b| a - b);
            let signal = ewm_span(&line, sig);
            vec![line.clone(), signal.clone(), map2(&line, &signal, |a, b| a - b)]
        }
        "adx" => {
            let p = ip(0);
            let up = diff(h);
            let dn: Vec<f64> = diff(l).iter().map(|d| -d).collect();
            let mut plus = vec![0.0; h.len()];
            let mut minus = vec![0.0; h.len()];
            for i in 0..h.len() {
                if !up[i].is_nan() && !dn[i].is_nan() {
                    if up[i] > dn[i] && up[i] > 0.0 {
                        plus[i] = up[i];
                    }
                    if dn[i] > up[i] && dn[i] > 0.0 {
                        minus[i] = dn[i];
                    }
                }
            }
            let a = atr(h, l, c, p);
            let pdi = div_num(&ewm_alpha_n(&plus, p), &a).iter().map(|x| 100.0 * x).collect::<Vec<_>>();
            let mdi = div_num(&ewm_alpha_n(&minus, p), &a).iter().map(|x| 100.0 * x).collect::<Vec<_>>();
            let denom: Vec<f64> = pdi.iter().zip(&mdi).map(|(a, b)| a + b).collect();
            let dxnum: Vec<f64> = pdi
                .iter()
                .zip(&mdi)
                .map(|(a, b)| 100.0 * (a - b).abs())
                .collect();
            let dx = div_zero_nan(&dxnum, &denom);
            vec![ewm_alpha_n(&dx, p), pdi, mdi]
        }
        "aroon" => {
            let p = ip(0);
            let w = p + 1;
            let n = h.len();
            let mut up = vec![NAN; n];
            let mut dn = vec![NAN; n];
            for i in 0..n {
                if i + 1 < w {
                    continue;
                }
                let mut hi_i = i + 1 - w;
                let mut lo_i = i + 1 - w;
                let mut bad = false;
                for j in (i + 1 - w)..=i {
                    if h[j].is_nan() || l[j].is_nan() {
                        bad = true;
                        break;
                    }
                    if h[j] > h[hi_i] {
                        hi_i = j;
                    }
                    if l[j] < l[lo_i] {
                        lo_i = j;
                    }
                }
                if !bad {
                    up[i] = 100.0 * (hi_i - (i + 1 - w)) as f64 / p as f64;
                    dn[i] = 100.0 * (lo_i - (i + 1 - w)) as f64 / p as f64;
                }
            }
            vec![up, dn]
        }
        "supertrend" => {
            let period = ip(0);
            let mult = fp(1);
            let a = atr(h, l, c, period);
            let n = c.len();
            let mut fup = vec![NAN; n];
            let mut flo = vec![NAN; n];
            let mut trend = vec![0.0; n];
            let mut line = vec![NAN; n];
            for i in 1..n {
                if !a[i].is_finite() {
                    continue;
                }
                let hl2 = (h[i] + l[i]) / 2.0;
                let upper = hl2 + mult * a[i];
                let lower = hl2 - mult * a[i];
                fup[i] = if upper < fup[i - 1] || c[i - 1] > fup[i - 1] || !fup[i - 1].is_finite() {
                    upper
                } else {
                    fup[i - 1]
                };
                flo[i] = if lower > flo[i - 1] || c[i - 1] < flo[i - 1] || !flo[i - 1].is_finite() {
                    lower
                } else {
                    flo[i - 1]
                };
                if !fup[i - 1].is_finite() {
                    trend[i] = 1.0;
                } else if trend[i - 1] == 1.0 {
                    trend[i] = if c[i] < flo[i] { -1.0 } else { 1.0 };
                } else {
                    trend[i] = if c[i] > fup[i] { 1.0 } else { -1.0 };
                }
                line[i] = if trend[i] == 1.0 { flo[i] } else { fup[i] };
            }
            vec![trend, line]
        }
        "psar" => {
            let step = fp(0);
            let max_step = fp(1);
            let n = h.len();
            let mut out = vec![NAN; n];
            if n >= 2 {
                let mut bull = true;
                let mut af = step;
                let mut ep = h[0];
                let mut sar = l[0];
                out[0] = sar;
                for i in 1..n {
                    sar += af * (ep - sar);
                    if bull {
                        if l[i] < sar {
                            bull = false;
                            sar = ep;
                            ep = l[i];
                            af = step;
                        } else if h[i] > ep {
                            ep = h[i];
                            af = (af + step).min(max_step);
                        }
                    } else if h[i] > sar {
                        bull = true;
                        sar = ep;
                        ep = h[i];
                        af = step;
                    } else if l[i] < ep {
                        ep = l[i];
                        af = (af + step).min(max_step);
                    }
                    out[i] = sar;
                }
            }
            vec![out]
        }
        "ichimoku" => {
            let (t, k, sk) = (ip(0), ip(1), ip(2));
            let th = roll_max(h, t);
            let tl = roll_min(l, t);
            let tenkan = map2(&th, &tl, |a, b| (a + b) / 2.0);
            let kh = roll_max(h, k);
            let kl = roll_min(l, k);
            let kijun = map2(&kh, &kl, |a, b| (a + b) / 2.0);
            let a = map2(&tenkan, &kijun, |x, y| (x + y) / 2.0);
            let sh = roll_max(h, sk);
            let sl = roll_min(l, sk);
            let b = map2(&sh, &sl, |x, y| (x + y) / 2.0);
            vec![tenkan, kijun, a, b]
        }
        "alligator" => {
            let hl2: Vec<f64> = h.iter().zip(l).map(|(a, b)| (a + b) / 2.0).collect();
            vec![ewm_alpha_n(&hl2, ip(0)), ewm_alpha_n(&hl2, ip(1)), ewm_alpha_n(&hl2, ip(2))]
        }
        "vortex" => {
            let p = ip(0);
            let pc = shift(c, 1);
            let mut tr = vec![0.0; h.len()];
            for i in 0..h.len() {
                let hl = h[i] - l[i];
                let mut m = hl;
                let a = (h[i] - pc[i]).abs();
                let b = (l[i] - pc[i]).abs();
                if !a.is_nan() && a > m {
                    m = a;
                }
                if !b.is_nan() && b > m {
                    m = b;
                }
                if pc[i].is_nan() {
                    m = hl;
                }
                tr[i] = m;
            }
            let prev_l = shift(l, 1);
            let prev_h = shift(h, 1);
            let vp: Vec<f64> = h.iter().zip(&prev_l).map(|(a, b)| (a - b).abs()).collect();
            let vm: Vec<f64> = l.iter().zip(&prev_h).map(|(a, b)| (a - b).abs()).collect();
            let trs = roll_sum(&tr, p, p);
            vec![div_zero_nan(&roll_sum(&vp, p, p), &trs), div_zero_nan(&roll_sum(&vm, p, p), &trs)]
        }
        // --------------------------------------------------------- momentum
        "rsi" => {
            let p = ip(0);
            let d = diff(c);
            let up: Vec<f64> = d.iter().map(|x| if x.is_nan() { NAN } else { x.max(0.0) }).collect();
            let dn: Vec<f64> = d.iter().map(|x| if x.is_nan() { NAN } else { (-x.min(0.0)).max(0.0) }).collect();
            let au = ewm_alpha_n(&up, p);
            let ad = ewm_alpha_n(&dn, p);
            let rs = div_zero_nan(&au, &ad);
            let one = vec![1.0; c.len()];
            let den = map2(&one, &rs, |a, b| a + b);
            let num = vec![100.0; c.len()];
            let out = div_num(&num, &den).iter().map(|x| 100.0 - x).collect::<Vec<_>>();
            vec![out]
        }
        "stochastic" => {
            let (k, d, sm) = (ip(0), ip(1), ip(2));
            let ll = roll_min(l, k);
            let hh = roll_max(h, k);
            let span = map2(&hh, &ll, |a, b| a - b);
            let num = map2(c, &ll, |a, b| 100.0 * (a - b));
            let raw = div_zero_nan(&num, &span);
            let kk = roll_mean(&raw, sm, sm);
            let dd = roll_mean(&kk, d, d);
            vec![kk, dd]
        }
        "stochrsi" => {
            let (rp, sp, k, d) = (ip(0), ip(1), ip(2), ip(3));
            let r = {
                let dd = diff(c);
                let up: Vec<f64> = dd.iter().map(|x| if x.is_nan() { NAN } else { x.max(0.0) }).collect();
                let dn: Vec<f64> = dd.iter().map(|x| if x.is_nan() { NAN } else { (-x.min(0.0)).max(0.0) }).collect();
                let au = ewm_alpha_n(&up, rp);
                let ad = ewm_alpha_n(&dn, rp);
                let rs = div_zero_nan(&au, &ad);
                let one = vec![1.0; c.len()];
                let den = map2(&one, &rs, |a, b| a + b);
                let num = vec![100.0; c.len()];
                div_num(&num, &den).iter().map(|x| 100.0 - x).collect::<Vec<f64>>()
            };
            let ll = roll_min(&r, sp);
            let hh = roll_max(&r, sp);
            let span = map2(&hh, &ll, |a, b| a - b);
            let num = map2(&r, &ll, |a, b| 100.0 * (a - b));
            let raw = div_zero_nan(&num, &span);
            let kk = roll_mean(&raw, k, k);
            let dd = roll_mean(&kk, d, d);
            vec![kk, dd]
        }
        "cci" => {
            let p = ip(0);
            let tp: Vec<f64> = (0..h.len()).map(|i| (h[i] + l[i] + c[i]) / 3.0).collect();
            let ma = roll_mean(&tp, p, p);
            let dev: Vec<f64> = tp.iter().zip(&ma).map(|(a, b)| (a - b).abs()).collect();
            let md = roll_mean(&dev, p, p);
            let num: Vec<f64> = tp.iter().zip(&ma).map(|(a, b)| a - b).collect();
            let den: Vec<f64> = md.iter().map(|x| 0.015 * x).collect();
            vec![div_zero_nan(&num, &den)]
        }
        "williams_r" => {
            let p = ip(0);
            let hh = roll_max(h, p);
            let ll = roll_min(l, p);
            let span = map2(&hh, &ll, |a, b| a - b);
            let num = map2(&hh, c, |a, b| -100.0 * (a - b));
            vec![div_zero_nan(&num, &span)]
        }
        "mfi" => {
            let p = ip(0);
            let tp: Vec<f64> = (0..h.len()).map(|i| (h[i] + l[i] + c[i]) / 3.0).collect();
            let mf: Vec<f64> = tp.iter().zip(v).map(|(a, b)| a * b).collect();
            let prev = shift(&tp, 1);
            let mut pos = vec![0.0; tp.len()];
            let mut neg = vec![0.0; tp.len()];
            for i in 0..tp.len() {
                if !prev[i].is_nan() && tp[i] > prev[i] {
                    pos[i] = mf[i];
                }
                if !prev[i].is_nan() && tp[i] < prev[i] {
                    neg[i] = mf[i];
                }
            }
            let sp = roll_sum_exact(&pos, p, p);
            let sn = roll_sum_exact(&neg, p, p);
            let r = div_zero_nan(&sp, &sn);
            let one = vec![1.0; tp.len()];
            let den = map2(&one, &r, |a, b| a + b);
            let num = vec![100.0; tp.len()];
            let out = div_num(&num, &den).iter().map(|x| 100.0 - x).collect::<Vec<f64>>();
            vec![out]
        }
        "roc" => {
            let p = ip(0);
            let prev = shift(c, p);
            vec![map2(c, &prev, |a, b| 100.0 * (a / b - 1.0))]
        }
        "momentum" => {
            let p = ip(0);
            vec![map2(c, &shift(c, p), |a, b| a - b)]
        }
        "tsi" => {
            let (lo, sh) = (ip(0), ip(1));
            let m = diff(c);
            let abs: Vec<f64> = m.iter().map(|x| x.abs()).collect();
            let num = ewm_span(&ewm_span(&m, lo), sh);
            let den = ewm_span(&ewm_span(&abs, lo), sh);
            let scaled: Vec<f64> = num.iter().map(|x| 100.0 * x).collect();
            vec![div_zero_nan(&scaled, &den)]
        }
        "ultimate" => {
            let (p1, p2, p3) = (ip(0), ip(1), ip(2));
            let pc = shift(c, 1);
            let mut bp = vec![0.0; h.len()];
            let mut tr = vec![0.0; h.len()];
            for i in 0..h.len() {
                let low_min = if pc[i].is_nan() || l[i] < pc[i] { l[i] } else { pc[i] };
                let high_max = if pc[i].is_nan() || h[i] > pc[i] { h[i] } else { pc[i] };
                bp[i] = c[i] - low_min;
                tr[i] = high_max - low_min;
            }
            let avg = |n: usize| div_zero_nan(&roll_sum(&bp, n, n), &roll_sum(&tr, n, n));
            let a1 = avg(p1);
            let a2 = avg(p2);
            let a3 = avg(p3);
            let n = h.len();
            let mut out = vec![NAN; n];
            for i in 0..n {
                let v = 4.0 * a1[i] + 2.0 * a2[i] + a3[i];
                out[i] = 100.0 * v / 7.0;
            }
            vec![out]
        }
        "awesome" => {
            let (f, sl) = (ip(0), ip(1));
            let hl2: Vec<f64> = h.iter().zip(l).map(|(a, b)| (a + b) / 2.0).collect();
            vec![map2(&roll_mean(&hl2, f, f), &roll_mean(&hl2, sl, sl), |a, b| a - b)]
        }
        "fisher" => {
            let p = ip(0);
            let n = h.len();
            let hl2: Vec<f64> = h.iter().zip(l).map(|(a, b)| (a + b) / 2.0).collect();
            let mut out = vec![NAN; n];
            let mut val = 0.0f64;
            let mut prev = 0.0f64;
            for i in p..n {
                let window = &hl2[i + 1 - p..=i];
                let mut lo = f64::INFINITY;
                let mut hi = f64::NEG_INFINITY;
                for &x in window {
                    if !x.is_nan() {
                        if x < lo {
                            lo = x;
                        }
                        if x > hi {
                            hi = x;
                        }
                    }
                }
                if !lo.is_finite() || hi == lo {
                    continue;
                }
                let x = 2.0 * ((hl2[i] - lo) / (hi - lo) - 0.5);
                val = 0.33 * x + 0.67 * val;
                val = val.clamp(-0.999, 0.999);
                let f = 0.5 * ((1.0 + val) / (1.0 - val)).ln() + 0.5 * prev;
                prev = f;
                out[i] = f;
            }
            vec![out]
        }
        "demarker" => {
            let p = ip(0);
            let hp = shift(h, 1);
            let lp = shift(l, 1);
            let mut dmax = vec![NAN; h.len()];
            let mut dmin = vec![NAN; h.len()];
            for i in 0..h.len() {
                if !hp[i].is_nan() {
                    dmax[i] = (h[i] - hp[i]).max(0.0);
                }
                if !lp[i].is_nan() {
                    dmin[i] = (lp[i] - l[i]).max(0.0);
                }
            }
            let a = roll_mean(&dmax, p, p);
            let b = roll_mean(&dmin, p, p);
            let den = map2(&a, &b, |x, y| x + y);
            vec![div_zero_nan(&a, &den)]
        }
        "trix" => {
            let (p, sig) = (ip(0), ip(1));
            let e = ewm_span(&ewm_span(&ewm_span(c, p), p), p);
            let rate: Vec<f64> = {
                let prev = shift(&e, 1);
                map2(&e, &prev, |a, b| 100.0 * (a / b - 1.0))
            };
            vec![rate.clone(), ewm_span(&rate, sig)]
        }
        // ------------------------------------------------------- volatility
        "atr" => {
            // catalogue order: atr_ratio, atr_pct
            let a = atr(h, l, c, ip(0));
            let pct = div_num(&a, c).iter().map(|x| 100.0 * x).collect::<Vec<_>>();
            vec![ratio(&a, 100), pct]
        }
        "bbands" => {
            let (p, std) = (ip(0), fp(1));
            let mid = roll_mean(c, p, p);
            let sd = roll_std0(c, p, p);
            let up = map2(&mid, &sd, |m, s| m + std * s);
            let lo = map2(&mid, &sd, |m, s| m - std * s);
            let span = map2(&up, &lo, |a, b| a - b);
            let pctb = div_zero_nan(&map2(c, &lo, |a, b| a - b), &span);
            let width = div_zero_nan(&span, &mid).iter().map(|x| 100.0 * x).collect::<Vec<_>>();
            let wr = ratio(&width, 100);
            // catalogue outputs: upper, mid, lower, percent_b, width_ratio
            vec![up, mid, lo, pctb, wr]
        }
        "keltner" => {
            let (ep, ap, mult) = (ip(0), ip(1), fp(2));
            let mid = ewm_span(c, ep);
            let a = atr(h, l, c, ap);
            let up = map2(&mid, &a, |m, x| m + mult * x);
            let lo = map2(&mid, &a, |m, x| m - mult * x);
            vec![up, mid.clone(), lo]
        }
        "donchian" => {
            let p = ip(0);
            let up = shift(&roll_max(h, p), 1);
            let lo = shift(&roll_min(l, p), 1);
            let mid = map2(&up, &lo, |a, b| (a + b) / 2.0);
            vec![up, mid, lo]
        }
        "stddev" => {
            let p = ip(0);
            let pc = pct_change(c);
            let sd = roll_std0(&pc, p, p);
            let s: Vec<f64> = sd.iter().map(|x| x * 100.0).collect();
            let r = ratio(&s, 100);
            // catalogue order: std_ratio, std_pct
            vec![r, s]
        }
        "histvol" => {
            let p = ip(0);
            let prev = shift(c, 1);
            let lr: Vec<f64> = map2(c, &prev, |a, b| (a / b).ln());
            let sd = roll_std0(&lr, p, p);
            let hv: Vec<f64> = sd.iter().map(|x| x * 252f64.sqrt() * 100.0).collect();
            let r = ratio(&hv, 100);
            // catalogue order: vol_ratio, histvol
            vec![r, hv]
        }
        // ----------------------------------------------------------- volume
        "obv" => {
            let p = ip(0);
            let d = diff(c);
            let mut obv = Vec::with_capacity(c.len());
            let mut acc = 0.0;
            for i in 0..c.len() {
                let sgn = if d[i].is_nan() {
                    0.0
                } else if d[i] > 0.0 {
                    1.0
                } else if d[i] < 0.0 {
                    -1.0
                } else {
                    0.0
                };
                acc += sgn * v[i];
                obv.push(acc);
            }
            let prev = shift(&obv, p);
            vec![div_zero_nan(&map2(&obv, &prev, |a, b| a - b), &roll_sum(v, p, p))]
        }
        "ad" => {
            let p = ip(0);
            let mut ad = Vec::with_capacity(c.len());
            let mut acc = 0.0;
            for i in 0..c.len() {
                let hl = h[i] - l[i];
                if hl == 0.0 {
                    // NaN in the source series: cumsum skips it, value stays NaN
                    ad.push(NAN);
                    continue;
                }
                acc += (((c[i] - l[i]) - (h[i] - c[i])) / hl) * v[i];
                ad.push(acc);
            }
            let prev = shift(&ad, p);
            vec![div_zero_nan(&map2(&ad, &prev, |a, b| a - b), &roll_sum(v, p, p))]
        }
        "cmf" => {
            let p = ip(0);
            let mut mfv = vec![0.0; c.len()];
            for i in 0..c.len() {
                let hl = h[i] - l[i];
                mfv[i] = if hl == 0.0 {
                    NAN
                } else {
                    (((c[i] - l[i]) - (h[i] - c[i])) / hl) * v[i]
                };
            }
            vec![div_zero_nan(
                &roll_sum_exact(&mfv, p, p),
                &roll_sum_exact(v, p, p),
            )]
        }
        "volume" => {
            let p = ip(0);
            let mean = roll_mean(v, p, p);
            vec![div_zero_nan(v, &mean)]
        }
        "force" => {
            let p = ip(0);
            let d = diff(c);
            let fi: Vec<f64> = d.iter().zip(v).map(|(a, b)| a * b).collect();
            vec![ewm_span(&fi, p)]
        }
        // ----------------------------------------------------- price action
        "breakout" => {
            let p = ip(0);
            let hi = shift(&roll_max(h, p), 1);
            let lo = shift(&roll_min(l, p), 1);
            let up: Vec<bool> = (0..c.len()).map(|i| !hi[i].is_nan() && c[i] > hi[i]).collect();
            let dn: Vec<bool> = (0..c.len()).map(|i| !lo[i].is_nan() && c[i] < lo[i]).collect();
            vec![bools(up, c.len()), bools(dn, c.len())]
        }
        "patterns" => {
            let body_ratio = fp(1);
            let wick_ratio = fp(2);
            let n = c.len();
            let mut inside = vec![false; n];
            let mut outside = vec![false; n];
            let mut bull = vec![false; n];
            let mut bear = vec![false; n];
            let mut pin_bull = vec![false; n];
            let mut pin_bear = vec![false; n];
            let mut doji = vec![false; n];
            for i in 1..n {
                let po = o[i - 1];
                let pc = c[i - 1];
                inside[i] = h[i] <= h[i - 1] && l[i] >= l[i - 1];
                outside[i] = h[i] > h[i - 1] && l[i] < l[i - 1];
                bull[i] = c[i] > o[i] && pc < po && c[i] >= po && o[i] <= pc;
                bear[i] = c[i] < o[i] && pc > po && c[i] <= po && o[i] >= pc;
                let body = (c[i] - o[i]).abs();
                let rng = h[i] - l[i];
                let lower_wick = o[i].min(c[i]) - l[i];
                let upper_wick = h[i] - o[i].max(c[i]);
                pin_bull[i] = lower_wick > wick_ratio * body
                    && lower_wick > wick_ratio * upper_wick
                    && c[i] >= o[i];
                pin_bear[i] = upper_wick > wick_ratio * body
                    && upper_wick > wick_ratio * lower_wick
                    && c[i] <= o[i];
                doji[i] = !(rng == 0.0) && body <= body_ratio * rng;
            }
            // catalogue order: bull_engulf, bear_engulf, pin_bull, pin_bear,
            // inside_bar, outside_bar, doji
            vec![
                bools(bull, n),
                bools(bear, n),
                bools(pin_bull, n),
                bools(pin_bear, n),
                bools(inside, n),
                bools(outside, n),
                bools(doji, n),
            ]
        }
        "higher" => {
            let h1 = shift(h, 1);
            let h2 = shift(h, 2);
            let l1 = shift(l, 1);
            let l2 = shift(l, 2);
            let n = c.len();
            let hh: Vec<bool> = (0..n).map(|i| h[i] > h1[i] && h1[i] > h2[i]).collect();
            let ll: Vec<bool> = (0..n).map(|i| l[i] < l1[i] && l1[i] < l2[i]).collect();
            vec![bools(hh, n), bools(ll, n)]
        }
        "body" => {
            let a = atr(h, l, c, ip(0));
            let num: Vec<f64> = c.iter().zip(o).map(|(x, y)| x - y).collect();
            vec![div_zero_nan(&num, &a)]
        }
        "range" => {
            let p = ip(0);
            let r: Vec<f64> = h.iter().zip(l).map(|(a, b)| a - b).collect();
            vec![ratio(&r, p)]
        }
        "gap" => {
            let a = atr(h, l, c, ip(0));
            let prev = shift(c, 1);
            let num: Vec<f64> = o.iter().zip(&prev).map(|(x, y)| x - y).collect();
            vec![div_zero_nan(&num, &a)]
        }
        // ------------------------------------------------------------- HTF
        "iHigh" | "iLow" => {
            let tf = HTF_CHOICES
                .get(ip(0))
                .copied()
                .unwrap_or("D1")
                .to_string();
            let lookback = ip(1);
            let want_max = id == "iHigh";
            let field = if want_max { h } else { l };
            vec![htf_rolling(ts, field, &tf, lookback, want_max)]
        }
        "iDailyHigh" | "iDailyLow" => {
            let want_max = id == "iDailyHigh";
            let field = if want_max { h } else { l };
            vec![htf_offset(ts, field, "D1", ip(0), want_max)]
        }
        "iMonthlyHigh" | "iMonthlyLow" => {
            let want_max = id == "iMonthlyHigh";
            let field = if want_max { h } else { l };
            vec![htf_offset(ts, field, "MN1", ip(0), want_max)]
        }
        other => panic!("unknown indicator: {other}"),
    }
}

/// Resolves a spec's named params into positional args in catalogue order.
pub fn args_from_params(ind: &Ind, params: &serde_json::Map<String, serde_json::Value>) -> Vec<f64> {
    let mut out = Vec::with_capacity(ind.params.len());
    for p in &ind.params {
        let raw = params.get(&p.name);
        if !p.choices.is_empty() {
            match raw {
                Some(serde_json::Value::String(s)) => {
                    let idx = p.choices.iter().position(|c| c == s).unwrap_or(0);
                    out.push(idx as f64);
                }
                Some(v) => out.push(v.as_f64().unwrap_or(p.default)),
                None => out.push(p.default),
            }
            continue;
        }
        let v = match raw {
            Some(v) => v.as_f64().unwrap_or_else(|| {
                if let Some(s) = v.as_str() {
                    s.parse().unwrap_or(p.default)
                } else {
                    p.default
                }
            }),
            None => p.default,
        };
        out.push(if p.integer { v.round() } else { v });
    }
    out
}

/// Computes only the output a strategy actually asked for.
///
/// The composite indicators build several series, and some of those (the
/// `_ratio` outputs need a rolling median) cost more than everything else the
/// attempt does.  A strategy requests exactly one output, so computing the
/// rest is pure waste — `atr_pct` for a stop-loss must not pay for
/// `atr_ratio`.
pub fn compute_subset(
    id: &str,
    args: &[f64],
    s: &Series,
    ts: &[i64],
    ind: &Ind,
    key: &str,
) -> Vec<f64> {
    let ip = |k: usize| -> usize { args.get(k).copied().unwrap_or(0.0).round() as usize };
    let fp = |k: usize| -> f64 { args.get(k).copied().unwrap_or(0.0) };
    let c = s.close;
    let zero_nan = |x: Vec<f64>| x;
    match (id, key) {
        ("atr", "atr_pct") => {
            let a = atr(s.high, s.low, c, ip(0));
            div_num(&a, c).iter().map(|x| 100.0 * x).collect()
        }
        ("atr", "atr_ratio") => ratio(&atr(s.high, s.low, c, ip(0)), 100),
        ("bbands", _) => {
            let (p, std) = (ip(0), fp(1));
            let mid = roll_mean(c, p, p);
            let sd = roll_std0(c, p, p);
            let up = map2(&mid, &sd, |m, x| m + std * x);
            let lo = map2(&mid, &sd, |m, x| m - std * x);
            match key {
                "upper" => up,
                "mid" => mid,
                "lower" => lo,
                "percent_b" => {
                    let span = map2(&up, &lo, |a, b| a - b);
                    div_zero_nan(&map2(c, &lo, |a, b| a - b), &span)
                }
                _ => {
                    let span = map2(&up, &lo, |a, b| a - b);
                    let width = div_zero_nan(&span, &mid)
                        .iter()
                        .map(|x| 100.0 * x)
                        .collect::<Vec<_>>();
                    ratio(&width, 100)
                }
            }
        }
        ("stddev", _) => {
            let pc = pct_change(c);
            let sd = roll_std0(&pc, ip(0), ip(0));
            let scaled: Vec<f64> = sd.iter().map(|x| x * 100.0).collect();
            if key == "std_pct" {
                scaled
            } else {
                ratio(&scaled, 100)
            }
        }
        ("histvol", _) => {
            let prev = shift(c, 1);
            let lr: Vec<f64> = map2(c, &prev, |a, b| (a / b).ln());
            let sd = roll_std0(&lr, ip(0), ip(0));
            let hv: Vec<f64> = sd.iter().map(|x| x * 252f64.sqrt() * 100.0).collect();
            if key == "histvol" {
                hv
            } else {
                ratio(&hv, 100)
            }
        }
        _ => {
            let all = compute(id, args, s, ts, ind);
            let idx = ind.outputs.iter().position(|o| o.key == key).unwrap_or(0);
            zero_nan(all.into_iter().nth(idx).unwrap_or_else(|| vec![NAN; c.len()]))
        }
    }
}
