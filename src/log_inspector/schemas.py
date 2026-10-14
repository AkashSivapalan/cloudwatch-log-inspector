from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from log_inspector.aws.canned_queries import CANNED_QUERIES
from log_inspector.aws.policies import ROLE_ARN_PATTERN
from log_inspector.models import ConnectionStatus

AWS_REGION_PATTERN = r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$"


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["production"])
    region: str = Field(default="us-east-1", pattern=AWS_REGION_PATTERN, examples=["us-east-1"])


class RoleUpdate(BaseModel):
    role_arn: str = Field(examples=["arn:aws:iam::123456789012:role/LogInspectorReadOnly"])

    @field_validator("role_arn")
    @classmethod
    def _valid_role_arn(cls, value: str) -> str:
        # Checked with Python's re: the pattern is too large for pydantic's Rust regex engine.
        if not ROLE_ARN_PATTERN.match(value):
            raise ValueError("must be an IAM role ARN, e.g. arn:aws:iam::123456789012:role/Name")
        return value


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    region: str
    external_id: str
    role_arn: str | None
    status: ConnectionStatus
    last_error: str | None
    last_verified_at: datetime | None
    created_at: datetime


class SetupInstructions(BaseModel):
    inspector_principal_arn: str
    external_id: str
    trust_policy: dict
    permissions_policy: dict
    steps: list[str]


class ConnectionCreated(BaseModel):
    connection: ConnectionOut
    setup: SetupInstructions


class VerificationOut(BaseModel):
    verified: bool
    status: ConnectionStatus
    account_id: str | None
    sample_log_groups: list[str]
    error: str | None


class LogGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    arn: str | None
    stored_bytes: int | None
    retention_days: int | None
    created_at: datetime | None


class LogGroupPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_groups: list[LogGroupOut]
    next_token: str | None = Field(description="Pass as next_token to fetch the next page.")


class CannedQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    query: str


LogGroupName = Annotated[str, Field(min_length=1, max_length=512)]


class QueryRequest(BaseModel):
    log_groups: list[LogGroupName] = Field(
        min_length=1, max_length=50, examples=[["/aws/lambda/checkout"]]
    )
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=10000,
        description="A Logs Insights query. Provide this or canned_query.",
        examples=["fields @timestamp, @message | sort @timestamp desc | limit 20"],
    )
    canned_query: str | None = Field(
        default=None,
        description="Name of a ready-made query from GET /canned-queries.",
        examples=["errors"],
    )
    start_time: datetime | None = Field(
        default=None, description="Defaults to one hour before end_time. UTC if no timezone."
    )
    end_time: datetime | None = Field(default=None, description="Defaults to now.")
    limit: int = Field(
        default=1000, ge=1, le=10000, description="Max rows, unless the query has a limit."
    )

    @model_validator(mode="after")
    def _exactly_one_query(self) -> "QueryRequest":
        if (self.query is None) == (self.canned_query is None):
            raise ValueError("Provide exactly one of 'query' or 'canned_query'.")
        if self.canned_query is not None and self.canned_query not in CANNED_QUERIES:
            options = ", ".join(CANNED_QUERIES)
            raise ValueError(f"Unknown canned_query '{self.canned_query}'. Options: {options}.")
        return self


class QueryStatistics(BaseModel):
    records_matched: float
    records_scanned: float
    bytes_scanned: float


class QueryResponse(BaseModel):
    query_id: str
    query: str
    log_groups: list[str]
    start_time: datetime
    end_time: datetime
    row_count: int
    rows: list[dict[str, str]]
    statistics: QueryStatistics


class AnalyzeRequest(BaseModel):
    log_groups: list[LogGroupName] = Field(
        min_length=1, max_length=50, examples=[["/log-inspector/demo"]]
    )
    start_time: datetime | None = Field(
        default=None, description="Defaults to one hour before end_time. UTC if no timezone."
    )
    end_time: datetime | None = Field(default=None, description="Defaults to now.")
    max_events: int | None = Field(
        default=None,
        ge=1,
        le=10000,
        description="Most recent error/warning lines to cluster. Defaults to the server setting.",
    )


class StackTraceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exception_type: str
    message: str
    origin: str | None
    frames: list[str]


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    template: str
    level: str
    count: int
    first_seen: datetime
    last_seen: datetime
    log_groups: list[str]
    examples: list[str]
    stack_trace: StackTraceOut | None


class SpikeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start: datetime
    end: datetime
    error_count: int
    peak_errors: int
    baseline_errors: float


class TimelineEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    time: datetime
    kind: str
    description: str


class VolumeBucketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start: datetime
    errors: int
    total: int | None


class AnalysisOut(BaseModel):
    log_groups: list[str]
    start_time: datetime
    end_time: datetime
    bucket_seconds: int
    total_events: int | None = Field(description="All log lines in the range.")
    total_errors: int = Field(description="Lines matching the error pattern in the range.")
    events_analyzed: int = Field(description="Error/warning lines fetched and clustered.")
    events_truncated: bool = Field(
        description="True if more lines matched than max_events; only the newest were analyzed."
    )
    cluster_count: int
    clusters: list[ClusterOut]
    spikes: list[SpikeOut]
    timeline: list[TimelineEntryOut]
    volume: list[VolumeBucketOut]
    redactions: dict[str, int] = Field(description="How many values of each kind were redacted.")


class DiagnoseRequest(AnalyzeRequest):
    question: str | None = Field(
        default=None,
        max_length=2000,
        description="What you want to know. Defaults to asking what went wrong and why.",
        examples=["Why did checkout start failing?"],
    )
    include_analysis: bool = Field(
        default=False, description="Also return the full M3 analysis the diagnosis was based on."
    )


class EvidenceOut(BaseModel):
    pattern_id: str
    log_excerpt: str
    relevance: str
    verified: bool = Field(
        description="True if the excerpt was found in that pattern's log lines. False means "
        "Claude cited something it wasn't shown; treat that evidence with suspicion."
    )


class RootCauseOut(BaseModel):
    title: str
    explanation: str
    confidence: str
    evidence: list[EvidenceOut]
    remediation: list[str]


class FollowUpQueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purpose: str
    query: str


class DiagnosisOut(BaseModel):
    summary: str
    severity: str
    root_causes: list[RootCauseOut]
    affected_components: list[str]
    follow_up_queries: list[FollowUpQueryOut]
    open_questions: list[str]


class PatternRefOut(BaseModel):
    id: str
    level: str
    count: int
    template: str


class DiagnosisUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class DiagnoseResponse(BaseModel):
    question: str
    log_groups: list[str]
    start_time: datetime
    end_time: datetime
    diagnosis: DiagnosisOut
    patterns: list[PatternRefOut] = Field(description="The patterns Claude was shown, by ID.")
    patterns_omitted: int = Field(description="Patterns left out to fit the prompt size limit.")
    model: str | None = Field(description="The model that wrote the diagnosis.")
    fallback_used: bool
    llm_skipped: bool = Field(
        description="True when no errors or warnings were found, so Claude wasn't called."
    )
    usage: DiagnosisUsageOut
    analysis: AnalysisOut | None = None


class InvestigationCreate(AnalyzeRequest):
    connection_id: str
    question: str | None = Field(
        default=None,
        max_length=2000,
        description="What you want to know. Defaults to asking what went wrong and why.",
        examples=["Why did checkout fail?"],
    )


class InvestigationOut(BaseModel):
    id: str
    connection_id: str
    status: str = Field(description="queued, querying, analyzing, diagnosing, then done or failed.")
    finished: bool
    question: str | None
    log_groups: list[str]
    start_time: datetime
    end_time: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None = Field(description="From start to finish, once finished.")
    error: str | None
    error_status: int | None = Field(
        description="The HTTP status this failure would have had on a synchronous call."
    )
    result: DiagnoseResponse | None = Field(description="The diagnosis, once status is done.")


class InvestigationSummaryOut(BaseModel):
    id: str
    connection_id: str
    status: str
    question: str | None
    log_groups: list[str]
    created_at: datetime
    finished_at: datetime | None
    severity: str | None = Field(description="From the diagnosis, once done.")
    summary: str | None = Field(description="From the diagnosis, once done.")
    error: str | None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str = Field(description="The public part of the key, to tell keys apart.")
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["postman"])


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The API key. It is shown only once: store it now.")
