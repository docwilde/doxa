//! Conservative per-request holds for direct API turns.
use doxa_vendors::{Completion, Error};
use serde_json::{json, Value};

pub struct RequestBudget {
    remaining: u64,
    input_rate: f64,
    output_rate: f64,
    model: String,
    pending: Option<(u64, u64, u64)>,
}

fn cost(tokens: u64, rate: f64) -> Result<u64, Error> {
    let micros = (tokens as f64 * rate).ceil();
    if !micros.is_finite() || micros < 0.0 || micros >= u64::MAX as f64 { return Err(Error::SpendLimit); }
    Ok(micros as u64)
}

impl RequestBudget {
    pub fn new(usd: f64, engine: &str, model: &str) -> Result<Self, Error> {
        let price = crate::budget_host::vendor_price(engine, model).ok_or(Error::SpendLimit)?;
        if !usd.is_finite() || usd <= 0.0 || usd * 1_000_000.0 >= u64::MAX as f64
            || !price.input.is_finite() || price.input < 0.0 || !price.output.is_finite() || price.output <= 0.0 {
            return Err(Error::SpendLimit);
        }
        Ok(Self { remaining:(usd * 1_000_000.0).floor() as u64, input_rate:price.input,
            output_rate:price.output, model:model.into(), pending:None })
    }

    pub fn admit(&mut self, body: &mut Value) -> Result<(), Error> {
        if self.pending.is_some() || body["model"].as_str() != Some(&self.model) { return Err(Error::SpendLimit); }
        // One serialized byte bounds at most one text token. The extra bytes
        // cover the max_tokens field inserted below, including its digits.
        let input = serde_json::to_vec(body).map_err(|_| Error::SpendLimit)?.len()
            .checked_add(64).ok_or(Error::SpendLimit)? as u64;
        let input_cost = cost(input, self.input_rate)?;
        let available = self.remaining.checked_sub(input_cost).ok_or(Error::SpendLimit)?;
        let cap = body["max_tokens"].as_u64().unwrap_or(32768).min(32768);
        let mut output = ((available as f64 / self.output_rate).floor() as u64).min(cap);
        while output > 0 && cost(output, self.output_rate)? > available { output -= 1; }
        if output == 0 { return Err(Error::SpendLimit); }
        let hold = input_cost.checked_add(cost(output, self.output_rate)?).ok_or(Error::SpendLimit)?;
        self.remaining = self.remaining.checked_sub(hold).ok_or(Error::SpendLimit)?;
        self.pending = Some((input, output, hold));
        body["max_tokens"] = json!(output);
        Ok(())
    }

    pub fn settle(&mut self, completion: &Completion) -> Result<(), Error> {
        let (input_cap, output_cap, hold) = self.pending.ok_or(Error::SpendLimit)?;
        let usage = completion.usage.as_ref().ok_or(Error::SpendLimit)?;
        let input = usage["prompt_tokens"].as_u64().filter(|n| *n > 0 && *n <= input_cap).ok_or(Error::SpendLimit)?;
        let output = usage["completion_tokens"].as_u64().filter(|n| *n <= output_cap).ok_or(Error::SpendLimit)?;
        if completion.model_conflict || completion.model.as_deref() != Some(&self.model)
            || (output == 0 && (!completion.text.is_empty() || !completion.reasoning.is_empty() || !completion.tool_calls.is_empty())) {
            return Err(Error::SpendLimit);
        }
        let actual = cost(input, self.input_rate)?.checked_add(cost(output, self.output_rate)?).ok_or(Error::SpendLimit)?;
        self.remaining = self.remaining.checked_add(hold.checked_sub(actual).ok_or(Error::SpendLimit)?)
            .ok_or(Error::SpendLimit)?;
        self.pending = None;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn completion(usage: Option<Value>) -> Completion {
        Completion {text:"Answer".into(),reasoning:String::new(),model:Some("deepseek-flash".into()),
            model_conflict:false,usage,finish_reason:Some("stop".into()),tool_calls:vec![],malformed_chunks:0}
    }
    #[test]
    fn request_holds_cover_input_and_output_and_release_only_verified_usage() {
        let mut budget=RequestBudget::new(0.001,"deepseek","deepseek-flash").unwrap();
        let mut body=json!({"model":"deepseek-flash","messages":[{"role":"user","content":"Hello"}]});
        budget.admit(&mut body).unwrap();
        let output=body["max_tokens"].as_u64().unwrap();
        assert!((serde_json::to_vec(&body).unwrap().len() as f64*0.3+output as f64*1.2)<=1000.0);
        assert!(budget.admit(&mut body.clone()).is_err(),"an in-flight hold cannot be reused");
        budget.settle(&completion(Some(json!({"prompt_tokens":10,"completion_tokens":10})))).unwrap();
        assert_eq!(budget.remaining,985);
        budget.admit(&mut body).unwrap();
    }
    #[test]
    fn missing_or_excess_usage_and_wrong_models_never_reopen_the_hold() {
        for case in ["missing","excess_input","excess_output","wrong_model"] {
            let mut budget=RequestBudget::new(1.0,"deepseek","deepseek-flash").unwrap();
            let mut body=json!({"model":"deepseek-flash","max_tokens":10,"messages":[]});
            budget.admit(&mut body).unwrap();
            let mut result=completion(Some(json!({"prompt_tokens":1,"completion_tokens":1})));
            match case {
                "missing"=>result.usage=None,
                "excess_input"=>result.usage.as_mut().unwrap()["prompt_tokens"]=json!(u64::MAX),
                "excess_output"=>result.usage.as_mut().unwrap()["completion_tokens"]=json!(11),
                _=>result.model=Some("unpriced-model".into()),
            }
            assert!(budget.settle(&result).is_err(),"{case}");
            assert!(budget.admit(&mut body).is_err(),"{case}");
        }
    }
}
