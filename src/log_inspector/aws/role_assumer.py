import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

from log_inspector.aws.errors import RoleAssumptionFailed, ServiceCredentialsMissing
from log_inspector.aws.policies import caller_arn_to_principal

logger = logging.getLogger(__name__)

# Refresh cached credentials this long before they expire.
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class TemporaryCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime


class RoleAssumer:
    """Assumes users' IAM roles using the service's own AWS identity, caching credentials."""

    def __init__(
        self,
        region: str,
        duration_seconds: int = 3600,
        principal_arn_override: str | None = None,
        profile_name: str | None = None,
    ) -> None:
        self._region = region
        self._duration_seconds = duration_seconds
        self._principal_arn = principal_arn_override
        self._profile_name = profile_name
        self._cache: dict[tuple[str, str], TemporaryCredentials] = {}
        self._lock = threading.Lock()

    def _sts(self):
        try:
            session = boto3.Session(profile_name=self._profile_name)
        except ProfileNotFound as exc:
            raise ServiceCredentialsMissing(
                f"AWS profile '{self._profile_name}' was not found. Create it with "
                f"'aws configure --profile {self._profile_name}' or change "
                "LOG_INSPECTOR_AWS_PROFILE."
            ) from exc
        return session.client("sts", region_name=self._region)

    def inspector_principal_arn(self) -> str:
        """ARN that users must trust in their role's trust policy."""
        if self._principal_arn is None:
            try:
                caller_arn = self._sts().get_caller_identity()["Arn"]
            except NoCredentialsError as exc:
                raise ServiceCredentialsMissing(
                    "The service has no AWS credentials configured. Set AWS_PROFILE or "
                    "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or set "
                    "LOG_INSPECTOR_INSPECTOR_PRINCIPAL_ARN."
                ) from exc
            self._principal_arn = caller_arn_to_principal(caller_arn)
        return self._principal_arn

    def session_for(
        self, role_arn: str, external_id: str, session_name: str, region: str
    ) -> boto3.Session:
        """Return a boto3 session acting as the given role."""
        creds = self._credentials(role_arn, external_id, session_name)
        return boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            aws_session_token=creds.session_token,
            region_name=region,
        )

    def invalidate(self, role_arn: str, external_id: str) -> None:
        with self._lock:
            self._cache.pop((role_arn, external_id), None)

    def _credentials(
        self, role_arn: str, external_id: str, session_name: str
    ) -> TemporaryCredentials:
        key = (role_arn, external_id)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expiration - REFRESH_MARGIN > datetime.now(UTC):
                return cached

            creds = self._assume_role(role_arn, external_id, session_name)
            self._cache[key] = creds
            return creds

    def _assume_role(
        self, role_arn: str, external_id: str, session_name: str
    ) -> TemporaryCredentials:
        try:
            response = self._sts().assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                ExternalId=external_id,
                DurationSeconds=self._duration_seconds,
            )
        except NoCredentialsError as exc:
            raise ServiceCredentialsMissing(
                "The service has no AWS credentials configured, so it cannot assume roles."
            ) from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            logger.info("AssumeRole failed for %s: %s", role_arn, code)
            if code == "AccessDenied":
                raise RoleAssumptionFailed(
                    "Access denied when assuming the role. Check that the role's trust policy "
                    "names the inspector principal and the exact external ID shown in the setup "
                    "instructions, and that the role ARN is correct."
                ) from exc
            raise RoleAssumptionFailed(f"Could not assume the role ({code}).") from exc
        except BotoCoreError as exc:
            raise RoleAssumptionFailed(f"Could not assume the role: {exc}") from exc

        c = response["Credentials"]
        expiration = c["Expiration"]
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=UTC)
        return TemporaryCredentials(
            access_key_id=c["AccessKeyId"],
            secret_access_key=c["SecretAccessKey"],
            session_token=c["SessionToken"],
            expiration=expiration,
        )
