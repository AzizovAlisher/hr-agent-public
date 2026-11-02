"""Protected-trait invariance at the model-input boundary.

The pilot does not accept a non-zero protected-trait signal. Resume pairs that
differ only in identity or protected attributes must produce byte-identical
candidate evidence before any configured model can see it.
"""

import json

import pytest

from app.engine.ai_boundary import untrusted_candidate_block


@pytest.mark.parametrize(
    ("left_identity", "right_identity"),
    [
        (
            """Name: Alex Smith
Gender: male
Age: 28
Nationality: Tajik
Religion: Muslim
Marital status: married
Photo: included""",
            """Name: Aisha Khan
Gender: female
Age: 54
Nationality: Uzbek
Religion: Christian
Marital status: single
Photo: not included""",
        ),
        (
            """ФИО: Алексей Иванов
Пол: мужской
Возраст: 31
Национальность: таджик
Религия: ислам
Семейное положение: женат""",
            """ФИО: Мадина Каримова
Пол: женский
Возраст: 46
Национальность: узбечка
Религия: христианство
Семейное положение: не замужем""",
        ),
    ],
)
def test_protected_trait_pairs_are_identical_before_model(
    left_identity: str,
    right_identity: str,
):
    professional_evidence = """
Employer: Example Systems
Role: Senior Backend Engineer
Dates: 2020-2025
Project: PostgreSQL-backed payment platform
Outcome: reduced p95 latency from 800 ms to 240 ms for 50,000 daily users
Skills: Python, PostgreSQL, distributed tracing
"""

    left = untrusted_candidate_block(
        f"{left_identity}\n{professional_evidence}",
        label="candidate_resume",
        extra_names=("Alex Smith", "Алексей Иванов"),
        max_chars=12_000,
    )
    right = untrusted_candidate_block(
        f"{right_identity}\n{professional_evidence}",
        label="candidate_resume",
        extra_names=("Aisha Khan", "Мадина Каримова"),
        max_chars=12_000,
    )

    assert left == right
    decoded = json.loads(left)
    assert decoded["classification"] == "untrusted_candidate_evidence"
    assert decoded["content"].startswith("Employer: Example Systems")
    for forbidden in (
        "male",
        "female",
        "мужской",
        "женский",
        "Tajik",
        "Uzbek",
        "таджик",
        "узбечка",
        "Muslim",
        "Christian",
        "ислам",
        "христианство",
        "married",
        "single",
        "женат",
        "замужем",
    ):
        assert forbidden.casefold() not in decoded["content"].casefold()
