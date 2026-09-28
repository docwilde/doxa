//! DOXA's private provider contract. No inference or history mutation may pass a denied gate.
pub(crate) fn allow_compaction(required_previews: usize, reviewed_runs: &[bool], stopped: bool) -> bool {
    required_previews == 1 && reviewed_runs == [true] && !stopped
}

#[cfg(test)]
mod tests {
    use super::allow_compaction;

    #[test]
    fn infrastructure_failures_never_authorize_compaction() {
        for runs in [vec![], vec![false], vec![true, false], vec![true, true]] {
            assert!(!allow_compaction(1, &runs, false));
        }
        for previews in [0, 2] {
            assert!(!allow_compaction(previews, &[true], false));
        }
        assert!(!allow_compaction(1, &[true], true));
        assert!(allow_compaction(1, &[true], false));
    }
}
