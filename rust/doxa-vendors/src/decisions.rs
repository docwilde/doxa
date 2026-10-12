//! Pinned typed decision services. Prices checked 2026-10-12 against
//! https://docs.typesafe.ai/models and https://subq.ai/#pricing.
use crate::credentials::{self, ApiProvider};
use std::io;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecisionModel { Jev, SubqSnap }
impl DecisionModel {
    pub fn parse(model: &str) -> Option<Self> { match model {
        "jev-1.13.0" => Some(Self::Jev), "subq-snap-preview" => Some(Self::SubqSnap), _ => None,
    } }
    pub fn model(self) -> &'static str { match self { Self::Jev=>"jev-1.13.0", Self::SubqSnap=>"subq-snap-preview" } }
    pub fn label(self) -> &'static str { match self { Self::Jev=>"Jev", Self::SubqSnap=>"SubQ Snap" } }
    pub fn provider(self) -> &'static str { match self { Self::Jev=>"jev", Self::SubqSnap=>"subq" } }
    pub fn endpoint(self) -> &'static str { match self {
        Self::Jev=>"https://api.typesafe.ai/v1/systemone", Self::SubqSnap=>"https://api.subq.ai/v1/systemone",
    } }
    pub fn input_usd_micros_per_million(self) -> u64 { match self { Self::Jev=>42_000, Self::SubqSnap=>50_000 } }
    pub fn credential(self) -> io::Result<String> {
        credentials::search_resolve(match self { Self::Jev=>ApiProvider::Jev, Self::SubqSnap=>ApiProvider::Subq })?
            .ok_or_else(||io::Error::new(io::ErrorKind::NotFound,"Decision service credential unavailable"))
    }
}
