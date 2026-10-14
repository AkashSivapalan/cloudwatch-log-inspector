"""Investigations: diagnoses that run in the background and are kept as history."""

import json
import logging
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from log_inspector.aws.errors import AwsError
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings
from log_inspector.diagnosis.diagnoser import Diagnoser, DiagnosisError
from log_inspector.models import (
    Connection,
    ConnectionStatus,
    Investigation,
    InvestigationStatus,
    User,
)
from log_inspector.presenters import diagnose_response
from log_inspector.services.diagnosis import diagnose_logs
from log_inspector.services.logs import ConnectionNotReady, TimeRange

logger = logging.getLogger(__name__)

_UNFINISHED = [
    InvestigationStatus.QUEUED,
    InvestigationStatus.QUERYING,
    InvestigationStatus.ANALYZING,
    InvestigationStatus.DIAGNOSING,
]


class InvestigationNotFound(Exception):
    pass


class TooManyActiveInvestigations(Exception):
    pass


def _owned_by(owner: User):
    return Investigation.connection.has(Connection.owner_id == owner.id)


def create_investigation(
    db: Session,
    connection: Connection,
    question: str | None,
    log_groups: list[str],
    time_range: TimeRange,
    max_events: int,
    max_active: int,
) -> Investigation:
    """connection must already belong to the requesting user (see get_connection)."""
    if connection.status != ConnectionStatus.VERIFIED:
        raise ConnectionNotReady(
            f"Connection is '{connection.status}'. Verify it with "
            f"POST /connections/{connection.id}/verify before investigating."
        )
    active = db.scalar(
        select(func.count())
        .select_from(Investigation)
        .join(Connection)
        .where(Connection.owner_id == connection.owner_id, Investigation.status.in_(_UNFINISHED))
    )
    if active >= max_active:
        raise TooManyActiveInvestigations(
            f"You already have {active} investigations running (the limit is {max_active}). "
            "Wait for one to finish."
        )
    investigation = Investigation(
        connection_id=connection.id,
        question=question,
        log_groups=log_groups,
        start_time=time_range.start,
        end_time=time_range.end,
        max_events=max_events,
    )
    db.add(investigation)
    db.commit()
    db.refresh(investigation)
    return investigation


def get_investigation(db: Session, investigation_id: str, owner: User) -> Investigation:
    investigation = db.scalar(
        select(Investigation).where(Investigation.id == investigation_id, _owned_by(owner))
    )
    if investigation is None:
        raise InvestigationNotFound(investigation_id)
    return investigation


def list_investigations(
    db: Session,
    owner: User,
    connection_id: str | None,
    status: InvestigationStatus | None,
    limit: int,
) -> list[Investigation]:
    query = (
        select(Investigation)
        .where(_owned_by(owner))
        .order_by(Investigation.created_at.desc())
        .limit(limit)
    )
    if connection_id:
        query = query.where(Investigation.connection_id == connection_id)
    if status:
        query = query.where(Investigation.status == status)
    return list(db.scalars(query))


def fail_interrupted(db: Session) -> int:
    """Mark investigations left unfinished by a previous server process as failed."""
    result = db.execute(
        update(Investigation)
        .where(Investigation.status.in_(_UNFINISHED))
        .values(
            status=InvestigationStatus.FAILED,
            error="Interrupted: the server restarted before this investigation finished. "
            "Start a new one.",
            error_status=503,
            finished_at=datetime.now(UTC),
        )
    )
    db.commit()
    return result.rowcount


def _last_sign_of_life():
    """When an investigation last had a reason to be alive: when it started running, or, if it
    never did, when it was requested. A run that waited in the queue is not stale just because
    it was requested long ago; one that started more than `timeout` ago certainly is, since
    nothing runs longer than that."""
    return func.coalesce(Investigation.started_at, Investigation.created_at)


def is_stale(investigation: Investigation, timeout: timedelta) -> bool:
    """Whether an unfinished investigation has outlived `timeout` (see fail_stale)."""
    if investigation.status.finished:
        return False
    since = investigation.started_at or investigation.created_at
    return datetime.now(UTC) - since > timeout


def fail_stale(db: Session, timeout: timedelta, owner: User | None = None) -> int:
    """Mark investigations unfinished for longer than timeout as failed.

    Used where several instances run at once (Lambda), so no instance can assume that
    unfinished work belongs to a dead process. A worker killed from outside records nothing,
    so this is how such investigations get their failure. With `owner`, only that user's.
    """
    cutoff = datetime.now(UTC) - timeout
    query = update(Investigation).where(
        Investigation.status.in_(_UNFINISHED), _last_sign_of_life() < cutoff
    )
    if owner is not None:
        query = query.where(_owned_by(owner))
    result = db.execute(
        query.execution_options(synchronize_session=False).values(
            status=InvestigationStatus.FAILED,
            error=f"Timed out: not finished within {int(timeout.total_seconds())} seconds. "
            "Start a new one.",
            error_status=504,
            finished_at=datetime.now(UTC),
        )
    )
    db.commit()
    return result.rowcount


class InvestigationRunner:
    """Runs investigations, recording progress in the database.

    By default on a small thread pool in this process; with investigation_queue_url set,
    by sending them to SQS for a worker (see lambda_handlers.worker).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        assumer: RoleAssumer,
        diagnoser: Diagnoser,
        settings: Settings,
        executor: Executor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._assumer = assumer
        self.diagnoser = diagnoser
        self._settings = settings
        self._executor = executor or ThreadPoolExecutor(
            max_workers=settings.investigation_workers, thread_name_prefix="investigation"
        )

    def submit(self, investigation_id: str) -> None:
        queue_url = self._settings.investigation_queue_url
        if not queue_url:
            self._executor.submit(self.run, investigation_id)
            return
        try:
            sqs = boto3.Session(profile_name=self._settings.aws_profile).client(
                "sqs", region_name=self._settings.aws_region
            )
            sqs.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps({"investigation_id": investigation_id})
            )
        except (BotoCoreError, ClientError):
            logger.exception("Could not queue investigation %s", investigation_id)
            with self._session_factory() as db:
                investigation = db.get(Investigation, investigation_id)
                self._fail(investigation, 503, "Could not queue the investigation. Try again.")
                investigation.finished_at = datetime.now(UTC)
                db.commit()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def run(self, investigation_id: str, deadline: float | None = None) -> None:
        """Run one investigation to the end, always recording success or failure.

        deadline (a time.monotonic() value) is when the caller will be stopped from outside,
        e.g. a Lambda running out of time. The Claude step is cut short before it so the
        investigation ends as failed with a reason, not left half-finished.
        """
        with self._session_factory() as db:
            investigation = db.get(Investigation, investigation_id)
            if investigation is None or investigation.status != InvestigationStatus.QUEUED:
                return

            def progress(status: str) -> None:
                investigation.status = InvestigationStatus(status)
                db.commit()

            investigation.started_at = datetime.now(UTC)
            progress(InvestigationStatus.QUERYING)
            try:
                report = diagnose_logs(
                    investigation.connection,
                    self._assumer,
                    self.diagnoser,
                    question=investigation.question,
                    log_groups=investigation.log_groups,
                    time_range=TimeRange(investigation.start_time, investigation.end_time),
                    max_events=investigation.max_events,
                    settings=self._settings,
                    on_progress=progress,
                    deadline=deadline,
                )
                response = diagnose_response(investigation.question, report, include_analysis=True)
                investigation.result = response.model_dump(mode="json")
                investigation.status = InvestigationStatus.DONE
            except (AwsError, DiagnosisError) as exc:
                self._fail(investigation, exc.status_code, str(exc))
            except ConnectionNotReady as exc:
                self._fail(investigation, 409, str(exc))
            except Exception:
                logger.exception("Investigation %s failed unexpectedly", investigation_id)
                self._fail(investigation, 500, "Unexpected error. See the server log for details.")
            finally:
                investigation.finished_at = datetime.now(UTC)
                db.commit()

    @staticmethod
    def _fail(investigation: Investigation, status: int, message: str) -> None:
        investigation.status = InvestigationStatus.FAILED
        investigation.error = message
        investigation.error_status = status
