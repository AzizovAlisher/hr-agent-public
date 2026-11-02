"""Generate the prose "who we are looking for" portrait for a position.

Two distinct texts come out of here and they must not be confused:

* ``candidate_profile`` — INTERNAL. A recruiter-facing portrait derived from
  the position's requirements. Free to name internal systems, because it never
  leaves the authenticated app.
* ``public_description`` — PUBLIC. A deliberately generalised stand-in for the
  real description, used when ``sensitivity != 'public'`` so a confidential
  role can still be advertised without exposing what is actually being built.

Both are stored on the position and are hand-editable afterwards; regenerating
overwrites. See app/services/public_positions.py for how the public one is
actually served.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.ai_boundary import (
    CandidateProfilePayload,
    PublicDescriptionPayload,
    normalize_ai_locale,
    output_language_instruction,
    trusted_context_block,
    validate_json_object,
)
from app.llm import ChatMessage, LogContext, get_llm_client
from app.models.position import Position
from app.services.enterprise import organization_llm_client
from app.services.public_positions import (
    SENSITIVITY_PUBLIC,
    SENSITIVITY_RESTRICTED,
    normalize_sensitivity,
)


# messages.purpose is String(32); chat history reads filter on purpose == "chat",
# so a distinct value here keeps these calls out of recruiters' chat windows.
_PURPOSE = "candidate_profile"

_MAX_SOURCE_CHARS = 6000


PROFILE_PROMPT = """You are an experienced technical recruiter. Draft the internal ideal-candidate brief for the role supplied in a trusted_recruiter_context JSON object.

RULES:
- Follow the caller's explicit RU/EN output-language directive.
- Return one strict JSON object with exactly these keys:
  {"looking_for":"one or two sentences","must_have":["skill"],"nice_to_have":["skill"],"signals":["concrete CV signal"]}
- Supply 1-20 non-negotiable skills, zero or more optional skills, and 1-10 concrete signals.
- Be specific to this role. Never write filler like "responsible and hardworking", "team player", or "strong communication skills" unless the requirements actually demand it.
- Derive everything from the requirements and description given. Do not invent technologies that are not there.
- Do not restate the salary, the employment type or the location — those are already shown as chips next to this text.

This is recruiter guidance for human review. Never make an automatic hiring or rejection decision.
OUTPUT: the JSON object only. No preamble or markdown fences.
"""


PUBLIC_DESCRIPTION_PROMPT = """You are a recruiting copywriter working under a confidentiality constraint. Rewrite the job description in the trusted_recruiter_context JSON object so it can be published on a PUBLIC page without revealing what the employer is actually building.

THIS ROLE IS CONFIDENTIAL. Read its sensitivity level and any additional redaction rules from the trusted context.

MUST REMOVE — these are the leak:
- Internal system, module, service and component names (e.g. "Tier 1: oauth, integration, process, external-task, payment" → "core platform modules").
- Internal tier/zone/segment classifications and anything that describes the system topology.
- The employer's name, brand, sector-identifying detail, and the names of partners, vendors or integrated third parties.
- Project code names, internal team names, ticket/product identifiers.
- Anything that would let a reader infer which organisation this is, or map its infrastructure.
MUST KEEP — otherwise the vacancy is useless:
- What the person will actually do, described generically ("develops core business logic of a high-load platform").
- The seniority and the general engineering domain.
- The tone and caller-selected RU/EN output language.

RULES:
- 1-3 sentences. Hard cap: 350 characters.
- Plain prose. No markdown, no lists, no quotes, no preamble.
- Never invent a fake employer, a fake industry or details that are not in the source.
- If after removing everything confidential there is nothing meaningful left, set sufficient to false and description to null.

OUTPUT: exactly one JSON object and no other text:
{"sufficient": true, "description": "1-3 public-safe sentences"}
or {"sufficient": false, "description": null}
"""

_RESTRICTED_EXTRA_RULES = """- Also remove the specific technology stack (named languages, frameworks, brokers, databases, CI systems). Refer to it only in general terms such as "современный JVM-стек" or "enterprise-стек". On a role of state importance the exact stack fingerprints the operator's infrastructure.
- Also remove any mention of compensation."""

def _cap_sentence(text: str, limit: int = 380, target: int = 350) -> str:
    """Hard-cap prose on a sentence boundary — mirrors condense_descriptions."""

    if len(text) <= limit:
        return text
    cut = text[:target]
    last_dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    trimmed = cut[: last_dot + 1] if last_dot > 200 else cut
    return trimmed.rstrip() + "…"


def _source_text(position: Position) -> str:
    """Richest available description, matching the batch-script convention."""

    source = position.full_description or position.description or ""
    return source[:_MAX_SOURCE_CHARS]


async def _client(db: AsyncSession, organization_id: UUID):
    return await organization_llm_client(
        db, organization_id, fallback_factory=get_llm_client
    )


async def generate_candidate_profile(
    db: AsyncSession,
    position: Position,
    organization_id: UUID,
    *,
    locale: str | None = None,
) -> str | None:
    """Produce the internal ideal-candidate brief. Returns None if unusable."""

    requirements = position.requirements if isinstance(position.requirements, dict) else {}
    output_locale = normalize_ai_locale(
        locale,
        trusted_text="\n".join(
            part
            for part in (position.title, _source_text(position))
            if isinstance(part, str)
        ),
    )
    language_instruction = output_language_instruction(output_locale)
    role_context = trusted_context_block(
        json.dumps(
            {
                "title": position.title or "",
                "description": _source_text(position),
                "requirements": requirements,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )[:_MAX_SOURCE_CHARS],
        label="position_profile_source",
    )
    prompt = f"{PROFILE_PROMPT}\n\n{language_instruction}\n\n{role_context}"

    llm = await _client(db, organization_id)
    result = await llm.complete(
        messages=[ChatMessage(role="user", content=prompt)],
        temperature=0.2,
        log=LogContext(
            db=db,
            organization_id=organization_id,
            session_id=None,
            purpose=_PURPOSE,
            request_role="user",
            prompt_version="candidate-profile-v2-structured",
        ),
    )
    try:
        parsed = validate_json_object(result.content, CandidateProfilePayload)
    except ValueError:
        return None
    labels = (
        ("ИЩЕМ", "ОБЯЗАТЕЛЬНО", "БУДЕТ ПЛЮСОМ", "НА ЧТО СМОТРЕТЬ В CV")
        if output_locale == "ru"
        else ("LOOKING FOR", "MUST HAVE", "NICE TO HAVE", "SIGNALS TO LOOK FOR")
    )
    blocks = [
        f"{labels[0]}\n{parsed.looking_for}",
        f"{labels[1]}\n{', '.join(parsed.must_have)}",
    ]
    if parsed.nice_to_have:
        blocks.append(f"{labels[2]}\n{', '.join(parsed.nice_to_have)}")
    blocks.append(f"{labels[3]}\n{' '.join(parsed.signals)}")
    return "\n\n".join(blocks)


async def generate_public_description(
    db: AsyncSession,
    position: Position,
    organization_id: UUID,
    *,
    locale: str | None = None,
) -> str | None:
    """Produce the redacted public stand-in description for a confidential role.

    Returns None when the role is public (nothing to redact) or when the model
    reports that nothing publishable survives redaction — in which case the
    public page shows no description at all, which is the safe outcome.
    """

    sensitivity = normalize_sensitivity(position.sensitivity)
    if sensitivity == SENSITIVITY_PUBLIC:
        return None

    source = _source_text(position)
    if not source.strip():
        return None

    extra = (
        _RESTRICTED_EXTRA_RULES if sensitivity == SENSITIVITY_RESTRICTED else ""
    )
    output_locale = normalize_ai_locale(locale, trusted_text=source)
    language_instruction = output_language_instruction(output_locale)
    source_context = trusted_context_block(
        json.dumps(
            {
                "sensitivity": sensitivity,
                "description": source,
                "additional_redaction_rules": extra,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        label="confidential_position_description",
    )
    prompt = f"{PUBLIC_DESCRIPTION_PROMPT}\n\n{language_instruction}\n\n{source_context}"

    llm = await _client(db, organization_id)
    result = await llm.complete(
        messages=[ChatMessage(role="user", content=prompt)],
        temperature=0.2,
        log=LogContext(
            db=db,
            organization_id=organization_id,
            session_id=None,
            purpose=_PURPOSE,
            request_role="user",
            prompt_version="public-description-v2-structured-redaction",
        ),
    )
    try:
        parsed = validate_json_object(result.content, PublicDescriptionPayload)
    except ValueError:
        return None
    if not parsed.sufficient or not parsed.description:
        return None
    return _cap_sentence(parsed.description)
