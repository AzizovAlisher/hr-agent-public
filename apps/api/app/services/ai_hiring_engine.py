"""Bounded planning and assessment engine for AI hiring missions.

An organization-configured LLM may improve request interpretation, but the
entire request-to-shortlist flow has a deterministic local implementation.
Candidate ranking never depends on model-generated tool calls and consumes only
the masked evidence view produced by :mod:`ai_hiring_safety`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.parsing import extract_json_from_response
from app.llm import ChatMessage
from app.services.ai_hiring_safety import (
    candidate_extra_names,
    mask_candidate_text,
    normalized_terms,
    safe_evidence_excerpt,
    stable_input_hash,
)
from app.services.enterprise import organization_llm_client

AI_BRIEF_PROMPT_VERSION = "ai-hiring-brief-v1"
DETERMINISTIC_MODEL = "deterministic-local-v1"
_MAX_REQUEST_CHARS = 12_000


def is_russian_text(value: str) -> bool:
    """Return whether mission-facing text should use Russian.

    The web locale and persisted user locale can differ, so mission artifacts
    follow the language of the hiring request itself.
    """

    return bool(re.search(r"[\u0400-\u04ff]", value))


@dataclass(frozen=True)
class GeneratedMissionPlan:
    brief: dict[str, Any]
    rubric: dict[str, Any]
    model_used: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class CandidateAssessmentResult:
    assessment: dict[str, Any]
    evidence: list[dict[str, Any]]


def _clean_list(values: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()[:240]
            for item in values[:limit]
            if isinstance(item, str) and item.strip()
        )
    )


def _infer_headcount(request: str, default: int) -> int:
    word_values = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "один": 1, "одного": 1, "одну": 1, "два": 2, "двух": 2,
        "две": 2, "три": 3, "трех": 3, "трёх": 3, "четыре": 4,
        "четырех": 4, "четырёх": 4, "пять": 5, "пятерых": 5,
    }
    word_pattern = "|".join(sorted(map(re.escape, word_values), key=len, reverse=True))
    word_patterns = (
        rf"\b(?:hire|recruit|find|need|looking for)\s+({word_pattern})\b",
        rf"\b(?:нанять|найти|найди|подбери|нужно|требуется)\s+({word_pattern})\b",
    )
    for pattern in word_patterns:
        if match := re.search(pattern, request, re.IGNORECASE):
            return word_values[match.group(1).lower()]
    patterns = (
        r"\b(?:hire|recruit|find|need|looking for)\s+(\d{1,2})\b",
        r"\b(\d{1,2})\s+(?:people|engineers?|developers?|designers?|recruiters?|candidates?)\b",
        r"\b(?:нанять|найти|найди|подбери|нужно|требуется)\s+(\d{1,2})\b",
        r"\b(\d{1,2})\s+(?:человек|разработчик(?:а|ов)?|инженер(?:а|ов)?|кандидат(?:а|ов)?)\b",
    )
    for pattern in patterns:
        if match := re.search(pattern, request, re.IGNORECASE):
            return max(1, min(int(match.group(1)), 50))
    return max(1, min(default, 50))


def _infer_title(request: str) -> str:
    text = re.sub(r"\s+", " ", request).strip()
    patterns = (
        r"(?:hire|recruit|find|need|looking for)\s+(?:\d+\s+)?(?:an?\s+)?([^,.!?;]{2,80})",
        r"(?:нанять|найти|найди|подбери|нужен|нужна|нужно|требуется)\s+(?:\d+\s+)?([^,.!?;]{2,80})",
    )
    title = ""
    for pattern in patterns:
        if match := re.search(pattern, text, re.IGNORECASE):
            title = match.group(1)
            break
    if not title:
        # A short request is very often just the role title.
        title = text if 1 <= len(text.split()) <= 10 else ""
    title = re.split(
        r"\s+(?:in|within|with|for|by|at|в|на|со|с|до)\s+",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    title = re.sub(
        r"^(?:one|two|three|four|five|один|одного|одну|два|двух|две|три|"
        r"трех|трёх|четыре|четырех|четырёх|пять|пятерых)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^(?:a|an|the)\s+", "", title, flags=re.IGNORECASE).strip(" .,:;-")
    if re.fullmatch(
        r"(?:help|someone|somebody|a person|people|staff|talent|candidates?)"
        r"(?:\s+(?:asap|as soon as possible|urgently|срочно))?",
        title,
        re.IGNORECASE,
    ):
        return ""
    return title[:160]


def _infer_location(request: str) -> str | None:
    patterns = (
        r"\b(?:in|based in|location[:\s]+)\s*([A-ZА-ЯЁ][\wА-Яа-яЁё -]{2,60})",
        r"\b(?:в городе|локация[:\s]+|город[:\s]+|в)\s*([А-ЯЁA-Z][\wА-Яа-яЁё -]{2,60})",
    )
    for pattern in patterns:
        if match := re.search(pattern, request):
            return re.split(r"\s+(?:with|within|by|до|с|со)\s+", match.group(1), maxsplit=1)[0].strip()[:120]
    return None


def _infer_work_format(request: str) -> str | None:
    lowered = request.lower()
    if re.search(r"\b(remote|удаленн|удалённ)\w*\b", lowered):
        return "remote"
    if re.search(r"\b(hybrid|гибрид)\w*\b", lowered):
        return "hybrid"
    if re.search(r"\b(on[- ]?site|office|офис)\w*\b", lowered):
        return "onsite"
    return None


def _infer_compensation(request: str) -> dict[str, Any] | None:
    currency_match = re.search(r"\b(USD|EUR|TJS|RUB|сомони|руб(?:лей|ля)?|\$|€)\b", request, re.IGNORECASE)
    range_match = re.search(
        r"(?:salary|budget|compensation|зарплат\w*|бюджет)\D{0,20}"
        r"(\d[\d ,.]{1,12})\s*(?:-|–|—|to|до)\s*(\d[\d ,.]{1,12})",
        request,
        re.IGNORECASE,
    )
    if not range_match:
        return None

    def number(value: str) -> int:
        return int(re.sub(r"\D", "", value))

    low, high = number(range_match.group(1)), number(range_match.group(2))
    if high < low:
        low, high = high, low
    raw_currency = currency_match.group(1).lower() if currency_match else "TJS"
    currency = {
        "$": "USD", "€": "EUR", "сомони": "TJS", "руб": "RUB",
        "рубля": "RUB", "рублей": "RUB",
    }.get(raw_currency, raw_currency.upper())
    return {"min": low, "max": high, "currency": currency}


def _infer_deadline(request: str) -> str | None:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", request)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None


_SKILL_ALIASES = {
    "python": ["python"],
    "fastapi": ["fastapi"],
    "javascript": ["javascript", "typescript", "node.js", "react"],
    "typescript": ["typescript"],
    "java": ["java", "spring"],
    "golang": ["golang", "go"],
    "react": ["react", "next.js", "nextjs"],
    "sql": ["sql", "postgresql", "mysql"],
    "postgresql": ["postgresql", "postgres"],
    "aws": ["aws", "amazon web services"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "machine learning": ["machine learning", "ml", "pytorch", "tensorflow"],
    "sales": ["sales", "crm", "pipeline"],
    "recruiting": ["recruiting", "recruitment", "sourcing", "подбор"],
    "design": ["figma", "product design", "ux", "ui"],
}


def _skills_from_request(request: str) -> list[str]:
    lowered = request.lower()
    found: list[str] = []
    for label, aliases in _SKILL_ALIASES.items():
        if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered) for alias in aliases):
            found.append(label)
    return found


def deterministic_brief(
    request: str,
    *,
    default_headcount: int = 1,
    default_shortlist_size: int = 5,
    hiring_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Interpret a mission without network access or hidden assumptions."""

    defaults = hiring_defaults or {}
    request = request[:_MAX_REQUEST_CHARS].strip()
    headcount = _infer_headcount(request, int(defaults.get("headcount", default_headcount)))
    title = _infer_title(request)
    calculated_shortlist = max(5, headcount * 3)
    configured_shortlist = int(defaults.get("shortlist_size", default_shortlist_size))
    # The configured value is an organization preference, but never produces a
    # slate smaller than the mission default formula.
    shortlist_size = min(20, max(calculated_shortlist, configured_shortlist))
    skills = _skills_from_request(request)
    russian = is_russian_text(request)
    location = _infer_location(request) or defaults.get("location")
    work_format = _infer_work_format(request) or defaults.get("work_format")
    compensation = _infer_compensation(request) or defaults.get("compensation")
    deadline = _infer_deadline(request) or defaults.get("deadline")
    assumptions: list[str] = []
    if not re.search(r"\b\d{1,2}\b", request):
        assumptions.append(
            f"Количество сотрудников по умолчанию: {headcount}."
            if russian
            else f"Headcount defaulted to {headcount}."
        )
    if not skills and title:
        assumptions.append(
            "В начальной шкале используются критерии соответствия роли, поскольку явные навыки не указаны."
            if russian
            else "The initial rubric uses role-relevance criteria because no explicit skills were provided."
        )
    if not location:
        assumptions.append("Локация не ограничена." if russian else "Location is not constrained.")
    if not compensation:
        assumptions.append(
            "Компенсация не указана и не используется при ранжировании."
            if russian
            else "Compensation is unspecified and is not used in ranking."
        )
    blockers = (
        []
        if title
        else [
            "Не удалось определить название роли из запроса."
            if russian
            else "A role title could not be inferred from the request."
        ]
    )
    return {
        "role_title": title,
        "headcount": headcount,
        "responsibilities": (
            [
                f"Достигать результатов, ожидаемых от роли «{title}»."
                if russian
                else f"Deliver outcomes expected from the {title} role."
            ]
            if title
            else []
        ),
        "must_haves": skills,
        "preferences": [],
        "location": str(location)[:120] if location else None,
        "work_format": work_format if work_format in {"remote", "hybrid", "onsite"} else None,
        "compensation": compensation,
        "deadline": str(deadline) if deadline else None,
        "shortlist_size": shortlist_size,
        "assumptions": assumptions,
        "blockers": blockers,
    }


def deterministic_rubric(brief: dict[str, Any]) -> dict[str, Any]:
    role = str(brief.get("role_title") or "the role")
    russian = is_russian_text(role)
    must_haves = _clean_list(brief.get("must_haves"), limit=6)
    definitions: list[dict[str, Any]] = []
    for skill in must_haves:
        definitions.append(
            {
                "key": re.sub(r"[^a-z0-9]+", "_", skill.lower()).strip("_")[:48] or "required_skill",
                "label": skill,
                "description": (
                    f"Подтверждение применения навыка {skill} в релевантной работе."
                    if russian
                    else f"Evidence that the candidate can apply {skill} in relevant work."
                ),
                "must_have": True,
                "keywords": _SKILL_ALIASES.get(skill.lower(), [skill]),
            }
        )
    definitions.extend(
        [
            {
                "key": "role_relevance",
                "label": "Соответствие роли" if russian else "Role relevance",
                "description": (
                    f"Подтверждение опыта, напрямую связанного с ролью «{role}»."
                    if russian
                    else f"Evidence of work directly relevant to {role}."
                ),
                "must_have": False,
                "keywords": normalized_terms(role),
            },
            {
                "key": "relevant_experience",
                "label": "Релевантный опыт" if russian else "Relevant experience",
                "description": (
                    "Глубина и актуальность релевантного профессионального опыта."
                    if russian
                    else "Depth and recency of relevant professional experience."
                ),
                "must_have": False,
                "keywords": ["experience", "worked", "developed", "built", "led", "опыт", "работал", "разработал"],
            },
            {
                "key": "evidence_quality",
                "label": "Качество доказательств" if russian else "Evidence quality",
                "description": (
                    "Конкретные проверяемые примеры вместо неподтверждённых утверждений."
                    if russian
                    else "Specific, verifiable examples rather than unsupported claims."
                ),
                "must_have": False,
                "keywords": ["project", "result", "impact", "increased", "reduced", "проект", "результат", "%"],
            },
        ]
    )
    # Keep 3–10 unique criteria and distribute integer weights exactly to 100.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for definition in definitions:
        if definition["key"] in seen:
            continue
        seen.add(definition["key"])
        unique.append(definition)
    unique = unique[:10]
    base, remainder = divmod(100, len(unique))
    for index, definition in enumerate(unique):
        definition["weight"] = base + (1 if index < remainder else 0)
    return {"version": 1, "criteria": unique, "weight_total": 100}


def _validated_plan_payload(
    raw: dict[str, Any],
    fallback_brief: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("brief"), dict):
        return None
    candidate = raw["brief"]
    title = str(candidate.get("role_title") or fallback_brief["role_title"]).strip()[:160]
    if not title:
        return None
    headcount = max(1, min(int(candidate.get("headcount") or fallback_brief["headcount"]), 50))
    shortlist_size = max(1, min(int(candidate.get("shortlist_size") or max(5, headcount * 3)), 20))
    brief = {
        **fallback_brief,
        "role_title": title,
        "headcount": headcount,
        "responsibilities": _clean_list(candidate.get("responsibilities")) or fallback_brief["responsibilities"],
        "must_haves": _clean_list(candidate.get("must_haves"), limit=10),
        "preferences": _clean_list(candidate.get("preferences"), limit=10),
        "location": str(candidate["location"])[:120] if candidate.get("location") else fallback_brief["location"],
        "work_format": candidate.get("work_format") if candidate.get("work_format") in {"remote", "hybrid", "onsite"} else fallback_brief["work_format"],
        "shortlist_size": shortlist_size,
        "assumptions": _clean_list(candidate.get("assumptions")) or fallback_brief["assumptions"],
        "blockers": _clean_list(candidate.get("blockers")),
    }
    # Rubric generation remains deterministic and validated; the LLM helps
    # identify relevant criteria but cannot choose executable actions.
    rubric = deterministic_rubric(brief)
    return brief, rubric


async def generate_mission_plan(
    db: AsyncSession,
    *,
    organization_id: UUID,
    request: str,
    default_headcount: int = 1,
    default_shortlist_size: int = 5,
    hiring_defaults: dict[str, Any] | None = None,
    mission_id: UUID | None = None,
    model_budget_cents: int | None = None,
) -> GeneratedMissionPlan:
    fallback = deterministic_brief(
        request,
        default_headcount=default_headcount,
        default_shortlist_size=default_shortlist_size,
        hiring_defaults=hiring_defaults,
    )
    if fallback["blockers"]:
        return GeneratedMissionPlan(fallback, deterministic_rubric(fallback), DETERMINISTIC_MODEL, "local")

    system = (
        "You convert a recruiter's request into JSON only. Do not create actions. "
        "Do not infer protected traits or use contact availability as merit. "
        "Do not invent salary, deadline, location, or requirements. Return "
        "Keep all natural-language fields in the same language as the user request. "
        "{\"brief\":{role_title,headcount,responsibilities,must_haves,preferences,"
        "location,work_format,shortlist_size,assumptions,blockers}}."
    )
    try:
        client = await organization_llm_client(
            db,
            organization_id,
            budget_resource_id=mission_id,
            model_budget_cents=model_budget_cents,
        )
        completion = await client.complete(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=request[:_MAX_REQUEST_CHARS]),
            ],
            temperature=0.0,
            max_tokens=1_400,
            log=None,  # Raw prompts are intentionally not stored.
        )
        parsed = extract_json_from_response(completion.content)
        validated = _validated_plan_payload(parsed or {}, fallback)
        if validated is not None:
            brief, rubric = validated
            return GeneratedMissionPlan(
                brief,
                rubric,
                completion.model_used,
                type(client).__name__,
                completion.tokens_in,
                completion.tokens_out,
            )
    except Exception:  # noqa: BLE001 - policy-safe local fallback is deliberate
        # Missing credentials, unreachable private models, malformed JSON, and
        # provider failures all degrade to the local implementation.
        return GeneratedMissionPlan(
            fallback,
            deterministic_rubric(fallback),
            DETERMINISTIC_MODEL,
            "local",
        )
    return GeneratedMissionPlan(fallback, deterministic_rubric(fallback), DETERMINISTIC_MODEL, "local")


def assess_candidate(
    *,
    candidate_id: UUID,
    raw_cv: str | None,
    parsed_cv: dict[str, Any] | None,
    brief: dict[str, Any],
    rubric: dict[str, Any],
) -> CandidateAssessmentResult:
    safe_text = mask_candidate_text(raw_cv, extra_names=candidate_extra_names(parsed_cv))
    russian = is_russian_text(str(brief.get("role_title") or ""))
    lowered = safe_text.lower()
    criteria_results: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    weighted_score = 0.0
    matched_criteria = 0
    failed_must_haves: list[str] = []

    for criterion in rubric.get("criteria", []):
        key = str(criterion.get("key") or "criterion")
        terms = normalized_terms(criterion.get("keywords") or criterion.get("label") or "")
        matched = [term for term in terms if term in lowered]
        ratio = len(matched) / max(1, min(len(terms), 5))
        score = min(100, round(ratio * 100)) if terms else 0
        if matched:
            matched_criteria += 1
            excerpt = safe_evidence_excerpt(safe_text, matched)
            if excerpt:
                evidence.append(
                    {
                        "criterion_key": key,
                        "claim": (
                            f"В данных кандидата упоминаются: {', '.join(matched[:4])}."
                            if russian
                            else f"Candidate evidence mentions {', '.join(matched[:4])}."
                        ),
                        "source_type": "candidate_cv",
                        "source_id": str(candidate_id),
                        "source_locator": "masked_cv_excerpt",
                        "stance": "supports",
                        "confidence": "medium" if len(matched) == 1 else "high",
                        "content_hash": stable_input_hash(excerpt),
                        "excerpt": excerpt,
                    }
                )
        elif criterion.get("must_have"):
            failed_must_haves.append(str(criterion.get("label") or key))
        weight = int(criterion.get("weight") or 0)
        weighted_score += score * weight / 100
        criteria_results.append(
            {
                "key": key,
                "score": score,
                "matched_terms": matched[:8],
                # Populated with durable evidence IDs after persistence.
                "evidence_claim_ids": [],
            }
        )

    criterion_count = max(1, len(criteria_results))
    coverage = round(matched_criteria / criterion_count, 4)
    overall = round(weighted_score)
    if failed_must_haves:
        overall = min(overall, 49)
    confidence = "high" if coverage >= 0.7 else "medium" if coverage >= 0.4 else "low"
    if coverage < 0.3:
        recommendation = "insufficient_evidence"
    elif failed_must_haves:
        recommendation = "not_recommended"
    elif overall >= 80:
        recommendation = "strong_fit"
    elif overall >= 65:
        recommendation = "fit"
    elif overall >= 45:
        recommendation = "possible_fit"
    else:
        recommendation = "not_recommended"

    concerns = [
        (
            f"Не найдено подтверждений обязательного критерия: {item}."
            if russian
            else f"No evidence found for required criterion: {item}."
        )
        for item in failed_must_haves
    ]
    missing = [
        str(item.get("key"))
        for item in criteria_results
        if not item.get("matched_terms")
    ]
    assessment = {
        "candidate_id": str(candidate_id),
        "overall_score": overall,
        "confidence": confidence,
        "recommendation": recommendation,
        "criteria": criteria_results,
        "concerns": concerns,
        "missing_evidence": missing,
        "evidence_coverage": coverage,
        "concise_rationale": (
            (
                f"Доказательства подтверждают {matched_criteria} из {criterion_count} критериев; "
                f"для {len(failed_must_haves)} обязательных критериев данных недостаточно."
            )
            if russian
            else (
                f"Evidence supports {matched_criteria} of {criterion_count} criteria; "
                f"{len(failed_must_haves)} required criteria lack evidence."
            )
        ),
    }
    return CandidateAssessmentResult(assessment=assessment, evidence=evidence)


_CONFIDENCE_ORDER = {"high": 2, "medium": 1, "low": 0}


def shortlist_sort_key(assessment: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic newest-plan ranking, including stable UUID tie-breaker."""

    return (
        -int(assessment.get("overall_score") or 0),
        -float(assessment.get("evidence_coverage") or 0),
        -_CONFIDENCE_ORDER.get(str(assessment.get("confidence")), 0),
        str(assessment.get("candidate_id") or ""),
    )


def json_for_audit(value: Any) -> str:
    """Canonical serialization used when recording safe input/result hashes."""

    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
