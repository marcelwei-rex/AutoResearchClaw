# conftest.py — shared pytest fixtures for researchclaw tests

import pytest


@pytest.fixture
def canonical_evidence_migration_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let legacy behavior tests exercise their target below the C0 gate."""
    from researchclaw.pipeline import canonical_evidence_capabilities as capabilities

    monkeypatch.setattr(
        capabilities,
        "CANONICAL_EVIDENCE_CAPABILITIES",
        {
            name: capabilities.CAPABILITY_SCHEMA_VERSION
            for name in capabilities.REQUIRED_CAPABILITIES
        },
    )
