//! Deterministic PRNG for the sampler (splitmix64).
//!
//! Python's sampler uses Mersenne Twister; replaying MT19937 streams is
//! pointless here because sampling is 0.004% of an attempt and what must match
//! is the distribution, not the draw order.  Specs are exchanged, not seeds.

pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng {
            state: seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ 0xDEAD_BEEF_CAFE_F00D,
        }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    pub fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Uniform integer in [0, n] inclusive, like `random.Random.randint`.
    pub fn range_inclusive(&mut self, n: u64) -> u64 {
        if n == 0 {
            return 0;
        }
        self.next_u64() % (n + 1)
    }

    pub fn choice<'a, T>(&mut self, xs: &'a [T]) -> &'a T {
        &xs[(self.next_u64() % xs.len() as u64) as usize]
    }

    /// `random.sample(population, k)` without replacement, order preserved.
    pub fn sample<T: Clone>(&mut self, xs: &[T], k: usize) -> Vec<T> {
        let mut pool: Vec<T> = xs.to_vec();
        let mut out = Vec::with_capacity(k);
        for _ in 0..k.min(pool.len()) {
            let i = (self.next_u64() % pool.len() as u64) as usize;
            out.push(pool.remove(i));
        }
        out
    }
}
