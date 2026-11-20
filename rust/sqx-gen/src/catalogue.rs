//! Indicator metadata exported from `sqx.indicators.INDICATORS`.
//!
//! The Rust sampler has no hard-coded ranges: it reads this table, so the two
//! engines cannot drift when the Python catalogue changes.

use std::fs;
use std::path::Path;

#[derive(Clone, Debug)]
pub struct Param {
    pub name: String,
    pub min: f64,
    pub max: f64,
    pub step: f64,
    pub integer: bool,
    pub default: f64,
    pub choices: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Output {
    pub key: String,
    pub kind: String, // price | level | bool
    pub levels: Vec<f64>,
}

#[derive(Clone, Debug)]
pub struct Ind {
    pub id: String,
    pub params: Vec<Param>,
    pub outputs: Vec<Output>,
}

pub fn load(path: &Path) -> Vec<Ind> {
    let text = fs::read_to_string(path).expect("catalogue file");
    let mut out: Vec<Ind> = Vec::new();
    for line in text.lines() {
        let mut it = line.split(' ');
        let tag = it.next().unwrap_or("");
        match tag {
            "IND" => out.push(Ind {
                id: it.next().unwrap_or("").to_string(),
                params: Vec::new(),
                outputs: Vec::new(),
            }),
            "PARAM" => {
                let ind = out.last_mut().expect("PARAM before IND");
                let name = it.next().unwrap_or("").to_string();
                let min: f64 = it.next().unwrap().parse().unwrap();
                let max: f64 = it.next().unwrap().parse().unwrap();
                let step: f64 = it.next().unwrap().parse().unwrap();
                let integer = it.next().unwrap() == "1";
                let default: f64 = it.next().unwrap().parse().unwrap_or(0.0);
                let raw = it.next().unwrap_or("-");
                let choices = if raw == "-" {
                    Vec::new()
                } else {
                    raw.split('|').map(|s| s.to_string()).collect()
                };
                ind.params.push(Param {
                    name,
                    min,
                    max,
                    step,
                    integer,
                    default,
                    choices,
                });
            }
            "OUT" => {
                let ind = out.last_mut().expect("OUT before IND");
                let key = it.next().unwrap_or("").to_string();
                let kind = it.next().unwrap_or("").to_string();
                let raw = it.next().unwrap_or("-");
                let levels = if raw == "-" {
                    Vec::new()
                } else {
                    raw.split('|').map(|s| s.parse().unwrap()).collect()
                };
                ind.outputs.push(Output { key, kind, levels });
            }
            _ => {}
        }
    }
    out
}
