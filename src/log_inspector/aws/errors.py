from botocore.exceptions import ClientError


class AwsError(Exception):
    """Base class for AWS errors surfaced to API users, with the HTTP status to return."""

    status_code = 502


class ServiceCredentialsMissing(AwsError):
    """The service itself has no usable AWS credentials, so it cannot call STS."""

    status_code = 503


class RoleAssumptionFailed(AwsError):
    """sts:AssumeRole on the user's role was rejected."""

    status_code = 403


class LogsAccessDenied(AwsError):
    """The role was assumed, but it lacks the CloudWatch Logs permissions we need."""

    status_code = 403


class LogGroupNotFound(AwsError):
    status_code = 404


class InvalidQuery(AwsError):
    """The Logs Insights query or its parameters were rejected."""

    status_code = 400


class QueryLimitExceeded(AwsError):
    """Too many concurrent Logs Insights queries in the user's account."""

    status_code = 429


class QueryTimedOut(AwsError):
    status_code = 504


class QueryFailed(AwsError):
    """Logs Insights ended the query as Failed, Cancelled or Timeout."""


class AwsRequestFailed(AwsError):
    """Any other AWS error."""


def translate_logs_error(exc: ClientError, action: str) -> AwsError:
    """Map a CloudWatch Logs ClientError to an error with a user-facing explanation."""
    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    message = error.get("Message", "")

    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return LogsAccessDenied(
            f"The role is not allowed to call logs:{action}. Attach the permissions policy from "
            "the setup instructions to the role."
        )
    if code == "ResourceNotFoundException":
        return LogGroupNotFound(f"Log group not found: {message}")
    if code in ("MalformedQueryException", "InvalidParameterException"):
        return InvalidQuery(f"CloudWatch rejected the query: {message}")
    if code == "LimitExceededException":
        return QueryLimitExceeded(
            "Too many Logs Insights queries are running in this account. Try again shortly."
        )
    return AwsRequestFailed(f"CloudWatch Logs {action} failed ({code}): {message}")
