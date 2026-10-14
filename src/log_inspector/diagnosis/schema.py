"""The diagnosis Claude returns, and the JSON schema that constrains its output."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Evidence(BaseModel):
    pattern_id: str = Field(description="ID of the log pattern this comes from, e.g. 'P1'.")
    log_excerpt: str = Field(description="Text quoted verbatim from that pattern's examples.")
    relevance: str = Field(description="Why this line supports the root cause.")


class RootCause(BaseModel):
    title: str
    explanation: str
    confidence: Confidence
    evidence: list[Evidence]
    remediation: list[str]


class FollowUpQuery(BaseModel):
    purpose: str
    query: str = Field(description="A CloudWatch Logs Insights query.")


class Diagnosis(BaseModel):
    summary: str
    severity: Severity
    root_causes: list[RootCause] = Field(description="Most likely first.")
    affected_components: list[str]
    follow_up_queries: list[FollowUpQuery]
    open_questions: list[str]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema, made self-contained and strict for structured outputs.

    Inlines $refs, drops titles, and requires every property with no extras allowed.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(definitions[node["$ref"].split("/")[-1]])
        # Drop Pydantic's "title" annotations, but keep properties that happen to be named
        # "title" (like RootCause.title): "properties" maps field names, not keywords.
        node = {
            key: (
                {name: convert(prop) for name, prop in value.items()}
                if key == "properties"
                else convert(value)
            )
            for key, value in node.items()
            if key != "title"
        }
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        return node

    return convert(schema)


DIAGNOSIS_SCHEMA = strict_json_schema(Diagnosis)
