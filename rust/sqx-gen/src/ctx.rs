//! Reads the context snapshot written by `bench/rust/export_cases.py`.
//!
//! Layout: magic `SQXCTX01`, u32 array count, then per array: u8 dtype
//! (0 = i64, 1 = f64, 2 = u8), u64 length, raw little-endian payload — in the
//! fixed order below.  Then 5 i64 scalars (split_idx, split_day, tf_seconds,
//! start_ts, end_ts) and one f64 (pip size).

use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

// Some fields are part of the snapshot contract even when the engine does not
// read them yet (completeness makes the format self-describing).
#[allow(dead_code)]
pub struct Ctx {
    pub tf_ts: Vec<i64>,
    pub tf_close_ts: Vec<i64>,
    pub tf_to_m1: Vec<i64>,
    pub m1_prev_tf: Vec<i64>,
    pub m1_ts: Vec<i64>,
    pub m1_open: Vec<f64>,
    pub m1_high: Vec<f64>,
    pub m1_low: Vec<f64>,
    pub m1_close: Vec<f64>,
    pub m1_day_id: Vec<i64>,
    pub day_ts: Vec<i64>,
    pub entry_ok: Vec<u8>,
    pub force_close: Vec<u8>,
    pub m1_tf_idx: Vec<i64>,
    pub tf_open: Vec<f64>,
    pub tf_high: Vec<f64>,
    pub tf_low: Vec<f64>,
    pub tf_close: Vec<f64>,
    pub tf_volume: Vec<f64>,
    pub split_idx: i64,
    pub split_day: i64,
    pub tf_seconds: i64,
    pub start_ts: i64,
    pub end_ts: i64,
    pub pip: f64,
}

fn read_u32(r: &mut impl Read) -> io::Result<u32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b)?;
    Ok(u32::from_le_bytes(b))
}

fn read_u64(r: &mut impl Read) -> io::Result<u64> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b)?;
    Ok(u64::from_le_bytes(b))
}

fn read_i64_arr(r: &mut impl Read, len: usize) -> io::Result<Vec<i64>> {
    let mut raw = vec![0u8; len * 8];
    r.read_exact(&mut raw)?;
    Ok(raw
        .chunks_exact(8)
        .map(|c| i64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
        .collect())
}

fn read_f64_arr(r: &mut impl Read, len: usize) -> io::Result<Vec<f64>> {
    let mut raw = vec![0u8; len * 8];
    r.read_exact(&mut raw)?;
    Ok(raw
        .chunks_exact(8)
        .map(|c| f64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]))
        .collect())
}

fn read_u8_arr(r: &mut impl Read, len: usize) -> io::Result<Vec<u8>> {
    let mut raw = vec![0u8; len];
    r.read_exact(&mut raw)?;
    Ok(raw)
}

impl Ctx {
    pub fn load(path: &Path) -> io::Result<Ctx> {
        let mut f = io::BufReader::new(File::open(path)?);
        let mut magic = [0u8; 8];
        f.read_exact(&mut magic)?;
        if &magic != b"SQXCTX01" {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "bad context magic"));
        }
        let n = read_u32(&mut f)? as usize;
        let mut i64s: Vec<Vec<i64>> = Vec::new();
        let mut f64s: Vec<Vec<f64>> = Vec::new();
        let mut u8s: Vec<Vec<u8>> = Vec::new();
        for _ in 0..n {
            let mut dt = [0u8; 1];
            f.read_exact(&mut dt)?;
            let len = read_u64(&mut f)? as usize;
            match dt[0] {
                0 => i64s.push(read_i64_arr(&mut f, len)?),
                1 => f64s.push(read_f64_arr(&mut f, len)?),
                2 => u8s.push(read_u8_arr(&mut f, len)?),
                _ => {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "bad dtype"));
                }
            }
        }
        let scalars = read_i64_arr(&mut f, 5)?;
        let pip = read_f64_arr(&mut f, 1)?[0];
        Ok(Ctx {
            tf_ts: i64s[0].clone(),
            tf_close_ts: i64s[1].clone(),
            tf_to_m1: i64s[2].clone(),
            m1_prev_tf: i64s[3].clone(),
            m1_ts: i64s[4].clone(),
            m1_open: f64s[0].clone(),
            m1_high: f64s[1].clone(),
            m1_low: f64s[2].clone(),
            m1_close: f64s[3].clone(),
            m1_day_id: i64s[5].clone(),
            day_ts: i64s[6].clone(),
            entry_ok: u8s[0].clone(),
            force_close: u8s[1].clone(),
            m1_tf_idx: i64s[7].clone(),
            tf_open: f64s[4].clone(),
            tf_high: f64s[5].clone(),
            tf_low: f64s[6].clone(),
            tf_close: f64s[7].clone(),
            tf_volume: f64s[8].clone(),
            split_idx: scalars[0],
            split_day: scalars[1],
            tf_seconds: scalars[2],
            start_ts: scalars[3],
            end_ts: scalars[4],
            pip,
        })
    }
}
