//! Provider-neutral execution outcome contract.
//!
//! Classifiers in this module only map signals that are evidenced by an
//! executor protocol. Unknown signals stay unknown rather than being guessed.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::Type;
use ts_rs::TS;

use crate::executors::ExecutorError;

#[derive(Debug, Clone, Type, Serialize, Deserialize, PartialEq, Eq, TS)]
#[sqlx(type_name = "execution_outcome_class", rename_all = "snake_case")]
#[serde(rename_all = "snake_case")]
#[ts(use_ts_enum)]
pub enum ExecutionOutcomeClass {
    QuotaExhausted,
    AuthExpired,
    AuthInvalid,
    ModelUnavailable,
    RateLimitedTransient,
    NetworkTransient,
    UserStopped,
    TaskFailed,
    Unknown,
}

#[derive(Debug, Clone, Type, Serialize, Deserialize, PartialEq, Eq, TS)]
#[sqlx(type_name = "binding_scope", rename_all = "lowercase")]
#[serde(rename_all = "lowercase")]
#[ts(use_ts_enum)]
pub enum BindingScope {
    Account,
    Route,
    Global,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, TS)]
pub struct NormalizedOutcome {
    pub class: ExecutionOutcomeClass,
    pub provider_code: Option<String>,
    pub retry_after_seconds: Option<i64>,
    pub resets_at: Option<DateTime<Utc>>,
    pub binding_scope: Option<BindingScope>,
    pub safe_message: Option<String>,
}

impl NormalizedOutcome {
    pub fn new(class: ExecutionOutcomeClass) -> Self {
        Self {
            class,
            provider_code: None,
            retry_after_seconds: None,
            resets_at: None,
            binding_scope: None,
            safe_message: None,
        }
    }

    pub fn with_provider_code(mut self, provider_code: impl Into<String>) -> Self {
        self.provider_code = Some(provider_code.into());
        self
    }

    pub fn with_binding_scope(mut self, binding_scope: BindingScope) -> Self {
        self.binding_scope = Some(binding_scope);
        self
    }

    pub fn with_safe_message(mut self, safe_message: impl Into<String>) -> Self {
        self.safe_message = Some(safe_message.into());
        self
    }
}

/// Maps the observed `requires_openai_auth && account.is_none()` Codex launch
/// failure in `codex.rs` to a reauthentication requirement.
pub fn classify_codex_executor_error(error: &ExecutorError) -> NormalizedOutcome {
    match error {
        ExecutorError::AuthRequired(_) => {
            NormalizedOutcome::new(ExecutionOutcomeClass::AuthExpired)
                .with_provider_code("codex_account_requires_openai_auth")
                .with_binding_scope(BindingScope::Account)
        }
        _ => NormalizedOutcome::new(ExecutionOutcomeClass::Unknown),
    }
}

/// Claude's stream protocol only establishes that an error result is an error;
/// its subtype vocabulary is not stable here, so preserve it without guessing.
pub fn classify_claude_result(
    subtype: Option<&str>,
    is_error: Option<bool>,
    error: Option<&str>,
) -> Option<NormalizedOutcome> {
    if is_error != Some(true) {
        return None;
    }
    let mut outcome = NormalizedOutcome::new(ExecutionOutcomeClass::Unknown);
    if let Some(subtype) = subtype {
        outcome = outcome.with_provider_code(subtype);
    }
    if let Some(error) = error {
        outcome = outcome.with_safe_message(error);
    }
    Some(outcome)
}

/// Claude emits `type: rate_limit_event`; its payload schema is intentionally
/// not parsed until the protocol fields are evidenced and stable.
pub fn classify_claude_rate_limit_event() -> NormalizedOutcome {
    NormalizedOutcome::new(ExecutionOutcomeClass::RateLimitedTransient)
        .with_provider_code("claude_rate_limit_event")
        .with_safe_message("Claude rate limit reached")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_auth_requirement_is_account_reauthentication() {
        let outcome = classify_codex_executor_error(&ExecutorError::AuthRequired("login".into()));
        assert_eq!(outcome.class, ExecutionOutcomeClass::AuthExpired);
        assert_eq!(outcome.binding_scope, Some(BindingScope::Account));
    }

    #[test]
    fn unknown_codex_error_fails_closed() {
        let outcome = classify_codex_executor_error(&ExecutorError::SetupHelperNotSupported);
        assert_eq!(outcome.class, ExecutionOutcomeClass::Unknown);
    }

    #[test]
    fn claude_error_preserves_observed_fields() {
        let outcome =
            classify_claude_result(Some("error_max_turns"), Some(true), Some("stopped")).unwrap();
        assert_eq!(outcome.class, ExecutionOutcomeClass::Unknown);
        assert_eq!(outcome.provider_code.as_deref(), Some("error_max_turns"));
        assert_eq!(outcome.safe_message.as_deref(), Some("stopped"));
        assert!(classify_claude_result(Some("success"), Some(false), None).is_none());
    }

    #[test]
    fn claude_rate_limit_is_explicitly_transient() {
        assert_eq!(
            classify_claude_rate_limit_event().class,
            ExecutionOutcomeClass::RateLimitedTransient
        );
    }
}
