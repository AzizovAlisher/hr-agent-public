"""GitHub profile enrichment for a candidate.

PUT attaches/refreshes: fetch public profile + repos, LLM-absorb into a short
summary, persist the snapshot on the candidate (feeds AI scoring / chat /
interview). GET .../detect scans the CV for a github.com link so the UI can
offer a one-click attach. DELETE detaches.
"""
import json
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.candidate import Candidate
from app.models.user import User
from app.schemas.enrichment import GitHubAttachRequest, GitHubDetectOut, GitHubOut
from app.services.github_enrich import (
    GitHubNotFound,
    GitHubRateLimited,
    enrich_candidate_github,
    extract_github_login,
    normalize_github_login,
)
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate

router = APIRouter(
    prefix="/candidates", tags=["github"], dependencies=[Depends(current_user)]
)
async def _github_enrichment_unavailable() -> None:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "github_enrichment_unavailable",
            "detail": (
                "GitHub enrichment is unavailable in the pilot until its "
                "external fetch and AI summary run as durable jobs."
            ),
        },
    )


async def _get_candidate(candidate_id: UUID, user: User, db: AsyncSession) -> Candidate:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return c


@router.put("/{candidate_id}/github", response_model=GitHubOut)
async def attach_github(
    candidate_id: UUID,
    body: GitHubAttachRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    _capability: None = Depends(_github_enrichment_unavailable),
) -> GitHubOut:
    login = normalize_github_login(body.login)
    if not login:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Not a valid GitHub username or URL.",
        )
    c = await _get_candidate(candidate_id, user, db)
    try:
        data = await enrich_candidate_github(
            candidate=c,
            login=login,
            db=db,
            organization_id=user.organization_id,
            actor_user_id=user.id,
        )
    except GitHubNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except GitHubRateLimited as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e)) from e
    except httpx.HTTPError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"GitHub request failed: {e}"
        ) from e
    return GitHubOut(login=login, data=data, fetched_at=c.github_fetched_at)


@router.get("/{candidate_id}/github/detect", response_model=GitHubDetectOut)
async def detect_github(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> GitHubDetectOut:
    """Suggest a GitHub login from the CV (or the already-attached one)."""
    c = await _get_candidate(candidate_id, user, db)
    if c.github_login:
        return GitHubDetectOut(suggested_login=c.github_login)
    texts: list[str | None] = [c.raw_cv, c.source_ref]
    if isinstance(c.parsed_cv, dict):
        try:
            texts.append(json.dumps(c.parsed_cv, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return GitHubDetectOut(suggested_login=extract_github_login(*texts))


@router.delete("/{candidate_id}/github", status_code=status.HTTP_204_NO_CONTENT)
async def detach_github(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    c = await _get_candidate(candidate_id, user, db)
    old_login = c.github_login
    if old_login is None:
        return
    c.github_login = None
    c.github_data = None
    c.github_fetched_at = None
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=c.id,
        actor_user_id=user.id,
        event_type="github_detached",
        subject_type="candidate",
        subject_id=c.id,
        payload={"github_login": old_login},
    )
    await db.commit()
