//! Fragment framing for canonical scrubbing. This recognizes lexical boundaries,
//! not credential types: LORE remains the single owner of secret matchers.
use std::{collections::VecDeque, io};

const MAX_UNIT: usize = 8192;
const MAX_CONTEXT: usize = 2 * MAX_UNIT;
const MAX_PUSH: usize = 65536;

#[derive(Default)]
pub struct StreamScrubber {
    pending: String,
    quote: Option<char>,
    escaped: bool,
    pem: bool,
    whitespace: usize,
    context: VecDeque<String>,
    context_bytes: usize,
    clean_context: String,
    failed: bool,
}
impl StreamScrubber {
    fn refuse<T>(&mut self) -> io::Result<T> {
        self.failed = true;
        self.pending.clear();
        self.context.clear();
        self.clean_context.clear();
        Err(io::Error::other(
            "stream scrub framing refused uncertain span",
        ))
    }
    /// Emits complete lexical units immediately, retaining at most one unfinished
    /// word, quoted span or PEM block. Calls the supplied canonical scrubber on a
    /// bounded recent window, never on the accumulated turn.
    pub fn push(
        &mut self,
        text: &str,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<String> {
        if self.failed || text.len() > MAX_PUSH {
            return self.refuse();
        }
        let mut units = Vec::new();
        for c in text.chars() {
            let prior = self.pending.chars().last();
            if self.quote.is_some() {
                if self.escaped {
                    self.escaped = false;
                } else if c == '\\' {
                    self.escaped = true;
                } else if self.quote == Some(c) {
                    self.quote = None;
                }
            } else if !self.pem
                && matches!(c, '\'' | '"')
                && prior.is_none_or(|p| p.is_whitespace() || "=:([{,".contains(p))
            {
                self.quote = Some(c);
            }
            if !self.pem && c.is_whitespace() && self.pending.ends_with("-----BEGIN") {
                self.pem = true;
            }
            self.pending.push(c);
            if self.pending.len() > MAX_UNIT {
                return self.refuse();
            }
            if self.pem {
                if self.pending.contains("-----END ") && self.pending.ends_with("-----") {
                    self.pem = false;
                    units.push(std::mem::take(&mut self.pending));
                }
            } else if self.quote.is_none() && c.is_whitespace() {
                self.whitespace = if self.pending.trim().is_empty() {
                    self.whitespace + c.len_utf8()
                } else {
                    c.len_utf8()
                };
                if self.whitespace > MAX_UNIT {
                    return self.refuse();
                }
                units.push(std::mem::take(&mut self.pending));
            }
        }
        self.emit(units, &mut scrub)
    }
    /// Flush only at a genuine turn end or after emitting a visible lexical
    /// separator. Provider content-block stops are not lexical boundaries.
    /// An unfinished quoted/PEM span refuses instead of releasing uncertain bytes.
    pub fn finish(
        &mut self,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<String> {
        if self.failed || self.quote.is_some() || self.pem || self.pending.contains("-----BEGIN") {
            return self.refuse();
        }
        let units = if self.pending.is_empty() {
            vec![]
        } else {
            vec![std::mem::take(&mut self.pending)]
        };
        self.emit(units, &mut scrub)
    }
    fn emit(
        &mut self,
        units: Vec<String>,
        scrub: &mut impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<String> {
        if units.is_empty() {
            return Ok(String::new());
        }
        let mut input = self.context.iter().cloned().collect::<String>();
        input.extend(units.iter().map(String::as_str));
        let clean = match scrub(&input) {
            Ok(s) => s,
            Err(_) => return self.refuse(),
        };
        // A completed structural PEM span must be consumed by the canonical
        // scrubber; otherwise its framing/recognition is uncertain.
        if units.iter().any(|u| u.contains("-----BEGIN ")) && clean.contains("-----BEGIN ") {
            return self.refuse();
        }
        let common = self
            .clean_context
            .chars()
            .zip(clean.chars())
            .take_while(|(a, b)| a == b)
            .map(|(c, _)| c.len_utf8())
            .sum::<usize>();
        let output = clean[common..].to_owned();
        for unit in units {
            self.context_bytes += unit.len();
            self.context.push_back(unit);
        }
        let mut trimmed = false;
        while self.context_bytes > MAX_CONTEXT {
            trimmed = true;
            self.context_bytes -= self.context.pop_front().unwrap().len();
        }
        self.clean_context = if trimmed {
            let input = self.context.iter().cloned().collect::<String>();
            match scrub(&input) {
                Ok(s) => s,
                Err(_) => return self.refuse(),
            }
        } else {
            clean
        };
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scrub(s: &str) -> io::Result<String> {
        lore_core::scrub::scrub(s).map_err(io::Error::other)
    }
    #[test]
    fn every_split_retains_secrets_until_canonical_redaction() {
        let samples = [
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "eyJabcdefghijklm.abcdefghijklmnop.qrstuvwxyzabcdef",
            "Bearer abcdefghijklmnopqrstuvwxyz",
            "password  =   'long secret phrase with spaces'",
            "token = \"long secret phrase with \\\"escape\\\"\"",
            "-----BEGIN PRIVATE KEY-----\nfixturePrivateMaterial\n-----END PRIVATE KEY-----",
        ];
        for sample in samples {
            let text = format!("safe prose {sample} trailing prose");
            for split in (0..=text.len()).filter(|i| text.is_char_boundary(*i)) {
                let mut stream = StreamScrubber::default();
                let mut result = stream.push(&text[..split], scrub).unwrap();
                result.push_str(&stream.push(&text[split..], scrub).unwrap());
                result.push_str(&stream.finish(scrub).unwrap());
                assert!(result.contains("[REDACTED:"), "split {split}: {result}");
                for secret in ["abcdefgh", "long secret", "fixturePrivate", "secret phrase"] {
                    assert!(!result.contains(secret), "split {split}: {result}");
                }
            }
            let mut stream = StreamScrubber::default();
            let mut incremental = String::new();
            for c in text.chars() {
                incremental.push_str(&stream.push(&c.to_string(), scrub).unwrap());
                for secret in ["abcdefgh", "long secret", "fixturePrivate", "secret phrase"] {
                    assert!(!incremental.contains(secret));
                }
            }
            incremental.push_str(&stream.finish(scrub).unwrap());
            assert!(incremental.contains("[REDACTED:"));
        }
    }
    #[test]
    fn prose_is_fluent_and_storage_is_bounded() {
        let mut stream = StreamScrubber::default();
        assert_eq!(
            stream.push("We don't delay café ", scrub).unwrap(),
            "We don't delay café "
        );
        for _ in 0..1000 {
            assert_eq!(
                stream.push("ordinary words ", scrub).unwrap(),
                "ordinary words "
            );
        }
        assert!(stream.context_bytes <= MAX_CONTEXT);
        assert_eq!(stream.push("unfinished", scrub).unwrap(), "");
        assert_eq!(stream.finish(scrub).unwrap(), "unfinished");
        assert!(StreamScrubber::default()
            .push(&"x".repeat(MAX_UNIT + 1), scrub)
            .is_err());
        let mut stream = StreamScrubber::default();
        assert_eq!(stream.push("password='private ", scrub).unwrap(), "");
        assert!(stream.finish(scrub).is_err());
    }
}
