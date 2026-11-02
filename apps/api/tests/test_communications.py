import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import func, select

from app.auth.authorization import FieldClass
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationMessage,
    CommunicationThread,
)
from app.models.enterprise import EnterprisePolicy, PermissionOverride
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.routers import (
    communications as communications_router_module,
)
from app.routers import (
    operations as operations_router_module,
)
from app.routers.communications import (
    cancel_communication_message,
    communication_capabilities,
    create_bulk_communication,
    create_candidate_contact_point,
    create_communication_template,
    list_communication_accounts,
    list_communication_inbox,
    list_communication_messages,
    mark_communication_thread_read,
    preview_bulk_communication,
    rotate_communication_webhook_secret,
    send_candidate_communication,
    simulate_sandbox_inbound,
    update_candidate_contact_point,
)
from app.routers.operations import entitlements as get_entitlements
from app.schemas.communications import (
    BulkCommunicationCreateRequest,
    BulkCommunicationPreviewRequest,
    CandidateCommunicationSend,
    CandidateContactPointCreate,
    CandidateContactPointUpdate,
    CommunicationAccountCreate,
    CommunicationTemplateCreate,
)
from app.schemas.integrations import WebhookEnvelope
from app.services.candidate_merge import merge_candidates
from app.services.communications import (
    CommunicationPreparationError,
    ensure_default_sandbox_account,
    process_due_messages,
)
from app.services.enterprise import can_access_field, has_permission
from app.services.integrations.webhooks import canonical_envelope_bytes, sign_webhook

pytestmark = pytest.mark.asyncio


def _webhook_request(path: str, body: bytes) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 43100),
            "server": ("testserver", 80),
        },
        receive,
    )


def _signed_inbound(
    *, secret: str, event_id: str, data: dict, created_at: datetime | None = None
) -> tuple[bytes, str, str]:
    envelope = WebhookEnvelope(
        id=event_id,
        type="communication.message.received",
        created_at=created_at or datetime.now(UTC),
        data=data,
    )
    raw = canonical_envelope_bytes(envelope)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    return raw, timestamp, sign_webhook(secret, int(timestamp), raw)


async def seed_team(db, suffix: str = "communications"):
    organization = Organization(
        name=f"Org {suffix}", slug=f"org-{suffix}-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"recruiter-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        parsed_cv={"first_name": "Ada", "last_name": "Lovelace"},
        raw_cv="A sufficiently detailed candidate CV for communication tests. " * 2,
    )
    db.add_all([user, candidate])
    await db.commit()
    return organization, user, candidate


async def add_email(db, user, candidate, value: str = "ada@example.com"):
    return await create_candidate_contact_point(
        candidate.id,
        CandidateContactPointCreate(channel="email", value=value, is_primary=True),
        db=db,
        user=user,
    )


async def test_contact_crud_is_canonical_tenant_scoped_and_safe(db):
    organization, user, candidate = await seed_team(db, "contact")
    contact = await add_email(db, user, candidate, " Ada@Example.COM ")
    assert contact.value == "Ada@Example.COM"
    persisted = await db.get(CandidateContactPoint, contact.id)
    assert persisted is not None
    assert persisted.normalized_value == "ada@example.com"
    assert candidate.email_normalized == "ada@example.com"

    opted_out = await update_candidate_contact_point(
        candidate.id,
        contact.id,
        CandidateContactPointUpdate(is_opted_out=True),
        db=db,
        user=user,
    )
    assert opted_out.is_opted_out is True
    assert opted_out.opted_out_at is not None

    events = list(
        (
            await db.execute(
                select(CandidateActivity)
                .where(CandidateActivity.candidate_id == candidate.id)
                .order_by(CandidateActivity.created_at)
            )
        ).scalars()
    )
    assert [event.event_type for event in events] == [
        "contact_point_added",
        "contact_point_opted_out",
    ]
    assert all(event.payload == {"channel": "email"} for event in events)
    assert "ada" not in str([event.payload for event in events]).lower()

    other_org, outsider, _other_candidate = await seed_team(db, "outsider")
    assert other_org.id != organization.id
    with pytest.raises(HTTPException) as hidden:
        await update_candidate_contact_point(
            candidate.id,
            contact.id,
            CandidateContactPointUpdate(is_opted_out=False),
            db=db,
            user=outsider,
        )
    assert hidden.value.status_code == 404


async def test_template_allowlist_and_immediate_sandbox_send_are_idempotent(db):
    _organization, user, candidate = await seed_team(db, "send")
    await add_email(db, user, candidate)
    template = await create_communication_template(
        CommunicationTemplateCreate(
            name="Invite",
            channel="email",
            language="en",
            subject_template="Hello {candidate.first_name}",
            body_template=(
                "Hi {candidate.full_name}, this is {recruiter.email} from "
                "{organization.name}."
            ),
        ),
        db=db,
        user=user,
    )
    with pytest.raises(HTTPException) as duplicate_name:
        await create_communication_template(
            CommunicationTemplateCreate(
                name="invite",
                channel="email",
                body_template="Case-insensitive duplicate",
            ),
            db=db,
            user=user,
        )
    assert duplicate_name.value.status_code == 409
    await db.refresh(user)
    await db.refresh(candidate)
    with pytest.raises(HTTPException) as forbidden:
        await create_communication_template(
            CommunicationTemplateCreate(
                name="Unsafe",
                channel="email",
                body_template="Hi {candidate.email}",
            ),
            db=db,
            user=user,
        )
    assert forbidden.value.status_code == 422

    request = CandidateCommunicationSend(
        channel="email",
        template_id=template.id,
        idempotency_key="single-send-1",
    )
    queued = await send_candidate_communication(candidate.id, request, db=db, user=user)
    assert queued.status == "queued"
    assert queued.subject == "Hello Ada"
    assert "Ada Lovelace" in queued.body
    counts = await process_due_messages(
        db,
        organization_id=user.organization_id,
        message_ids=[queued.id],
        limit=1,
    )
    assert counts == {"claimed": 1, "sent": 1, "retrying": 0, "failed": 0}

    repeated = await send_candidate_communication(
        candidate.id, request, db=db, user=user
    )
    assert repeated.id == queued.id
    assert repeated.status == "sent"
    assert (
        len(
            list(
                (
                    await db.execute(
                        select(CommunicationMessage).where(
                            CommunicationMessage.idempotency_key == "single-send-1"
                        )
                    )
                ).scalars()
            )
        )
        == 1
    )

    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id,
                    CandidateActivity.event_type == "message_sent",
                )
            )
        ).scalars()
    )
    assert len(events) == 1
    assert events[0].payload == {"channel": "email", "status": "sent"}
    serialized = str(events[0].payload).lower()
    assert "subject" not in serialized and "body" not in serialized
    assert "ada@example.com" not in serialized


async def test_schedule_cancel_and_due_claim_processing(db):
    _organization, user, candidate = await seed_team(db, "schedule")
    await add_email(db, user, candidate)
    future = datetime.now(UTC) + timedelta(hours=2)
    scheduled = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            body="Scheduled hello",
            idempotency_key="scheduled-cancel",
            scheduled_for=future,
        ),
        db=db,
        user=user,
    )
    assert scheduled.status == "queued"
    cancelled = await cancel_communication_message(scheduled.id, db=db, user=user)
    assert cancelled.status == "cancelled"

    due = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            body="Send later",
            idempotency_key="scheduled-due",
            scheduled_for=future,
        ),
        db=db,
        user=user,
    )
    due_row = await db.get(CommunicationMessage, due.id)
    due_row.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    counts = await process_due_messages(
        db,
        organization_id=user.organization_id,
        message_ids=[due.id],
        limit=1,
    )
    assert counts == {"claimed": 1, "sent": 1, "retrying": 0, "failed": 0}
    db.expire_all()
    assert (await db.get(CommunicationMessage, due.id)).status == "sent"


async def test_legacy_smtp_account_is_read_only_and_cannot_send(db):
    organization, user, candidate = await seed_team(db, "external")
    contact = await add_email(db, user, candidate)
    account = CommunicationAccount(
        organization_id=organization.id,
        created_by_user_id=user.id,
        channel="email",
        provider="smtp",
        name="Corporate SMTP",
        configuration={"host": "smtp.example.test"},
        is_active=False,
    )
    db.add(account)
    await db.commit()
    with pytest.raises(HTTPException) as unavailable:
        await send_candidate_communication(
            candidate.id,
            CandidateCommunicationSend(
                channel="email",
                account_id=account.id,
                contact_point_id=contact.id,
                body="Safe outbound body",
                idempotency_key="external-failure",
            ),
            db=db,
            user=user,
        )
    assert unavailable.value.status_code == 422
    assert unavailable.value.detail["code"] == "account_unavailable"


async def test_whatsapp_account_filter_is_read_only_and_creates_no_sandbox(db):
    organization, user, _candidate = await seed_team(db, "whatsapp-read-only")
    accounts = await list_communication_accounts(channel="whatsapp", db=db, user=user)
    assert accounts.items == []
    assert (
        await db.scalar(
            select(func.count(CommunicationAccount.id)).where(
                CommunicationAccount.organization_id == organization.id,
                CommunicationAccount.channel == "whatsapp",
            )
        )
        == 0
    )
    with pytest.raises(CommunicationPreparationError) as unavailable:
        await ensure_default_sandbox_account(
            db,
            organization_id=organization.id,
            channel="whatsapp",
            created_by_user_id=user.id,
        )
    assert unavailable.value.code == "capability_unavailable"


async def test_account_listing_redacts_legacy_configuration_despite_override(db):
    organization, user, _candidate = await seed_team(db, "account-redaction")
    db.add_all(
        [
            EnterprisePolicy(
                organization_id=organization.id,
                field_visibility={"secrets": ["admin"]},
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="integrations.credentials",
                allowed=True,
            ),
        ]
    )
    account = CommunicationAccount(
        organization_id=organization.id,
        created_by_user_id=user.id,
        channel="email",
        provider="smtp",
        name="Legacy sender",
        configuration={"host": "smtp.example.test", "password": "CANARY"},
        is_active=False,
    )
    db.add(account)
    await db.commit()

    assert await has_permission(db, user, "integrations.credentials") is True
    assert await can_access_field(db, user, FieldClass.SECRET) is False

    accounts = await list_communication_accounts(
        channel="email",
        db=db,
        user=user,
    )
    listed = next(item for item in accounts.items if item.id == account.id)
    assert listed.configuration == {}
    assert "CANARY" not in listed.model_dump_json()


async def test_authenticated_inbound_is_replay_safe_and_populates_inbox(db):
    organization, user, candidate = await seed_team(db, "inbound")
    await add_email(db, user, candidate)
    account = await ensure_default_sandbox_account(
        db,
        organization_id=organization.id,
        channel="email",
        created_by_user_id=user.id,
    )
    await db.commit()
    account_id = account.id
    secret = await rotate_communication_webhook_secret(account_id, db=db, user=user)
    path = f"/communications/webhooks/sandbox/{account_id}"
    data = {
        "candidate_id": str(candidate.id),
        "from_value": "ADA@example.com",
        "subject": "Re: interview",
        "body": "Thank you, I can attend.",
        "provider_message_id": "sandbox-inbound-1",
    }
    event_id = f"evt_{uuid4().hex}"
    raw, timestamp, signature = _signed_inbound(
        secret=secret.webhook_secret,
        event_id=event_id,
        data=data,
    )
    inbound = await simulate_sandbox_inbound(
        account_id,
        _webhook_request(path, raw),
        webhook_secret=secret.webhook_secret,
        webhook_timestamp=timestamp,
        webhook_signature=signature,
        db=db,
    )
    assert inbound.status == "received"
    assert inbound.webhook_event_id == event_id
    assert inbound.replay is False
    replay = await simulate_sandbox_inbound(
        account_id,
        _webhook_request(path, raw),
        webhook_secret=secret.webhook_secret,
        webhook_timestamp=timestamp,
        webhook_signature=signature,
        db=db,
    )
    assert replay.id == inbound.id
    assert replay.replay is True

    with pytest.raises(HTTPException) as hidden:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, raw),
            webhook_secret="wrong-secret",
            webhook_timestamp=timestamp,
            webhook_signature=signature,
            db=db,
        )
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as missing_signature:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, raw),
            webhook_secret=secret.webhook_secret,
            webhook_timestamp=timestamp,
            webhook_signature=None,
            db=db,
        )
    assert missing_signature.value.status_code == 404

    with pytest.raises(HTTPException) as tampered:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, raw + b" "),
            webhook_secret=secret.webhook_secret,
            webhook_timestamp=timestamp,
            webhook_signature=signature,
            db=db,
        )
    assert tampered.value.status_code == 404

    stale_timestamp = str(int((datetime.now(UTC) - timedelta(minutes=6)).timestamp()))
    with pytest.raises(HTTPException) as stale:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, raw),
            webhook_secret=secret.webhook_secret,
            webhook_timestamp=stale_timestamp,
            webhook_signature=sign_webhook(
                secret.webhook_secret, int(stale_timestamp), raw
            ),
            db=db,
        )
    assert stale.value.status_code == 404

    conflicting_data = {**data, "body": "Changed after the first delivery."}
    conflicting_raw, conflicting_timestamp, conflicting_signature = _signed_inbound(
        secret=secret.webhook_secret,
        event_id=event_id,
        data=conflicting_data,
    )
    with pytest.raises(HTTPException) as conflict:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, conflicting_raw),
            webhook_secret=secret.webhook_secret,
            webhook_timestamp=conflicting_timestamp,
            webhook_signature=conflicting_signature,
            db=db,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "webhook_event_conflict"
    await db.refresh(user)

    with pytest.raises(HTTPException) as oversized:
        await simulate_sandbox_inbound(
            account_id,
            _webhook_request(path, b"{" + b" " * 256_000),
            webhook_secret=None,
            webhook_timestamp=None,
            webhook_signature=None,
            db=db,
        )
    assert oversized.value.status_code == 413

    inbox = await list_communication_inbox(
        limit=30,
        cursor=None,
        channel=None,
        unread=True,
        db=db,
        user=user,
    )
    assert len(inbox.items) == 1
    assert inbox.items[0].unread_count == 1
    assert inbox.items[0].latest_message.id == inbound.id
    persisted = await db.get(CommunicationMessage, inbound.id)
    assert persisted is not None
    assert persisted.webhook_event_id == event_id
    assert persisted.webhook_payload_sha256 == hashlib.sha256(raw).hexdigest()
    read = await mark_communication_thread_read(inbox.items[0].id, db=db, user=user)
    assert read.unread_count == 0
    assert (
        await db.execute(
            select(Notification).where(Notification.type == "communication_received")
        )
    ).scalar_one()


async def test_bulk_preview_creation_idempotency_and_cursor_boundaries(db):
    organization, user, first = await seed_team(db, "bulk")
    second = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="second",
        parsed_cv={"name": "Grace Hopper"},
    )
    third = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="third-no-contact",
    )
    db.add_all([second, third])
    await db.commit()
    await add_email(db, user, first, "ada@example.com")
    await add_email(db, user, second, "grace@example.com")
    template = await create_communication_template(
        CommunicationTemplateCreate(
            name="Bulk invite",
            channel="email",
            body_template="Hello {candidate.first_name}",
        ),
        db=db,
        user=user,
    )
    preview_request = BulkCommunicationPreviewRequest(
        candidate_ids=[first.id, second.id, third.id],
        channel="email",
        template_id=template.id,
    )
    preview = await preview_bulk_communication(preview_request, db=db, user=user)
    assert preview.ready_count == 2
    assert preview.skipped_count == 1
    assert preview.items[-1].status == "missing_contact"

    request = BulkCommunicationCreateRequest(
        **preview_request.model_dump(), idempotency_key="bulk-1"
    )
    created = await create_bulk_communication(request, db=db, user=user)
    assert created.created is True
    assert created.batch.requested_count == 3
    assert created.batch.total_count == 2
    assert created.batch.skipped_count == 1
    assert created.batch.status == "queued"
    assert [message.status for message in created.messages] == ["queued", "queued"]
    counts = await process_due_messages(
        db,
        organization_id=user.organization_id,
        message_ids=[message.id for message in created.messages],
        limit=len(created.messages),
    )
    assert counts == {"claimed": 2, "sent": 2, "retrying": 0, "failed": 0}
    repeated = await create_bulk_communication(request, db=db, user=user)
    assert repeated.created is False
    assert repeated.batch.id == created.batch.id
    assert len(repeated.messages) == 2
    assert len(repeated.skipped) == 1

    thread = (
        await db.execute(
            select(CommunicationThread).where(
                CommunicationThread.candidate_id == first.id
            )
        )
    ).scalar_one()
    same_time = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    extras = [
        CommunicationMessage(
            organization_id=organization.id,
            thread_id=thread.id,
            candidate_id=first.id,
            contact_point_id=thread.contact_point_id,
            account_id=thread.account_id,
            direction="outbound",
            channel="email",
            status="sent",
            body=f"cursor-{index}",
            sent_at=same_time,
            created_at=same_time,
        )
        for index in range(3)
    ]
    db.add_all(extras)
    await db.commit()
    first_page = await list_communication_messages(
        thread.id, limit=2, cursor=None, db=db, user=user
    )
    assert first_page.next_cursor is not None
    second_page = await list_communication_messages(
        thread.id, limit=2, cursor=first_page.next_cursor, db=db, user=user
    )
    assert not (
        {item.id for item in first_page.items} & {item.id for item in second_page.items}
    )


async def test_large_bulk_is_durable_while_synchronous_preview_is_bounded(
    db, monkeypatch
):
    organization, user, first = await seed_team(db, "bulk-configured-limit")
    extra = [
        Candidate(
            organization_id=organization.id,
            source="manual",
            source_ref=f"bulk-limit-{index}",
        )
        for index in range(2)
    ]
    db.add_all(extra)
    await db.commit()
    monkeypatch.setattr(
        communications_router_module,
        "settings",
        SimpleNamespace(communications_max_bulk_candidates=2),
    )
    monkeypatch.setattr(
        operations_router_module,
        "settings",
        SimpleNamespace(communications_max_bulk_candidates=2),
    )

    capabilities = await communication_capabilities()
    assert capabilities.synchronous_recipient_limit == 2
    assert capabilities.durable_recipient_limit == 1_000
    user.role = "admin"
    await db.commit()
    contract = await get_entitlements(db=db, user=user)
    assert contract.technical_limits["synchronous_recipients"] == 2
    request = BulkCommunicationPreviewRequest(
        candidate_ids=[first.id, *(candidate.id for candidate in extra)],
        channel="email",
        body="Configured limit regression",
    )
    preview = await preview_bulk_communication(request, db=db, user=user)
    assert preview.complete is False
    assert preview.delivery_mode == "durable_queue"
    assert preview.synchronous_recipient_limit == 2
    assert preview.requested_count == 3
    assert preview.items == []

    for index, candidate in enumerate([first, *extra]):
        await add_email(
            db,
            user,
            candidate,
            f"durable-bulk-{index}@example.com",
        )
    created = await create_bulk_communication(
        BulkCommunicationCreateRequest(
            candidate_ids=[first.id, *(candidate.id for candidate in extra)],
            channel="email",
            body="Queued through the durable communication batch",
            idempotency_key="configured-large-durable-batch",
        ),
        db=db,
        user=user,
    )
    assert created.created is True
    assert created.delivery_mode == "durable_queue"
    assert created.queued_count == 3
    assert created.messages_truncated is True
    assert created.messages == []
    assert created.batch.status == "queued"
    assert created.batch.requested_count == 3
    assert created.batch.total_count == 3
    assert (
        await db.scalar(
            select(func.count(CommunicationMessage.id)).where(
                CommunicationMessage.batch_id == created.batch.id,
                CommunicationMessage.status == "queued",
            )
        )
        == 3
    )

    replay = await create_bulk_communication(
        BulkCommunicationCreateRequest(
            candidate_ids=[first.id, *(candidate.id for candidate in extra)],
            channel="email",
            body="Queued through the durable communication batch",
            idempotency_key="configured-large-durable-batch",
        ),
        db=db,
        user=user,
    )
    assert replay.created is False
    assert replay.batch.id == created.batch.id
    assert replay.queued_count == 3
    assert replay.messages_truncated is True


async def test_candidate_merge_rehomes_contacts_threads_and_messages(db):
    organization, user, primary = await seed_team(db, "merge")
    duplicate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="duplicate",
    )
    db.add(duplicate)
    await db.commit()
    await add_email(db, user, primary, "same@example.com")
    duplicate_contact = await add_email(db, user, duplicate, "same@example.com")
    sent = await send_candidate_communication(
        duplicate.id,
        CandidateCommunicationSend(
            channel="email",
            contact_point_id=duplicate_contact.id,
            body="Before merge",
            idempotency_key="merge-message",
        ),
        db=db,
        user=user,
    )
    organization_id = organization.id
    user_id = user.id
    primary_id = primary.id
    duplicate_id = duplicate.id
    duplicate_contact_id = duplicate_contact.id
    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=user_id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[duplicate_id],
        score_winners={},
    )
    db.expire_all()
    message = await db.get(CommunicationMessage, sent.id)
    thread = await db.get(CommunicationThread, sent.thread_id)
    assert message.candidate_id == primary_id
    assert thread.candidate_id == primary_id
    assert message.contact_point_id != duplicate_contact_id
    contacts = list(
        (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.candidate_id == primary_id,
                    CandidateContactPoint.normalized_value == "same@example.com",
                )
            )
        ).scalars()
    )
    assert len(contacts) == 1


async def test_account_configuration_rejects_secret_like_keys():
    with pytest.raises(ValueError):
        CommunicationAccountCreate(
            channel="email",
            provider="sandbox",
            name="Unsafe",
            configuration={"nested": {"api_key": "must-not-persist"}},
        )
