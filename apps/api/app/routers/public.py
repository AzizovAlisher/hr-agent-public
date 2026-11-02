"""Public-facing endpoints — NO authentication required.

These power the /apply/{position_id} page that any candidate (typically reaching
us via a somon.tj job posting or a Telegram channel) can land on, upload their
CV, and self-onboard into the recruiter's pipeline. Org_id is inferred from
the position being applied to — the candidate doesn't need to know it exists.

Also exposes the shortlist read endpoint (recruiter-issued share link).
"""

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, RootModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.db import (
    bootstrap_public_position,
    forced_tenant_rls,
    get_session,
    resolve_capability,
    set_tenant_context,
)
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog
from app.models.folder import Folder
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.shortlist import ShortlistLink
from app.models.sourcing import ReferralLink
from app.models.user import User
from app.schemas.careers import PublicCareerApplicationAccepted
from app.services.enterprise import can_access_field, has_permission
from app.services.public_applications import submit_public_application
from app.services.public_positions import (
    public_position_filters,
    redact_position,
)

router = APIRouter(prefix="/public", tags=["public"])

_PUBLIC_TOKEN_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


def _shortlist_token_error(status_code: int, detail: str | dict[str, str]) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_PUBLIC_TOKEN_HEADERS,
    )


# ---------- response shapes ----------


class PublicPositionOut(BaseModel):
    """Minimal public projection of a position — no internal_notes, no created_by.

    Deliberately absent: ``candidate_profile`` (internal prose) and the real
    ``description`` of a confidential role. /openapi.json is unauthenticated,
    so field names here are themselves public.
    """

    id: UUID
    title: str
    description: str | None
    full_description: str | None
    requirements: dict | None
    organization_name: str | None
    organization_logo_path: str | None
    organization_accent_color: str | None
    # True when the employer must stay unnamed; the page renders a localized
    # "confidential employer" label in place of the brand.
    employer_confidential: bool = False


ApplicationAcceptedOut = PublicCareerApplicationAccepted


# ---------- helpers ----------


async def _load_public_position(position_id: UUID, db: AsyncSession) -> Position:
    """Load a position that is allowed to receive public applications.

    Returns 404 if the position doesn't exist, isn't open, or has the public
    apply flag turned off — never reveals which.

    Confidential roles ARE reachable here: this endpoint backs the direct
    /apply/{id} link the recruiter hands out deliberately. They are excluded
    from the broadcast careers feed instead, and what renders is decided by
    :func:`redact_position`.
    """
    if forced_tenant_rls():
        organization_id = await bootstrap_public_position(db, position_id)
        if organization_id is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
        await set_tenant_context(
            db,
            organization_id=organization_id,
            actor_id=None,
            identity_kind="public",
        )
    p = (
        await db.execute(
            select(Position)
            .outerjoin(Folder, Folder.id == Position.folder_id)
            .where(
                Position.id == position_id,
                *public_position_filters(include_confidential=True),
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
    return p


# ---------- endpoints ----------


@router.head(
    "/positions/{position_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def preview_public_position(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Cheap existence check used before the web tier starts streaming HTML."""
    await _load_public_position(position_id, db)
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers=_PUBLIC_TOKEN_HEADERS,
    )


@router.get("/positions/{position_id}", response_model=PublicPositionOut)
async def get_public_position(
    position_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> PublicPositionOut:
    response.headers.update(_PUBLIC_TOKEN_HEADERS)
    p = await _load_public_position(position_id, db)
    org = (
        await db.execute(
            select(Organization).where(Organization.id == p.organization_id)
        )
    ).scalar_one()
    view = redact_position(p)
    show_org = view.show_organization_identity
    return PublicPositionOut(
        id=p.id,
        title=view.title,
        description=view.description,
        full_description=view.full_description,
        requirements=view.requirements,
        # Withhold the org's name, logo AND accent colour together — a brand
        # colour alone is a strong identifier when the buyer is a well-known
        # operator, so leaking it would undo the name suppression.
        organization_name=org.name if show_org else None,
        organization_logo_path=org.logo_path if show_org else None,
        organization_accent_color=org.accent_color if show_org else None,
        employer_confidential=view.employer_confidential,
    )


@router.post(
    "/positions/{position_id}/apply",
    response_model=ApplicationAcceptedOut,
    status_code=status.HTTP_201_CREATED,
)
async def public_apply(
    position_id: UUID,
    name: str = Form(..., min_length=1, max_length=200),
    phone: str = Form(..., min_length=4, max_length=64),
    email: Annotated[str | None, Form(max_length=320)] = None,
    source: Annotated[str | None, Form(max_length=64)] = None,
    referral_token: Annotated[str | None, Form(max_length=128)] = None,
    file: Annotated[UploadFile | None, File()] = None,
    raw_text: Annotated[str | None, Form()] = None,
    privacy_notice_version: Annotated[str | None, Form(max_length=32)] = None,
    privacy_acknowledged: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=160),
    ] = None,
    db: AsyncSession = Depends(get_session),
) -> ApplicationAcceptedOut:
    """Receive an applicant submission and queue scoring in the background.

    Either `file` (PDF/DOCX) or `raw_text` (pasted CV) is required. `source`
    is the utm_source param ("somon", "telegram", "direct") so the recruiter
    can see which channel each candidate came in through.

    Contact identity is organization-scoped: a retry for this position is
    idempotent, while the same person may apply to another open position.
    Ambiguous contact signals enter human identity review; public callers see
    the same generic accepted response as every other valid submission.
    """
    p = await _load_public_position(position_id, db)

    referral_link = None
    if isinstance(referral_token, str) and referral_token:
        now = datetime.now(UTC)
        referral_link = (
            await db.execute(
                select(ReferralLink)
                .where(
                    ReferralLink.token_digest
                    == hashlib.sha256(referral_token.encode()).hexdigest(),
                    ReferralLink.organization_id == p.organization_id,
                    ReferralLink.position_id == p.id,
                    ReferralLink.is_active.is_(True),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if referral_link is None or (
            referral_link.expires_at is not None and referral_link.expires_at <= now
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")
        source = referral_link.source

    await submit_public_application(
        position=p,
        name=name,
        phone=phone,
        email=email,
        source=source,
        file=file,
        raw_text=raw_text,
        referral_link=referral_link,
        idempotency_key=idempotency_key,
        privacy_notice_version=privacy_notice_version,
        privacy_acknowledged=privacy_acknowledged,
        db=db,
    )
    return ApplicationAcceptedOut(
        accepted=True,
        message="Application received. We'll be in touch.",
    )


# ---------- shortlist (read-only) ----------


class PublicShortlistCandidateComparison(BaseModel):
    """Share-safe rubric values common to both shortlist projections.

    Anonymous links deliberately expose no persistent candidate handle and no
    free-form evidence.  The rank-local label and numeric rubric are sufficient
    for a reviewer to compare candidates without receiving CV-derived prose.
    """

    label: str
    overall_score: int | None
    score_technical_skills: int | None
    score_quality_of_projects: int | None
    score_previous_experience: int | None
    score_impact: int | None


class PublicShortlistAnonymousCandidate(PublicShortlistCandidateComparison):
    """Anonymous comparison row with no stable or re-identifying fields."""


class PublicShortlistContactCandidate(PublicShortlistCandidateComparison):
    """Full row for a recruiter-authorized contact-bearing share link."""

    candidate_id: UUID
    source: str
    comment_technical_skills: str | None
    comment_quality_of_projects: str | None
    comment_previous_experience: str | None
    comment_impact: str | None
    contact_name: str | None
    contact_phone: str | None
    contact_email: str | None


class PublicShortlistBaseOut(BaseModel):
    position_title: str
    position_description: str | None
    # The hiring company for this specific role (from requirements.company);
    # public branding prefers this over the org name.
    position_company: str | None
    organization_name: str | None
    organization_logo_path: str | None
    organization_accent_color: str | None
    # Set when the employer is withheld; the page renders a localized
    # "confidential employer" label instead of the brand.
    employer_confidential: bool = False
    top_n: int
    issued_at: datetime


class PublicShortlistAnonymousOut(PublicShortlistBaseOut):
    include_contact: Literal[False] = False
    candidates: list[PublicShortlistAnonymousCandidate]


class PublicShortlistContactOut(PublicShortlistBaseOut):
    include_contact: Literal[True] = True
    candidates: list[PublicShortlistContactCandidate]


class PublicShortlistOut(
    RootModel[PublicShortlistAnonymousOut | PublicShortlistContactOut]
):
    """Privacy-separated public contract that makes anonymous leakage impossible.

    Keeping a named root model preserves the stable ``PublicShortlistOut``
    OpenAPI component while documenting the two materially different privacy
    projections for generated clients.
    """


def _candidate_label(c: Candidate, idx: int, include_contact: bool) -> str:
    """Pick a human label for the public shortlist row.

    With contact: prefer applicant_name from parsed_cv (set on public apply),
    else source_ref, else "Candidate #N".
    Without contact: anonymized "Candidate #N" so a forwarded link doesn't
    leak who's in the org's pipeline.
    """
    if include_contact:
        parsed = c.parsed_cv if isinstance(c.parsed_cv, dict) else {}
        name = (parsed.get("applicant_name") if parsed else None) or None
        if name and isinstance(name, str) and name.strip():
            return name.strip()
        if c.source_ref and c.source_ref.strip():
            return c.source_ref.strip()
    return f"#{idx:02d}"


@router.get("/shortlists/{token}", response_model=PublicShortlistOut)
async def get_public_shortlist(
    token: str,
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> PublicShortlistOut:
    response.headers.update(_PUBLIC_TOKEN_HEADERS)
    if len(token) < 32 or len(token) > 128:
        raise _shortlist_token_error(status.HTTP_404_NOT_FOUND, "Shortlist not found.")
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(db, kind="shortlist", digest=token_digest)
        if bootstrap is None:
            raise _shortlist_token_error(
                status.HTTP_404_NOT_FOUND, "Shortlist not found."
            )
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    shortlist_stmt = select(ShortlistLink).where(
        ShortlistLink.token_digest == token_digest
    )
    if bootstrap is not None:
        shortlist_stmt = shortlist_stmt.where(
            ShortlistLink.id == bootstrap.capability_id,
            ShortlistLink.organization_id == bootstrap.organization_id,
        )
    s = (await db.execute(shortlist_stmt)).scalar_one_or_none()
    if s is None:
        raise _shortlist_token_error(status.HTTP_404_NOT_FOUND, "Shortlist not found.")
    if s.revoked_at is not None:
        raise _shortlist_token_error(
            status.HTTP_410_GONE,
            {"code": "shortlist_revoked", "detail": "Shortlist has been revoked."},
        )
    if s.expires_at is not None and s.expires_at <= datetime.now(UTC):
        raise _shortlist_token_error(
            status.HTTP_410_GONE,
            {"code": "shortlist_expired", "detail": "Shortlist has expired."},
        )

    creator = None
    if s.created_by is not None:
        creator = (
            await db.execute(
                select(User).where(
                    User.id == s.created_by,
                    User.organization_id == s.organization_id,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    creator_authorized = (
        creator is not None
        and await has_permission(db, creator, "candidates.export")
        and await can_access_field(db, creator, FieldClass.EXPORT)
        and await can_access_field(db, creator, FieldClass.ASSESSMENT_RESULT)
    )
    if creator_authorized and s.include_contact:
        creator_authorized = await can_access_field(db, creator, FieldClass.CONTACT)
    if not creator_authorized:
        raise _shortlist_token_error(
            status.HTTP_410_GONE,
            {
                "code": "shortlist_authorization_revoked",
                "detail": "Shortlist authorization has been revoked.",
            },
        )

    p = (
        await db.execute(select(Position).where(Position.id == s.position_id))
    ).scalar_one_or_none()
    if p is None:
        raise _shortlist_token_error(status.HTTP_404_NOT_FOUND, "Shortlist not found.")

    org = (
        await db.execute(
            select(Organization).where(Organization.id == s.organization_id)
        )
    ).scalar_one()

    # Same position-framing redaction policy as /apply. Contact-enabled links
    # retain the deliberate full assessment share; anonymous links use the
    # numeric-only projection built below so forwarded URLs cannot reveal CV
    # evidence, sourcing provenance, or a persistent candidate handle.
    view = redact_position(p)
    show_org = view.show_organization_identity
    common = dict(
        position_title=view.title,
        position_description=view.description,
        position_company=view.company,
        organization_name=org.name if show_org else None,
        organization_logo_path=org.logo_path if show_org else None,
        organization_accent_color=org.accent_color if show_org else None,
        employer_confidential=view.employer_confidential,
        top_n=s.top_n,
        issued_at=s.created_at,
    )

    if s.include_contact:
        contact_rows = (
            await db.execute(
                select(Score, Candidate)
                .join(Candidate, Candidate.id == Score.candidate_id)
                .where(
                    Score.position_id == s.position_id,
                    Score.organization_id == s.organization_id,
                    Candidate.merged_at.is_(None),
                )
                .order_by(Score.overall_score.desc().nulls_last())
                .limit(s.top_n)
            )
        ).all()
        contact_candidates: list[PublicShortlistContactCandidate] = []
        for idx, (score, cand) in enumerate(contact_rows, start=1):
            contact_name: str | None = None
            contact_phone: str | None = None
            contact_email: str | None = None
            parsed = cand.parsed_cv if isinstance(cand.parsed_cv, dict) else {}
            if parsed:
                contact_name = parsed.get("applicant_name")
                contact_phone = parsed.get("applicant_phone")
                contact_email = parsed.get("applicant_email")
            contact_candidates.append(
                PublicShortlistContactCandidate(
                    candidate_id=cand.id,
                    label=_candidate_label(cand, idx, True),
                    source=cand.source,
                    overall_score=score.overall_score,
                    score_technical_skills=score.score_technical_skills,
                    score_quality_of_projects=score.score_quality_of_projects,
                    score_previous_experience=score.score_previous_experience,
                    score_impact=score.score_impact,
                    comment_technical_skills=score.comment_technical_skills,
                    comment_quality_of_projects=score.comment_quality_of_projects,
                    comment_previous_experience=score.comment_previous_experience,
                    comment_impact=score.comment_impact,
                    contact_name=contact_name,
                    contact_phone=contact_phone,
                    contact_email=contact_email,
                )
            )
        output = PublicShortlistOut(
            PublicShortlistContactOut(
                **common,
                candidates=contact_candidates,
            )
        )
        db.add(
            AuditLog(
                organization_id=s.organization_id,
                actor_user_id=None,
                actor_email_snapshot=None,
                action="sensitive_read.get_public_shortlist",
                resource_type="shortlist",
                resource_id=s.id,
                path_template="/public/shortlists/{token}",
                method="GET",
                status_code=200,
                request_id=getattr(request.state, "request_id", uuid4()),
                metadata_json={
                    "sensitive_read": True,
                    "field_classes": ["assessment_results", "candidate_contacts"],
                    "capability_actor": "public",
                },
                occurred_at=datetime.now(UTC),
            )
        )
        await db.commit()
        return output

    # Do not hydrate Candidate records or free-form Score evidence for an
    # anonymous share.  Besides constraining the response model, this keeps
    # re-identifying material out of the serialization path entirely.
    anonymous_rows = (
        await db.execute(
            select(
                Score.overall_score,
                Score.score_technical_skills,
                Score.score_quality_of_projects,
                Score.score_previous_experience,
                Score.score_impact,
            )
            .join(Candidate, Candidate.id == Score.candidate_id)
            .where(
                Score.position_id == s.position_id,
                Score.organization_id == s.organization_id,
                Candidate.merged_at.is_(None),
            )
            .order_by(Score.overall_score.desc().nulls_last())
            .limit(s.top_n)
        )
    ).all()
    anonymous_candidates = [
        PublicShortlistAnonymousCandidate(
            label=f"#{idx:02d}",
            overall_score=row.overall_score,
            score_technical_skills=row.score_technical_skills,
            score_quality_of_projects=row.score_quality_of_projects,
            score_previous_experience=row.score_previous_experience,
            score_impact=row.score_impact,
        )
        for idx, row in enumerate(anonymous_rows, start=1)
    ]
    output = PublicShortlistOut(
        PublicShortlistAnonymousOut(
            **common,
            candidates=anonymous_candidates,
        )
    )
    db.add(
        AuditLog(
            organization_id=s.organization_id,
            actor_user_id=None,
            actor_email_snapshot=None,
            action="sensitive_read.get_public_shortlist",
            resource_type="shortlist",
            resource_id=s.id,
            path_template="/public/shortlists/{token}",
            method="GET",
            status_code=200,
            request_id=getattr(request.state, "request_id", uuid4()),
            metadata_json={
                "sensitive_read": True,
                "field_classes": ["assessment_results"],
                "capability_actor": "public",
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return output
