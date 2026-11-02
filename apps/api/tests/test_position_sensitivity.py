"""Contract tests for confidential positions (sensitivity = nda | restricted).

The guarantee under test is that a confidential role can be advertised through
a direct /apply link without revealing WHO is hiring or WHAT they are building,
and that it is absent from the broadcast careers feed entirely — including from
the feed's free-text search, which would otherwise act as an oracle over the
very text we are suppressing.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models.organization import Organization
from app.models.position import Position
from app.services.public_positions import (
    SENSITIVITY_NDA,
    SENSITIVITY_PUBLIC,
    SENSITIVITY_RESTRICTED,
    normalize_sensitivity,
    redact_position,
)


pytestmark = pytest.mark.asyncio


_SEEDED_AT = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)

# The real leak from the reported bug: internal module names and tier
# classification sitting in a description on an unauthenticated page.
_LEAKY_DESCRIPTION = (
    "Ведущий разработчик ядра высоконагруженной платформы. Работает с модулями "
    "Tier 1: oauth, integration, process, external-task, payment."
)
_VETTED_PUBLIC_TEXT = (
    "Ведущий разработчик ядра высоконагруженной платформы: бизнес-логика, "
    "интеграции и архитектурные решения."
)

_REQUIREMENTS = {
    "company": "Телеком Демо",
    "location": "Dushanbe",
    "employment_type": "Full-time",
    "seniority": "Senior",
    "salary_range": "2500-3500 $/мес",
    "java": "5+ years",
    "camunda_bpm": "required",
    "source_url": "https://private.example/source",
}


async def _client():
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.example.test",
    )


def _position(
    *,
    pid: int,
    sensitivity: str,
    org_id,
    public_description=None,
    description=_LEAKY_DESCRIPTION,
    full_description="RAW AD TEXT: ОАО Телеком Демо объявляет конкурс…",
    company="Телеком Демо",
):
    return Position(
        id=UUID(int=pid),
        organization_id=org_id,
        title="Senior Java Developer (2)",
        description=description,
        full_description=full_description,
        requirements={**_REQUIREMENTS, "company": company},
        internal_notes="Never publish this.",
        candidate_profile="ИЩЕМ\nSenior-разработчик…",
        public_description=public_description,
        sensitivity=sensitivity,
        status="open",
        public_apply_enabled=True,
        created_at=_SEEDED_AT,
        updated_at=_SEEDED_AT,
    )


async def _seed(db):
    organization = Organization(
        id=UUID(int=1),
        name="Телеком Демо",
        slug="demo-telecom",
        logo_path="logos/demo.png",
        accent_color="#3366FF",
    )
    db.add(organization)
    await db.flush()

    rows = {
        # The public role carries benign text on purpose, so the oracle test
        # below can attribute any search hit to a confidential row.
        "public": _position(
            pid=101,
            sensitivity=SENSITIVITY_PUBLIC,
            org_id=organization.id,
            description="Обычная открытая вакансия без внутренних деталей.",
            full_description="Публичный текст объявления.",
            company="Открытая Компания",
        ),
        "nda": _position(
            pid=102,
            sensitivity=SENSITIVITY_NDA,
            org_id=organization.id,
            public_description=_VETTED_PUBLIC_TEXT,
        ),
        "nda_ungenerated": _position(
            pid=103, sensitivity=SENSITIVITY_NDA, org_id=organization.id
        ),
        "restricted": _position(
            pid=104,
            sensitivity=SENSITIVITY_RESTRICTED,
            org_id=organization.id,
            public_description=_VETTED_PUBLIC_TEXT,
        ),
    }
    db.add_all(list(rows.values()))
    await db.commit()
    return {"organization": organization, **rows}


# ---------- the direct /apply link ----------


async def test_public_role_is_unchanged(db):
    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get(f"/public/positions/{seeded['public'].id}")

    assert rsp.status_code == 200
    body = rsp.json()
    assert body["employer_confidential"] is False
    assert body["organization_name"] == "Телеком Демо"
    assert body["description"] == "Обычная открытая вакансия без внутренних деталей."
    assert body["full_description"] == "Публичный текст объявления."
    assert body["requirements"]["company"] == "Открытая Компания"
    assert body["requirements"]["salary_range"] == "2500-3500 $/мес"
    # Pre-existing denylist still applies.
    assert "source_url" not in body["requirements"]


async def test_nda_role_hides_employer_raw_ad_and_internal_modules(db):
    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get(f"/public/positions/{seeded['nda'].id}")

    assert rsp.status_code == 200
    body = rsp.json()

    # Employer identity — name, logo AND accent colour go together.
    assert body["employer_confidential"] is True
    assert body["organization_name"] is None
    assert body["organization_logo_path"] is None
    assert body["organization_accent_color"] is None
    assert "company" not in (body["requirements"] or {})

    # The vetted stand-in replaces the leaky description; the raw ad is gone.
    assert body["description"] == _VETTED_PUBLIC_TEXT
    assert body["full_description"] is None

    # The actual reported leak must not appear anywhere in the payload.
    serialized = rsp.text
    for secret in ("external-task", "Tier 1", "Телеком Демо", "RAW AD TEXT"):
        assert secret not in serialized

    # NDA keeps the ordinary stack and pay — otherwise the vacancy is unusable.
    assert body["requirements"]["java"] == "5+ years"
    assert body["requirements"]["salary_range"] == "2500-3500 $/мес"


async def test_nda_without_generated_text_shows_nothing_rather_than_the_real_one(db):
    """Fail closed: no vetted public text must never fall back to the internal
    description, which is precisely the text being protected."""

    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get(f"/public/positions/{seeded['nda_ungenerated'].id}")

    assert rsp.status_code == 200
    body = rsp.json()
    assert body["description"] is None
    assert body["full_description"] is None
    assert "external-task" not in rsp.text


async def test_restricted_role_also_drops_stack_and_salary(db):
    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get(f"/public/positions/{seeded['restricted'].id}")

    assert rsp.status_code == 200
    requirements = rsp.json()["requirements"]

    # Allowlist survivors only.
    assert requirements == {
        "location": "Dushanbe",
        "employment_type": "Full-time",
        "seniority": "Senior",
    }
    # Stack fingerprint and budget are withheld at this tier.
    for dropped in ("java", "camunda_bpm", "salary_range", "company"):
        assert dropped not in requirements


async def test_candidate_profile_is_never_in_the_public_payload(db):
    """The internal brief is written against internal context — it must not be
    exposed even on a fully public role."""

    seeded = await _seed(db)
    async with await _client() as client:
        for key in ("public", "nda", "restricted"):
            rsp = await client.get(f"/public/positions/{seeded[key].id}")
            assert rsp.status_code == 200
            assert "candidate_profile" not in rsp.json()
            assert "ИЩЕМ" not in rsp.text
            assert "internal_notes" not in rsp.json()


# ---------- the broadcast careers feed ----------


async def test_confidential_roles_are_absent_from_the_careers_feed(db):
    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get("/public/v1/careers/demo-telecom/positions")

    assert rsp.status_code == 200
    payload = rsp.json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [str(seeded["public"].id)]


async def test_careers_search_cannot_be_used_as_an_oracle_over_hidden_text(db):
    """`q` ILIKEs full_description. If confidential rows stayed in the
    searchable set, a caller could confirm hidden phrases by watching `total`
    even with the field stripped from the projection."""

    await _seed(db)
    async with await _client() as client:
        rsp = await client.get(
            "/public/v1/careers/demo-telecom/positions",
            params={"q": "external-task"},
        )
        by_company = await client.get(
            "/public/v1/careers/demo-telecom/positions",
            params={"company": "Телеком Демо"},
        )

    assert rsp.status_code == 200
    assert rsp.json()["total"] == 0
    assert rsp.json()["items"] == []
    # The employer facet must not confirm the name either.
    assert by_company.status_code == 200
    assert by_company.json()["total"] == 0


async def test_careers_detail_404s_for_a_confidential_role(db):
    seeded = await _seed(db)
    async with await _client() as client:
        rsp = await client.get(
            f"/public/v1/careers/demo-telecom/positions/{seeded['nda'].id}"
        )

    assert rsp.status_code == 404


# ---------- the policy itself ----------


async def test_unknown_sensitivity_fails_closed(db):
    """A value we do not recognise degrades to the most restrictive tier
    rather than being treated as public."""

    assert normalize_sensitivity("public") == SENSITIVITY_PUBLIC
    assert normalize_sensitivity("nda") == SENSITIVITY_NDA
    assert normalize_sensitivity("totally-new-tier") == SENSITIVITY_RESTRICTED
    assert normalize_sensitivity("") == SENSITIVITY_RESTRICTED
    # Absent (pre-migration row loaded into a fresh object) stays public.
    assert normalize_sensitivity(None) == SENSITIVITY_PUBLIC


async def test_redaction_is_driven_by_sensitivity_not_by_the_caller(db):
    seeded = await _seed(db)
    view = redact_position(seeded["nda"])
    assert view.employer_confidential is True
    assert view.show_organization_identity is False
    assert view.full_description is None
    assert view.company is None


async def test_nested_requirements_cannot_smuggle_employer_or_salary(db):
    """requirements is free-form AI-generated jsonb, so a top-level-only filter
    is defeated by one level of nesting."""

    organization = Organization(id=UUID(int=5), name="Org", slug="nested-org")
    db.add(organization)
    await db.flush()
    nested = _position(
        pid=501,
        sensitivity=SENSITIVITY_NDA,
        org_id=organization.id,
        public_description=_VETTED_PUBLIC_TEXT,
    )
    nested.requirements = {
        "location": "Dushanbe",
        "meta": {"company": "Телеком Демо", "salary": "9000$"},
        "team": [{"employer": "Телеком Демо"}],
    }
    db.add(nested)
    await db.commit()

    async with await _client() as client:
        rsp = await client.get(f"/public/positions/{nested.id}")

    assert rsp.status_code == 200
    assert "Телеком Демо" not in rsp.text
    requirements = rsp.json()["requirements"] or {}
    assert "company" not in (requirements.get("meta") or {})
    assert requirements.get("location") == "Dushanbe"


async def test_tightening_the_tier_drops_the_previously_vetted_text(db):
    """Text vetted for nda still names the stack and salary, so it must not
    keep serving once the role becomes restricted."""

    from app.auth.jwt import encode_access_token
    from app.models.user import User

    seeded = await _seed(db)
    user = User(
        id=UUID(int=77),
        organization_id=seeded["organization"].id,
        email="admin@sensitivity.test",
        password_hash="unused",
        role="admin",
        locale="ru",
    )
    db.add(user)
    await db.commit()
    token = encode_access_token(user.id, user.role, user.locale)

    target = seeded["nda"]
    async with await _client() as client:
        patch = await client.patch(
            f"/positions/{target.id}",
            json={"sensitivity": "restricted"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["public_description"] is None

        public = await client.get(f"/public/positions/{target.id}")

    assert public.status_code == 200
    assert public.json()["description"] is None
    assert _VETTED_PUBLIC_TEXT not in public.text


async def test_database_rejects_an_invalid_sensitivity(db):
    """The CHECK constraint must exist under create_all too, not only under
    Alembic — otherwise a typo passes the test suite and fails in production."""

    organization = Organization(id=UUID(int=9), name="Org", slug="ck-org")
    db.add(organization)
    await db.flush()
    db.add(
        Position(
            id=UUID(int=901),
            organization_id=organization.id,
            title="Bad sensitivity",
            sensitivity="totally-bogus",
            status="open",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
