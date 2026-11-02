"""Cross-cutting candidate evidence builder.

Composes a single labeled text block from the external signals we now collect
about a candidate — GitHub activity, evaluated technical-task submissions, and
interview recordings (transcript / interview score). Scoring, chat, and
interview-prep all consume this so the AI grounds on the same evidence.

Application/position bound and bounded in output length. Legacy pair evidence is
included only when it can be attributed to one unambiguous application cycle.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application import Application
from app.models.candidate import Candidate
from app.models.interview_recording import InterviewRecording
from app.models.task_submission import TaskSubmission
from app.services.github_enrich import github_evidence_text

# Keep the evidence block bounded so it can't blow up a scoring/chat prompt.
_MAX_EVIDENCE_CHARS = 8000
_MAX_TRANSCRIPT_EXCERPT = 2000


async def _evidence_application_context(
    db: AsyncSession,
    candidate: Candidate,
    *,
    application_id: UUID | None,
    position_id: UUID | None,
) -> tuple[UUID | None, UUID | None, bool]:
    """Resolve the only safe context for legacy evidence without an app FK.

    Task submissions and recordings predate the Application model. They may be
    used only when the requested application/position matches the candidate and
    exactly one application cycle exists for that pair. Once a reapplication
    exists, the legacy row cannot be attributed to either cycle and is failed
    closed until it gains an authoritative application parent.
    """

    resolved_position_id = position_id
    resolved_application_id = application_id
    if application_id is not None:
        application = (
            await db.execute(
                select(Application).where(
                    Application.id == application_id,
                    Application.organization_id == candidate.organization_id,
                    Application.candidate_id == candidate.id,
                )
            )
        ).scalar_one_or_none()
        if application is None:
            return None, None, False
        if position_id is not None and application.position_id != position_id:
            return None, None, False
        resolved_position_id = application.position_id
        resolved_application_id = application.id
    if resolved_position_id is None:
        return None, None, False
    cycles = list(
        (
            await db.execute(
                select(Application.id)
                .where(
                    Application.organization_id == candidate.organization_id,
                    Application.candidate_id == candidate.id,
                    Application.position_id == resolved_position_id,
                )
                .limit(2)
            )
        ).scalars()
    )
    if resolved_application_id is None and len(cycles) == 1:
        resolved_application_id = cycles[0]
    return resolved_application_id, resolved_position_id, len(cycles) == 1


async def build_candidate_evidence(
    db: AsyncSession,
    candidate: Candidate,
    *,
    application_id: UUID | None = None,
    position_id: UUID | None = None,
) -> str:
    """Build the external-evidence block for a candidate. Returns "" when there
    is no GitHub, no evaluated task, and no usable recording."""
    sections: list[str] = []

    github_sources = [candidate]
    github_sources.extend(
        list(
            (
                await db.execute(
                    select(Candidate).where(
                        Candidate.organization_id == candidate.organization_id,
                        Candidate.merged_into_candidate_id == candidate.id,
                        Candidate.github_login.is_not(None),
                    )
                )
            ).scalars()
        )
    )
    seen_github_logins: set[str] = set()
    github_sections: list[str] = []
    for source in github_sources:
        login = (source.github_login or "").lower()
        if not login or login in seen_github_logins:
            continue
        seen_github_logins.add(login)
        if gh := github_evidence_text(source):
            github_sections.append(gh)
    if github_sections:
        sections.append("--- GitHub ---\n" + "\n\n".join(github_sections))

    evidence_application_id, evidence_position_id, include_legacy = (
        await _evidence_application_context(
        db,
        candidate,
        application_id=application_id,
        position_id=position_id,
    )
    )
    tasks = []
    if evidence_application_id is not None:
        task_scope = TaskSubmission.application_id == evidence_application_id
        if include_legacy and evidence_position_id is not None:
            task_scope = or_(
                task_scope,
                and_(
                    TaskSubmission.application_id.is_(None),
                    TaskSubmission.position_id == evidence_position_id,
                ),
            )
        tasks = list(
            (
                await db.execute(
                    select(TaskSubmission)
                    .where(
                        TaskSubmission.candidate_id == candidate.id,
                        task_scope,
                    )
                    .order_by(TaskSubmission.created_at.desc())
                )
            ).scalars()
        )
    task_lines: list[str] = []
    for t in tasks:
        if t.ai_score is None and not t.ai_summary:
            continue  # not evaluated yet — nothing verifiable to add
        line = f"- {t.title}"
        if t.ai_score is not None:
            line += f" (AI score {t.ai_score}/100)"
        if t.ai_summary:
            line += f": {t.ai_summary}"
        task_lines.append(line)
        ev = t.evaluation or {}
        for s in (ev.get("strengths") or [])[:3]:
            task_lines.append(f"    + {s}")
        for c in (ev.get("concerns") or [])[:3]:
            task_lines.append(f"    - {c}")
    if task_lines:
        sections.append("--- Technical task submissions ---\n" + "\n".join(task_lines))

    recordings = []
    if evidence_application_id is not None:
        recording_scope = InterviewRecording.application_id == evidence_application_id
        if include_legacy and evidence_position_id is not None:
            recording_scope = or_(
                recording_scope,
                and_(
                    InterviewRecording.application_id.is_(None),
                    InterviewRecording.position_id == evidence_position_id,
                ),
            )
        recordings = list(
            (
                await db.execute(
                    select(InterviewRecording)
                    .where(
                        InterviewRecording.candidate_id == candidate.id,
                        recording_scope,
                    )
                    .order_by(InterviewRecording.created_at.desc())
                )
            ).scalars()
        )
    rec_lines: list[str] = []
    for r in recordings:
        if r.interview_score is None and not r.interview_summary and not r.transcript:
            continue
        head = f"- {r.title}"
        if r.interview_score is not None:
            head += f" (interview score {r.interview_score}/100)"
        rec_lines.append(head)
        if r.interview_summary:
            rec_lines.append(f"    Summary: {r.interview_summary}")
        elif r.transcript:
            excerpt = r.transcript.strip()[:_MAX_TRANSCRIPT_EXCERPT]
            rec_lines.append(f"    Transcript excerpt: {excerpt}")
    if rec_lines:
        sections.append("--- Interview recordings ---\n" + "\n".join(rec_lines))

    if not sections:
        return ""
    block = "\n\n".join(sections)
    if len(block) > _MAX_EVIDENCE_CHARS:
        block = block[:_MAX_EVIDENCE_CHARS] + "\n[...truncated for length]"
    return block
