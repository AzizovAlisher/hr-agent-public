"""PostgreSQL-backed contract tests for the external careers API."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.communication import CandidateContactPoint
from app.models.folder import Folder
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _seed_careers(db):
    organization = Organization(
        id=UUID(int=1),
        name="Example Telecom",
        slug="example-telecom",
        logo_path="logos/example.png",
        accent_color="#3366FF",
    )
    other_organization = Organization(
        id=UUID(int=2),
        name="Other Tenant",
        slug="other-tenant",
    )
    active_folder = Folder(
        id=UUID(int=10),
        organization_id=organization.id,
        name="Engineering",
    )
    archived_folder = Folder(
        id=UUID(int=11),
        organization_id=organization.id,
        name="Archive",
        is_archived=True,
    )
    db.add_all([organization, other_organization])
    await db.flush()
    user = User(
        id=UUID(int=3),
        organization_id=organization.id,
        email="admin@example.test",
        password_hash="unused-in-contract-test",
        role="admin",
        locale="ru",
    )
    db.add(user)
    db.add_all([active_folder, archived_folder])
    await db.flush()

    shared_time = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    first = Position(
        id=UUID(int=101),
        organization_id=organization.id,
        title="Backend Engineer",
        description="Build dependable APIs.",
        full_description="A longer public role description.",
        requirements={
            "company": "Example Telecom",
            "location": "Dushanbe",
            "employment_type": "Full-time",
            "work_format": "Hybrid",
            "salary_range": "Negotiable",
            "skills": ["Python", {"name": "PostgreSQL", "contact": "secret"}],
            "source_url": "https://private.example/source",
            "contact_email": "recruiter@example.test",
            "_raw": "private import payload",
        },
        internal_notes="Never publish this.",
        status="open",
        public_apply_enabled=True,
        folder="Engineering",
        folder_id=active_folder.id,
        created_at=shared_time,
        updated_at=shared_time,
    )
    second = Position(
        id=UUID(int=102),
        organization_id=organization.id,
        title="Product Designer",
        description="Design recruiter workflows.",
        requirements={
            "company": "Design Studio",
            "location": "Khujand",
            "employment_type": "Contract",
        },
        status="open",
        public_apply_enabled=True,
        created_at=shared_time,
        updated_at=shared_time,
    )
    hidden = Position(
        id=UUID(int=103),
        organization_id=organization.id,
        title="Confidential Role",
        status="open",
        public_apply_enabled=False,
    )
    closed = Position(
        id=UUID(int=104),
        organization_id=organization.id,
        title="Closed Role",
        status="closed",
        public_apply_enabled=True,
    )
    archived = Position(
        id=UUID(int=105),
        organization_id=organization.id,
        title="Archived Role",
        status="open",
        public_apply_enabled=True,
        folder="Archive",
        folder_id=archived_folder.id,
    )
    legacy_archived = Position(
        id=UUID(int=106),
        organization_id=organization.id,
        title="Legacy Archived Role",
        status="open",
        public_apply_enabled=True,
        folder="_archive",
    )
    other_tenant = Position(
        id=UUID(int=107),
        organization_id=other_organization.id,
        title="Other Tenant Role",
        status="open",
        public_apply_enabled=True,
    )
    db.add_all(
        [first, second, hidden, closed, archived, legacy_archived, other_tenant]
    )
    await db.commit()
    return {
        "organization": organization,
        "user": user,
        "first": first,
        "second": second,
        "hidden": hidden,
        "closed": closed,
        "archived": archived,
        "legacy_archived": legacy_archived,
        "other_tenant": other_tenant,
    }


async def _client():
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.example.test",
    )


async def test_list_is_safe_paginated_cached_and_cross_origin(db):
    seeded = await _seed_careers(db)
    async with await _client() as client:
        first_page = await client.get(
            "/public/v1/careers/example-telecom/positions",
            params={"limit": 1},
            headers={"Origin": "https://company.example"},
        )

        assert first_page.status_code == 200
        assert first_page.headers["access-control-allow-origin"] == "*"
        assert first_page.headers["cache-control"].startswith("public, max-age=60")
        assert first_page.headers["x-total-count"] == "2"
        assert first_page.headers["etag"]

        payload = first_page.json()
        assert payload["organization"] == {
            "slug": "example-telecom",
            "name": "Example Telecom",
            "logo_url": "http://api.example.test/storage/logos/example.png",
            "accent_color": "#3366FF",
        }
        assert payload["total"] == 2
        assert [item["id"] for item in payload["items"]] == [
            str(seeded["second"].id)
        ]
        assert payload["next_cursor"]

        second_page = await client.get(
            "/public/v1/careers/example-telecom/positions",
            params={"limit": 1, "cursor": payload["next_cursor"]},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert [item["id"] for item in second_payload["items"]] == [
            str(seeded["first"].id)
        ]
        assert second_payload["next_cursor"] is None

        public_position = second_payload["items"][0]
        assert public_position["location"] == "Dushanbe"
        assert public_position["employment_type"] == "Full-time"
        assert public_position["apply_url"].endswith(
            f"/apply/{seeded['first'].id}?utm_source=careers_api"
        )
        assert public_position["api_url"].endswith(
            f"/public/v1/careers/example-telecom/positions/{seeded['first'].id}"
        )
        assert "source_url" not in public_position["requirements"]
        assert "contact_email" not in public_position["requirements"]
        assert "_raw" not in public_position["requirements"]
        assert "contact" not in public_position["requirements"]["skills"][1]
        assert "internal_notes" not in public_position
        assert "created_by" not in public_position

        cached = await client.get(
            "/public/v1/careers/example-telecom/positions",
            params={"limit": 1},
            headers={"If-None-Match": first_page.headers["etag"]},
        )
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers["etag"] == first_page.headers["etag"]


async def test_publication_rules_apply_to_feed_detail_and_apply_page(db):
    seeded = await _seed_careers(db)
    async with await _client() as client:
        visible = await client.get(
            f"/public/v1/careers/example-telecom/positions/{seeded['first'].id}"
        )
        assert visible.status_code == 200

        legacy_public = await client.get(
            f"/public/positions/{seeded['first'].id}"
        )
        assert legacy_public.status_code == 200
        assert "source_url" not in legacy_public.json()["requirements"]
        preview = await client.head(f"/public/positions/{seeded['first'].id}")
        assert preview.status_code == 204
        assert preview.content == b""
        assert preview.headers["cache-control"] == "private, no-store"

        for key in ("hidden", "closed", "archived", "legacy_archived"):
            position_id = seeded[key].id
            detail = await client.get(
                f"/public/v1/careers/example-telecom/positions/{position_id}"
            )
            assert detail.status_code == 404, key

            old_detail = await client.get(f"/public/positions/{position_id}")
            assert old_detail.status_code == 404, key
            old_preview = await client.head(f"/public/positions/{position_id}")
            assert old_preview.status_code == 404, key

        cross_tenant = await client.get(
            "/public/v1/careers/example-telecom/positions/"
            f"{seeded['other_tenant'].id}"
        )
        assert cross_tenant.status_code == 404

        # The legacy endpoint is intentionally position-ID based and remains
        # backward compatible for another tenant's own public apply link.
        other_tenant_legacy = await client.get(
            f"/public/positions/{seeded['other_tenant'].id}"
        )
        assert other_tenant_legacy.status_code == 200

        missing_org = await client.get(
            "/public/v1/careers/does-not-exist/positions",
            headers={"Origin": "https://company.example"},
        )
        assert missing_org.status_code == 404
        assert missing_org.headers["access-control-allow-origin"] == "*"

        invalid_cursor = await client.get(
            "/public/v1/careers/example-telecom/positions",
            params={"cursor": "not-a-cursor"},
        )
        assert invalid_cursor.status_code == 422


async def test_filters_preflight_and_unpublishing_invalidate_feed(db):
    seeded = await _seed_careers(db)
    async with await _client() as client:
        for params, expected_title in (
            ({"q": "dependable"}, "Backend Engineer"),
            ({"company": "design studio"}, "Product Designer"),
            ({"location": "dushanbe"}, "Backend Engineer"),
            ({"employment_type": "contract"}, "Product Designer"),
        ):
            response = await client.get(
                "/public/v1/careers/example-telecom/positions",
                params=params,
            )
            assert response.status_code == 200
            assert [item["title"] for item in response.json()["items"]] == [
                expected_title
            ]

        preflight = await client.options(
            "/public/v1/careers/example-telecom/positions",
            headers={
                "Origin": "https://company.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "*"
        assert preflight.headers["access-control-allow-methods"] == (
            "GET, POST, OPTIONS"
        )

        before = await client.get(
            "/public/v1/careers/example-telecom/positions"
        )
        before_etag = before.headers["etag"]

        seeded["first"].status = "closed"
        await db.commit()

        after = await client.get(
            "/public/v1/careers/example-telecom/positions"
        )
        assert after.status_code == 200
        assert after.json()["total"] == 1
        assert after.headers["etag"] != before_etag
        assert [item["title"] for item in after.json()["items"]] == [
            "Product Designer"
        ]


async def test_authenticated_position_api_controls_website_publication(db):
    seeded = await _seed_careers(db)
    token = encode_access_token(
        seeded["user"].id,
        seeded["user"].role,
        seeded["user"].locale,
    )
    headers = {"Authorization": f"Bearer {token}"}

    async with await _client() as client:
        before = await client.get(
            "/public/v1/careers/example-telecom/positions"
        )
        assert before.json()["total"] == 2

        publish = await client.patch(
            f"/positions/{seeded['hidden'].id}",
            json={"public_apply_enabled": True},
            headers=headers,
        )
        assert publish.status_code == 200
        assert publish.json()["public_apply_enabled"] is True

        published_feed = await client.get(
            "/public/v1/careers/example-telecom/positions"
        )
        assert published_feed.json()["total"] == 3
        assert "Confidential Role" in {
            item["title"] for item in published_feed.json()["items"]
        }

        cross_tenant_update = await client.patch(
            f"/positions/{seeded['other_tenant'].id}",
            json={"public_apply_enabled": False},
            headers=headers,
        )
        assert cross_tenant_update.status_code == 404

        unpublish = await client.patch(
            f"/positions/{seeded['hidden'].id}",
            json={"public_apply_enabled": False},
            headers=headers,
        )
        assert unpublish.status_code == 200

        unpublished_feed = await client.get(
            "/public/v1/careers/example-telecom/positions"
        )
        assert unpublished_feed.json()["total"] == 2
        assert "Confidential Role" not in {
            item["title"] for item in unpublished_feed.json()["items"]
        }


async def test_embeddable_widget_is_versioned_cacheable_and_cross_origin():
    async with await _client() as client:
        response = await client.get(
            "/public/v1/careers/widget.js",
            headers={"Origin": "https://company.example"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"].startswith("public, max-age=3600")
    assert response.headers["access-control-allow-origin"] == "*"
    assert "data-recruiter-careers" in response.text
    assert "innerHTML" not in response.text
    assert "/public/v1/careers/" in response.text
    assert "/apply" in response.text
    assert "15 * 1024 * 1024" in response.text
    assert "up to 15 MiB" in response.text
    assert "10 * 1024 * 1024" not in response.text


async def test_hosted_preview_embeds_the_exact_widget(db):
    await _seed_careers(db)
    async with await _client() as client:
        response = await client.get(
            "/public/v1/careers/example-telecom/preview",
            params={"locale": "en"},
        )
        missing = await client.get(
            "/public/v1/careers/missing-tenant/preview"
        )
        invalid_locale = await client.get(
            "/public/v1/careers/example-telecom/preview",
            params={"locale": "de"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"]
    assert 'data-organization="example-telecom"' in response.text
    assert 'data-locale="en"' in response.text
    assert 'data-source="company_website"' in response.text
    assert (
        'src="http://api.example.test/public/v1/careers/widget.js"'
        in response.text
    )
    assert missing.status_code == 404
    assert invalid_locale.status_code == 422


async def test_embedded_application_creates_candidate_activity_and_notification(
    db,
):
    seeded = await _seed_careers(db)
    organization_id = seeded["organization"].id
    position_id = seeded["first"].id
    user_id = seeded["user"].id

    endpoint = (
        "/public/v1/careers/example-telecom/positions/"
        f"{seeded['first'].id}/apply"
    )
    application = {
        "name": "Website Applicant",
        "phone": "+992 900 555 101",
        "email": "website-applicant@example.test",
        "source": "company_website",
        "raw_text": "Backend engineer with Python and PostgreSQL experience. " * 2,
    }

    async with await _client() as client:
        preflight = await client.options(
            endpoint,
            headers={
                "Origin": "https://company.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 204
        assert "POST" in preflight.headers["access-control-allow-methods"]

        response = await client.post(
            endpoint,
            data=application,
            headers={
                "Origin": "https://company.example",
                "Idempotency-Key": "website-application-once",
            },
        )
        assert response.status_code == 201
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.json() == {
            "accepted": True,
            "message": "Application received. We'll be in touch.",
        }
        candidate_id = (
            await db.execute(
                select(Candidate.id).where(
                    Candidate.organization_id == organization_id,
                    Candidate.email_normalized == "website-applicant@example.test",
                )
            )
        ).scalar_one()

        # Intake must be visible in the selected vacancy before background AI
        # scoring finishes (or even if the provider is unavailable).
        token = encode_access_token(
            seeded["user"].id,
            seeded["user"].role,
            seeded["user"].locale,
        )
        position_page = await client.get(
            f"/positions/{position_id}/applications",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert position_page.status_code == 200
        immediate_match = next(
            item
            for item in position_page.json()["items"]
            if item["candidate_id"] == str(candidate_id)
        )
        assert immediate_match["stage"] == "new"
        assert immediate_match["overall_score"] is None
        assert immediate_match["display_name"] == "Website Applicant"

        duplicate = await client.post(
            endpoint,
            data={
                **application,
                "phone": "+992 900 999 999",
                "email": "different-person@example.test",
            },
            headers={"Idempotency-Key": "website-application-once"},
        )
        assert duplicate.status_code == 201
        assert duplicate.json() == response.json()

        cross_tenant = await client.post(
            "/public/v1/careers/other-tenant/positions/"
            f"{seeded['first'].id}/apply",
            data={**application, "phone": "+992 900 555 102"},
        )
        assert cross_tenant.status_code == 404

        hidden = await client.post(
            "/public/v1/careers/example-telecom/positions/"
            f"{seeded['hidden'].id}/apply",
            data={**application, "phone": "+992 900 555 103"},
        )
        assert hidden.status_code == 404

    db.expire_all()
    candidate = await db.get(Candidate, candidate_id)
    assert candidate is not None
    assert candidate.organization_id == organization_id
    assert candidate.source == "public"
    assert candidate.phone_normalized == "+992900555101"
    assert candidate.email_normalized == "website-applicant@example.test"
    assert candidate.parsed_cv == {
        "applicant_name": "Website Applicant",
        "applicant_phone": "+992 900 555 101",
        "applicant_email": "website-applicant@example.test",
        "applied_via": "company_website",
    }

    pending_score = (
        await db.execute(
            select(Score).where(
                Score.candidate_id == candidate_id,
                Score.position_id == position_id,
            )
        )
    ).scalar_one()
    assert pending_score.stage == "new"
    assert pending_score.overall_score is None
    assert pending_score.model_used is None

    activity = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == candidate_id
            )
        )
    ).scalar_one()
    assert activity.actor_user_id is None
    assert activity.position_id == position_id
    assert activity.payload == {
        "source": "public",
        "applied_via": "company_website",
        "position_title": "Backend Engineer",
    }

    notification = (
        await db.execute(
            select(Notification).where(
                Notification.payload["candidate_id"].astext == str(candidate_id)
            )
        )
    ).scalar_one()
    assert notification.user_id == user_id
    assert notification.type == "candidate_applied"
    assert int(
        (
            await db.execute(
                select(func.count(Application.id)).where(
                    Application.candidate_id == candidate_id,
                    Application.position_id == position_id,
                )
            )
        ).scalar_one()
    ) == 1
    assert int(
        (
            await db.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.organization_id == organization_id
                )
            )
        ).scalar_one()
    ) == 1
    assert int(
        (
            await db.execute(
                select(func.count(CandidateActivity.id)).where(
                    CandidateActivity.candidate_id == candidate_id
                )
            )
        ).scalar_one()
    ) == 1
    assert int(
        (
            await db.execute(
                select(func.count(Notification.id)).where(
                    Notification.payload["candidate_id"].astext == str(candidate_id)
                )
            )
        ).scalar_one()
    ) == 1


async def test_conflicting_contacts_are_generically_accepted_once_for_human_review(db):
    seeded = await _seed_careers(db)
    organization = seeded["organization"]
    email_owner = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Existing email owner",
        raw_cv="Existing email owner CV",
        email_normalized="conflict@example.test",
    )
    phone_owner = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Existing phone owner",
        raw_cv="Existing phone owner CV",
        phone_normalized="+992900777111",
    )
    db.add_all([email_owner, phone_owner])
    await db.commit()
    endpoint = (
        "/public/v1/careers/example-telecom/positions/"
        f"{seeded['first'].id}/apply"
    )
    payload = {
        "name": "Needs Identity Review",
        "phone": "+992 900 777 111",
        "email": "conflict@example.test",
        "source": "company_website",
        "raw_text": "Experienced engineer whose identity signals need human review. "
        * 2,
    }

    async with await _client() as client:
        first = await client.post(endpoint, data=payload)
        retry = await client.post(endpoint, data=payload)

    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json() == {
        "accepted": True,
        "message": "Application received. We'll be in touch.",
    }
    review_candidates = list(
        (
            await db.execute(
                select(Candidate).where(
                    Candidate.organization_id == organization.id,
                    Candidate.source == "identity_review",
                )
            )
        ).scalars()
    )
    assert len(review_candidates) == 1
    review = review_candidates[0]
    assert review.source_ref.startswith("identity-review:")
    assert "conflict@example.test" not in review.source_ref
    assert "992900777111" not in review.source_ref
    assert review.email_normalized is None
    assert review.phone_normalized is None
    assert review.parsed_cv["identity_review_required"] is True
    assert review.parsed_cv["applicant_name"] == "Needs Identity Review"
    assert (
        await db.scalar(
            select(func.count(CandidateContactPoint.id)).where(
                CandidateContactPoint.candidate_id == review.id
            )
        )
        == 0
    )
    application = (
        await db.execute(
            select(Application).where(Application.candidate_id == review.id)
        )
    ).scalar_one()
    assert application.application_metadata["identity_review_required"] is True
    assert (
        await db.scalar(
            select(func.count(CandidateActivity.id)).where(
                CandidateActivity.candidate_id == review.id
            )
        )
        == 1
    )
    assert (
        await db.scalar(
            select(func.count(Notification.id)).where(
                Notification.payload["candidate_id"].astext == str(review.id)
            )
        )
        == 1
    )
