"""Migration capability gate for canonical experiment evidence."""

from __future__ import annotations

from collections.abc import Mapping

from researchclaw.pipeline.stages import Stage


CAPABILITY_SCHEMA_VERSION = 1
REQUIRED_CAPABILITIES = (
    "stage10_sealed_input",
    "stage12_result_set",
    "stage13_refinement_set",
    "stage14_candidate_and_promotion",
    "shared_accessor",
    "stage15_17_consumers",
    "stage19_22_consumers",
    "stage24_release_consumers",
    "external_and_persistent_consumers",
    "independent_release_reconstruction",
)

# C0 deliberately leaves the runtime blocked. Later migration commits may only
# change their owned component from 0 to CAPABILITY_SCHEMA_VERSION.
CANONICAL_EVIDENCE_CAPABILITIES: dict[str, int] = {
    "stage10_sealed_input": CAPABILITY_SCHEMA_VERSION,
    "stage12_result_set": 0,
    "stage13_refinement_set": 0,
    "stage14_candidate_and_promotion": 0,
    "shared_accessor": 0,
    "stage15_17_consumers": 0,
    "stage19_22_consumers": 0,
    "stage24_release_consumers": 0,
    "external_and_persistent_consumers": 0,
    "independent_release_reconstruction": 0,
}


class CanonicalEvidenceMigrationIncomplete(RuntimeError):
    """Raised while any canonical-evidence migration component is incomplete."""

    code = "canonical_evidence_migration_incomplete"

    def __init__(self, entrypoint: str, incomplete: tuple[str, ...]) -> None:
        self.entrypoint = entrypoint
        self.incomplete = incomplete
        detail = ", ".join(incomplete)
        super().__init__(f"{self.code}: {entrypoint}: incomplete components: {detail}")


def incomplete_canonical_evidence_capabilities(
    capabilities: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    """Return missing, unknown, or unsupported capability declarations."""
    values = capabilities if capabilities is not None else CANONICAL_EVIDENCE_CAPABILITIES
    incomplete: list[str] = []
    if set(values) != set(REQUIRED_CAPABILITIES):
        incomplete.extend(sorted(set(REQUIRED_CAPABILITIES) - set(values)))
        incomplete.extend(f"unknown:{name}" for name in sorted(set(values) - set(REQUIRED_CAPABILITIES)))
    for name in REQUIRED_CAPABILITIES:
        value = values.get(name)
        if isinstance(value, bool) or value != CAPABILITY_SCHEMA_VERSION:
            incomplete.append(name)
    return tuple(dict.fromkeys(incomplete))


def require_canonical_evidence_capabilities(
    entrypoint: str,
) -> None:
    """Fail before any legacy experiment evidence can be read or persisted."""
    incomplete = incomplete_canonical_evidence_capabilities()
    if incomplete:
        raise CanonicalEvidenceMigrationIncomplete(entrypoint, incomplete)


def requested_range_requires_canonical_evidence(
    from_stage: Stage,
    to_stage: Stage | None,
) -> bool:
    """Return whether a pipeline request reaches Stage 12 or any later stage."""
    del from_stage
    return to_stage is None or int(to_stage) >= int(Stage.EXPERIMENT_RUN)
