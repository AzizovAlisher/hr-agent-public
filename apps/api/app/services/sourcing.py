"""Idempotent sourcing intake shared by authenticated imports and webhooks."""

import hashlib
import json
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationConnection
from app.models.position import Position
from app.models.sourcing import (
    SourceCandidateReceipt,
    SourceImportRun,
    SourceIntegration,
)
from app.schemas.sourcing import (
    SourceCandidateInput,
    SourceCapabilitiesOut,
    SourceCapabilityState,
)
from app.services.activities import append_candidate_activity
from app.services.applications import ensure_application_link
from app.services.candidate_identity import (
    attach_candidate_identity,
    normalize_email,
    normalize_phone,
    resolve_candidate_identity,
)
from app.services.entitlements import record_usage
from app.services.uploads import candidate_from_text


class SourceImportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


SOURCE_CONNECTION_PROVIDERS: dict[str, str] = {
    "hh": "hh_ru",
    "telegram": "telegram",
    "somon": "somon",
    "csv": "csv_xlsx",
    "generic_webhook": "generic_webhook",
}

SOURCE_PROVIDER_CAPABILITIES: dict[str, frozenset[str]] = {
    "hh": frozenset({"candidate.import", "vacancy.publish"}),
    "telegram": frozenset({"vacancy.publish"}),
    "somon": frozenset({"tracked_link.create", "file.import"}),
    "csv": frozenset({"file.import"}),
    "generic_webhook": frozenset({"webhook.receive"}),
}


def validate_source_connection(
    provider: str,
    connection: IntegrationConnection,
    *,
    organization_id: UUID,
) -> None:
    """Fail closed when a source is bound to an unrelated tenant/provider."""

    expected = SOURCE_CONNECTION_PROVIDERS.get(provider)
    if expected is None:
        raise SourceImportError("source_provider_unsupported")
    if connection.organization_id != organization_id:
        raise SourceImportError("connection_not_found")
    if connection.provider != expected:
        raise SourceImportError("connection_provider_mismatch")
    if connection.status != "connected":
        raise SourceImportError("connection_not_ready")
    required = SOURCE_PROVIDER_CAPABILITIES.get(provider, frozenset())
    if not required.issubset(set(connection.capabilities or [])):
        raise SourceImportError("connection_capability_mismatch")


def source_connection_ready(
    provider: str,
    connection: IntegrationConnection | None,
    *,
    required_capability: str | None = None,
) -> bool:
    if connection is None or connection.status != "connected":
        return False
    if connection.provider != SOURCE_CONNECTION_PROVIDERS.get(provider):
        return False
    if required_capability is None:
        return True
    return required_capability in set(connection.capabilities or [])


def new_source_webhook_secret() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_source_webhook_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def source_capabilities(
    provider: str,
    configuration: dict | None = None,
    connection: IntegrationConnection | None = None,
) -> SourceCapabilitiesOut:
    """Describe exactly what this build can do for a provider.

    hh.ru remains credential-gated through the unified provider connection.
    Somon supports tracked links and validated files only; this service never
    scrapes the marketplace. Telegram sandbox publication is explicitly local.
    """

    sandbox = (configuration or {}).get("mode") == "sandbox"
    candidate_import = "unsupported"
    file_import = "unsupported"
    webhook_intake = "unsupported"
    vacancy_publish = "unsupported"
    supported = SOURCE_PROVIDER_CAPABILITIES.get(provider, frozenset())

    def state(capability: str) -> SourceCapabilityState:
        if capability not in supported:
            return "unsupported"
        if sandbox:
            return "sandbox"
        if source_connection_ready(
            provider, connection, required_capability=capability
        ):
            return "available"
        return "credentials_not_configured"

    if provider == "csv":
        # Spreadsheet rows pass through the same canonical candidate ingestion
        # path; the provider catalog advertises the external-facing file action.
        file_import = state("file.import")
        candidate_import = file_import
    elif provider == "generic_webhook":
        webhook_intake = state("webhook.receive")
        candidate_import = webhook_intake
    elif provider == "somon":
        file_import = state("file.import")
        candidate_import = file_import
        vacancy_publish = state("tracked_link.create")
    elif provider == "hh":
        candidate_import = state("candidate.import")
        vacancy_publish = state("vacancy.publish")
    elif provider == "telegram":
        vacancy_publish = state("vacancy.publish")
    return SourceCapabilitiesOut(
        candidate_import=candidate_import,
        file_import=file_import,
        webhook_intake=webhook_intake,
        vacancy_publish=vacancy_publish,
    )


def source_integration_status(
    provider: str,
    configuration: dict | None = None,
    *,
    is_active: bool = True,
    connection: IntegrationConnection | None = None,
) -> str:
    if not is_active:
        return "disabled"
    if (configuration or {}).get("mode") == "sandbox":
        return "ready"
    if source_connection_ready(provider, connection):
        return "ready"
    if connection is not None and connection.status in {
        "degraded",
        "expired",
        "revoked",
        "unavailable",
    }:
        return "error"
    return "credentials_not_configured"


async def ingest_source_candidates(
    db: AsyncSession,
    *,
    integration: SourceIntegration,
    items: list[SourceCandidateInput],
    actor_user_id: UUID | None,
    idempotency_key: str | None = None,
    request_fingerprint_override: str | None = None,
    commit: bool = True,
) -> SourceImportRun:
    """Ingest a bounded batch and attach every accepted row to its vacancy.

    Receipt IDs make provider retries idempotent. Contact matches reuse the
    canonical candidate and add a missing application instead of multiplying
    people in the CRM.
    """

    now = datetime.now(UTC)
    candidate_fingerprint = hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in items],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    if request_fingerprint_override is not None and (
        len(request_fingerprint_override) != 64
        or any(
            character not in "0123456789abcdef"
            for character in request_fingerprint_override
        )
    ):
        raise SourceImportError("request_fingerprint_invalid")
    # Signed webhook callers fingerprint the complete versioned envelope so
    # one event ID cannot be replayed with altered metadata or candidate data.
    request_fingerprint = request_fingerprint_override or candidate_fingerprint
    idempotency_key = idempotency_key or f"auto:{request_fingerprint}"
    # Serialize receipts per connector so concurrent provider retries cannot
    # create two candidates before the unique external-id constraint is seen.
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"source:{integration.id}", 0)
            )
        )
    )
    existing_run = (
        await db.execute(
            select(SourceImportRun).where(
                SourceImportRun.organization_id == integration.organization_id,
                SourceImportRun.integration_id == integration.id,
                SourceImportRun.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing_run is not None:
        if existing_run.request_fingerprint != request_fingerprint:
            raise SourceImportError("idempotency_conflict")
        existing_run._idempotent_replay = True
        return existing_run
    run_id = uuid4()
    await record_usage(
        db,
        organization_id=integration.organization_id,
        metric="candidate_import_rows",
        amount=len(items),
        kind="usage",
        idempotency_key=(
            "candidate-import:source:"
            + hashlib.sha256(f"{integration.id}:{idempotency_key}".encode()).hexdigest()
        ),
        resource_type="source_import",
        resource_id=run_id,
        metadata={"provider": integration.provider},
    )
    run = SourceImportRun(
        id=run_id,
        organization_id=integration.organization_id,
        integration_id=integration.id,
        started_by_user_id=actor_user_id,
        provider_snapshot=integration.provider,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        status="processing",
        requested_count=len(items),
        created_count=0,
        duplicate_count=0,
        failed_count=0,
        started_at=now,
    )
    db.add(run)
    run._idempotent_replay = False
    await db.flush()

    position_ids = list({item.position_id for item in items})
    positions = list(
        (
            await db.execute(
                select(Position).where(
                    Position.organization_id == integration.organization_id,
                    Position.id.in_(position_ids),
                )
            )
        ).scalars()
    )
    positions_by_id = {position.id: position for position in positions}

    external_ids = [item.external_id for item in items]
    existing_receipts = {
        external_id: candidate_id
        for external_id, candidate_id in (
            await db.execute(
                select(
                    SourceCandidateReceipt.external_id,
                    SourceCandidateReceipt.candidate_id,
                ).where(
                    SourceCandidateReceipt.integration_id == integration.id,
                    SourceCandidateReceipt.external_id.in_(external_ids),
                )
            )
        ).all()
    }

    seen_external: set[str] = set()
    for item in items:
        if item.external_id in existing_receipts or item.external_id in seen_external:
            run.duplicate_count += 1
            continue
        seen_external.add(item.external_id)
        position = positions_by_id.get(item.position_id)
        if position is None:
            run.failed_count += 1
            continue
        phone = normalize_phone(item.phone)
        email = normalize_email(item.email)
        identity = await resolve_candidate_identity(
            db,
            integration.organization_id,
            email=email,
            phone=phone,
        )
        if identity.conflict:
            run.failed_count += 1
            continue
        candidate = identity.candidate
        candidate_created = candidate is None
        if candidate is None:
            source_ref = item.source_url or item.external_id
            candidate = candidate_from_text(item.cv_text, source_ref=source_ref)
            candidate.id = candidate.id or uuid4()
            candidate.organization_id = integration.organization_id
            candidate.source = integration.provider
            candidate.phone_normalized = phone
            candidate.email_normalized = email
            candidate.parsed_cv = {
                "applicant_name": item.name,
                "applied_via": integration.provider,
            }
            if item.utm_campaign is not None:
                candidate.parsed_cv["utm_campaign"] = item.utm_campaign
            await record_usage(
                db,
                organization_id=integration.organization_id,
                metric="candidates",
                amount=1,
                kind="usage",
                idempotency_key=f"candidate:source:{candidate.id}",
                resource_type="candidate",
                resource_id=candidate.id,
                metadata={"provider": integration.provider},
            )
            db.add(candidate)
            await db.flush()
            await attach_candidate_identity(
                db,
                candidate,
                email=email,
                phone=phone,
                source=integration.provider,
                actor_user_id=actor_user_id,
            )
        else:
            await attach_candidate_identity(
                db,
                candidate,
                email=email,
                phone=phone,
                source=integration.provider,
                actor_user_id=actor_user_id,
            )
        application_link = await ensure_application_link(
            db,
            organization_id=integration.organization_id,
            candidate_id=candidate.id,
            position_id=position.id,
            source=integration.provider,
            owner_user_id=position.created_by,
            actor_user_id=actor_user_id,
            actor_kind="human" if actor_user_id is not None else "automation",
            reason="source_import_received",
            external_ref=item.external_id,
            metadata={"campaign": item.utm_campaign},
            occurred_at=now,
        )
        if application_link.new_pair and not candidate_created:
            append_candidate_activity(
                db,
                organization_id=integration.organization_id,
                candidate_id=candidate.id,
                actor_user_id=actor_user_id,
                application_id=application_link.application.id,
                position_id=position.id,
                event_type="candidate_sourced",
                subject_type="application",
                subject_id=application_link.application.id,
                payload={
                    "source": integration.provider,
                    "position_title": position.title,
                },
                occurred_at=now,
            )
        elif application_link.new_pair:
            append_candidate_activity(
                db,
                organization_id=integration.organization_id,
                candidate_id=candidate.id,
                actor_user_id=actor_user_id,
                application_id=application_link.application.id,
                position_id=position.id,
                event_type="candidate_added",
                subject_type="candidate",
                subject_id=candidate.id,
                payload={
                    "source": integration.provider,
                    "position_title": position.title,
                },
                occurred_at=now,
            )
        receipt = SourceCandidateReceipt(
            organization_id=integration.organization_id,
            integration_id=integration.id,
            import_run_id=run.id,
            candidate_id=candidate.id,
            position_id=position.id,
            application_id=application_link.application.id,
            external_id=item.external_id,
            source_url=item.source_url,
            provider_snapshot=integration.provider,
            received_at=now,
        )
        db.add(receipt)
        if candidate_created:
            run.created_count += 1
        else:
            run.duplicate_count += 1

    if run.failed_count == len(items):
        run.status = "failed"
        run.error_code = "no_valid_rows"
    elif run.failed_count:
        run.status = "partial"
    else:
        run.status = "completed"
    run.completed_at = datetime.now(UTC)
    integration.last_sync_at = run.completed_at
    # Canonical provider state is derived by the router from the bound
    # IntegrationConnection. Do not overwrite it here from legacy `mode`.
    if commit:
        await db.commit()
        await db.refresh(run)
    else:
        await db.flush()
    return run
