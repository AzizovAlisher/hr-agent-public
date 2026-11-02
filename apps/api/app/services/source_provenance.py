"""Canonical, non-PII source labels used by public hiring intake."""

import hashlib
import re


SOURCE_SLUG_MAX_LENGTH = 32
_SOURCE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_PUBLIC_SOURCE_ALIASES = {
    "careers": "careers",
    "careers_api": "careers_api",
    "company_website": "company_website",
    "direct": "direct",
    "employee_referral": "employee_referral",
    "facebook": "facebook",
    "headhunter": "hh",
    "hh": "hh",
    "hh.ru": "hh",
    "instagram": "instagram",
    "linkedin": "linkedin",
    "public": "public",
    "referral": "referral",
    "somon": "somon",
    "somon.tj": "somon",
    "telegram": "telegram",
    "website": "website",
}


def configured_source_slug(value: str) -> str:
    """Return a stable operator-configured attribution slug."""

    cleaned = value.strip().casefold()
    if not _SOURCE_SLUG.fullmatch(cleaned):
        raise ValueError(
            "source must be a 1-32 character lowercase slug containing only "
            "letters, numbers, underscores, or hyphens"
        )
    return cleaned


def public_source_slug(value: str | None) -> str:
    """Map untrusted campaign input to a short, non-PII provenance label."""

    raw = (value or "").strip().casefold()
    if not raw:
        return "public"
    known = _PUBLIC_SOURCE_ALIASES.get(raw)
    if known is not None:
        return known
    # Public query/form fields remain untrusted even when they happen to look
    # slug-like. Hashing preserves attribution uniqueness without copying a
    # caller-controlled name, phone, email, or campaign phrase into broadly
    # projected provenance fields.
    return f"other-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"
