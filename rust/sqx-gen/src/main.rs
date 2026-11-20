//! sqx-gen — the SQX generation engine in Rust.
//!
//! Modes:
//!   --check-indicators FILE   diff every recorded indicator array against this binary
//!   --sample N --out FILE     sample N strategies and write JSONL (sampler gate)
//!   --specs FILE --out FILE   replay specs and write per-spec results (parity gate)
//!   --specs FILE --bench      throughput per thread count + phase split (bench gate)

mod catalogue;
mod ctx;
mod hash;
mod indicators;
mod kernel;
mod metrics;
mod rng;
mod sample;
mod signals;

use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::catalogue::Ind;
use crate::ctx::Ctx;
use crate::metrics::round_to;
use crate::signals::{run_backtest, BacktestResult, Cache};

struct Args {
    ctx: PathBuf,
    catalogue: PathBuf,
    specs: Option<PathBuf>,
    out: Option<PathBuf>,
    check_indicators: Option<PathBuf>,
    sample_n: Option<usize>,
    time_indicators: bool,
    bench: bool,
    threads: Vec<usize>,
    reps: usize,
    seed: u64,
    symbol: String,
    timeframe: String,
}

fn parse_args() -> Args {
    let mut a = Args {
        ctx: PathBuf::new(),
        catalogue: PathBuf::new(),
        specs: None,
        out: None,
        check_indicators: None,
        sample_n: None,
        time_indicators: false,
        bench: false,
        threads: vec![1],
        reps: 2,
        seed: 42,
        symbol: "USDJPY".into(),
        timeframe: "M30".into(),
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        let key = argv[i].as_str();
        let mut next = || {
            i += 1;
            argv.get(i).cloned().unwrap_or_default()
        };
        match key {
            "--ctx" => a.ctx = PathBuf::from(next()),
            "--catalogue" => a.catalogue = PathBuf::from(next()),
            "--specs" => a.specs = Some(PathBuf::from(next())),
            "--out" => a.out = Some(PathBuf::from(next())),
            "--check-indicators" => a.check_indicators = Some(PathBuf::from(next())),
            "--sample" => a.sample_n = Some(next().parse().unwrap()),
            "--seed" => a.seed = next().parse().unwrap(),
            "--reps" => a.reps = next().parse().unwrap(),
            "--threads" => {
                a.threads = next()
                    .split(',')
                    .filter_map(|s| s.parse().ok())
                    .collect();
            }
            "--bench" => a.bench = true,
            "--time-indicators" => a.time_indicators = true,
            "--symbol" => a.symbol = next(),
            "--timeframe" => a.timeframe = next(),
            other => panic!("unknown flag {other}"),
        }
        i += 1;
    }
    a
}

fn load_specs(path: &Path) -> Vec<Value> {
    let f = BufReader::new(File::open(path).expect("specs file"));
    f.lines()
        .filter_map(|l| l.ok())
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(&l).expect("spec json"))
        .collect()
}

fn result_json(idx: usize, r: &BacktestResult) -> Value {
    let trades: Vec<Value> = r
        .trades
        .iter()
        .map(|t| {
            json!([t.entry_i, t.exit_i, t.side, t.entry_px, t.exit_px,
                   round_to(t.pnl, 6), t.reason])
        })
        .collect();
    json!({
        "idx": idx,
        "metrics_is": Value::Object(r.metrics_is.clone()),
        "metrics_oos": Value::Object(r.metrics_oos.clone()),
        "metrics_all": Value::Object(r.metrics_all.clone()),
        "final_equity": r.final_equity,
        "max_dd_m1": r.max_dd_m1,
        "split_day": r.split_day,
        "signal_counts": {
            "long": r.signal_counts.0,
            "short": r.signal_counts.1,
            "exit": r.signal_counts.2,
        },
        "trades": trades,
        "daily_eq": r.daily_eq,
    })
}

// ------------------------------------------------------------- indicator diff
fn check_indicators(path: &Path, ctx: &Ctx, catalogue: &[Ind]) -> bool {
    let mut f = BufReader::new(File::open(path).expect("golden file"));
    let mut magic = [0u8; 8];
    f.read_exact(&mut magic).expect("magic");
    assert_eq!(&magic, b"SQXIND01", "bad indicator golden magic");
    let mut b4 = [0u8; 4];
    f.read_exact(&mut b4).unwrap();
    let n = u32::from_le_bytes(b4) as usize;

    let series = indicators::Series {
        open: &ctx.tf_open,
        high: &ctx.tf_high,
        low: &ctx.tf_low,
        close: &ctx.tf_close,
        volume: &ctx.tf_volume,
    };

    let mut arrays = 0usize;
    let mut worst: Map<String, Value> = Map::new();
    let mut failures: Vec<Value> = Vec::new();
    let mut per_ind_max: std::collections::HashMap<String, f64> = std::collections::HashMap::new();

    for _ in 0..n {
        let mut b2 = [0u8; 2];
        f.read_exact(&mut b2).unwrap();
        let idlen = u16::from_le_bytes(b2) as usize;
        let mut idb = vec![0u8; idlen];
        f.read_exact(&mut idb).unwrap();
        let id = String::from_utf8(idb).unwrap();
        f.read_exact(&mut b2).unwrap();
        let nargs = u16::from_le_bytes(b2) as usize;
        let mut args = vec![0.0f64; nargs];
        for a in args.iter_mut() {
            let mut b8 = [0u8; 8];
            f.read_exact(&mut b8).unwrap();
            *a = f64::from_le_bytes(b8);
        }
        let ind = catalogue.iter().find(|i| i.id == id).expect("indicator").clone();
        let got = indicators::compute(&id, &args, &series, &ctx.tf_ts, &ind);
        f.read_exact(&mut b2).unwrap();
        let nout = u16::from_le_bytes(b2) as usize;
        for _k in 0..nout {
            f.read_exact(&mut b2).unwrap();
            let klen = u16::from_le_bytes(b2) as usize;
            let mut kb = vec![0u8; klen];
            f.read_exact(&mut kb).unwrap();
            let key = String::from_utf8(kb).unwrap();
            let mut b8 = [0u8; 8];
            f.read_exact(&mut b8).unwrap();
            let len = u64::from_le_bytes(b8) as usize;
            let mut exp = vec![0.0f64; len];
            for e in exp.iter_mut() {
                let mut w = [0u8; 8];
                f.read_exact(&mut w).unwrap();
                *e = f64::from_le_bytes(w);
            }
            // Match by key: `compute` returns catalogue order, the golden file
            // follows the Python dict order.  Keys the catalogue does not expose
            // (e.g. bbands' internal `bandwidth`) can never be selected by a
            // strategy, so they are not part of the contract.
            let Some(slot) = ind.outputs.iter().position(|o| o.key == key) else {
                continue;
            };
            arrays += 1;
            let mut max_abs = 0.0f64;
            let mut max_rel = 0.0f64;
            let mut mismatched = 0usize;
            let mut first_bad: Option<usize> = None;
            if slot >= got.len() || len != got[slot].len() {
                failures.push(json!({"indicator": id, "args": args, "output": key,
                                     "error": format!("len {} vs {:?}", len,
                                        got.get(slot).map(|a| a.len()))}));
                continue;
            }
            for i in 0..len {
                let g = got[slot][i];
                let e = exp[i];
                let ok = match (g.is_nan(), e.is_nan()) {
                    (true, true) => true,
                    (false, true) | (true, false) => false,
                    (false, false) => {
                        let d = (g - e).abs();
                        if d > max_abs {
                            max_abs = d;
                        }
                        let rel = if e.abs() > 0.0 { d / e.abs() } else { d };
                        if rel > max_rel {
                            max_rel = rel;
                        }
                        d <= 1e-5 || rel <= 1e-6
                    }
                };
                if !ok {
                    mismatched += 1;
                    if first_bad.is_none() {
                        first_bad = Some(i);
                    }
                }
            }
            let e_big = max_abs > 1e-5 && max_rel > 1e-6;
            let entry = per_ind_max.entry(id.clone()).or_insert(0.0);
            if max_abs > *entry {
                *entry = max_abs;
            }
            if mismatched > 0 && e_big {
                failures.push(json!({
                    "indicator": id, "args": args, "output": key,
                    "mismatched": mismatched, "n": len,
                    "max_abs": max_abs, "max_rel": max_rel, "first_bad": first_bad,
                }));
            }
        }
    }
    for (k, v) in per_ind_max.iter() {
        worst.insert(k.clone(), json!(v));
    }
    let pass = failures.is_empty();
    println!(
        "{}",
        json!({"mode": "check-indicators", "records": n, "arrays": arrays,
               "pass": pass, "failures": failures, "worst_abs": worst})
    );
    pass
}

// --------------------------------------------------------------- bench mode
fn bench(specs: &[Value], ctx: &Ctx, catalogue: &[Ind], threads: &[usize], reps: usize) {
    println!(
        "bench: {} specs, threads {:?}, reps {}",
        specs.len(),
        threads,
        reps
    );
    let mut rows: Map<String, Value> = Map::new();

    // serial, with phase instrumentation on the last rep
    let mut best_serial = f64::INFINITY;
    let mut phases = json!({});
    for rep in 0..reps.max(1) {
        let mut sig_ns = 0u128;
        let mut ker_ns = 0u128;
        let mut met_ns = 0u128;
        let mut computes = 0usize;
        let last = rep + 1 == reps.max(1);
        let t0 = Instant::now();
        for spec in specs {
            let mut cache = Cache::new();
            let mut tm = signals::Timings::default();
            let _ = run_backtest(spec, ctx, catalogue, &mut cache, if last { Some(&mut tm) } else { None });
            sig_ns += tm.signals_ns;
            ker_ns += tm.kernel_ns;
            met_ns += tm.metrics_ns;
            computes += cache.computes;
        }
        let elapsed = t0.elapsed().as_secs_f64();
        if elapsed < best_serial {
            best_serial = elapsed;
        }
        if last {
            let n = specs.len() as f64;
            phases = json!({
                "signals_ms": round_to(sig_ns as f64 / 1e6 / n, 1),
                "kernel_ms": round_to(ker_ns as f64 / 1e6 / n, 1),
                "metrics_ms": round_to(met_ns as f64 / 1e6 / n, 1),
                "total_ms": round_to(elapsed * 1000.0 / n, 1),
                "indicator_computes_per_attempt": round_to(computes as f64 / n, 1),
            });
        }
    }
    rows.insert(
        "1".into(),
        json!({
            "workers": 1,
            "best_seconds": best_serial,
            "attempts_per_sec": round_to(specs.len() as f64 / best_serial, 2),
        }),
    );

    for &k in threads {
        if k == 1 {
            continue;
        }
        let mut best = f64::INFINITY;
        for _ in 0..reps.max(1) {
            let t0 = Instant::now();
            let chunk = (specs.len() + k - 1) / k;
            std::thread::scope(|s| {
                for part in specs.chunks(chunk.max(1)) {
                    s.spawn(move || {
                        for spec in part {
                            let mut cache = Cache::new();
                            let _ = run_backtest(spec, ctx, catalogue, &mut cache, None);
                        }
                    });
                }
            });
            let e = t0.elapsed().as_secs_f64();
            if e < best {
                best = e;
            }
        }
        rows.insert(
            k.to_string(),
            json!({
                "workers": k,
                "best_seconds": best,
                "attempts_per_sec": round_to(specs.len() as f64 / best, 2),
            }),
        );
    }

    let serial_tps = specs.len() as f64 / best_serial;
    println!(
        "{}",
        json!({
            "mode": "bench",
            "specs": specs.len(),
            "threads": rows,
            "phases": phases,
            "serial_attempts_per_sec": round_to(serial_tps, 2),
        })
    );
}

fn main() {
    let args = parse_args();
    let ctx = Ctx::load(&args.ctx).expect("context");
    let catalogue = catalogue::load(&args.catalogue);
    println!(
        "ctx: {} M1 bars, {} TF bars, {} days; {} indicators",
        ctx.m1_ts.len(),
        ctx.tf_close.len(),
        ctx.day_ts.len(),
        catalogue.len()
    );

    if args.time_indicators {
        let series = indicators::Series {
            open: &ctx.tf_open,
            high: &ctx.tf_high,
            low: &ctx.tf_low,
            close: &ctx.tf_close,
            volume: &ctx.tf_volume,
        };
        let mut rows = Map::new();
        for ind in &catalogue {
            let paramsv: Vec<f64> = ind.params.iter().map(|p| p.default).collect();
            let _ = indicators::compute(&ind.id, &paramsv, &series, &ctx.tf_ts, ind);
            let t = Instant::now();
            let reps = 3;
            for _ in 0..reps {
                let _ = indicators::compute(&ind.id, &paramsv, &series, &ctx.tf_ts, ind);
            }
            rows.insert(
                ind.id.clone(),
                json!(round_to(t.elapsed().as_secs_f64() * 1000.0 / reps as f64, 3)),
            );
        }
        println!("{}", json!(rows));
        return;
    }

    if let Some(p) = &args.check_indicators {
        let pass = check_indicators(p, &ctx, &catalogue);
        std::process::exit(if pass { 0 } else { 1 });
    }

    if let Some(n) = args.sample_n {
        let mut rng = rng::Rng::new(args.seed);
        let out = args.out.clone().unwrap_or_else(|| PathBuf::from("specs.jsonl"));
        let mut f = std::io::BufWriter::new(File::create(&out).expect("out"));
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut written = 0usize;
        for _ in 0..n {
            let spec = sample::sample_strategy(&mut rng, &catalogue, &args.symbol, &args.timeframe);
            let h = sample::hash_spec(&spec);
            seen.insert(h);
            writeln!(f, "{}", serde_json::to_string(&spec).unwrap()).unwrap();
            written += 1;
        }
        f.flush().unwrap();
        println!(
            "{}",
            json!({"mode": "sample", "specs": written, "unique": seen.len(), "out": out})
        );
        return;
    }

    let specs_path = args.specs.as_ref().expect("--specs required");
    let specs = load_specs(specs_path);
    if args.bench {
        bench(&specs, &ctx, &catalogue, &args.threads, args.reps);
        return;
    }
    let out = args.out.as_ref().expect("--out required");
    let mut f = std::io::BufWriter::new(File::create(out).expect("out"));
    let t0 = Instant::now();
    for (i, spec) in specs.iter().enumerate() {
        let mut cache = Cache::new();
        let r = run_backtest(spec, &ctx, &catalogue, &mut cache, None);
        writeln!(f, "{}", result_json(i, &r)).unwrap();
    }
    f.flush().unwrap();
    let elapsed = t0.elapsed().as_secs_f64();
    println!(
        "{}",
        json!({"mode": "replay", "specs": specs.len(), "seconds": elapsed,
               "attempts_per_sec": round_to(specs.len() as f64 / elapsed, 2),
               "out": out})
    );
}
