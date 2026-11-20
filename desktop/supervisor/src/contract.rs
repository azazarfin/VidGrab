//! Validate the generated v1 schemas without giving clients the engine secret.

use serde_json::{json, Value};
use std::sync::OnceLock;

include!("../../../packages/protocol/generated/operations.rs");

fn schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| serde_json::from_str(include_str!("../../../packages/protocol/schema-v1.json"))
        .expect("The checked generated protocol schema must be valid JSON"))
}

fn identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && value.bytes().all(|byte| (33..=126).contains(&byte))
}

pub fn request_id(value: &Value) -> Option<&str> {
    value.get("request_id").and_then(Value::as_str).filter(|value| identifier(value))
}

fn matches(spec: &Value, value: &Value, root: &Value, depth: usize) -> bool {
    if depth > 64 { return false; }
    if let Some(reference) = spec.get("$ref").and_then(Value::as_str) {
        return reference.strip_prefix('#').and_then(|path| root.pointer(path))
            .is_some_and(|target| matches(target, value, root, depth + 1));
    }
    if let Some(parts) = spec.get("anyOf").and_then(Value::as_array) {
        if !parts.iter().any(|part| matches(part, value, root, depth + 1)) { return false; }
    }
    if let Some(parts) = spec.get("oneOf").and_then(Value::as_array) {
        if parts.iter().filter(|part| matches(part, value, root, depth + 1)).count() != 1 { return false; }
    }
    if spec.get("const").is_some_and(|expected| expected != value) { return false; }
    if spec.get("enum").and_then(Value::as_array).is_some_and(|items| !items.contains(value)) { return false; }
    if let Some(kind) = spec.get("type").and_then(Value::as_str) {
        let valid = match kind {
            "null" => value.is_null(), "boolean" => value.is_boolean(), "number" => value.is_number(),
            "integer" => value.is_i64() || value.is_u64(), "string" => value.is_string(),
            "array" => value.is_array(), "object" => value.is_object(), _ => false,
        };
        if !valid { return false; }
    }
    if let Some(number) = value.as_f64() {
        if spec.get("minimum").and_then(Value::as_f64).is_some_and(|limit| number < limit)
            || spec.get("maximum").and_then(Value::as_f64).is_some_and(|limit| number > limit) { return false; }
    }
    if let Some(text) = value.as_str() {
        let length = text.chars().count() as u64;
        if spec.get("minLength").and_then(Value::as_u64).is_some_and(|limit| length < limit)
            || spec.get("maxLength").and_then(Value::as_u64).is_some_and(|limit| length > limit) { return false; }
        if let Some(pattern) = spec.get("pattern").and_then(Value::as_str) {
            // The source contract currently has one pattern. Unknown future
            // patterns fail closed until the generator/validator is extended.
            if pattern != "^[!-~]{1,128}$" || !identifier(text) { return false; }
        }
    }
    if let Some(items) = value.as_array() {
        if spec.get("minItems").and_then(Value::as_u64).is_some_and(|limit| (items.len() as u64) < limit)
            || spec.get("maxItems").and_then(Value::as_u64).is_some_and(|limit| (items.len() as u64) > limit) { return false; }
        if let Some(item_spec) = spec.get("items") {
            if !items.iter().all(|item| matches(item_spec, item, root, depth + 1)) { return false; }
        }
    }
    if let Some(fields) = value.as_object() {
        if spec.get("required").and_then(Value::as_array)
            .is_some_and(|required| required.iter().any(|key| !fields.contains_key(key.as_str().unwrap_or_default()))) { return false; }
        for (name, item) in fields {
            if let Some(property) = spec.get("properties").and_then(|properties| properties.get(name)) {
                if !matches(property, item, root, depth + 1) { return false; }
            } else if let Some(additional) = spec.get("additionalProperties") {
                if additional == &Value::Bool(false) { return false; }
                if additional.is_object() && !matches(additional, item, root, depth + 1) { return false; }
            }
        }
    }
    true
}

pub fn validate_request(value: &Value) -> Result<(), &'static str> {
    if value.get("protocol_version").and_then(Value::as_u64).is_some_and(|version| version != VERSION) {
        return Err("APP_INCOMPATIBLE");
    }
    let contract = schema();
    if serde_json::to_vec(value).map_err(|_| "BAD_REQUEST")?.len() as u64 > contract["max_payload_bytes"].as_u64().unwrap_or(0) {
        return Err("MESSAGE_TOO_LARGE");
    }
    if !matches(&contract["request"], value, &contract["request"], 0) { return Err("BAD_REQUEST"); }
    let operation = value["operation"].as_str().ok_or("BAD_REQUEST")?;
    let (_, mutation) = OPERATIONS.iter().find(|(name, _)| *name == operation).ok_or("UNSUPPORTED_OPERATION")?;
    if *mutation && value.get("idempotency_key").and_then(Value::as_str).is_none() { return Err("BAD_REQUEST"); }
    let parameters = &contract["operations"][operation]["params"];
    if !matches(parameters, &value["params"], parameters, 0) { return Err("BAD_REQUEST"); }
    Ok(())
}

pub fn validate_response(value: &Value) -> bool {
    let response = &schema()["response"];
    matches(response, value, response, 0)
        && (value["ok"] == true || value["error"]["request_id"] == value["request_id"])
}

pub fn is_mutation(value: &Value) -> bool {
    OPERATIONS.iter().any(|(name, mutation)| *mutation && value["operation"] == *name)
}

pub fn failure(id: Option<&str>, code: &str) -> Value {
    let code = match code {
        "INCOMPATIBLE_VERSION" | "UNSUPPORTED_OPERATION" => "APP_INCOMPATIBLE",
        code => code,
    };
    let message = if code == "APP_INCOMPATIBLE" { "Update VidGrab and the browser extension to compatible versions" }
        else if code == "REQUEST_OUTCOME_UNKNOWN" { "The request may have been accepted; reconnect with the same key to check it" }
        else { "The companion control request could not be completed" };
    json!({"protocol_version":1,"request_id":id,"ok":false,"error":{"code":code,"message":message,
        "request_id":id,"retryable":false,"retry_at":null,"provenance":"local","details":{}}})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(operation: &str, params: Value) -> Value {
        json!({"protocol_version":1,"request_id":"media","operation":operation,
            "params":params,"idempotency_key":"stable-media"})
    }

    #[test]
    fn generated_media_operations_preserve_mutation_and_option_contracts() {
        let cases = [
            ("probes.create", json!({"url":"https://fixture.invalid/media","format_limit":100}), false),
            ("auth.create", json!({"instance_id":"engine","url":"https://fixture.invalid/media",
                "cookies":[{"domain":".fixture.invalid","name":"session","value":"private"}],"ttl_seconds":900}), true),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"retry","auth_ref":"reference"}), true),
            ("downloads.authenticate", json!({"job_id":"job","expected_revision":2,"auth_ref":"reference"}), true),
            ("downloads.enqueue", json!({"url":"https://fixture.invalid/media","auth_ref":"reference",
                "options":{"download_subtitles":true,"subtitle_langs":["en","pt-BR"],"embed_thumbnail":true}}), true),
        ];
        for (operation, params, mutation) in cases {
            let value = request(operation, params);
            assert_eq!(validate_request(&value), Ok(()), "{operation}");
            assert_eq!(is_mutation(&value), mutation, "{operation}");
            if mutation {
                let mut without_key = value;
                without_key.as_object_mut().unwrap().remove("idempotency_key");
                assert_eq!(validate_request(&without_key), Err("BAD_REQUEST"), "{operation}");
            }
        }
    }

    #[test]
    fn generated_media_bounds_reject_unsafe_options_and_unbounded_contexts() {
        let cases = [
            ("probes.create", json!({"url":"https://fixture.invalid/media","format_limit":101})),
            ("probes.create", json!({"url":"https://fixture.invalid/media","entry_limit":129})),
            ("auth.create", json!({"instance_id":"engine","url":"https://fixture.invalid/media",
                "cookies":vec![json!({"domain":".fixture.invalid","name":"session","value":"private"});129]})),
            ("auth.create", json!({"instance_id":"engine","url":"https://fixture.invalid/media",
                "cookies":[{"domain":".fixture.invalid","name":"session","value":"private"}],"headers":{"Authorization":"private"}})),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"execute"})),
            ("downloads.authenticate", json!({"job_id":"job","expected_revision":"2","auth_ref":"reference"})),
            ("downloads.enqueue", json!({"url":"https://fixture.invalid/media","options":{"extra_args":{"exec":"command"}}})),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"set_priority","priority":11})),
            ("downloads.enqueue", json!({"url":"https://fixture.invalid/media","priority":-11})),
        ];
        for (operation, params) in cases {
            assert_eq!(validate_request(&request(operation, params)), Err("BAD_REQUEST"), "{operation}");
        }
    }

    #[test]
    fn generated_p06_manager_operations_validate_schemas_and_mutations() {
        let cases = [
            ("downloads.get", json!({"job_id":"job-1"}), false),
            ("downloads.clear_history", json!({}), true),
            ("diagnostics.get", json!({}), false),
            ("downloads.list", json!({"view":"history","limit":25}), false),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"pause"}), true),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"resume"}), true),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"set_priority","priority":5}), true),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"set_schedule","scheduled_at":"2026-12-01T10:00:00Z"}), true),
            ("downloads.action", json!({"job_id":"job","expected_revision":2,"action":"delete_history"}), true),
            ("downloads.enqueue", json!({"url":"https://fixture.invalid/media","priority":3,"scheduled_at":"2026-12-01T10:00:00+00:00"}), true),
            ("settings.patch", json!({"expected_revision":1,"patch":{"transliterate_bangla_filenames":false}}), true),
        ];
        for (operation, params, mutation) in cases {
            let value = request(operation, params);
            assert_eq!(validate_request(&value), Ok(()), "{operation}");
            assert_eq!(is_mutation(&value), mutation, "{operation}");
        }
    }
}
