//! Stable engine-local hash used for structural de-duplication.
//!
//! Python's `spec_hash` is a sha1 over canonical JSON.  Reproducing it would
//! require identical float formatting across languages, and the hash is only
//! ever compared against hashes from the same engine, so the Rust sampler uses
//! a 128-bit FNV-1a pair over its own canonical serialisation instead.

pub fn spec_hash(canonical: &str) -> String {
    fn fnv(text: &[u8], offset: u64) -> u64 {
        let mut h = offset;
        for b in text {
            h ^= *b as u64;
            h = h.wrapping_mul(0x1000_0000_01B3);
        }
        h
    }
    let a = fnv(canonical.as_bytes(), 0xcbf2_9ce4_8422_2325);
    let b = fnv(canonical.as_bytes(), 0x9e37_79b9_7f4a_7c15);
    format!("{a:016x}{b:016x}")[..20].to_string()
}
