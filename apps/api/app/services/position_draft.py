"""AI-guided position drafting.

Calls the LLM with conversation history + a strict-JSON system prompt and
returns the next assistant message + the current best-effort structured draft.
"""

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.draft_prompts import DRAFT_SYSTEM_PROMPT, PROJECT_EXTRACT_PROMPT
from app.llm import ChatMessage, LogContext, get_llm_client
from app.schemas.position_draft import (
    DraftConverseResponse,
    DraftMessage,
    PositionDraft,
    ProjectExtractResponse,
    QuickReply,
)
from app.services.enterprise import organization_llm_client


_VALID_FIELDS = {
    "seniority",
    "work_format",
    "employment_type",
    "team_size",
    "location",
}


REQUIRED_FIELDS = ("title", "description", "requirements")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text


def _extract_json(text: str) -> dict[str, Any] | None:
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
    return None


def _missing_required(draft: PositionDraft) -> list[str]:
    missing = []
    if not draft.title or not draft.title.strip():
        missing.append("title")
    if not draft.description or not draft.description.strip():
        missing.append("description")
    if not draft.requirements or not isinstance(draft.requirements, dict) or len(draft.requirements) == 0:
        missing.append("requirements")
    return missing


async def converse(
    messages: list[DraftMessage],
    current_draft: PositionDraft | None,
    *,
    db: AsyncSession,
    organization_id: UUID,
) -> DraftConverseResponse:
    """Generate the next assistant turn and updated draft."""
    llm = await organization_llm_client(
        db, organization_id, fallback_factory=get_llm_client
    )

    # Build the message stream: system prompt + conversation. We also remind the
    # model of its current accumulated draft so it doesn't drop fields it already
    # knows when extracting from the latest user turn.
    system = DRAFT_SYSTEM_PROMPT
    if current_draft is not None:
        system += "\n\nCURRENT DRAFT SO FAR (preserve known values, refine as needed):\n"
        system += json.dumps(
            current_draft.model_dump(exclude_none=False), ensure_ascii=False, indent=2
        )

    chat: list[ChatMessage] = [ChatMessage(role="system", content=system)]
    for m in messages:
        chat.append(ChatMessage(role=m.role, content=m.content))

    completion = await llm.complete(
        messages=chat,
        temperature=0.3,
        log=LogContext(
            db=db,
            organization_id=organization_id,
            purpose="position_draft_conversation",
            prompt_version="position-draft-conversation-v1",
        ),
    )
    parsed = _extract_json(completion.content)

    if parsed is None:
        # Fallback: surface the raw text as the assistant message and keep the
        # current draft as-is. This shouldn't happen with a competent model.
        return DraftConverseResponse(
            assistant_message=completion.content
            or "Sorry, I couldn't parse my own response. Try rephrasing?",
            draft=current_draft or PositionDraft(),
            is_complete=False,
            missing_required=_missing_required(current_draft or PositionDraft()),
            model_used=completion.model_used,
        )

    raw_draft = parsed.get("draft") or {}
    try:
        draft = PositionDraft.model_validate(raw_draft)
    except Exception:
        draft = current_draft or PositionDraft()

    missing = _missing_required(draft)
    is_complete = bool(parsed.get("is_complete")) and not missing

    quick_replies: list[QuickReply] = []
    for raw in parsed.get("quick_replies") or []:
        if not isinstance(raw, dict):
            continue
        try:
            qr = QuickReply.model_validate(raw)
        except Exception:
            continue
        if qr.field not in _VALID_FIELDS:
            continue
        if not qr.value or not qr.label:
            continue
        quick_replies.append(qr)

    return DraftConverseResponse(
        assistant_message=str(parsed.get("assistant_message") or "").strip()
        or "What else should I know?",
        draft=draft,
        is_complete=is_complete,
        missing_required=missing,
        quick_replies=quick_replies,
        model_used=completion.model_used,
    )


_FOLDER_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def _slugify_folder(value: str | None) -> str | None:
    """Coerce an LLM-suggested folder name into a safe, dashboard-friendly slug.

    Keeps ASCII lowercase + digits + hyphens, collapses runs, caps at 40 chars.
    Returns None for empty input so the UI can decide on its own default."""
    if not value:
        return None
    v = value.strip().lower().replace(" ", "-").replace("_", "-")
    v = _FOLDER_SLUG_RE.sub("", v)
    v = re.sub(r"-+", "-", v).strip("-")
    if not v:
        return None
    return v[:40]


async def extract_project(
    *,
    text: str,
    source_filename: str | None,
    db: AsyncSession,
    organization_id: UUID,
) -> ProjectExtractResponse:
    """Parse a project-hiring document into a list of position drafts.

    Single LLM call, strict-JSON output. Empty `positions` is a valid result
    (e.g. unreadable doc) — the UI surfaces "no positions detected" in that case.
    """
    llm = await organization_llm_client(
        db, organization_id, fallback_factory=get_llm_client
    )
    chat: list[ChatMessage] = [
        ChatMessage(role="system", content=PROJECT_EXTRACT_PROMPT),
        ChatMessage(role="user", content=text),
    ]
    completion = await llm.complete(
        messages=chat,
        temperature=0.2,
        log=LogContext(
            db=db,
            organization_id=organization_id,
            purpose="position_project_extract",
            prompt_version="position-project-extract-v1",
        ),
    )
    parsed = _extract_json(completion.content)
    if parsed is None:
        return ProjectExtractResponse(
            source_filename=source_filename,
            source_chars=len(text),
            model_used=completion.model_used,
        )

    raw_positions = parsed.get("positions") or []
    positions: list[PositionDraft] = []
    if isinstance(raw_positions, list):
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            try:
                positions.append(PositionDraft.model_validate(raw))
            except Exception:
                # Skip malformed entries silently — the recruiter can add the
                # missing role manually if anything important got dropped.
                continue

    project_name = parsed.get("project_name")
    if isinstance(project_name, str):
        project_name = project_name.strip() or None
    else:
        project_name = None

    folder = _slugify_folder(parsed.get("folder") if isinstance(parsed.get("folder"), str) else None)
    if not folder:
        folder = _slugify_folder(project_name)

    return ProjectExtractResponse(
        project_name=project_name,
        folder=folder,
        positions=positions,
        source_filename=source_filename,
        source_chars=len(text),
        model_used=completion.model_used,
    )
