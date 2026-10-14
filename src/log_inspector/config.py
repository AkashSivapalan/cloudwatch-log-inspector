from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (prefix LOG_INSPECTOR_) or .env."""

    model_config = SettingsConfigDict(
        env_prefix="LOG_INSPECTOR_", env_file=".env", extra="ignore", populate_by_name=True
    )

    app_name: str = "CloudWatch Log Inspector"
    environment: str = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./log_inspector.db"

    # AWS CLI profile holding the service's own credentials. If unset, the standard AWS
    # credential chain is used (AWS_PROFILE, AWS_ACCESS_KEY_ID, or an instance/task role).
    aws_profile: str | None = None
    # Region used for the service's own STS calls.
    aws_region: str = "us-east-1"
    # ARN of the service's own AWS identity, which users put in their role's trust policy.
    # If unset, it is derived from the service's current AWS credentials (sts:GetCallerIdentity).
    inspector_principal_arn: str | None = None
    assume_role_duration_seconds: int = 3600

    # How long a Logs Insights query may run before it is stopped.
    query_timeout_seconds: int = 60
    # Longest time range a single query may cover. Insights is billed by data scanned.
    max_query_range_days: int = 7

    # Analysis (M3)
    analysis_max_events: int = 5000
    analysis_max_clusters: int = 50
    # Secrets (keys, tokens, passwords) are always redacted. These control personal data.
    redact_emails: bool = True
    redact_ip_addresses: bool = True

    # Diagnosis (M4). The key is read from ANTHROPIC_API_KEY (or LOG_INSPECTOR_ANTHROPIC_API_KEY),
    # in the environment or .env. If unset, the Anthropic SDK's own credential lookup is used.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LOG_INSPECTOR_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    diagnosis_model: str = "claude-opus-5"
    diagnosis_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    diagnosis_max_tokens: int = 32000
    # The most time the whole Claude step may take. The response is streamed, and a stream that
    # keeps producing text never trips an HTTP timeout, so this is enforced as a wall-clock limit.
    diagnosis_timeout_seconds: float = 480
    # How long Claude may go silent (no data at all) before the request is given up on.
    diagnosis_idle_timeout_seconds: float = 90
    # Retries for a request that fails before any text arrives (connection errors, 429, 5xx).
    # Each retry can take up to the idle timeout, so keep this low.
    diagnosis_max_retries: int = 1
    # If Claude declines a request, re-run it on Anthropic's recommended fallback model.
    diagnosis_fallbacks: bool = True
    # Upper bound on the log summary sent to Claude; lower-priority patterns are left out.
    diagnosis_max_prompt_chars: int = 120_000

    # Investigations (M5): how many run at once. Each holds Insights queries and a Claude call.
    investigation_workers: int = 2
    # If set, investigations are sent to this SQS queue for a separate worker (AWS Lambda)
    # instead of the in-process thread pool.
    investigation_queue_url: str | None = None
    # Investigations still unfinished this long after being requested are marked failed.
    investigation_timeout_seconds: int = 900
    # Single-process mode: at startup, fail every unfinished investigation (none can be
    # running). With several instances (Lambda), turn this off; timeouts apply instead.
    recover_interrupted_on_startup: bool = True
    # Turn off where migrations run as a separate deploy step (Lambda: the admin function).
    run_migrations_on_startup: bool = True

    # Security and cost guards (M6). Limits are per user.
    rate_limits_enabled: bool = True
    rate_limit_per_minute: int = 120
    # Endpoints that run Insights queries or call Claude: query, analyze, diagnose,
    # and starting investigations.
    expensive_rate_limit_per_hour: int = 30
    max_active_investigations: int = 3

    # Browser origins allowed to call the API (the frontend). Comma-separated in the environment
    # variable or a JSON list, e.g. LOG_INSPECTOR_CORS_ORIGINS=http://localhost:4200
    cors_origins: Annotated[list[str], NoDecode] = []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value

    # Amazon Cognito login (the frontend). If both are set, the API also accepts Cognito access
    # tokens from this user pool and app client; API keys keep working either way.
    cognito_user_pool_id: str | None = None
    cognito_client_id: str | None = None

    # "json" writes one JSON object per log line, for log aggregation in production.
    log_format: Literal["text", "json"] = "text"


@lru_cache
def get_settings() -> Settings:
    return Settings()
