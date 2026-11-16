//! Exact binomial tail inversion for recorded evaluation counts.
//! The item-wise model assumes independent observations. Fleet clustering
//! requires separate analysis; these bounds never establish judge quality.
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct BinomialBounds {
    pub successes: usize,
    pub observations: usize,
    pub rate: f64,
    /// Clopper–Pearson one-sided 95% lower bound.
    pub lower_95: f64,
    /// Clopper–Pearson one-sided 95% upper bound.
    pub upper_95: f64,
}

// Sum in log space to retain tails for the bounded 10,000-row datasets.
fn binomial_cdf(n: usize, k: usize, p: f64) -> f64 {
    if k >= n || p == 0.0 { return 1.0; }
    if p == 1.0 { return 0.0; }
    let log_p = p.ln();
    let log_q = (-p).ln_1p();
    let mut mass = n as f64 * log_q;
    let mut terms = Vec::with_capacity(k + 1);
    terms.push(mass);
    for j in 1..=k {
        mass += ((n - j + 1) as f64).ln() - (j as f64).ln() + log_p - log_q;
        terms.push(mass);
    }
    let maximum = terms.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    (maximum.exp() * terms.iter().map(|mass| (mass - maximum).exp()).sum::<f64>()).clamp(0.0, 1.0)
}

fn inverse_tail(n: usize, k: usize, target_cdf: f64) -> f64 {
    let (mut low, mut high) = (0.0, 1.0);
    for _ in 0..64 {
        let middle = (low + high) / 2.0;
        if binomial_cdf(n, k, middle) > target_cdf { low = middle; }
        else { high = middle; }
    }
    (low + high) / 2.0
}

/// Return separate one-sided 95% bounds, not a joint 95% interval.
/// Missing or invalid counts have no estimable rate and return `None`.
/// The work bound matches the private evaluation dataset limit.
pub fn binomial_bounds(successes: usize, observations: usize) -> Option<BinomialBounds> {
    if observations == 0 || observations > 10_000 || successes > observations { return None; }
    Some(BinomialBounds {
        successes, observations,
        rate: successes as f64 / observations as f64,
        lower_95: if successes == 0 { 0.0 } else { inverse_tail(observations, successes - 1, 0.95) },
        upper_95: if successes == observations { 1.0 } else { inverse_tail(observations, successes, 0.05) },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_event_and_all_event_bounds_match_closed_forms() {
        for n in [1, 2, 59, 100, 10_000] {
            let zero = binomial_bounds(0, n).unwrap();
            let all = binomial_bounds(n, n).unwrap();
            let tail = 0.05_f64.powf(1.0 / n as f64);
            assert_eq!(zero.lower_95, 0.0);
            assert!((zero.upper_95 - (1.0 - tail)).abs() < 1e-11);
            assert!((all.lower_95 - tail).abs() < 1e-11);
            assert_eq!(all.upper_95, 1.0);
        }
    }

    #[test]
    fn sixty_checkpoint_example_matches_independent_binomial_sum() {
        // P[Binomial(60, p) >= 54] = .05 at this independently computed p.
        let result = binomial_bounds(54, 60).unwrap();
        assert!((result.lower_95 - 0.8121426180110176).abs() < 1e-11);
        assert_eq!(result.rate, 0.9);
    }

    #[test]
    fn documented_risky_message_floors_are_minimal() {
        for (misses, n) in [(0, 59), (1, 93), (2, 124), (3, 153), (5, 208)] {
            assert!(binomial_bounds(misses, n).unwrap().upper_95 <= 0.05);
            assert!(binomial_bounds(misses, n - 1).unwrap().upper_95 > 0.05);
        }
    }

    #[test]
    fn bounds_are_symmetric_finite_and_ordered() {
        for (k, n) in [(1, 2), (9, 100), (91, 100), (5000, 10000), (9999, 10000)] {
            let a = binomial_bounds(k, n).unwrap();
            let b = binomial_bounds(n - k, n).unwrap();
            assert!(a.lower_95.is_finite() && a.upper_95.is_finite());
            assert!(0.0 <= a.lower_95 && a.lower_95 <= a.rate && a.rate <= a.upper_95 && a.upper_95 <= 1.0);
            assert!((a.lower_95 - (1.0 - b.upper_95)).abs() < 1e-9);
            assert!((a.upper_95 - (1.0 - b.lower_95)).abs() < 1e-9);
        }
    }

    #[test]
    fn missing_impossible_and_unbounded_samples_have_no_estimate() {
        for (k, n) in [(0, 0), (2, 1), (1, 10001), (usize::MAX, usize::MAX)] {
            assert!(binomial_bounds(k, n).is_none());
        }
    }
}
