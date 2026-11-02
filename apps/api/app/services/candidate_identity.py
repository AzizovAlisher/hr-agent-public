"""Organization-scoped contact normalization and candidate identity resolution."""

import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.communication import CandidateContactPoint


@dataclass(frozen=True)
class CandidateIdentityResolution:
    candidate: Candidate | None
    conflict: bool = False


def normalize_email(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        normalized = validate_email(
            value.strip(), check_deliverability=False, test_environment=True
        ).normalized
    except EmailNotValidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid contact information"
        ) from exc
    return normalized.casefold()


def normalize_phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    digits: list[str] = []
    for character in value:
        if not character.isdigit():
            continue
        try:
            digits.append(str(unicodedata.digit(character)))
        except (TypeError, ValueError):
            continue
    normalized = "".join(digits)
    if not 7 <= len(normalized) <= 15:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid contact information"
        )
    return f"+{normalized}"


async def _identity_lock(
    db: AsyncSession,
    organization_id: UUID,
    *,
    email: str | None,
    phone: str | None,
) -> None:
    """Serialize concurrent intake for every supplied normalized identity."""

    keys = sorted(
        value
        for value in (
            f"candidate-email:{organization_id}:{email}" if email else None,
            f"candidate-phone:{organization_id}:{phone}" if phone else None,
        )
        if value is not None
    )
    for key in keys:
        await db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )


async def resolve_candidate_identity(
    db: AsyncSession,
    organization_id: UUID,
    *,
    email: str | None,
    phone: str | None,
    lock: bool = True,
) -> CandidateIdentityResolution:
    """Resolve one canonical person without ever merging ambiguous contacts.

    A phone matching one candidate and an email matching another is reported as
    a conflict. Callers may return a generic response, but must not guess which
    person's record should receive an application.
    """

    if lock:
        await _identity_lock(
            db, organization_id, email=email, phone=phone
        )
    if email is None and phone is None:
        return CandidateIdentityResolution(candidate=None)

    async def matching_ids(channel: str, value: str | None) -> set[UUID]:
        if value is None:
            return set()
        direct_column = (
            Candidate.email_normalized
            if channel == "email"
            else Candidate.phone_normalized
        )
        direct = set(
            (
                await db.execute(
                    select(Candidate.id).where(
                        Candidate.organization_id == organization_id,
                        Candidate.merged_at.is_(None),
                        direct_column == value,
                    )
                )
            ).scalars()
        )
        contacts = set(
            (
                await db.execute(
                    select(CandidateContactPoint.candidate_id)
                    .join(Candidate, Candidate.id == CandidateContactPoint.candidate_id)
                    .where(
                        CandidateContactPoint.organization_id == organization_id,
                        CandidateContactPoint.channel == channel,
                        CandidateContactPoint.normalized_value == value,
                        Candidate.merged_at.is_(None),
                    )
                )
            ).scalars()
        )
        return direct | contacts

    email_ids = await matching_ids("email", email)
    phone_ids = await matching_ids("phone", phone)
    if email is not None and phone is not None:
        intersection = email_ids & phone_ids
        if len(intersection) == 1:
            candidate_ids = intersection
        elif len(intersection) > 1 or (email_ids and phone_ids):
            return CandidateIdentityResolution(candidate=None, conflict=True)
        else:
            candidate_ids = email_ids or phone_ids
    else:
        candidate_ids = email_ids or phone_ids
    if len(candidate_ids) > 1:
        return CandidateIdentityResolution(candidate=None, conflict=True)
    if not candidate_ids:
        return CandidateIdentityResolution(candidate=None)

    stmt = select(Candidate).where(
        Candidate.id == next(iter(candidate_ids)),
        Candidate.organization_id == organization_id,
        Candidate.merged_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    candidate = (await db.execute(stmt)).scalar_one_or_none()
    return CandidateIdentityResolution(candidate=candidate)


async def attach_candidate_identity(
    db: AsyncSession,
    candidate: Candidate,
    *,
    email: str | None,
    phone: str | None,
    source: str,
    actor_user_id: UUID | None = None,
) -> None:
    """Persist canonical columns and non-duplicated contact-point records."""

    if candidate.email_normalized is None and email is not None:
        candidate.email_normalized = email
    if candidate.phone_normalized is None and phone is not None:
        candidate.phone_normalized = phone
    for channel, value in (("email", email), ("phone", phone)):
        if value is None:
            continue
        existing = (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.organization_id
                    == candidate.organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == channel,
                    CandidateContactPoint.normalized_value == value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        is_primary = (
            candidate.email_normalized == value
            if channel == "email"
            else candidate.phone_normalized == value
        )
        db.add(
            CandidateContactPoint(
                organization_id=candidate.organization_id,
                candidate_id=candidate.id,
                created_by_user_id=actor_user_id,
                channel=channel,
                value=value,
                normalized_value=value,
                source=re.sub(r"[^a-z0-9_]+", "_", source.casefold())[:32]
                or "manual",
                is_primary=is_primary,
            )
        )
