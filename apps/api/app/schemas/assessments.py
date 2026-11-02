"""Versioned native-assessment contracts.

Internal contracts include grading and knockout configuration. Public
contracts deliberately omit both so an invitation token cannot reveal keys.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

QuestionType = Literal[
    "boolean", "single", "multi", "number", "short", "long", "file", "url"
]
ASSESSMENT_FILE_MAX_BYTES = 15 * 1024 * 1024
ASSESSMENT_FILE_EXTENSIONS = {"pdf", "docx", "txt", "png", "jpg", "jpeg"}
ConditionOperator = Literal[
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "in",
    "answered",
    "not_answered",
    "gt",
    "gte",
    "lt",
    "lte",
]

CONDITION_OPERATORS_BY_QUESTION_TYPE: dict[str, frozenset[str]] = {
    "boolean": frozenset({"equals", "not_equals", "answered", "not_answered"}),
    "single": frozenset({"equals", "not_equals", "in", "answered", "not_answered"}),
    "multi": frozenset({"contains", "not_contains", "answered", "not_answered"}),
    "number": frozenset(
        {
            "equals",
            "not_equals",
            "answered",
            "not_answered",
            "gt",
            "gte",
            "lt",
            "lte",
        }
    ),
    "short": frozenset(
        {
            "equals",
            "not_equals",
            "contains",
            "not_contains",
            "in",
            "answered",
            "not_answered",
        }
    ),
    "long": frozenset(
        {
            "equals",
            "not_equals",
            "contains",
            "not_contains",
            "in",
            "answered",
            "not_answered",
        }
    ),
    "url": frozenset(
        {
            "equals",
            "not_equals",
            "contains",
            "not_contains",
            "in",
            "answered",
            "not_answered",
        }
    ),
    "file": frozenset({"answered", "not_answered"}),
}
_VALUELESS_OPERATORS = {"answered", "not_answered"}


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return number if number.is_finite() else None


def _validate_typed_rule(
    rule: "AssessmentCondition | AssessmentKnockoutRule",
    *,
    question_type: QuestionType,
    option_values: list[str],
    label: str,
) -> None:
    allowed = CONDITION_OPERATORS_BY_QUESTION_TYPE[question_type]
    if rule.operator not in allowed:
        raise ValueError(
            f"{label} operator '{rule.operator}' is not valid for "
            f"{question_type} questions"
        )
    if rule.operator in _VALUELESS_OPERATORS:
        if rule.value is not None:
            raise ValueError(f"{label} {rule.operator} does not accept a value")
        return

    value = rule.value
    if question_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{label} requires a boolean value")
        return
    if question_type == "single":
        if rule.operator == "in":
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(item, str) for item in value)
                or not set(value).issubset(set(option_values))
            ):
                raise ValueError(
                    f"{label} in value must be a non-empty list of option values"
                )
        elif not isinstance(value, str) or value not in option_values:
            raise ValueError(f"{label} value must match an option")
        return
    if question_type == "multi":
        if not isinstance(value, str) or value not in option_values:
            raise ValueError(f"{label} value must match an option")
        return
    if question_type == "number":
        if _finite_decimal(value) is None:
            raise ValueError(f"{label} requires a finite numeric value")
        return
    if question_type in {"short", "long", "url"}:
        if rule.operator == "in":
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(
                    f"{label} in value must be a non-empty list of strings"
                )
        elif not isinstance(value, str):
            raise ValueError(f"{label} requires a text value")
        return
    raise ValueError(f"{label} is not valid for {question_type} questions")


def _clean(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be blank")
    return cleaned


class AssessmentOption(BaseModel):
    value: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=500)

    _clean_value = field_validator("value")(_clean)
    _clean_label = field_validator("label")(_clean)


class AssessmentCondition(BaseModel):
    question_key: str = Field(min_length=1, max_length=64)
    operator: ConditionOperator = "equals"
    value: Any = None


class AssessmentConditionGroup(BaseModel):
    mode: Literal["all", "any"] = "all"
    rules: list[AssessmentCondition] = Field(min_length=1, max_length=20)


class AssessmentValidationRule(BaseModel):
    min_length: int | None = Field(default=None, ge=0, le=100_000)
    max_length: int | None = Field(default=None, ge=1, le=100_000)
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    allowed_file_extensions: list[str] = Field(default_factory=list, max_length=50)
    max_file_size_bytes: int | None = Field(
        default=None, ge=1, le=ASSESSMENT_FILE_MAX_BYTES
    )

    @model_validator(mode="after")
    def valid_ranges(self) -> "AssessmentValidationRule":
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("min_value cannot exceed max_value")
        self.allowed_file_extensions = [
            item.strip().lower().lstrip(".") for item in self.allowed_file_extensions
        ]
        if any(not item for item in self.allowed_file_extensions):
            raise ValueError("file extensions cannot be blank")
        unsupported = set(self.allowed_file_extensions) - ASSESSMENT_FILE_EXTENSIONS
        if unsupported:
            raise ValueError(
                "unsupported assessment file extension: "
                + ", ".join(sorted(unsupported))
            )
        return self


class AssessmentScoringRule(BaseModel):
    correct: Any = None
    accepted_values: list[str] = Field(default_factory=list, max_length=500)
    tolerance: Decimal | None = Field(default=None, ge=0)
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    partial_credit: bool = False
    case_sensitive: bool = False


class AssessmentKnockoutRule(BaseModel):
    operator: ConditionOperator
    value: Any = None
    reason: str | None = Field(default=None, max_length=2_000)


class AssessmentQuestionCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    question_type: QuestionType
    prompt: str = Field(min_length=1, max_length=10_000)
    description: str | None = Field(default=None, max_length=10_000)
    required: bool = False
    options: list[AssessmentOption] = Field(default_factory=list, max_length=100)
    validation: AssessmentValidationRule = Field(
        default_factory=AssessmentValidationRule
    )
    condition: AssessmentCondition | AssessmentConditionGroup | None = None
    scoring: AssessmentScoringRule | None = None
    knockout: AssessmentKnockoutRule | None = None
    max_score: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("1000000"))

    _clean_prompt = field_validator("prompt")(_clean)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        if not normalized:
            raise ValueError("question key must contain a letter or number")
        return normalized

    @model_validator(mode="after")
    def validate_type_configuration(self) -> "AssessmentQuestionCreate":
        option_values = [option.value for option in self.options]
        if self.question_type in {"single", "multi"}:
            if len(self.options) < 2:
                raise ValueError(
                    "single and multi questions require at least two options"
                )
            if len(set(option_values)) != len(option_values):
                raise ValueError("option values must be unique")
        elif self.options:
            raise ValueError("options are only valid for single and multi questions")

        if self.knockout is not None:
            _validate_typed_rule(
                self.knockout,
                question_type=self.question_type,
                option_values=option_values,
                label="knockout rule",
            )

        if self.scoring is not None and self.max_score == 0:
            raise ValueError("a scoring rule requires max_score greater than zero")
        if self.scoring is not None and self.question_type == "file":
            raise ValueError("file questions cannot be automatically scored")
        if self.scoring is not None and self.question_type == "boolean":
            if not isinstance(self.scoring.correct, bool):
                raise ValueError("boolean scoring requires a boolean correct value")
        if self.scoring is not None and self.question_type == "single":
            if self.scoring.correct not in option_values:
                raise ValueError("single correct value must match an option")
        if self.scoring is not None and self.question_type == "multi":
            correct = self.scoring.correct
            if not isinstance(correct, list) or not correct:
                raise ValueError("multi scoring requires a non-empty correct list")
            if not set(correct).issubset(set(option_values)):
                raise ValueError("multi correct values must match options")
        if self.scoring is not None and self.question_type == "number":
            has_exact = self.scoring.correct is not None
            has_range = (
                self.scoring.min_value is not None or self.scoring.max_value is not None
            )
            if has_exact == has_range:
                raise ValueError(
                    "number scoring requires exactly one of an exact value or range"
                )
            if has_exact:
                if _finite_decimal(self.scoring.correct) is None:
                    raise ValueError("number scoring correct value must be finite")
                if (
                    self.scoring.tolerance is not None
                    and not self.scoring.tolerance.is_finite()
                ):
                    raise ValueError("number scoring tolerance must be finite")
            else:
                if self.scoring.min_value is None or self.scoring.max_value is None:
                    raise ValueError("number scoring range requires both bounds")
                if (
                    not self.scoring.min_value.is_finite()
                    or not self.scoring.max_value.is_finite()
                ):
                    raise ValueError("number scoring range bounds must be finite")
                if self.scoring.min_value > self.scoring.max_value:
                    raise ValueError("number scoring minimum cannot exceed maximum")
                if self.scoring.tolerance is not None:
                    raise ValueError(
                        "number scoring tolerance is only valid for an exact value"
                    )
        if self.scoring is not None and self.question_type in {
            "short",
            "long",
            "url",
        }:
            accepted = self.scoring.accepted_values
            if not accepted or any(not item.strip() for item in accepted):
                raise ValueError("text scoring requires non-blank accepted values")
        return self


class AssessmentSectionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    questions: list[AssessmentQuestionCreate] = Field(min_length=1, max_length=200)

    _clean_title = field_validator("title")(_clean)


class AssessmentVersionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    instructions: str | None = Field(default=None, max_length=20_000)
    passing_percentage: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=10_080)
    sections: list[AssessmentSectionCreate] = Field(min_length=1, max_length=50)

    _clean_title = field_validator("title")(_clean)

    @model_validator(mode="after")
    def validate_question_graph(self) -> "AssessmentVersionCreate":
        questions = [
            question for section in self.sections for question in section.questions
        ]
        keys = [question.key for question in questions]
        if len(keys) != len(set(keys)):
            raise ValueError("question keys must be unique within a version")
        known: dict[str, AssessmentQuestionCreate] = {}
        for question in questions:
            if question.condition is not None:
                rules = (
                    question.condition.rules
                    if isinstance(question.condition, AssessmentConditionGroup)
                    else [question.condition]
                )
                for rule in rules:
                    dependency = rule.question_key
                    if dependency not in known:
                        raise ValueError(
                            "conditions may only reference earlier questions"
                        )
                    if dependency == question.key:
                        raise ValueError("a question cannot depend on itself")
                    source = known[dependency]
                    _validate_typed_rule(
                        rule,
                        question_type=source.question_type,
                        option_values=[option.value for option in source.options],
                        label=f"condition on '{question.key}'",
                    )
            known[question.key] = question
        total_maximum = sum(
            (question.max_score for question in questions), Decimal("0")
        )
        if total_maximum <= 0:
            raise ValueError(
                "assessment versions require at least one question with a positive maximum score"
            )
        return self


class AssessmentTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=10_000)

    _clean_name = field_validator("name")(_clean)


class AssessmentTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=10_000)
    status: Literal["active", "archived"] | None = None

    _clean_name = field_validator("name")(_clean)

    @model_validator(mode="after")
    def require_change(self) -> "AssessmentTemplateUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        return self


class AssessmentQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    question_type: QuestionType
    prompt: str
    description: str | None
    required: bool
    position: int
    options: list[dict[str, Any]]
    validation: dict[str, Any]
    condition: dict[str, Any]
    scoring: dict[str, Any]
    knockout: dict[str, Any]
    max_score: Decimal


class AssessmentSectionOut(BaseModel):
    id: UUID
    title: str
    description: str | None
    position: int
    questions: list[AssessmentQuestionOut]


class AssessmentVersionOut(BaseModel):
    id: UUID
    template_id: UUID
    version: int
    title: str
    instructions: str | None
    passing_percentage: Decimal
    time_limit_minutes: int | None
    published_at: datetime
    sections: list[AssessmentSectionOut]


class AssessmentTemplateOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: Literal["active", "archived"]
    created_at: datetime
    updated_at: datetime
    versions: list[AssessmentVersionOut] = Field(default_factory=list)


class AssessmentAccommodations(BaseModel):
    extra_time_minutes: int = Field(default=0, ge=0, le=1_440)
    breaks_allowed: bool = Field(
        default=False,
        description="The candidate may step away, but the assessment timer continues.",
    )
    screen_reader_optimized: bool = Field(
        default=False,
        description=(
            "Records that screen-reader support was requested; it does not certify "
            "compatibility with every assistive-technology setup."
        ),
    )


class AssessmentInvitationCreate(BaseModel):
    template_version_id: UUID
    expires_at: datetime
    max_attempts: int = Field(default=1, ge=1, le=20)
    recipient_email: EmailStr | None = None
    accommodations: AssessmentAccommodations = Field(
        default_factory=AssessmentAccommodations
    )

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class AssessmentInvitationReissueRequest(BaseModel):
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class AssessmentInvitationOut(BaseModel):
    id: UUID
    application_id: UUID
    template_version_id: UUID
    token_prefix: str
    recipient_email: str | None
    status: Literal["active", "revoked", "completed", "expired"]
    expires_at: datetime
    max_attempts: int
    authorized_attempts: int
    accommodations: AssessmentAccommodations | None = None
    first_opened_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class AssessmentInvitationCreated(AssessmentInvitationOut):
    token: str


class PublicAssessmentQuestion(BaseModel):
    id: UUID
    key: str
    question_type: QuestionType
    prompt: str
    description: str | None
    required: bool
    position: int
    options: list[dict[str, Any]]
    validation: dict[str, Any]
    condition: dict[str, Any]


class PublicAssessmentSection(BaseModel):
    id: UUID
    title: str
    description: str | None
    position: int
    questions: list[PublicAssessmentQuestion]


class PublicAssessmentResultOut(BaseModel):
    attempt_id: UUID
    submitted_at: datetime
    percentage: Decimal
    passed: bool
    needs_manual_review: bool


class PublicAssessmentLaunch(BaseModel):
    invitation_id: UUID
    title: str
    instructions: str | None
    expires_at: datetime
    time_limit_minutes: int | None
    max_attempts: int
    authorized_attempts: int
    attempts_started: int
    attempts_remaining: int
    can_start_attempt: bool
    accommodations: AssessmentAccommodations
    sections: list[PublicAssessmentSection]
    attempt: "AssessmentAttemptOut | None" = None
    result: PublicAssessmentResultOut | None = None


class AssessmentAnswerInput(BaseModel):
    question_id: UUID
    value: Any


class AssessmentAutosaveRequest(BaseModel):
    answers: list[AssessmentAnswerInput] = Field(min_length=1, max_length=200)

    @field_validator("answers")
    @classmethod
    def unique_questions(
        cls, answers: list[AssessmentAnswerInput]
    ) -> list[AssessmentAnswerInput]:
        ids = [answer.question_id for answer in answers]
        if len(ids) != len(set(ids)):
            raise ValueError("each question may appear only once per autosave")
        return answers


class AssessmentAnswerOut(BaseModel):
    question_id: UUID
    value: Any
    updated_at: datetime


class AssessmentAttemptOut(BaseModel):
    id: UUID
    invitation_id: UUID
    sequence: int
    status: Literal["in_progress", "submitted", "expired"]
    started_at: datetime
    last_saved_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    answers: list[AssessmentAnswerOut] = Field(default_factory=list)


class AssessmentKnockoutReviewInput(BaseModel):
    outcome: Literal["confirmed", "cleared"]
    reason: str = Field(min_length=1, max_length=4_000)

    _clean_reason = field_validator("reason")(_clean)


class AssessmentKnockoutReviewOut(AssessmentKnockoutReviewInput):
    reviewed_by_user_id: UUID
    reviewed_at: datetime


class AssessmentResultOut(BaseModel):
    id: UUID
    attempt_id: UUID
    application_id: UUID
    raw_score: Decimal
    max_score: Decimal
    percentage: Decimal
    passed: bool
    knocked_out: bool
    knockout_question_id: UUID | None
    knockout_reason: str | None
    knockout_review: AssessmentKnockoutReviewOut | None = None
    needs_manual_review: bool
    breakdown: list[dict[str, Any]]
    graded_at: datetime


class AssessmentManualGradeInput(BaseModel):
    question_id: UUID
    score: Decimal = Field(ge=0, le=Decimal("1000000"))
    evidence_comment: str = Field(min_length=1, max_length=4_000)

    _clean_evidence = field_validator("evidence_comment")(_clean)


class AssessmentManualGradeRequest(BaseModel):
    expected_graded_at: datetime
    grades: list[AssessmentManualGradeInput] = Field(
        default_factory=list,
        max_length=200,
    )
    knockout_review: AssessmentKnockoutReviewInput | None = None

    @field_validator("expected_graded_at")
    @classmethod
    def aware_expected_version(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_graded_at must include a timezone")
        return value

    @field_validator("grades")
    @classmethod
    def unique_manual_questions(
        cls, grades: list[AssessmentManualGradeInput]
    ) -> list[AssessmentManualGradeInput]:
        ids = [grade.question_id for grade in grades]
        if len(ids) != len(set(ids)):
            raise ValueError("each manual question may be graded only once")
        return grades

    @model_validator(mode="after")
    def require_review_work(self) -> "AssessmentManualGradeRequest":
        if not self.grades and self.knockout_review is None:
            raise ValueError("manual grades or a knockout review are required")
        return self


class AssessmentFileUploadOut(BaseModel):
    file_id: UUID
    question_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    status: Literal["ready"]


class AssessmentReviewerFileOut(AssessmentFileUploadOut):
    download_url: str


class AssessmentReviewerResultSummary(BaseModel):
    id: UUID
    raw_score: Decimal
    max_score: Decimal
    percentage: Decimal
    passed: bool
    knocked_out: bool
    knockout_question_id: UUID | None
    knockout_reason: str | None
    knockout_review: AssessmentKnockoutReviewOut | None = None
    needs_manual_review: bool
    graded_at: datetime


class AssessmentAttemptSummaryOut(BaseModel):
    id: UUID
    invitation_id: UUID
    sequence: int
    status: Literal["in_progress", "submitted", "expired"]
    started_at: datetime
    last_saved_at: datetime
    expires_at: datetime
    submitted_at: datetime | None
    result: AssessmentReviewerResultSummary | None = None
    files: list[AssessmentReviewerFileOut] = Field(default_factory=list)


class AssessmentSubmissionOut(BaseModel):
    attempt: AssessmentAttemptOut
    result: AssessmentResultOut


class PublicAssessmentSubmissionOut(BaseModel):
    """Candidate-safe receipt without reviewer evidence or internal identifiers."""

    attempt: AssessmentAttemptOut
    result: PublicAssessmentResultOut


PublicAssessmentLaunch.model_rebuild()
