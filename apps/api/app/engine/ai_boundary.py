"""Shared safety and validation contracts for legacy AI-assisted features.

Candidate artifacts are evidence, never instructions.  This module turns them
into a bounded JSON value after removing PII and common prompt-injection text;
JSON encoding prevents candidate text from closing or forging the surrounding
prompt structure.  It also owns the strict response schemas used before any AI
output is persisted.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from app.services.ai_hiring_safety import mask_candidate_text

AILocale = Literal["ru", "en"]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]

_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,30}\d(?!\w)")


def normalize_ai_locale(value: str | None, *, trusted_text: str = "") -> AILocale:
    """Return a supported output locale without consulting candidate evidence."""

    normalized = (value or "").strip().casefold()
    if normalized in {"ru", "en"}:
        return normalized  # type: ignore[return-value]
    cyrillic = len(_CYRILLIC_RE.findall(trusted_text))
    latin = len(_LATIN_RE.findall(trusted_text))
    return "ru" if cyrillic > latin else "en"


def output_language_instruction(locale: AILocale) -> str:
    return (
        "Write every human-readable output field in Russian."
        if locale == "ru"
        else "Write every human-readable output field in English."
    )


def has_contact_marker(value: str | None) -> bool:
    """Detect actionability before PII is removed from model input."""

    if not value:
        return False
    if _EMAIL_RE.search(value):
        return True
    return any(
        9 <= sum(character.isdigit() for character in match.group(0)) <= 16
        for match in _PHONE_CANDIDATE_RE.finditer(value)
    )


def trusted_context_block(value: str, *, label: str) -> str:
    """Encode recruiter-authored context without ambiguous text delimiters."""

    return json.dumps(
        {"classification": "trusted_recruiter_context", "label": label, "content": value},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def untrusted_candidate_block(
    value: str | None,
    *,
    label: str,
    extra_names: tuple[str, ...] = (),
    max_chars: int,
) -> str:
    """Return a JSON-encoded, PII-masked candidate-evidence envelope.

    The content is truncated *after* masking.  Even if the source contains a
    closing tag, JSON quote, or role marker, it remains a string value inside
    one untrusted envelope rather than changing the prompt's structure.
    """

    safe = mask_candidate_text(value, extra_names=extra_names)
    if len(safe) > max_chars:
        safe = safe[:max_chars].rstrip() + "\n[truncated]"
    return json.dumps(
        {
            "classification": "untrusted_candidate_evidence",
            "label": label,
            "content": safe,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class StrictAIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScoringCriterion(StrictAIModel):
    score: int = Field(ge=0, le=100)
    comment: NonEmptyText


class ScoringPayload(StrictAIModel):
    technical_skills: ScoringCriterion
    quality_of_projects: ScoringCriterion
    previous_experience: ScoringCriterion
    impact: ScoringCriterion


class InterviewQuestionPayload(StrictAIModel):
    category: Literal["technical", "behavioral", "situational", "culture"]
    question_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1_200),
    ]
    rationale: NonEmptyText
    expected_signals: NonEmptyText


class InterviewPayload(StrictAIModel):
    questions: list[InterviewQuestionPayload] = Field(min_length=1, max_length=30)

    @field_validator("questions")
    @classmethod
    def questions_are_unique(
        cls, value: list[InterviewQuestionPayload]
    ) -> list[InterviewQuestionPayload]:
        normalized = [item.question_text.casefold() for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Interview questions must be unique")
        return value


class GitHubSummaryPayload(StrictAIModel):
    summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
    ]


class TaskEvaluationPayload(StrictAIModel):
    score: int = Field(ge=0, le=100)
    summary: NonEmptyText
    strengths: list[NonEmptyText] = Field(default_factory=list, max_length=10)
    concerns: list[NonEmptyText] = Field(default_factory=list, max_length=10)
    recommendation: Literal["strong_yes", "yes", "maybe", "no"]


class CandidateProfilePayload(StrictAIModel):
    looking_for: NonEmptyText
    must_have: list[NonEmptyText] = Field(min_length=1, max_length=20)
    nice_to_have: list[NonEmptyText] = Field(default_factory=list, max_length=20)
    signals: list[NonEmptyText] = Field(min_length=1, max_length=10)


class PublicDescriptionPayload(StrictAIModel):
    sufficient: bool
    description: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=380),
    ] = None

    @model_validator(mode="after")
    def description_matches_sufficiency(self):
        if self.sufficient and self.description is None:
            raise ValueError("description is required when sufficient is true")
        if not self.sufficient and self.description is not None:
            raise ValueError("description must be null when sufficient is false")
        return self


AIModel = TypeVar("AIModel", bound=BaseModel)


def validate_json_object(text: str, schema: type[AIModel]) -> AIModel:
    """Extract one JSON object and validate it against a strict schema."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    candidates = [stripped]
    match = _JSON_OBJECT_RE.search(stripped)
    if match is not None and match.group(0) != stripped:
        candidates.append(match.group(0))
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if not isinstance(value, dict):
                raise ValueError("AI response must be a JSON object")
            return schema.model_validate(value)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
    raise ValueError("AI response did not match the required schema") from last_error
