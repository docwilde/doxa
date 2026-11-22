//! Bounded, transport-free stdio adapter for the Python admission fixture.

use doxa_router::{
    pilot_forward::{Forward, Request, Target},
    Config,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{self, Read};

const MAX_INPUT: u64 = 256 * 1024;

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum Input {
    Fixed {
        request: String,
        arm: String,
    },
    Prepare {
        request: String,
        config: Config,
    },
    Finish {
        request: String,
        config: Config,
        decision_sha256: String,
        response: String,
    },
}

fn wire_value(forward: &Forward) -> io::Result<Value> {
    Ok(json!({
        "wire": std::str::from_utf8(forward.body()).map_err(io::Error::other)?,
        "receipt": forward.receipt()
    }))
}

fn run() -> io::Result<Value> {
    let mut input = Vec::new();
    io::stdin().take(MAX_INPUT + 1).read_to_end(&mut input)?;
    if input.len() as u64 > MAX_INPUT {
        return Err(io::Error::other("pilot fixture input is too large"));
    }
    match serde_json::from_slice::<Input>(&input)? {
        Input::Fixed { request, arm } => {
            let target = match arm.as_str() {
                "fixed_flash_none" => Target::FlashNone,
                "fixed_pro_high" => Target::ProHigh,
                _ => return Err(io::Error::other("unknown fixed pilot arm")),
            };
            wire_value(&Request::parse(request.as_bytes())?.for_fixed(target)?)
        }
        Input::Prepare { request, config } => {
            let route = Request::parse(request.as_bytes())?.for_jev(&config)?;
            let decision = route.decision();
            Ok(json!({
                "wire": serde_json::to_string(&decision.body)?,
                "receipt": {
                    "model": config.jev_model,
                    "request_sha256": decision.request_sha256,
                    "input_bound": decision.input_token_reservation,
                    "held_usd_micros": decision.reservation_usd_micros
                }
            }))
        }
        Input::Finish {
            request,
            config,
            decision_sha256,
            response,
        } => {
            let route = Request::parse(request.as_bytes())?.for_jev(&config)?;
            if route.decision().request_sha256 != decision_sha256 {
                return Err(io::Error::other("pilot fixture decision binding changed"));
            }
            let (forward, decision) = route.observe(response.as_bytes(), 0)?;
            Ok(json!({
                "repair": forward.as_ref().map(wire_value).transpose()?,
                "decision": {
                    "request_sha256": decision.request_sha256,
                    "model": decision.model,
                    "usage": decision.usage
                }
            }))
        }
    }
}

fn main() -> io::Result<()> {
    let output = run().map_err(|_| io::Error::other("pilot wire fixture refused"))?;
    serde_json::to_writer(io::stdout().lock(), &output)?;
    Ok(())
}
