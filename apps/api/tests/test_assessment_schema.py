"""Pure publish-contract regressions for native assessment graphs."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from app.auth.authorization import (
    FIELD_DEFAULT_ROLES,
    ROLE_DEFAULTS,
    FieldClass,
    ResourceScope,
    operation_policy,
)
from app.schemas.assessments import (
    AssessmentCondition,
    AssessmentConditionGroup,
    AssessmentKnockoutRule,
    AssessmentManualGradeRequest,
    AssessmentOption,
    AssessmentQuestionCreate,
    AssessmentScoringRule,
    AssessmentSectionCreate,
    AssessmentVersionCreate,
)
from pydantic import ValidationError


def test_assessment_graph_rejects_type_incompatible_rules():
    scored_boolean = AssessmentQuestionCreate(
        key="consent",
        question_type="boolean",
        prompt="Consent",
        scoring=AssessmentScoringRule(correct=True),
        max_score=1,
    )
    with pytest.raises(ValueError, match="contains.*boolean"):
        AssessmentVersionCreate(
            title="Invalid boolean condition",
            sections=[
                AssessmentSectionCreate(
                    title="Core",
                    questions=[
                        scored_boolean,
                        AssessmentQuestionCreate(
                            key="follow_up",
                            question_type="short",
                            prompt="Follow up",
                            condition=AssessmentCondition(
                                question_key="consent",
                                operator="contains",
                                value=True,
                            ),
                        ),
                    ],
                )
            ],
        )

    with pytest.raises(ValueError, match="knockout rule operator.*number"):
        AssessmentQuestionCreate(
            key="years",
            question_type="number",
            prompt="Years",
            knockout=AssessmentKnockoutRule(operator="contains", value=3),
        )

    with pytest.raises(ValueError, match="answered does not accept a value"):
        AssessmentVersionCreate(
            title="Invalid valueless condition",
            sections=[
                AssessmentSectionCreate(
                    title="Core",
                    questions=[
                        scored_boolean,
                        AssessmentQuestionCreate(
                            key="follow_up",
                            question_type="short",
                            prompt="Follow up",
                            condition=AssessmentCondition(
                                question_key="consent",
                                operator="answered",
                                value=True,
                            ),
                        ),
                    ],
                )
            ],
        )


def test_assessment_graph_accepts_typed_option_and_text_rules():
    version = AssessmentVersionCreate(
        title="Compatible rules",
        sections=[
            AssessmentSectionCreate(
                title="Core",
                questions=[
                    AssessmentQuestionCreate(
                        key="track",
                        question_type="single",
                        prompt="Track",
                        options=[
                            AssessmentOption(value="backend", label="Backend"),
                            AssessmentOption(value="frontend", label="Frontend"),
                        ],
                        scoring=AssessmentScoringRule(correct="backend"),
                        knockout=AssessmentKnockoutRule(
                            operator="not_equals", value="backend"
                        ),
                        max_score=1,
                    ),
                    AssessmentQuestionCreate(
                        key="details",
                        question_type="short",
                        prompt="Details",
                        condition=AssessmentCondition(
                            question_key="track",
                            operator="in",
                            value=["backend", "frontend"],
                        ),
                    ),
                ],
            )
        ],
    )
    assert version.sections[0].questions[1].condition is not None


def test_assessment_graph_accepts_type_safe_all_and_any_condition_groups():
    version = AssessmentVersionCreate(
        title="Compound branching",
        sections=[
            AssessmentSectionCreate(
                title="Core",
                questions=[
                    AssessmentQuestionCreate(
                        key="consent",
                        question_type="boolean",
                        prompt="Consent",
                        scoring=AssessmentScoringRule(correct=True),
                        max_score=1,
                    ),
                    AssessmentQuestionCreate(
                        key="track",
                        question_type="single",
                        prompt="Track",
                        options=[
                            AssessmentOption(value="backend", label="Backend"),
                            AssessmentOption(value="frontend", label="Frontend"),
                        ],
                    ),
                    AssessmentQuestionCreate(
                        key="details",
                        question_type="long",
                        prompt="Details",
                        condition=AssessmentConditionGroup(
                            mode="all",
                            rules=[
                                AssessmentCondition(
                                    question_key="consent",
                                    operator="equals",
                                    value=True,
                                ),
                                AssessmentCondition(
                                    question_key="track",
                                    operator="in",
                                    value=["backend", "frontend"],
                                ),
                            ],
                        ),
                        max_score=2,
                    ),
                ],
            )
        ],
    )
    condition = version.sections[0].questions[2].condition
    assert isinstance(condition, AssessmentConditionGroup)
    assert condition.mode == "all"
    assert len(condition.rules) == 2

    with pytest.raises(ValueError, match="earlier questions"):
        AssessmentVersionCreate(
            title="Invalid compound branch",
            sections=[
                AssessmentSectionCreate(
                    title="Core",
                    questions=[
                        AssessmentQuestionCreate(
                            key="first",
                            question_type="short",
                            prompt="First",
                            condition=AssessmentConditionGroup(
                                mode="any",
                                rules=[
                                    AssessmentCondition(
                                        question_key="later",
                                        operator="answered",
                                    )
                                ],
                            ),
                            max_score=1,
                        ),
                        AssessmentQuestionCreate(
                            key="later",
                            question_type="short",
                            prompt="Later",
                        ),
                    ],
                )
            ],
        )


def test_number_scoring_requires_exclusive_valid_exact_or_range():
    invalid_rules = [
        (
            AssessmentScoringRule(correct=5, min_value=1, max_value=10),
            "exactly one",
        ),
        (AssessmentScoringRule(min_value=10, max_value=1), "minimum cannot exceed"),
        (AssessmentScoringRule(min_value=1), "requires both bounds"),
        (
            AssessmentScoringRule(min_value=1, max_value=10, tolerance=0),
            "tolerance is only valid",
        ),
        (AssessmentScoringRule(correct=True), "correct value must be finite"),
    ]
    for scoring, message in invalid_rules:
        with pytest.raises(ValueError, match=message):
            AssessmentQuestionCreate(
                key="years",
                question_type="number",
                prompt="Years",
                scoring=scoring,
                max_score=1,
            )

    exact = AssessmentQuestionCreate(
        key="exact",
        question_type="number",
        prompt="Exact",
        scoring=AssessmentScoringRule(correct=5, tolerance=Decimal("0.25")),
        max_score=1,
    )
    ranged = AssessmentQuestionCreate(
        key="range",
        question_type="number",
        prompt="Range",
        scoring=AssessmentScoringRule(min_value=1, max_value=10),
        max_score=1,
    )
    assert exact.scoring is not None and exact.scoring.correct == 5
    assert ranged.scoring is not None and ranged.scoring.correct is None


def test_assessment_version_allows_manual_only_scoring_but_requires_a_maximum():
    manual = AssessmentVersionCreate(
        title="Manual-only graph",
        sections=[
            AssessmentSectionCreate(
                title="Review",
                questions=[
                    AssessmentQuestionCreate(
                        key="essay",
                        question_type="long",
                        prompt="Essay",
                        max_score=10,
                    )
                ],
            )
        ],
    )
    assert manual.sections[0].questions[0].scoring is None

    with pytest.raises(ValueError, match="positive maximum score"):
        AssessmentVersionCreate(
            title="Unscorable graph",
            sections=[
                AssessmentSectionCreate(
                    title="Review",
                    questions=[
                        AssessmentQuestionCreate(
                            key="essay",
                            question_type="long",
                            prompt="Essay",
                            max_score=0,
                        )
                    ],
                )
            ],
        )


def test_manual_grade_contract_requires_unique_questions_and_evidence():
    question_id = uuid4()
    with pytest.raises(ValidationError, match="manual grades or a knockout review"):
        AssessmentManualGradeRequest(expected_graded_at=datetime.now(UTC))

    knockout_only = AssessmentManualGradeRequest(
        expected_graded_at=datetime.now(UTC),
        knockout_review={
            "outcome": "cleared",
            "reason": "  Evidence shows that the automated match was not applicable.  ",
        },
    )
    assert knockout_only.grades == []
    assert knockout_only.knockout_review is not None
    assert (
        knockout_only.knockout_review.reason
        == "Evidence shows that the automated match was not applicable."
    )
    with pytest.raises(ValidationError, match="value cannot be blank"):
        AssessmentManualGradeRequest(
            expected_graded_at=datetime.now(UTC),
            knockout_review={"outcome": "confirmed", "reason": "   "},
        )

    with pytest.raises(ValidationError, match="evidence_comment"):
        AssessmentManualGradeRequest(
            expected_graded_at=datetime.now(UTC),
            grades=[
                {
                    "question_id": question_id,
                    "score": 1,
                    "evidence_comment": "   ",
                }
            ],
        )
    with pytest.raises(ValidationError, match="only once"):
        AssessmentManualGradeRequest(
            expected_graded_at=datetime.now(UTC),
            grades=[
                {
                    "question_id": question_id,
                    "score": 1,
                    "evidence_comment": "Evidence A",
                },
                {
                    "question_id": question_id,
                    "score": 2,
                    "evidence_comment": "Evidence B",
                },
            ],
        )


def test_manual_grade_route_is_assignment_scoped_and_sensitive():
    policy = operation_policy("POST", "/assessment-attempts/{attempt_id}/manual-grade")
    assert policy is not None
    assert policy.actions == ("assessments.grade",)
    assert policy.scope == ResourceScope.HIRING_ASSIGNMENT
    assert policy.fields == frozenset({FieldClass.ASSESSMENT_RESULT})
    for role in ("admin", "recruiter", "hiring_manager"):
        assert "assessments.grade" in ROLE_DEFAULTS[role]
        assert role in FIELD_DEFAULT_ROLES[FieldClass.ASSESSMENT_RESULT]
    for role in ("interviewer", "restricted"):
        assert "assessments.grade" not in ROLE_DEFAULTS[role]
