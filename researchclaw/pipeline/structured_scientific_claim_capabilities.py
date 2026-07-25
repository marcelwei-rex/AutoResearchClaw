"""Inactive capability gate for structured scientific-claim publication.

This declaration is independent from canonical-evidence migration authority.
B3 implementation commits must leave every component at zero.
"""

from __future__ import annotations


STRUCTURED_CAPABILITY_SCHEMA_VERSION = 1
REQUIRED_STRUCTURED_CAPABILITIES = (
    "stage17_publication",
    "stage19_revision",
    "stage20_replay",
    "stage24_and_release_integration",
)

STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES: dict[str, int] = {
    "stage17_publication": 0,
    "stage19_revision": 0,
    "stage20_replay": 0,
    "stage24_and_release_integration": 0,
}


class StructuredScientificClaimCapabilityError(RuntimeError):
    """The code-owned structured capability declaration is malformed."""

    code = "structured_scientific_claim_capability_invalid"


class StructuredScientificClaimCapabilityIncomplete(RuntimeError):
    """The complete structured capability has not been declared."""

    code = "structured_scientific_claim_capability_incomplete"

    def __init__(self, entrypoint: str, incomplete: tuple[str, ...]) -> None:
        self.entrypoint = entrypoint
        self.incomplete = incomplete
        detail = ", ".join(incomplete)
        super().__init__(f"{self.code}: {entrypoint}: incomplete components: {detail}")


def structured_publication_is_eligible() -> bool:
    """Inspect only code-owned declarations; perform no I/O."""

    try:
        _validate_code_owned_declaration()
    except StructuredScientificClaimCapabilityError:
        return False
    return all(
        STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES[name] == 1
        for name in REQUIRED_STRUCTURED_CAPABILITIES
    )


def require_complete_structured_capability(entrypoint: str) -> None:
    """Mechanically reject direct structured entry until exact completion."""

    if type(entrypoint) is not str or not entrypoint:
        raise StructuredScientificClaimCapabilityError(
            "structured capability entrypoint must be a nonempty string"
        )
    _validate_code_owned_declaration()
    if all(
        STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES[name] == 1
        for name in REQUIRED_STRUCTURED_CAPABILITIES
    ):
        return
    incomplete = tuple(
        name
        for name in REQUIRED_STRUCTURED_CAPABILITIES
        if STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES[name] != 1
    )
    raise StructuredScientificClaimCapabilityIncomplete(entrypoint, incomplete)


def code_owned_structured_capability_snapshot() -> dict[str, int]:
    """Return a defensive copy after validating the code-owned declaration."""

    _validate_code_owned_declaration()
    return {
        name: STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES[name]
        for name in REQUIRED_STRUCTURED_CAPABILITIES
    }


def _validate_code_owned_declaration() -> None:
    if (
        type(STRUCTURED_CAPABILITY_SCHEMA_VERSION) is not int
        or STRUCTURED_CAPABILITY_SCHEMA_VERSION != 1
    ):
        raise StructuredScientificClaimCapabilityError(
            "structured capability schema version must be the true integer 1"
        )
    values = STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES
    if type(values) is not dict or set(values) != set(REQUIRED_STRUCTURED_CAPABILITIES):
        raise StructuredScientificClaimCapabilityError(
            "structured capability keys mismatch"
        )
    for name in REQUIRED_STRUCTURED_CAPABILITIES:
        value = values[name]
        if type(value) is not int or value not in {0, 1}:
            raise StructuredScientificClaimCapabilityError(
                f"structured capability {name} must be the true integer 0 or 1"
            )
