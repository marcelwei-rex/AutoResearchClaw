"""B3-A inactive structured-scientific-claim capability contracts."""

from __future__ import annotations

import inspect

import pytest

from researchclaw.pipeline import structured_scientific_claim_capabilities as capability


EXPECTED_KEYS = (
    "stage17_publication",
    "stage19_revision",
    "stage20_replay",
    "stage24_and_release_integration",
)


def test_b4_declaration_is_independent_exact_1110_and_inactive() -> None:
    assert capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION == 1
    assert type(capability.STRUCTURED_CAPABILITY_SCHEMA_VERSION) is int
    assert tuple(capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES) == EXPECTED_KEYS
    assert capability.STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }
    assert capability.code_owned_structured_capability_snapshot() == {
        "stage17_publication": 1,
        "stage19_revision": 1,
        "stage20_replay": 1,
        "stage24_and_release_integration": 0,
    }
    assert capability.structured_publication_is_eligible() is False


def test_ordinary_eligibility_and_direct_guard_accept_no_authority_map() -> None:
    assert tuple(inspect.signature(capability.structured_publication_is_eligible).parameters) == ()
    assert tuple(inspect.signature(capability.require_complete_structured_capability).parameters) == (
        "entrypoint",
    )
    with pytest.raises(
        capability.StructuredScientificClaimCapabilityIncomplete,
        match="structured_scientific_claim_capability_incomplete",
    ) as exc_info:
        capability.require_complete_structured_capability("test")
    assert exc_info.value.incomplete == (
        "stage24_and_release_integration",
    )


@pytest.mark.parametrize(
    "replacement",
    [
        {},
        {"unknown": 0},
        {**{key: 0 for key in EXPECTED_KEYS}, "unknown": 0},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: True},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: 1.0},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: "1"},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: None},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: -1},
        {**{key: 0 for key in EXPECTED_KEYS}, EXPECTED_KEYS[0]: 2},
    ],
)
def test_invalid_code_owned_capability_declarations_are_ineligible_but_strict(
    monkeypatch: pytest.MonkeyPatch,
    replacement: dict[str, object],
) -> None:
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        replacement,
    )
    assert capability.structured_publication_is_eligible() is False
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        capability.require_complete_structured_capability("test")
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        capability.code_owned_structured_capability_snapshot()


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 0, 2])
def test_invalid_code_owned_schema_versions_are_ineligible_but_strict(
    monkeypatch: pytest.MonkeyPatch,
    version: object,
) -> None:
    monkeypatch.setattr(
        capability,
        "STRUCTURED_CAPABILITY_SCHEMA_VERSION",
        version,
    )
    assert capability.structured_publication_is_eligible() is False
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        capability.require_complete_structured_capability("test")
    with pytest.raises(capability.StructuredScientificClaimCapabilityError):
        capability.code_owned_structured_capability_snapshot()


def test_complete_predicate_is_true_only_for_exact_1111(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        {key: 1 for key in EXPECTED_KEYS},
    )
    assert capability.structured_publication_is_eligible() is True
    capability.require_complete_structured_capability("test")


@pytest.mark.parametrize("incomplete_name", EXPECTED_KEYS)
def test_complete_predicate_is_false_when_any_required_capability_is_zero(
    monkeypatch: pytest.MonkeyPatch,
    incomplete_name: str,
) -> None:
    replacement = {key: 1 for key in EXPECTED_KEYS}
    replacement[incomplete_name] = 0
    monkeypatch.setattr(
        capability,
        "STRUCTURED_SCIENTIFIC_CLAIM_CAPABILITIES",
        replacement,
    )
    assert capability.structured_publication_is_eligible() is False
    with pytest.raises(
        capability.StructuredScientificClaimCapabilityIncomplete
    ) as exc_info:
        capability.require_complete_structured_capability("test")
    assert exc_info.value.incomplete == (incomplete_name,)
