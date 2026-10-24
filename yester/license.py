"""Offline licence checks for the open-core distribution.

Yester's personal edition is fully functional and free.  Team and
enterprise features are gated behind a licence key that is validated
*offline* — no phone-home, no telemetry.  The key format is documented
so organisations can audit the gate; it is a product boundary, not a
security boundary.
"""
from __future__ import annotations

import hashlib

TIERS = ("free", "team", "enterprise")

#: feature -> minimum tier.  ONLY features that are actually gated somewhere
#: in the codebase belong here: advertising a paid feature that does not exist
#: (or gating nothing) is how open-core projects lose trust.  See
#: PLANNED_FEATURES for the roadmap.
FEATURE_TIERS: dict[str, str] = {
    "audit-export": "team",
    "retention-mode": "team",
}

#: announced, not implemented — never granted, never sold
PLANNED_FEATURES: dict[str, str] = {
    "publish-custom-domain": "Custom domains for published sites",
    "semantic-search": "Cross-version semantic search",
    "sso-config": "SSO / SAML configuration",
}

#: shipped in the free edition on purpose (kept out of the paid list)
FREE_FEATURES: dict[str, str] = {
    "scan-hook": "Pre-commit secret scanning (`yester scan`, `yester hook`)",
}

FEATURE_DESCRIPTIONS: dict[str, str] = {
    "audit-export": "Compliance audit trail export (JSON/CSV)",
    "retention-mode": "Retention mode: immutable history, no auto gc",
    **FREE_FEATURES,
    **PLANNED_FEATURES,
}


class LicenseError(Exception):
    """Raised when a licensed feature is used without a valid licence."""


def _checksum(payload: str) -> str:
    digest = hashlib.sha256(f"yester-licence:{payload}".encode()).hexdigest()
    return digest[:8].upper()


def validate_key(key: str) -> str | None:
    """Return the tier for a well-formed key, else ``None``.

    Format: ``YESTER-<TIER>-<8 chars>-<checksum>``
    """
    parts = key.strip().upper().split("-")
    if len(parts) != 4 or parts[0] != "YESTER":
        return None
    _, tier, payload, checksum = parts
    if tier.lower() not in TIERS or tier.lower() == "free":
        return None
    if len(payload) != 8 or _checksum(f"{tier}-{payload}") != checksum:
        return None
    return tier.lower()


def issue_key(tier: str, payload: str | None = None) -> str:
    """Deterministically build a key (used by support tooling and tests)."""
    tier = tier.lower()
    if tier not in TIERS or tier == "free":
        raise LicenseError(f"cannot issue a key for tier {tier!r}")
    payload = (payload or "DEMO0001")[:8].upper().ljust(8, "0")
    return f"YESTER-{tier.upper()}-{payload}-{_checksum(f'{tier.upper()}-{payload}')}"


def current_tier(key: str) -> str:
    return validate_key(key) or "free"


def require(feature: str, key: str) -> None:
    if feature in PLANNED_FEATURES:
        raise LicenseError(
            f"{feature!r} is on the roadmap and is not implemented in this "
            f"release — no licence can unlock it"
        )
    needed = FEATURE_TIERS.get(feature)
    if needed is None:
        return          # free feature, or an unknown name (callers typo-safe)
    have = current_tier(key)
    if TIERS.index(have) < TIERS.index(needed):
        raise LicenseError(
            f"{feature!r} ({FEATURE_DESCRIPTIONS.get(feature, feature)}) "
            f"requires a {needed} licence — run `yester activate <key>`"
        )
