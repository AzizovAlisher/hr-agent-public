"""Capability-token and tenant-boundary regressions for public shortlists."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.shortlist import ShortlistLink
from app.models.user import User


pytestmark = pytest.mark.asyncio


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def seed_shortlist_data(db, suffix: str):
    organization = Organization(
        name=f"Shortlist organization {suffix}",
        slug=f"shortlist-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"shortlist-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"Candidate {suffix}",
        raw_cv="Private candidate evidence",
    )
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        status="open",
    )
    db.add_all([user, candidate, position])
    await db.flush()
    position.created_by = user.id
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            overall_score=84,
            stage="screening",
        )
    )
    await db.commit()
    return organization, user, position


async def seed_privacy_shortlist_data(db):
    """Create PII/evidence canaries that must never cross an anonymous link."""

    organization = Organization(
        name="Privacy-safe shortlist organization",
        slug=f"shortlist-privacy-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"shortlist-owner-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"shortlist-interviewer-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    position = Position(
        organization_id=organization.id,
        title="Platform engineer",
        status="open",
    )
    db.add_all([recruiter, interviewer, position])
    await db.flush()
    position.created_by = recruiter.id

    canaries = {
        "pii": "PII_CANARY_ALINA_998877",
        "phone": "+992900001122",
        "email": "pii-canary-alina@example.test",
        "employer": "EMPLOYER_CANARY_ORBITAL_BANK",
        "project": "PROJECT_CANARY_SECRET_LEDGER",
        "outcome": "OUTCOME_CANARY_REVENUE_41_PERCENT",
        "source": "source-canary",
        "source_ref": "SOURCE_REF_CANARY_PRIVATE_PROFILE",
        "raw_cv": "RAW_CV_CANARY_DO_NOT_SHARE",
    }
    primary = Candidate(
        organization_id=organization.id,
        source=canaries["source"],
        source_ref=canaries["source_ref"],
        raw_cv=(
            f"{canaries['raw_cv']} {canaries['employer']} "
            f"{canaries['project']} {canaries['outcome']}"
        ),
        parsed_cv={
            "applicant_name": canaries["pii"],
            "applicant_phone": canaries["phone"],
            "applicant_email": canaries["email"],
            "experience": [
                {
                    "employer": canaries["employer"],
                    "project": canaries["project"],
                    "outcome": canaries["outcome"],
                }
            ],
        },
    )
    secondary = Candidate(
        organization_id=organization.id,
        source="referral",
        source_ref="SECONDARY_SOURCE_REF_CANARY",
        raw_cv="SECONDARY_RAW_CV_CANARY",
        parsed_cv={"applicant_name": "SECONDARY_PII_CANARY"},
    )
    db.add_all([primary, secondary])
    await db.flush()
    db.add_all(
        [
            Score(
                organization_id=organization.id,
                candidate_id=primary.id,
                position_id=position.id,
                overall_score=93,
                score_technical_skills=91,
                score_quality_of_projects=92,
                score_previous_experience=94,
                score_impact=95,
                comment_technical_skills=(
                    f"{canaries['pii']} worked at {canaries['employer']}"
                ),
                comment_quality_of_projects=(
                    f"Built {canaries['project']} for {canaries['employer']}"
                ),
                comment_previous_experience=canaries["raw_cv"],
                comment_impact=canaries["outcome"],
                overall_recommendation=(
                    f"Hire {canaries['pii']} because of {canaries['project']}"
                ),
                stage="screening",
            ),
            Score(
                organization_id=organization.id,
                candidate_id=secondary.id,
                position_id=position.id,
                overall_score=81,
                score_technical_skills=82,
                score_quality_of_projects=80,
                score_previous_experience=79,
                score_impact=83,
                comment_technical_skills="SECONDARY_COMMENT_CANARY",
                stage="screening",
            ),
        ]
    )
    await db.commit()
    return {
        "organization": organization,
        "recruiter": recruiter,
        "interviewer": interviewer,
        "position": position,
        "primary": primary,
        "secondary": secondary,
        "canaries": canaries,
    }


def assert_private_token_headers(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in response.headers["x-robots-tag"]


async def test_anonymous_shortlist_exposes_only_share_safe_numeric_comparison(db):
    seeded = await seed_privacy_shortlist_data(db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/positions/{seeded['position'].id}/shortlists",
            headers=auth(seeded["recruiter"]),
            json={"top_n": 5, "include_contact": False, "expires_in_days": 7},
        )
        assert created.status_code == 201
        link_id = created.json()["id"]
        original_token = created.json()["token"]

        anonymous = await client.get(f"/public/shortlists/{original_token}")
        assert anonymous.status_code == 200
        assert_private_token_headers(anonymous)
        payload = anonymous.json()
        assert payload["include_contact"] is False
        assert [item["label"] for item in payload["candidates"]] == ["#01", "#02"]
        assert [item["overall_score"] for item in payload["candidates"]] == [93, 81]

        safe_candidate_keys = {
            "label",
            "overall_score",
            "score_technical_skills",
            "score_quality_of_projects",
            "score_previous_experience",
            "score_impact",
        }
        assert all(set(item) == safe_candidate_keys for item in payload["candidates"])

        serialized = json.dumps(payload, sort_keys=True)
        assert str(seeded["primary"].id) not in serialized
        assert str(seeded["secondary"].id) not in serialized
        for value in seeded["canaries"].values():
            assert value not in serialized
        for value in (
            "SECONDARY_PII_CANARY",
            "SECONDARY_SOURCE_REF_CANARY",
            "SECONDARY_RAW_CV_CANARY",
            "SECONDARY_COMMENT_CANARY",
        ):
            assert value not in serialized

        # Rotation must not weaken the projection, and the superseded token
        # must become non-enumerating immediately while retaining public-token
        # cache/referrer/SEO protections.
        rotated = await client.post(
            f"/shortlists/{link_id}/rotate",
            headers=auth(seeded["recruiter"]),
            json={},
        )
        assert rotated.status_code == 200
        rotated_token = rotated.json()["token"]
        superseded = await client.get(f"/public/shortlists/{original_token}")
        assert superseded.status_code == 404
        assert_private_token_headers(superseded)
        after_rotation = await client.get(f"/public/shortlists/{rotated_token}")
        assert after_rotation.status_code == 200
        assert_private_token_headers(after_rotation)
        assert after_rotation.json()["candidates"] == payload["candidates"]
        assert str(seeded["primary"].id) not in after_rotation.text

        revoked = await client.post(
            f"/shortlists/{link_id}/revoke",
            headers=auth(seeded["recruiter"]),
            json={},
        )
        assert revoked.status_code == 200
        gone = await client.get(f"/public/shortlists/{rotated_token}")
        assert gone.status_code == 410
        assert gone.json()["code"] == "shortlist_revoked"
        assert_private_token_headers(gone)


async def test_contact_shortlist_keeps_full_projection_for_authorized_creator(db):
    seeded = await seed_privacy_shortlist_data(db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        forbidden = await client.post(
            f"/positions/{seeded['position'].id}/shortlists",
            headers=auth(seeded["interviewer"]),
            json={"top_n": 5, "include_contact": True},
        )
        assert forbidden.status_code == 403

        created = await client.post(
            f"/positions/{seeded['position'].id}/shortlists",
            headers=auth(seeded["recruiter"]),
            json={"top_n": 5, "include_contact": True, "expires_in_days": 7},
        )
        assert created.status_code == 201
        public = await client.get(
            f"/public/shortlists/{created.json()['token']}"
        )
        assert public.status_code == 200
        assert_private_token_headers(public)
        payload = public.json()
        assert payload["include_contact"] is True

        primary = payload["candidates"][0]
        canaries = seeded["canaries"]
        assert primary["candidate_id"] == str(seeded["primary"].id)
        assert primary["label"] == canaries["pii"]
        assert primary["source"] == canaries["source"]
        assert primary["contact_name"] == canaries["pii"]
        assert primary["contact_phone"] == canaries["phone"]
        assert primary["contact_email"] == canaries["email"]
        assert canaries["employer"] in primary["comment_technical_skills"]
        assert canaries["project"] in primary["comment_quality_of_projects"]
        assert primary["comment_previous_experience"] == canaries["raw_cv"]
        assert primary["comment_impact"] == canaries["outcome"]


async def test_shortlist_openapi_separates_anonymous_and_contact_candidate_contracts():
    schema = app.openapi()
    response_schema = schema["paths"]["/public/shortlists/{token}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/PublicShortlistOut"}

    components = schema["components"]["schemas"]
    public_contract = components["PublicShortlistOut"]
    variants = public_contract["anyOf"]
    assert {item["$ref"] for item in variants} == {
        "#/components/schemas/PublicShortlistAnonymousOut",
        "#/components/schemas/PublicShortlistContactOut",
    }

    anonymous_properties = components["PublicShortlistAnonymousCandidate"][
        "properties"
    ]
    contact_properties = components["PublicShortlistContactCandidate"]["properties"]
    safe_candidate_keys = {
        "label",
        "overall_score",
        "score_technical_skills",
        "score_quality_of_projects",
        "score_previous_experience",
        "score_impact",
    }
    assert set(anonymous_properties) == safe_candidate_keys
    assert {
        "candidate_id",
        "source",
        "comment_technical_skills",
        "comment_quality_of_projects",
        "comment_previous_experience",
        "comment_impact",
        "contact_name",
        "contact_phone",
        "contact_email",
    }.issubset(contact_properties)

    anonymous_variant = components["PublicShortlistAnonymousOut"]
    contact_variant = components["PublicShortlistContactOut"]
    assert anonymous_variant["properties"]["include_contact"]["const"] is False
    assert contact_variant["properties"]["include_contact"]["const"] is True
    assert anonymous_variant["properties"]["candidates"]["items"] == {
        "$ref": "#/components/schemas/PublicShortlistAnonymousCandidate"
    }
    assert contact_variant["properties"]["candidates"]["items"] == {
        "$ref": "#/components/schemas/PublicShortlistContactCandidate"
    }


async def test_shortlist_secret_is_one_time_digested_rotatable_and_revocable(db):
    _organization, user, position = await seed_shortlist_data(db, "primary")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/positions/{position.id}/shortlists",
            headers=auth(user),
            json={"top_n": 5, "include_contact": False, "expires_in_days": 7},
        )
        assert created.status_code == 201
        created_payload = created.json()
        raw_token = created_payload["token"]
        assert raw_token.startswith("sl_")
        assert created_payload["expires_at"] is not None

        record = (
            await db.execute(
                select(ShortlistLink).where(
                    ShortlistLink.id == created_payload["id"]
                )
            )
        ).scalar_one()
        assert record.token_digest == hashlib.sha256(raw_token.encode()).hexdigest()
        assert raw_token not in record.token_digest

        listed = await client.get(
            f"/positions/{position.id}/shortlists", headers=auth(user)
        )
        assert listed.status_code == 200
        assert listed.json()["next_cursor"] is None
        assert "token" not in listed.json()["items"][0]

        public = await client.get(f"/public/shortlists/{raw_token}")
        assert public.status_code == 200
        assert public.headers["cache-control"] == "private, no-store"
        assert public.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in public.headers["x-robots-tag"]

        rotated = await client.post(
            f"/shortlists/{record.id}/rotate", headers=auth(user), json={}
        )
        assert rotated.status_code == 200
        rotated_token = rotated.json()["token"]
        assert rotated_token != raw_token
        assert (await client.get(f"/public/shortlists/{raw_token}")).status_code == 404
        assert (await client.get(f"/public/shortlists/{rotated_token}")).status_code == 200

        revoked = await client.post(
            f"/shortlists/{record.id}/revoke", headers=auth(user), json={}
        )
        assert revoked.status_code == 200
        gone = await client.get(f"/public/shortlists/{rotated_token}")
        assert gone.status_code == 410
        assert gone.headers["cache-control"] == "private, no-store"


async def test_shortlist_tenant_scope_and_expiry_are_authoritative(db):
    _organization, user, position = await seed_shortlist_data(db, "owner")
    _other_organization, other_user, _other_position = await seed_shortlist_data(
        db, "other"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/positions/{position.id}/shortlists",
            headers=auth(user),
            json={"top_n": 3, "include_contact": False},
        )
        link_id = created.json()["id"]
        token = created.json()["token"]

        assert (
            await client.post(
                f"/shortlists/{link_id}/rotate", headers=auth(other_user), json={}
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/shortlists/{link_id}/revoke", headers=auth(other_user), json={}
            )
        ).status_code == 404

        record = await db.get(ShortlistLink, link_id)
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        expired = await client.get(f"/public/shortlists/{token}")
        assert expired.status_code == 410
        assert expired.headers["cache-control"] == "private, no-store"
