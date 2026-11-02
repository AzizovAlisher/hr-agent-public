"""Chat service: builds scoped system prompts from DB context and streams responses
through `LLMClient` so every message is logged in the `messages` table."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.ai_boundary import untrusted_candidate_block
from app.engine.chat_prompts import (
    candidate_scoped_prompt,
    free_scoped_prompt,
    position_scoped_prompt,
)
from app.llm import ChatMessage, get_llm_client
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.chat_session import ChatSession
from app.models.message import Message
from app.models.position import Position
from app.models.score import Score
from app.services.ai_hiring_safety import candidate_extra_names
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import organization_llm_client
from app.services.evidence import build_candidate_evidence

_MAX_CANDIDATE_CHAT_CONTEXT_CHARS = 24_000
_CHAT_TURN_NAMESPACE = UUID("c8ac6af8-6739-46e3-9dbd-feb73ab0a9c6")
_MAX_IDEMPOTENCY_KEY_CHARS = 160


class ChatIdempotencyConflict(RuntimeError):
    """One client turn key was reused with different user content."""

    code = "idempotency_conflict"


@dataclass(frozen=True, slots=True)
class _ChatTurn:
    assistant_id: UUID
    assistant_message: Message | None


@dataclass
class ChatStreamEvent:
    type: Literal["delta", "usage", "done"]
    content: str = ""
    message_id: UUID | None = None
    model_used: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    provider_used: str | None = None
    cost_microusd: int | None = None


async def _candidate_summary(
    db: AsyncSession,
    candidate_id: UUID,
    organization_id: UUID,
    *,
    position_id: UUID | None = None,
    application_id: UUID | None = None,
) -> str:
    candidate = await resolve_canonical_candidate(db, candidate_id, organization_id)
    if candidate is None:
        return "(candidate not found)"
    candidate_id = candidate.id
    candidate_names = list(candidate_extra_names(candidate.parsed_cv))
    if isinstance(candidate.parsed_cv, dict):
        applicant_name = candidate.parsed_cv.get("applicant_name")
        if isinstance(applicant_name, str):
            candidate_names.append(applicant_name)
    if candidate.github_login:
        candidate_names.append(candidate.github_login)
    if candidate.source_ref:
        candidate_names.append(candidate.source_ref)

    # A source reference can itself be an email, phone number, profile handle,
    # or provider identifier. It is not needed for recruiter reasoning and must
    # never leave the canonical record for a model provider.
    parts: list[str] = [f"Source type: {candidate.source}"]
    if candidate.raw_cv:
        parts.append(f"\nResume text:\n{candidate.raw_cv}")

    # Pull this candidate's own scores so the assistant can talk about strengths/gaps
    # numerically without having to infer from the resume text.
    score_query = (
        select(Score, Position)
        .join(Position, Position.id == Score.position_id)
        .where(
            Score.candidate_id == candidate_id,
            Score.organization_id == organization_id,
            Position.organization_id == organization_id,
        )
    )
    if application_id is not None:
        score_query = score_query.join(
            Application, Application.legacy_score_id == Score.id
        ).where(
            Application.id == application_id,
            Application.organization_id == organization_id,
            Application.candidate_id == candidate_id,
        )
    own_scores = (
        await db.execute(score_query.order_by(Score.overall_score.desc().nulls_last()))
    ).all()
    if own_scores:
        parts.append("\nThis candidate's scores:")
        for score, position in own_scores:
            parts.append(
                f"- vs '{position.title}': overall={score.overall_score}, "
                f"tech={score.score_technical_skills}, "
                f"projects={score.score_quality_of_projects}, "
                f"experience={score.score_previous_experience}, "
                f"impact={score.score_impact}"
            )
            for label, comment in (
                ("tech", score.comment_technical_skills),
                ("projects", score.comment_quality_of_projects),
                ("experience", score.comment_previous_experience),
                ("impact", score.comment_impact),
            ):
                if comment:
                    parts.append(f"  · {label}: {comment}")

        # And the other candidates ranked against the candidate's best-scored position
        # — so questions like "compare to top-3" have something to draw from.
        primary_position_id = own_scores[0][1].id
        primary_position_title = own_scores[0][1].title
        peers = (
            await db.execute(
                select(Score, Candidate)
                .join(Candidate, Candidate.id == Score.candidate_id)
                .where(
                    Score.position_id == primary_position_id,
                    Score.candidate_id != candidate_id,
                    Score.organization_id == organization_id,
                    Candidate.organization_id == organization_id,
                    Candidate.merged_at.is_(None),
                )
                .order_by(Score.overall_score.desc().nulls_last())
                .limit(15)
            )
        ).all()
        if peers:
            parts.append(
                f"\nOther candidates ranked against '{primary_position_title}' "
                f"(use these for comparison questions):"
            )
            for index, (s, c) in enumerate(peers, start=1):
                parts.append(
                    f"- Peer candidate {index} ({c.source}): overall={s.overall_score}, "
                    f"tech={s.score_technical_skills}, "
                    f"projects={s.score_quality_of_projects}, "
                    f"experience={s.score_previous_experience}, "
                    f"impact={s.score_impact}"
                )

    # External evidence (GitHub, evaluated tasks, interview recordings) so the
    # assistant can talk about real code / task results, not just the CV.
    evidence = await build_candidate_evidence(
        db,
        candidate,
        position_id=position_id,
        application_id=application_id,
    )
    if evidence:
        parts.append(
            "\nExternal evidence (GitHub / technical task / interview):\n" + evidence
        )

    # Candidate-controlled and provider-derived artifacts are one JSON-encoded
    # evidence value, never interpolated as instructions in the system prompt.
    # The shared boundary removes PII, protected traits, and known injection
    # patterns before applying the prompt-size bound.
    return untrusted_candidate_block(
        "\n".join(parts),
        label="candidate_chat_context",
        extra_names=tuple(candidate_names),
        max_chars=_MAX_CANDIDATE_CHAT_CONTEXT_CHARS,
    )


async def _position_summary(
    db: AsyncSession, position_id: UUID, organization_id: UUID
) -> tuple[str, str]:
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        return "(position not found)", ""

    parts = [f"Title: {position.title}", f"Status: {position.status}"]
    if position.description:
        parts.append(f"Description: {position.description}")
    if position.requirements:
        parts.append("Requirements:")
        for k, v in position.requirements.items():
            parts.append(f"  - {k}: {v}")
    pos_text = "\n".join(parts)

    rows = (
        await db.execute(
            select(Score, Candidate)
            .join(Candidate, Candidate.id == Score.candidate_id)
            .where(
                Score.position_id == position_id,
                Score.organization_id == organization_id,
                Candidate.organization_id == organization_id,
                Candidate.merged_at.is_(None),
            )
            .order_by(Score.overall_score.desc().nulls_last())
            .limit(20)
        )
    ).all()
    if not rows:
        return pos_text, "(no candidates scored yet)"

    cand_lines = []
    for score, cand in rows:
        ref = cand.source_ref or str(cand.id)[:8]
        cand_lines.append(
            f"- {ref} ({cand.source}): overall={score.overall_score}, "
            f"tech={score.score_technical_skills}, "
            f"projects={score.score_quality_of_projects}, "
            f"experience={score.score_previous_experience}, "
            f"impact={score.score_impact}"
        )
    return pos_text, "\n".join(cand_lines)


async def _build_system_prompt(db: AsyncSession, session: ChatSession) -> str:
    if session.scope == "candidate" and session.candidate_id:
        summary = await _candidate_summary(
            db,
            session.candidate_id,
            session.organization_id,
            position_id=session.position_id,
            application_id=session.application_id,
        )
        return candidate_scoped_prompt(candidate_summary=summary)
    if session.scope == "position" and session.position_id:
        pos, ranked = await _position_summary(
            db, session.position_id, session.organization_id
        )
        return position_scoped_prompt(position_summary=pos, ranked_candidates=ranked)
    return free_scoped_prompt()


async def _build_message_history(
    db: AsyncSession, session: ChatSession
) -> list[ChatMessage]:
    """System prompt + non-system messages from this session, in order."""
    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session.id, Message.purpose == "chat")
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    ).scalars().all()
    history: list[ChatMessage] = [
        ChatMessage(role="system", content=await _build_system_prompt(db, session))
    ]
    for m in rows:
        if m.role in ("user", "assistant"):
            history.append(ChatMessage(role=m.role, content=m.content))
    return history


def _chat_turn_key(idempotency_key: str | None) -> str:
    key = idempotency_key if idempotency_key is not None else uuid4().hex
    if not key or len(key) > _MAX_IDEMPOTENCY_KEY_CHARS:
        raise ValueError("Invalid chat Idempotency-Key")
    return key


def _chat_turn_identity(session: ChatSession, key: str) -> str:
    # Idempotency keys are opaque client credentials. Hash them before using
    # them in database lock labels or deterministic identifiers so they never
    # appear in PostgreSQL activity/lock diagnostics.
    digest = sha256(key.encode("utf-8")).hexdigest()
    return f"{session.organization_id}:{session.id}:{digest}"


def _chat_message_id(identity: str, role: Literal["user", "assistant"]) -> UUID:
    return uuid5(_CHAT_TURN_NAMESPACE, f"{identity}:{role}")


async def _prepare_chat_turn(
    *,
    db: AsyncSession,
    session: ChatSession,
    content: str,
    idempotency_key: str | None,
) -> _ChatTurn:
    """Serialize one logical client turn and materialize its user row once.

    The deterministic primary keys are the durable idempotency record. The
    transaction-scoped advisory lock makes concurrent replays wait for the
    first request, after which they either replay its assistant row or resume
    a pre-existing user-only turn. New turns keep the lock until both rows are
    committed atomically.
    """

    key = _chat_turn_key(idempotency_key)
    identity = _chat_turn_identity(session, key)
    user_id = _chat_message_id(identity, "user")
    assistant_id = _chat_message_id(identity, "assistant")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"chat-turn:{identity}"},
    )
    existing = list(
        (
            await db.execute(
                select(Message).where(
                    Message.organization_id == session.organization_id,
                    Message.session_id == session.id,
                    Message.purpose == "chat",
                    Message.id.in_((user_id, assistant_id)),
                )
            )
        ).scalars()
    )
    by_id = {message.id: message for message in existing}
    user_message = by_id.get(user_id)
    assistant_message = by_id.get(assistant_id)
    if user_message is not None and (
        user_message.role != "user" or user_message.content != content
    ):
        raise ChatIdempotencyConflict(
            "Idempotency-Key was already used for different chat content"
        )
    if assistant_message is not None and (
        assistant_message.role != "assistant" or user_message is None
    ):
        raise RuntimeError("Chat turn persistence is inconsistent")
    if user_message is None:
        user_message = Message(
            id=user_id,
            organization_id=session.organization_id,
            session_id=session.id,
            role="user",
            content=content,
            purpose="chat",
        )
        db.add(user_message)
        await db.flush()
    return _ChatTurn(
        assistant_id=assistant_id,
        assistant_message=assistant_message,
    )


def _assistant_message_from_completion(
    *,
    session: ChatSession,
    turn: _ChatTurn,
    content: str,
    model_used: str | None,
    tokens_in: int,
    tokens_out: int,
) -> Message:
    return Message(
        id=turn.assistant_id,
        organization_id=session.organization_id,
        session_id=session.id,
        role="assistant",
        content=content,
        model_used=model_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        purpose="chat",
    )


async def post_user_message_and_complete(
    *,
    db: AsyncSession,
    session: ChatSession,
    content: str,
    idempotency_key: str | None = None,
) -> Message:
    """Complete one logical turn and atomically persist its two messages."""
    try:
        turn = await _prepare_chat_turn(
            db=db,
            session=session,
            content=content,
            idempotency_key=idempotency_key,
        )
        if turn.assistant_message is not None:
            await db.commit()
            return turn.assistant_message

        # The just-flushed user message is already included by the history query.
        # Appending it again makes every user turn appear twice to the model.
        history = await _build_message_history(db, session)
        llm = await organization_llm_client(
            db, session.organization_id, fallback_factory=get_llm_client
        )
        completion = await llm.complete(messages=history, temperature=0.3)
        assistant_msg = _assistant_message_from_completion(
            session=session,
            turn=turn,
            content=completion.content,
            model_used=completion.model_used,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )
        db.add(assistant_msg)
        await db.commit()
        return assistant_msg
    except BaseException:
        await db.rollback()
        raise


async def stream_assistant_reply(
    *,
    db: AsyncSession,
    session: ChatSession,
    content: str,
    idempotency_key: str | None = None,
) -> AsyncIterator[ChatStreamEvent]:
    """Stream one turn and commit the user/assistant rows exactly once.

    Provider output is provisional until its one terminal event arrives. If the
    provider fails, emits an invalid stream, or the browser disconnects, this
    generator rolls the user and assistant rows back together. Reconnecting
    with the same key replays a committed assistant without another model call.
    """
    committed = False
    try:
        turn = await _prepare_chat_turn(
            db=db,
            session=session,
            content=content,
            idempotency_key=idempotency_key,
        )
        if turn.assistant_message is not None:
            assistant = turn.assistant_message
            await db.commit()
            committed = True
            if assistant.content:
                yield ChatStreamEvent(type="delta", content=assistant.content)
            yield ChatStreamEvent(
                type="usage",
                model_used=assistant.model_used,
                tokens_in=assistant.tokens_in or 0,
                tokens_out=assistant.tokens_out or 0,
            )
            yield ChatStreamEvent(type="done", message_id=assistant.id)
            return

        history = await _build_message_history(db, session)
        llm = await organization_llm_client(
            db, session.organization_id, fallback_factory=get_llm_client
        )
        full_text = ""
        model_used: str | None = None
        tokens_in = 0
        tokens_out = 0
        provider_used: str | None = None
        cost_microusd: int | None = None
        terminal_seen = False

        async for event in llm.stream_complete_events(
            messages=history, temperature=0.3
        ):
            if event.type == "delta":
                if terminal_seen:
                    raise RuntimeError(
                        "LLM stream emitted a delta after its terminal event"
                    )
                full_text += event.content
                yield ChatStreamEvent(type="delta", content=event.content)
            elif event.type == "done":
                if terminal_seen:
                    raise RuntimeError(
                        "LLM stream emitted more than one terminal event"
                    )
                terminal_seen = True
                model_used = event.model_used
                tokens_in = event.tokens_in
                tokens_out = event.tokens_out
                provider_used = event.provider_used
                cost_microusd = event.cost_microusd

        if not terminal_seen:
            raise RuntimeError("LLM stream ended without a terminal event")

        assistant_msg = _assistant_message_from_completion(
            session=session,
            turn=turn,
            content=full_text,
            model_used=model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        db.add(assistant_msg)
        await db.commit()
        committed = True
        yield ChatStreamEvent(
            type="usage",
            model_used=model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            provider_used=provider_used,
            cost_microusd=cost_microusd,
        )
        yield ChatStreamEvent(type="done", message_id=assistant_msg.id)
    except BaseException:
        if not committed:
            await db.rollback()
        raise
