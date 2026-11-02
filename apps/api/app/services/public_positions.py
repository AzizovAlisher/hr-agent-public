"""Shared publication rules for unauthenticated position endpoints.

Every unauthenticated surface that renders a position MUST route its projection
through :func:`redact_position` rather than reading the ORM object directly.
There are more of these surfaces than is obvious (/apply, the careers feed and
its widget, shortlist share links, scheduling links), and the whole point of
this module is that they cannot drift apart.

The policy is deliberately fail-closed: an unrecognised sensitivity value is
treated as the most restrictive tier, and a confidential role with no vetted
public text shows no text at all rather than falling back to the internal one.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.folder import Folder
from app.models.position import Position


SENSITIVITY_PUBLIC = "public"
SENSITIVITY_NDA = "nda"
SENSITIVITY_RESTRICTED = "restricted"

#: Accepted values for ``positions.sensitivity`` — mirrored by the
#: ``ck_positions_sensitivity`` CHECK constraint (migration a3f7c1d20e94).
SENSITIVITY_VALUES = (SENSITIVITY_PUBLIC, SENSITIVITY_NDA, SENSITIVITY_RESTRICTED)


# On a `restricted` role the requirement chips switch from a denylist to this
# ALLOWLIST. Rationale: `requirements` is free-form AI-generated jsonb, so a
# denylist is one prompt tweak away from failing open — and on a role of state
# importance the precise tech stack is itself the sensitive part, because it
# fingerprints the operator's infrastructure. What survives is only what a
# candidate needs in order to decide whether to apply at all.
_RESTRICTED_ALLOWED_KEYS = {
    "education",
    "employment_type",
    "experience",
    "experience_years",
    "format",
    "language",
    "languages",
    "level",
    "location",
    "relocation",
    "remote",
    "schedule",
    "seniority",
    "work_format",
    "years_of_experience",
}

# Dropped on every confidential tier: these name the employer directly.
# Kept deliberately broad — this is a denylist over AI-invented keys, so a
# near-synonym the prompt happens to emit ("client", "org_name", "holding")
# would otherwise republish the very name we are withholding.
_EMPLOYER_IDENTITY_KEYS = frozenset(
    {
        "brand",
        "client",
        "company",
        "company_name",
        "customer",
        "employer",
        "employer_name",
        "group",
        "holding",
        "org",
        "org_name",
        "organisation",
        "organization",
        "organization_name",
        "parent_company",
    }
)

# Dropped on `restricted` only — kept on `nda`, where pay transparency still
# helps and the budget of a commercial client is not the secret being kept.
_COMPENSATION_KEYS = frozenset(
    {
        "budget",
        "compensation",
        "pay",
        "rate",
        "salary",
        "salary_from",
        "salary_range",
        "salary_to",
        "wage",
    }
)


_PRIVATE_REQUIREMENT_KEYS = {
    "budget",
    "contact",
    "contact_email",
    "contact_phone",
    "created_by",
    "email",
    "external_id",
    "hiring_manager",
    "imported_from",
    "internal_notes",
    "llm_prompt",
    "phone",
    "prompt",
    "source",
    "source_id",
    "source_ref",
    "source_url",
    "telegram",
    "whatsapp",
}


def public_position_filters(
    organization_id: UUID | None = None,
    *,
    include_confidential: bool = False,
) -> list[ColumnElement[bool]]:
    """Return the canonical SQL filters for a website-published position.

    Callers must ``outerjoin(Folder, Folder.id == Position.folder_id)`` before
    applying these conditions.

    ``include_confidential`` distinguishes the two ways a candidate arrives:

    * **The broadcast feed** (careers list/detail/widget) leaves it False, so
      confidential roles are absent from the searchable set entirely. This is
      what closes the free-text ``q`` oracle — that filter ILIKEs
      ``full_description``, so merely stripping fields from the projection
      would still let a caller confirm hidden phrases by watching ``total``.
    * **A direct link** (/apply/{id}, handed out deliberately by the recruiter)
      passes True and relies on :func:`redact_position` to decide what shows.
    """

    filters: list[ColumnElement[bool]] = [
        Position.status == "open",
        Position.public_apply_enabled.is_(True),
        or_(Position.folder_id.is_(None), Folder.is_archived.is_(False)),
        or_(Position.folder.is_(None), Position.folder.not_like("\\_%", escape="\\")),
    ]
    if not include_confidential:
        filters.append(Position.sensitivity == SENSITIVITY_PUBLIC)
    if organization_id is not None:
        filters.insert(0, Position.organization_id == organization_id)
    return filters


def _sanitize_requirement_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_requirement_value(item)
            for key, item in value.items()
            if not str(key).startswith("_")
            and str(key).lower() not in _PRIVATE_REQUIREMENT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_requirement_value(item) for item in value]
    return value


def safe_public_requirements(value: Any) -> dict[str, Any] | None:
    """Recursively remove recruiter-only metadata from public requirements."""

    if not isinstance(value, dict):
        return None
    sanitized = _sanitize_requirement_value(value)
    return sanitized or None


def normalize_sensitivity(value: Any) -> str:
    """Coerce a stored sensitivity into a known tier, failing closed.

    A value we do not recognise (bad migration, hand-edited row, a tier added
    by a newer deploy talking to an older worker) must never be treated as
    public — it degrades to the most restrictive tier instead.
    """

    if value in SENSITIVITY_VALUES:
        return str(value)
    return SENSITIVITY_RESTRICTED if value is not None else SENSITIVITY_PUBLIC


def is_confidential(position: Position) -> bool:
    """True when this role must not be broadcast or attributed to its employer."""

    return normalize_sensitivity(position.sensitivity) != SENSITIVITY_PUBLIC


def _confidential_requirements(
    value: Any, *, sensitivity: str
) -> dict[str, Any] | None:
    """Project requirements for a confidential role.

    ``nda`` keeps the ordinary denylist result minus employer identity: the
    tech keys there are standard, publicly-known technologies and withholding
    them would make the vacancy unusable without protecting anything. The
    identifying detail on an NDA role lives in the prose, not the chips.

    ``restricted`` switches to :data:`_RESTRICTED_ALLOWED_KEYS`.
    """

    base = safe_public_requirements(value)
    if base is None:
        return None

    if sensitivity == SENSITIVITY_NDA:
        banned = _EMPLOYER_IDENTITY_KEYS
        kept = {
            key: item
            for key, item in base.items()
            if str(key).lower() not in banned
        }
    else:
        banned = _EMPLOYER_IDENTITY_KEYS | _COMPENSATION_KEYS
        kept = {
            key: item
            for key, item in base.items()
            if str(key).lower() in _RESTRICTED_ALLOWED_KEYS
            and str(key).lower() not in banned
        }

    # The top-level pass above is not enough. `requirements` is free-form
    # AI-generated jsonb, so an employer name or a salary can sit one level
    # down ({"meta": {"company": "…"}}) and would otherwise sail straight
    # through — on `restricted` even under an allowlisted key. Scrub the whole
    # surviving subtree by the same rules.
    scrubbed = _strip_keys_deep(kept, banned)
    return scrubbed or None


def _strip_keys_deep(value: Any, banned: frozenset[str] | set[str]) -> Any:
    """Recursively drop any dict key whose lowercased name is in ``banned``."""

    if isinstance(value, dict):
        return {
            key: _strip_keys_deep(item, banned)
            for key, item in value.items()
            if str(key).lower() not in banned
        }
    if isinstance(value, list):
        return [_strip_keys_deep(item, banned) for item in value]
    return value


@dataclass(frozen=True)
class PublicPositionView:
    """What an unauthenticated visitor is allowed to see for one position.

    ``employer_confidential`` is a flag rather than a rendered string on
    purpose: the public pages are bilingual (RU/EN), so the label belongs in
    the frontend message catalogue, not in the API payload.
    """

    title: str
    description: str | None
    full_description: str | None
    requirements: dict[str, Any] | None
    company: str | None
    employer_confidential: bool
    sensitivity: str

    @property
    def show_organization_identity(self) -> bool:
        """False when the org name / logo / accent colour must be withheld."""

        return not self.employer_confidential


def _position_company(position: Position) -> str | None:
    raw = position.requirements if isinstance(position.requirements, dict) else {}
    value = raw.get("company")
    return value if isinstance(value, str) and value.strip() else None


def redact_position(position: Position) -> PublicPositionView:
    """Build the public projection of ``position`` under its sensitivity tier.

    This is the single source of truth for public exposure. Callers should read
    only from the returned view — never mix in fields off the ORM object.
    """

    sensitivity = normalize_sensitivity(position.sensitivity)

    if sensitivity == SENSITIVITY_PUBLIC:
        return PublicPositionView(
            title=position.title,
            description=position.description,
            full_description=position.full_description,
            requirements=safe_public_requirements(position.requirements),
            company=_position_company(position),
            employer_confidential=False,
            sensitivity=sensitivity,
        )

    # Confidential: the vetted stand-in replaces the real description, and if
    # nobody has written one yet we show nothing. Falling back to the internal
    # description here would defeat the entire feature — that text is exactly
    # what names the internal modules and architecture.
    return PublicPositionView(
        title=position.title,
        description=(position.public_description or None),
        full_description=None,
        requirements=_confidential_requirements(
            position.requirements, sensitivity=sensitivity
        ),
        company=None,
        employer_confidential=True,
        sensitivity=sensitivity,
    )
